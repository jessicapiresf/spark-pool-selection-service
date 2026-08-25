from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pool_selection.adapters.memory import (
    InMemoryCounterStore,
    InMemorySnapshotStore,
    StaticCapacityProvider,
    StaticInstanceCatalogProvider,
    StaticPlacementScoreProvider,
)
from pool_selection.config import Settings
from pool_selection.domain.catalog import InstanceSpec
from pool_selection.domain.pool import Profile
from pool_selection.domain.scoring import (
    AZ_HALF_LIFE,
    Capacity,
    PlacementForecast,
    PoolState,
)
from pool_selection.domain.snapshot import Snapshot
from pool_selection.entrypoints.aggregator.handler import (
    aggregate,
    instance_types_by_profile,
    minutes_to_process,
    percentile,
    run,
    target_capacities,
)
from pool_selection.ports.counters import CounterDelta, CounterKey, Scope

NOW = datetime(2026, 8, 25, 12, 0, 30, tzinfo=UTC)


def store_com(minute: str, pool_id: str, successes: float, failures: float) -> InMemoryCounterStore:
    store = InMemoryCounterStore()
    store.add([CounterDelta(CounterKey(Scope.POOL, pool_id, minute), successes, failures)])
    return store


def test_primeira_execucao_pega_uma_janela_de_bootstrap() -> None:
    stamps, skipped = minutes_to_process(None, NOW, cap=360)
    assert len(stamps) == 20
    assert stamps[-1] == "2026-08-25T11:59"
    assert skipped == 0


def test_continua_de_onde_parou() -> None:
    stamps, _ = minutes_to_process("2026-08-25T11:56", NOW, cap=360)
    assert stamps == ["2026-08-25T11:57", "2026-08-25T11:58", "2026-08-25T11:59"]


def test_nada_a_fazer_quando_ja_esta_em_dia() -> None:
    assert minutes_to_process("2026-08-25T11:59", NOW, cap=360) == ([], 0)


def test_parada_longa_descarta_o_excedente_e_reporta() -> None:
    """Minuto antigo ja decaiu a quase nada; reprocessar custa mais do que vale."""
    stamps, skipped = minutes_to_process("2026-08-24T12:00", NOW, cap=60)
    assert len(stamps) == 60
    assert skipped > 1000
    assert stamps[-1] == "2026-08-25T11:59"


@pytest.mark.parametrize(
    ("values", "expected"), [([], 10), ([5], 5), ([1, 2, 3, 4, 100], 100), ([0, 0], 1)]
)
def test_percentil_para_capacidade_alvo(values: list[int], expected: int) -> None:
    assert percentile(values, 0.90) == expected


def test_capacidade_alvo_sai_do_tamanho_real_dos_pools() -> None:
    """O evento nao diz quantas instancias um job usou, entao a conta vem dos pools."""
    capacities = {
        "pool-r6.xlarge-us-east-1a": Capacity(used_count=20, idle_count=5),
        "pool-r6.xlarge-us-east-1b": Capacity(used_count=8),
        "pool-c6.xlarge-us-east-1a": Capacity(used_count=3),
    }
    catalog = {"r6.xlarge": "memory", "c6.xlarge": "compute"}
    assert target_capacities(capacities, catalog) == {"memory": 25, "compute": 3}


def test_agrupa_tipos_por_perfil_e_ignora_desconhecido() -> None:
    grouped = instance_types_by_profile(
        ["r6.xlarge", "r6.2xlarge", "c6.xlarge", "misterio.xlarge"],
        {"r6.xlarge": "memory", "r6.2xlarge": "memory", "c6.xlarge": "compute"},
    )
    assert grouped == {"memory": ["r6.xlarge", "r6.2xlarge"], "compute": ["c6.xlarge"]}


def test_acumula_contadores_no_snapshot() -> None:
    store = store_com("2026-08-25T11:59", "pool-r6.xlarge-us-east-1a", 8, 2)
    snapshot, report = aggregate(
        Snapshot(generated_at=NOW - timedelta(minutes=1), through_minute="2026-08-25T11:58"),
        store,
        {},
        {},
        {},
        None,
        NOW,
        360,
    )
    assert report.minutes == 1
    assert snapshot.through_minute == "2026-08-25T11:59"
    # O contador e do minuto 11:59 e o snapshot e de 12:00:30, entao 90 segundos de
    # decaimento ja se aplicaram. E o valor decaido que precisa estar la, nao o cru.
    decaimento = 0.5 ** (90 / AZ_HALF_LIFE.total_seconds())
    assert snapshot.pools[0].evidence.successes == pytest.approx(8.0 * decaimento)
    assert snapshot.pools[0].evidence.failures == pytest.approx(2.0 * decaimento)


