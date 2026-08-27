from __future__ import annotations

import json

from pool_selection.adapters.memory import InMemoryCounterStore
from pool_selection.domain.events import Weights
from pool_selection.entrypoints.ingestor.handler import ingest, s3_notifications

EVENTO = {
    "finished_at": "2026-08-25T12:00:10",
    "job_id": "etl-vendas",
    "pool_id": "pool-r6.xlarge-us-east-1c",
    "status": "SUCCESS",
    "reason": None,
}


def reader(lines: list[str]):
    return lambda _bucket, _key: iter(lines)


def objeto(key: str = "eventos.json", etag: str = "abc") -> dict[str, str]:
    return {"bucket": "eventos", "key": key, "etag": etag}


def test_conta_sucesso_no_pool_e_no_par_job_tipo() -> None:
    store = InMemoryCounterStore()
    ingest([objeto()], store, reader([json.dumps(EVENTO)]), Weights())
    counters = store.read_minute("2026-08-25T12:00")
    assert counters.pools["pool-r6.xlarge-us-east-1c"] == (1.0, 0.0)
    assert counters.jobs[("etl-vendas", "r6.xlarge")] == (1.0, 0.0)


def test_pre_agrega_em_memoria_antes_de_escrever() -> None:
    """Sem isso o ingestor vira o gargalo: milhares de escritas em vez de poucas."""
    linhas = [json.dumps(EVENTO) for _ in range(500)]
    store = InMemoryCounterStore()
    report = ingest([objeto()], store, reader(linhas), Weights())
    assert report.events == 500
    assert report.writes == 2  # um contador de pool e um de job, no mesmo minuto


def test_lote_reentregue_nao_conta_a_mesma_falha_de_novo() -> None:
    """A entrega do SQS e at-least-once; sem reivindicacao, um pool bom afundaria."""
    falha = EVENTO | {"status": "FAILED", "reason": "SPOT_INSTANCE_TERMINATION"}
    linhas = [json.dumps(falha)]
    store = InMemoryCounterStore()

    ingest([objeto()], store, reader(linhas), Weights())
    segunda = ingest([objeto()], store, reader(linhas), Weights())

    assert segunda.skipped_objects == 1
    assert segunda.events == 0
    assert store.read_minute("2026-08-25T12:00").pools["pool-r6.xlarge-us-east-1c"] == (0.0, 1.0)


def test_objeto_diferente_com_mesma_chave_e_reprocessado() -> None:
    """Sobrescrever o objeto muda o etag, e o conteudo novo precisa entrar."""
    store = InMemoryCounterStore()
    ingest([objeto(etag="v1")], store, reader([json.dumps(EVENTO)]), Weights())
    segunda = ingest([objeto(etag="v2")], store, reader([json.dumps(EVENTO)]), Weights())
    assert segunda.events == 1


def test_linha_torta_e_contada_e_nao_derruba_o_lote() -> None:
    linhas = ["{isso nao e json", json.dumps(EVENTO), json.dumps({"job_id": "sem_o_resto"})]
    report = ingest([objeto()], InMemoryCounterStore(), reader(linhas), Weights())
    assert report.events == 1
    assert report.malformed == 2


def test_bug_de_spark_nao_gera_escrita() -> None:
    bug = EVENTO | {"status": "FAILED", "reason": "SPARK_EXECUTION_ERROR"}
    store = InMemoryCounterStore()
    report = ingest([objeto()], store, reader([json.dumps(bug)]), Weights())
    assert report.irrelevant == 1
    assert report.writes == 0


def test_peso_de_timed_out_chega_ate_o_contador() -> None:
    timeout = EVENTO | {"status": "FAILED", "reason": "TIMED_OUT"}
    store = InMemoryCounterStore()
    ingest([objeto()], store, reader([json.dumps(timeout)]), Weights(timed_out=0.25))
    assert store.read_minute("2026-08-25T12:00").pools["pool-r6.xlarge-us-east-1c"] == (0.75, 0.25)


def test_extrai_notificacoes_do_s3_de_dentro_do_sqs() -> None:
    evento = {
        "Records": [
            {
                "messageId": "1",
                "body": json.dumps(
                    {
                        "Records": [
                            {
                                "s3": {
                                    "bucket": {"name": "eventos"},
                                    "object": {"key": "dia%3D2026-08-25/a.json", "eTag": "e1"},
                                }
                            }
                        ]
                    }
                ),
            }
        ]
    }
    notificacoes = list(s3_notifications(evento))
    assert notificacoes == [{"bucket": "eventos", "key": "dia=2026-08-25/a.json", "etag": "e1"}]


def test_notificacao_de_teste_do_s3_nao_e_erro() -> None:
    evento = {"Records": [{"messageId": "1", "body": json.dumps({"Event": "s3:TestEvent"})}]}
    assert list(s3_notifications(evento)) == []


def test_mensagem_ilegivel_e_pulada() -> None:
    evento = {"Records": [{"messageId": "1", "body": "nao e json"}]}
    assert list(s3_notifications(evento)) == []
