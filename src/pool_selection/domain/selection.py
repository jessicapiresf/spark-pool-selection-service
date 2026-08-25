"""O sorteio.

Pegar sempre o melhor conhecido tem tres problemas de uma vez: pool que parou de ser
recomendado nunca mais gera dado, um pico inteiro vai para o mesmo lugar, e um pool com
1 sucesso em 1 tentativa passa na frente de um com 200 em 210. Sortear dentro da faixa
de incerteza resolve os tres.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from pool_selection.domain.scoring import Factors
from pool_selection.domain.snapshot import PoolEntry, Snapshot


class Strategy(StrEnum):
    SAMPLING = "sampling"
    GREEDY = "greedy"


class NoCandidatesError(LookupError):
    """Nenhum pool sobreviveu ao filtro."""


@dataclass(frozen=True, slots=True)
class ScoredPool:
    pool: PoolEntry
    factors: Factors
    score: float

    @property
    def pool_id(self) -> str:
        return self.pool.pool_id.value

    @property
    def credible_interval(self) -> tuple[float, float]:
        """Faixa do fator de disponibilidade, que e o que carrega a incerteza util."""
        return self.factors.availability.credible_interval()


@dataclass(frozen=True, slots=True)
class Selection:
    chosen: ScoredPool
    alternatives: tuple[ScoredPool, ...]


def score_all(
    snapshot: Snapshot,
    pools: Sequence[PoolEntry],
    job_id: str | None,
    strategy: Strategy,
    rng: random.Random,
) -> list[ScoredPool]:
    scored = []
    for pool in pools:
        factors = snapshot.factors_for(pool, job_id)
        value = factors.expected if strategy is Strategy.GREEDY else factors.sample(rng)
        scored.append(ScoredPool(pool=pool, factors=factors, score=value))
    return scored


def select(
    snapshot: Snapshot,
    pools: Sequence[PoolEntry],
    *,
    job_id: str | None = None,
    strategy: Strategy = Strategy.SAMPLING,
    rng: random.Random | None = None,
    alternatives: int = 2,
) -> Selection:
    """Escolhe um pool e devolve reservas junto.

    As alternativas saem do mesmo sorteio, ordenadas pelo que ele produziu. Quem perder
    instancia no pool sugerido faz fallback sem uma segunda chamada.
    """
    if not pools:
        raise NoCandidatesError("nenhum pool atende aos filtros informados")

    scored = score_all(snapshot, pools, job_id, strategy, rng or random.Random())
    # Desempate estavel pelo pool_id: duas chamadas com a mesma semente e o mesmo
    # snapshot precisam devolver a mesma coisa, inclusive quando dois scores empatam.
    scored.sort(key=lambda item: (-item.score, item.pool_id))
    return Selection(chosen=scored[0], alternatives=tuple(scored[1 : 1 + max(0, alternatives)]))