def test_tudo_chega_ao_mesmo_relogio() -> None:
    """Senao um pool sem evento nenhum pareceria mais fresco do que e."""
    store = store_com("2026-08-25T11:59", "pool-r6.xlarge-us-east-1a", 5, 0)
    snapshot, _ = aggregate(Snapshot(generated_at=NOW), store, {}, {}, {}, None, NOW, 360)
    assert all(pool.evidence.updated_at == NOW for pool in snapshot.pools)


def test_candidatos_vem_da_api_de_pools_nao_do_historico() -> None:
    """Pool novo, que nunca apareceu em evento nenhum, seria invisivel."""
    capacities = {"pool-r6.xlarge-us-east-1c": Capacity()}
    snapshot, _ = aggregate(
        Snapshot(generated_at=NOW), InMemoryCounterStore(), capacities, {}, {}, None, NOW, 360
    )
    assert [pool.pool_id.value for pool in snapshot.pools] == ["pool-r6.xlarge-us-east-1c"]


def test_previsao_e_casada_por_az_e_perfil() -> None:
    forecast = PlacementForecast(9, 20, NOW)
    snapshot, _ = aggregate(
        Snapshot(generated_at=NOW),
        store_com("2026-08-25T11:59", "pool-r6.xlarge-us-east-1a", 1, 0),
        {},
        {("us-east-1a", "memory"): forecast},
        {"r6.xlarge": "memory"},
        NOW,
        NOW,
        360,
    )
    assert snapshot.pools[0].forecast == forecast
    assert snapshot.pools[0].profile is Profile.MEMORY


def test_evidencia_irrelevante_de_job_e_podada() -> None:
    store = InMemoryCounterStore()
    store.add([CounterDelta(CounterKey(Scope.JOB, "antigo#r6.xlarge", "2026-08-25T11:59"), 1, 0)])
    snapshot, _ = aggregate(Snapshot(generated_at=NOW), store, {}, {}, {}, None, NOW, 360)
    assert "antigo" in snapshot.job_fit

    muito_depois = NOW + timedelta(days=200)
    podado, report = aggregate(
        snapshot, InMemoryCounterStore(), {}, {}, {}, None, muito_depois, 360
    )
    assert podado.job_fit == {}
    assert report.pruned_jobs == 1


def test_perfil_agrega_o_comportamento_medio_dos_jobs() -> None:
    store = InMemoryCounterStore()
    store.add(
        [
            CounterDelta(CounterKey(Scope.JOB, "a#r6.xlarge", "2026-08-25T11:59"), 9, 1),
            CounterDelta(CounterKey(Scope.JOB, "b#r6.xlarge", "2026-08-25T11:59"), 1, 9),
        ]
    )
    snapshot, _ = aggregate(
        Snapshot(generated_at=NOW), store, {}, {}, {"r6.xlarge": "memory"}, NOW, NOW, 360
    )
    assert snapshot.profile_fit["memory"].trials == pytest.approx(20.0, rel=0.05)


def test_fonte_externa_indisponivel_nao_derruba_a_rodada() -> None:
    """Sem a Databricks o servico volta a operar so com o historico."""

    class Quebrado:
        def fetch(self):
            raise RuntimeError("token expirado")

    snapshots = InMemorySnapshotStore()
    report = run(
        snapshots,
        store_com("2026-08-25T11:59", "pool-r6.xlarge-us-east-1a", 3, 0),
        Quebrado(),
        None,
        None,
        Settings(),
        now=NOW,
    )
    assert report.pools == 1
    assert snapshots.saves == 1


def test_rodada_completa_com_as_tres_fontes() -> None:
    capacities = {
        "pool-r6.xlarge-us-east-1a": Capacity(max_capacity=40, used_count=10, idle_count=4),
        "pool-r6.xlarge-us-east-1c": Capacity(state=PoolState.STOPPED),
    }
    snapshots = InMemorySnapshotStore()
    report = run(
        snapshots,
        store_com("2026-08-25T11:59", "pool-r6.xlarge-us-east-1a", 10, 1),
        StaticCapacityProvider(capacities),
        StaticPlacementScoreProvider({("us-east-1a", "memory"): PlacementForecast(7, 14, NOW)}),
        StaticInstanceCatalogProvider([InstanceSpec("r6.xlarge", 4, 32768)]),
        Settings(),
        now=NOW,
    )
    saved = snapshots.load()
    assert report.pools == 2
    assert saved.catalog == {"r6.xlarge": "memory"}
    assert saved.entry("pool-r6.xlarge-us-east-1a").forecast.score == 7
    assert not saved.entry("pool-r6.xlarge-us-east-1c").is_selectable
