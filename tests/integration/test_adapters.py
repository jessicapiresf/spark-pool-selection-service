"""Adapters contra AWS simulada em processo.

LocalStack fica para o ambiente de dev. Aqui o moto e mais rapido e nao precisa de Docker
no CI, e o que interessa e provar que o contrato que o dominio assume sobrevive ao
comportamento real do DynamoDB e do S3, principalmente o incremento atomico e o
controle de objeto ja processado.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from pool_selection.adapters.dynamodb_counters import DynamoDBCounterStore
from pool_selection.adapters.s3_snapshots import S3SnapshotStore
from pool_selection.domain.events import Weights
from pool_selection.domain.pool import PoolId, Profile
from pool_selection.domain.scoring import Capacity, Evidence, PlacementForecast, SpotAvailability
from pool_selection.domain.snapshot import PoolEntry, Snapshot
from pool_selection.entrypoints.ingestor.handler import ingest
from pool_selection.ports.counters import CounterDelta, CounterKey, Scope
from pool_selection.ports.snapshots import SnapshotUnavailableError

REGION = "us-east-1"
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def dynamodb():
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        client.create_table(
            TableName="counters",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket="snapshots")
        yield client


def delta(pool_id: str, successes: float, failures: float, minute: str = "2026-08-25T11:59"):
    return CounterDelta(CounterKey(Scope.POOL, pool_id, minute), successes, failures)


def test_incremento_e_atomico_e_acumula(dynamodb) -> None:
    """E exatamente a operacao da ingestao, e por isso a escolha nao foi SQL."""
    store = DynamoDBCounterStore("counters", dynamodb)
    for _ in range(5):
        store.add([delta("pool-r6.xlarge-us-east-1a", 2.0, 0.5)])

    counters = store.read_minute("2026-08-25T11:59")
    assert counters.pools["pool-r6.xlarge-us-east-1a"] == (10.0, 2.5)


def test_le_o_minuto_inteiro_em_uma_consulta(dynamodb) -> None:
    store = DynamoDBCounterStore("counters", dynamodb)
    store.add(
        [
            delta("pool-r6.xlarge-us-east-1a", 3, 0),
            delta("pool-c6.xlarge-us-east-1b", 1, 1),
            CounterDelta(CounterKey(Scope.JOB, "etl#r6.xlarge", "2026-08-25T11:59"), 3, 0),
        ]
    )
    counters = store.read_minute("2026-08-25T11:59")
    assert len(counters.pools) == 2
    assert counters.jobs[("etl", "r6.xlarge")] == (3.0, 0.0)


def test_minutos_nao_se_misturam(dynamodb) -> None:
    store = DynamoDBCounterStore("counters", dynamodb)
    store.add([delta("pool-r6.xlarge-us-east-1a", 5, 0, "2026-08-25T11:58")])
    store.add([delta("pool-r6.xlarge-us-east-1a", 7, 0, "2026-08-25T11:59")])
    assert store.read_minute("2026-08-25T11:58").pools["pool-r6.xlarge-us-east-1a"] == (5.0, 0.0)
    assert store.read_minute("2026-08-25T11:59").pools["pool-r6.xlarge-us-east-1a"] == (7.0, 0.0)


def test_minuto_vazio_nao_e_erro(dynamodb) -> None:
    assert DynamoDBCounterStore("counters", dynamodb).read_minute("2026-01-01T00:00").is_empty


def test_delta_zerado_nao_gera_escrita(dynamodb) -> None:
    store = DynamoDBCounterStore("counters", dynamodb)
    store.add([delta("pool-r6.xlarge-us-east-1a", 0.0, 0.0)])
    assert store.read_minute("2026-08-25T11:59").is_empty


def test_objeto_marcado_nao_e_reprocessado(dynamodb) -> None:
    """A prova de que uma reentrega do SQS nao conta a mesma falha duas vezes."""
    store = DynamoDBCounterStore("counters", dynamodb)
    identidades = ["eventos/a.json#etag1", "eventos/b.json#etag1"]

    assert store.processed(identidades) == set()
    store.mark_processed(["eventos/a.json#etag1"])

    assert store.processed(identidades) == {"eventos/a.json#etag1"}
    # Mesmo arquivo, conteudo novo: etag diferente, objeto diferente.
    assert store.processed(["eventos/a.json#etag2"]) == set()


def test_consulta_de_processados_acima_do_limite_de_lote(dynamodb) -> None:
    """O BatchGetItem so aceita cem chaves, e um lote do SQS traz ate mil objetos."""
    store = DynamoDBCounterStore("counters", dynamodb)
    identidades = [f"eventos/{n}.json#etag" for n in range(250)]
    marcados = identidades[::2]
    store.mark_processed(marcados)

    assert store.processed(identidades) == set(marcados)


def test_marca_so_depois_de_gravar_para_nao_perder_evento(dynamodb) -> None:
    """Se a escrita dos contadores falha, o objeto nao pode ficar marcado.

    Marcar antes trocaria contagem dobrada por perda silenciosa. Aqui a escrita estoura no
    meio e o objeto precisa continuar elegivel para a reentrega.
    """
    store = DynamoDBCounterStore("counters", dynamodb)

    def explode(_deltas):
        raise RuntimeError("dynamo fora do ar")

    store.add = explode  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        ingest(
            [{"bucket": "eventos", "key": "a.json", "etag": "etag1"}],
            store,
            lambda _b, _k: iter(
                [
                    '{"finished_at":"2026-08-25T11:59:00","job_id":"j",'
                    '"pool_id":"pool-r6.xlarge-us-east-1a","status":"SUCCESS"}'
                ]
            ),
            Weights(),
        )

    assert store.processed(["eventos/a.json#etag1"]) == set()


def test_contadores_recebem_ttl(dynamodb) -> None:
    """A janela deslizante se implementa sem job de limpeza."""
    store = DynamoDBCounterStore("counters", dynamodb)
    store.add([delta("pool-r6.xlarge-us-east-1a", 1, 0)])
    item = dynamodb.get_item(
        TableName="counters",
        Key={"pk": {"S": "MIN#2026-08-25T11:59"}, "sk": {"S": "POOL#pool-r6.xlarge-us-east-1a"}},
    )["Item"]
    assert int(item["expires_at"]["N"]) > 0


def snapshot_completo() -> Snapshot:
    return Snapshot(
        generated_at=NOW,
        through_minute="2026-08-25T11:59",
        pools=(
            PoolEntry(
                pool_id=PoolId.parse("pool-r6.xlarge-us-east-1a"),
                profile=Profile.MEMORY,
                evidence=Evidence(120.5, 4.25, NOW),
                capacity=Capacity(
                    max_capacity=40,
                    used_count=9,
                    idle_count=3,
                    availability=SpotAvailability.SPOT_WITH_FALLBACK,
                ),
                forecast=PlacementForecast(8, 20, NOW),
            ),
        ),
        job_fit={"etl": {"r6.xlarge": Evidence(9.0, 1.0, NOW)}},
        profile_fit={"memory": Evidence(500.0, 30.0, NOW)},
        catalog={"r6.xlarge": "memory"},
        catalog_refreshed_at=NOW,
    )


def test_snapshot_sobrevive_a_ida_e_volta_pelo_s3(s3) -> None:
    store = S3SnapshotStore("snapshots", client=s3)
    original = snapshot_completo()
    store.save(original)
    restored = store.load()

    assert restored.pools[0].capacity == original.pools[0].capacity
    assert restored.pools[0].forecast == original.pools[0].forecast
    assert restored.job_fit["etl"]["r6.xlarge"].successes == pytest.approx(9.0)
    assert restored.through_minute == "2026-08-25T11:59"


def test_snapshot_vai_comprimido(s3) -> None:
    """O gzip e o que derruba o arquivo para poucos kilobytes."""
    store = S3SnapshotStore("snapshots", client=s3)
    store.save(snapshot_completo())
    body = s3.get_object(Bucket="snapshots", Key="snapshot/pools.json.gz")["Body"].read()

    assert len(gzip.decompress(body)) > len(body)
    assert json.loads(gzip.decompress(body))["version"] == 1


def test_snapshot_ausente_vira_erro_tipado(s3) -> None:
    with pytest.raises(SnapshotUnavailableError):
        S3SnapshotStore("snapshots", client=s3).load()


def test_snapshot_corrompido_nao_derruba_a_api(s3) -> None:
    """Precisa virar `SnapshotUnavailableError` para o cache cair no fallback."""
    s3.put_object(Bucket="snapshots", Key="snapshot/pools.json.gz", Body=b"isso nao e gzip")
    with pytest.raises(SnapshotUnavailableError):
        S3SnapshotStore("snapshots", client=s3).load()


def test_snapshot_com_json_invalido_vira_erro_tipado(s3) -> None:
    s3.put_object(
        Bucket="snapshots", Key="snapshot/pools.json.gz", Body=gzip.compress(b"{nao fecha")
    )
    with pytest.raises(SnapshotUnavailableError):
        S3SnapshotStore("snapshots", client=s3).load()


def test_sobrescrever_publica_a_versao_nova(s3) -> None:
    store = S3SnapshotStore("snapshots", client=s3)
    store.save(snapshot_completo())
    store.save(Snapshot(generated_at=NOW, through_minute="2026-08-25T12:05"))
    assert store.load().through_minute == "2026-08-25T12:05"
