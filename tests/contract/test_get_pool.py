"""O endpoint inteiro, incluindo validacao de parametro e formato da resposta."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import entry
from pool_selection.adapters.memory import InMemorySnapshotStore
from pool_selection.domain.pool import Profile
from pool_selection.domain.scoring import Capacity, Evidence, PlacementForecast, SpotAvailability
from pool_selection.domain.snapshot import Snapshot
from pool_selection.entrypoints.api.app import create_app, set_cache
from pool_selection.entrypoints.api.snapshot_cache import SnapshotCache
from pool_selection.ports.snapshots import SnapshotUnavailableError


@pytest.fixture
def now() -> datetime:
    """Relogio real: a API usa `datetime.now`, entao snapshot com data fixa nasce velho."""
    return datetime.now(UTC)


@pytest.fixture
def rich_snapshot(now: datetime) -> Snapshot:
    return Snapshot(
        generated_at=now,
        through_minute="2026-08-25T11:59",
        pools=(
            entry(
                "pool-r6.xlarge-us-east-1a",
                200,
                10,
                moment=now,
                capacity=Capacity(max_capacity=40, used_count=6, idle_count=3),
                forecast=PlacementForecast(9, 20, now),
            ),
            entry(
                "pool-r6.xlarge-us-east-1b",
                40,
                120,
                moment=now,
                capacity=Capacity(max_capacity=40, used_count=1),
            ),
            entry("pool-c6.xlarge-us-east-1a", 80, 4, profile=Profile.COMPUTE, moment=now),
            entry(
                "pool-m6.xlarge-us-east-1c",
                30,
                2,
                profile=Profile.GENERAL,
                moment=now,
                capacity=Capacity(availability=SpotAvailability.SPOT_WITH_FALLBACK),
            ),
        ),
        job_fit={"etl-pesado": {"r6.xlarge": Evidence(1.0, 12.0, now)}},
        profile_fit={"memory": Evidence(400.0, 50.0, now)},
        catalog={"r6.xlarge": "memory", "c6.xlarge": "compute", "m6.xlarge": "general"},
        catalog_refreshed_at=now,
    )


@pytest.fixture
def client(rich_snapshot: Snapshot) -> Iterator[TestClient]:
    set_cache(SnapshotCache(InMemorySnapshotStore(rich_snapshot), ttl_seconds=0))
    yield TestClient(create_app())
    set_cache(None)


def test_resposta_traz_o_formato_documentado(client: TestClient) -> None:
    body = client.get("/get-pool", params={"job_id": "etl-vendas", "seed": 1}).json()

    assert set(body) >= {
        "pool_id",
        "instance_type",
        "availability_zone",
        "score",
        "credible_interval",
        "evidence",
        "alternatives",
        "snapshot",
        "degraded",
    }
    assert body["pool_id"] == f"pool-{body['instance_type']}-{body['availability_zone']}"
    assert 0.0 <= body["score"] <= 1.0
    low, high = body["credible_interval"]
    assert 0.0 <= low <= high <= 1.0
    assert body["degraded"] is False
    assert body["snapshot"]["stale"] is False


def test_alternativas_permitem_fallback_sem_segunda_chamada(client: TestClient) -> None:
    body = client.get("/get-pool", params={"alternatives": 2, "seed": 1}).json()
    ids = [alt["pool_id"] for alt in body["alternatives"]]
    assert len(ids) == 2
    assert body["pool_id"] not in ids


def test_capacidade_e_previsao_aparecem_quando_existem(client: TestClient) -> None:
    body = client.get(
        "/get-pool", params={"instance_types": "r6.xlarge", "availability_zones": "us-east-1a"}
    ).json()
    assert body["capacity"] == {
        "free_slots": 34,
        "idle_instances": 3,
        "falls_back_to_on_demand": False,
    }
    assert body["az_outlook"]["spot_placement_score"] == 9
    assert body["az_outlook"]["target_capacity"] == 20


def test_pool_sem_capacidade_conhecida_omite_o_bloco(client: TestClient) -> None:
    body = client.get("/get-pool", params={"profile": "compute"}).json()
    assert body["capacity"] is None
    assert body["az_outlook"] is None


def test_fonte_da_recomendacao_diz_quando_e_palpite(client: TestClient) -> None:
    conhecido = client.get("/get-pool", params={"job_id": "etl-pesado", "family": "r6"}).json()
    assert conhecido["evidence"]["source"] == "job_history"
    assert conhecido["evidence"]["job_samples"] > 0

    novo = client.get("/get-pool", params={"job_id": "nunca-rodou", "family": "r6"}).json()
    assert novo["evidence"]["source"] == "profile_prior"

    sem_perfil = client.get("/get-pool", params={"job_id": "x", "profile": "compute"}).json()
    assert sem_perfil["evidence"]["source"] == "none"


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"instance_types": "c6.xlarge"}, "pool-c6.xlarge-us-east-1a"),
        ({"profile": "general"}, "pool-m6.xlarge-us-east-1c"),
        ({"availability_zones": "us-east-1b"}, "pool-r6.xlarge-us-east-1b"),
        ({"family": "c6"}, "pool-c6.xlarge-us-east-1a"),
    ],
)
def test_filtros_restringem_o_que_pode_voltar(
    client: TestClient, params: dict[str, str], expected: str
) -> None:
    assert client.get("/get-pool", params=params).json()["pool_id"] == expected


def test_exclude_pools_serve_para_retry(client: TestClient) -> None:
    body = client.get(
        "/get-pool",
        params={"family": "r6", "exclude_pools": "pool-r6.xlarge-us-east-1a", "strategy": "greedy"},
    ).json()
    assert body["pool_id"] == "pool-r6.xlarge-us-east-1b"


def test_min_samples_exige_evidencia(client: TestClient) -> None:
    assert client.get("/get-pool", params={"min_samples": 1000}).status_code == 404
    assert client.get("/get-pool", params={"min_samples": 100}).status_code == 200


def test_greedy_e_reprodutivel(client: TestClient) -> None:
    respostas = {
        client.get("/get-pool", params={"strategy": "greedy"}).json()["pool_id"] for _ in range(10)
    }
    assert len(respostas) == 1


def test_semente_fixa_torna_o_sorteio_reprodutivel(client: TestClient) -> None:
    primeira = client.get("/get-pool", params={"seed": 99}).json()
    segunda = client.get("/get-pool", params={"seed": 99}).json()
    assert primeira["pool_id"] == segunda["pool_id"]
    assert primeira["score"] == segunda["score"]


def test_filtro_que_nao_casa_com_nada_e_404_explicito(client: TestClient) -> None:
    response = client.get("/get-pool", params={"family": "zz9"})
    assert response.status_code == 404
    assert "filtro" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    "params",
    [
        {"profile": "inexistente"},
        {"strategy": "aleatorio"},
        {"alternatives": -1},
        {"alternatives": 99},
        {"min_samples": -5},
    ],
)
def test_parametro_invalido_e_422(client: TestClient, params: dict[str, object]) -> None:
    assert client.get("/get-pool", params=params).status_code == 422


def test_snapshot_velho_continua_respondendo_marcado(
    rich_snapshot: Snapshot, now: datetime
) -> None:
    """Um palpite informado e melhor que um 503."""
    velho = Snapshot(
        generated_at=now - timedelta(hours=2),
        pools=rich_snapshot.pools,
        catalog=rich_snapshot.catalog,
    )
    set_cache(SnapshotCache(InMemorySnapshotStore(velho), ttl_seconds=0, stale_after_seconds=300))
    response = TestClient(create_app()).get("/get-pool")
    set_cache(None)

    assert response.status_code == 200
    assert response.json()["snapshot"]["stale"] is True
    assert response.headers["cache-control"] == "no-store"


def test_sem_snapshot_responde_da_lista_estatica_marcado_como_degradado() -> None:
    class Vazio:
        def load(self) -> Snapshot:
            raise SnapshotUnavailableError("sem snapshot")

        def save(self, snapshot: Snapshot) -> None: ...

    set_cache(SnapshotCache(Vazio(), fallback_pools=("pool-r6.xlarge-us-east-1a",)))
    body = TestClient(create_app()).get("/get-pool").json()
    set_cache(None)

    assert body["degraded"] is True
    assert body["pool_id"] == "pool-r6.xlarge-us-east-1a"
    assert body["evidence"]["source"] == "none"


def test_sem_snapshot_e_sem_fallback_e_503() -> None:
    class Vazio:
        def load(self) -> Snapshot:
            raise SnapshotUnavailableError("sem snapshot")

        def save(self, snapshot: Snapshot) -> None: ...

    set_cache(SnapshotCache(Vazio()))
    response = TestClient(create_app()).get("/get-pool")
    set_cache(None)
    assert response.status_code == 503


def test_health_responde_sem_depender_do_snapshot() -> None:
    set_cache(None)
    assert TestClient(create_app()).get("/health").json() == {"status": "ok"}


def test_ready_falha_quando_nao_ha_snapshot_utilizavel() -> None:
    class Vazio:
        def load(self) -> Snapshot:
            raise SnapshotUnavailableError("sem snapshot")

        def save(self, snapshot: Snapshot) -> None: ...

    set_cache(SnapshotCache(Vazio()))
    assert TestClient(create_app()).get("/ready").status_code == 503
    set_cache(None)


def test_ready_reporta_idade_do_snapshot(client: TestClient) -> None:
    body = client.get("/ready").json()
    assert body["status"] == "ok"
    assert body["snapshot_age_seconds"] >= 0


def test_openapi_documenta_o_endpoint(client: TestClient) -> None:
    """A documentacao do endpoint e o OpenAPI gerado, entao ele precisa estar completo."""
    schema = client.get("/openapi.json").json()
    parametros = {p["name"] for p in schema["paths"]["/get-pool"]["get"]["parameters"]}
    assert parametros >= {
        "job_id",
        "instance_types",
        "family",
        "profile",
        "availability_zones",
        "exclude_pools",
        "min_samples",
        "strategy",
        "alternatives",
    }
    assert set(schema["paths"]["/get-pool"]["get"]["responses"]) >= {"200", "404", "422", "503"}
