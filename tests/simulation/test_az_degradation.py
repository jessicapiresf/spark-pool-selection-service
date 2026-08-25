"""O unico teste que prova que o algoritmo funciona.

Os outros verificam pecas. Este derruba a disponibilidade de uma AZ no meio do fluxo,
roda o pipeline inteiro minuto a minuto e verifica que a recomendacao migra antes de o
prejuizo acumular. E o que separa "o codigo faz o que eu escrevi" de "o servico resolve
o problema".
"""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from gen_events import DEFAULT_POOLS, HealthSchedule, generate
from pool_selection.adapters.memory import (
    InMemoryCounterStore,
    InMemorySnapshotStore,
    StaticCapacityProvider,
    StaticInstanceCatalogProvider,
    StaticPlacementScoreProvider,
)
from pool_selection.config import Settings
from pool_selection.domain.catalog import InstanceSpec
from pool_selection.domain.events import Weights
from pool_selection.domain.filters import PoolFilter, candidates
from pool_selection.domain.pool import Profile
from pool_selection.domain.scoring import Capacity, PlacementForecast
from pool_selection.domain.selection import Strategy, select
from pool_selection.entrypoints.aggregator.handler import run
from pool_selection.entrypoints.ingestor.handler import ingest

DEGRADED_AZ = "us-east-1c"
SPECS = [
    InstanceSpec("r6.xlarge", 4, 32768),
    InstanceSpec("r6.2xlarge", 8, 65536),
    InstanceSpec("c6.xlarge", 4, 8192),
    InstanceSpec("m6.xlarge", 4, 16384),
    InstanceSpec("i3.xlarge", 4, 31232, 950),
]


class Simulation:
    """Roda o pipeline de verdade, minuto a minuto, com adapters em memoria."""

    def __init__(self, *, forecasts: dict | None = None, seed: int = 11) -> None:
        self.counters = InMemoryCounterStore()
        self.snapshots = InMemorySnapshotStore()
        self.settings = Settings()
        self.forecasts = forecasts or {}
        self.seed = seed
        self.capacities = {p: Capacity(max_capacity=60, used_count=6) for p in DEFAULT_POOLS}

    def feed(self, minutes: int, start: datetime, changes: list, batch: str) -> None:
        events = generate(
            minutes=minutes,
            per_minute=12,
            start=start,
            schedule=HealthSchedule(base=dict.fromkeys(DEFAULT_POOLS, 0.95), changes=changes),
            seed=self.seed,
        )
        lines = [json.dumps(event) for event in events]
        ingest(
            [{"bucket": "eventos", "key": batch, "etag": batch}],
            self.counters,
            lambda _bucket, _key: iter(lines),
            Weights(),
        )

    def aggregate(self, now: datetime) -> None:
        run(
            self.snapshots,
            self.counters,
            StaticCapacityProvider(self.capacities),
            StaticPlacementScoreProvider(self.forecasts),
            StaticInstanceCatalogProvider(SPECS),
            self.settings,
            now=now,
        )

    def recommendations(self, count: int = 300, **filters) -> Counter[str]:
        snapshot = self.snapshots.load()
        pools = candidates(snapshot, PoolFilter(**filters))
        return Counter(
            select(snapshot, pools, rng=random.Random(seed)).chosen.pool_id.split("-", 2)[-1]
            for seed in range(count)
        )


def share_of(recommendations: Counter[str], az: str) -> float:
    total = sum(recommendations.values())
    return sum(n for zone, n in recommendations.items() if zone.endswith(az)) / total


@pytest.fixture
def clock() -> datetime:
    return datetime.now(UTC).replace(second=0, microsecond=0)


def test_recomendacao_migra_quando_uma_az_aperta(clock: datetime) -> None:
    sim = Simulation()
    sim.feed(30, clock - timedelta(minutes=61), [], "saudavel")
    sim.aggregate(clock - timedelta(minutes=31))
    antes = share_of(sim.recommendations(), DEGRADED_AZ)

    sim.feed(30, clock - timedelta(minutes=31), [(0, DEGRADED_AZ, 0.05)], "degradado")
    sim.aggregate(clock)
    depois = share_of(sim.recommendations(), DEGRADED_AZ)

    assert antes > 0.15, "a AZ precisa comecar sendo recomendada, senao o teste nao prova nada"
    assert depois < antes / 3
    assert depois < 0.10


def test_migracao_acontece_dentro_da_meta_de_vinte_minutos(clock: datetime) -> None:
    """A meta adotada e migrar em ate 20 minutos so com o historico."""
    sim = Simulation()
    sim.feed(40, clock - timedelta(minutes=61), [], "saudavel")
    sim.aggregate(clock - timedelta(minutes=21))
    antes = share_of(sim.recommendations(), DEGRADED_AZ)

    sim.feed(20, clock - timedelta(minutes=21), [(0, DEGRADED_AZ, 0.05)], "queda")
    sim.aggregate(clock - timedelta(minutes=1))

    assert share_of(sim.recommendations(), DEGRADED_AZ) < antes / 2


def test_previsao_move_a_recomendacao_antes_de_qualquer_falha(clock: datetime) -> None:
    """O ponto de ter fonte preditiva: nao esperar um job morrer para reagir."""
    reativo = Simulation()
    reativo.feed(40, clock - timedelta(minutes=41), [], "saudavel")
    reativo.aggregate(clock)
    sem_previsao = share_of(reativo.recommendations(profile=Profile.MEMORY), DEGRADED_AZ)

    preditivo = Simulation(
        forecasts={
            (az, "memory"): PlacementForecast(2 if az == DEGRADED_AZ else 9, 20, clock)
            for az in ("us-east-1a", "us-east-1b", "us-east-1c")
        }
    )
    preditivo.feed(40, clock - timedelta(minutes=41), [], "saudavel")
    preditivo.aggregate(clock)
    com_previsao = share_of(preditivo.recommendations(profile=Profile.MEMORY), DEGRADED_AZ)

    assert sem_previsao > 0.15, "sem previsao a AZ ruim continua sendo recomendada"
    assert com_previsao < sem_previsao / 2


def test_evidencia_observada_vence_a_previsao_quando_elas_discordam(clock: datetime) -> None:
    """Score alto nao pode sustentar um pool onde os jobs estao morrendo de verdade."""
    sim = Simulation(
        forecasts={
            (az, "memory"): PlacementForecast(10, 20, clock)
            for az in ("us-east-1a", "us-east-1b", "us-east-1c")
        }
    )
    sim.feed(40, clock - timedelta(minutes=41), [(0, DEGRADED_AZ, 0.02)], "az_ruim")
    sim.aggregate(clock)

    assert share_of(sim.recommendations(profile=Profile.MEMORY), DEGRADED_AZ) < 0.12


def test_job_pesado_aprende_que_nao_cabe_em_maquina_pequena(clock: datetime) -> None:
    """Sem nenhum campo novo no evento: o historico do job conta isso sozinho."""
    sim = Simulation()
    sim.feed(60, clock - timedelta(minutes=61), [], "historico")
    sim.aggregate(clock)
    snapshot = sim.snapshots.load()

    pools = candidates(snapshot, PoolFilter(family="r6"))
    pesado = select(snapshot, pools, job_id="etl-pesado", strategy=Strategy.GREEDY)
    leve = select(snapshot, pools, job_id="etl-vendas", strategy=Strategy.GREEDY)

    assert pesado.chosen.score < leve.chosen.score
    assert "2xlarge" in pesado.chosen.pool_id


def test_pipeline_e_idempotente_sob_reentrega(clock: datetime) -> None:
    """Reprocessar o mesmo lote nao pode mudar a recomendacao."""
    uma_vez = Simulation()
    uma_vez.feed(30, clock - timedelta(minutes=31), [], "lote")
    uma_vez.aggregate(clock)

    duas_vezes = Simulation()
    duas_vezes.feed(30, clock - timedelta(minutes=31), [], "lote")
    duas_vezes.feed(30, clock - timedelta(minutes=31), [], "lote")
    duas_vezes.aggregate(clock)

    assert uma_vez.recommendations() == duas_vezes.recommendations()


def test_ranking_continua_cabendo_na_memoria_da_api(clock: datetime) -> None:
    sim = Simulation()
    sim.feed(120, clock - timedelta(minutes=121), [], "volume")
    sim.aggregate(clock)
    assert len(json.dumps(sim.snapshots.load().to_dict())) < 60_000
