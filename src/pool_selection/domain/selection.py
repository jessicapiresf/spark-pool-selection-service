"""O sorteio e o teto por capacidade.

Pegar sempre o melhor conhecido tem tres problemas de uma vez: pool que parou de ser
recomendado nunca mais gera dado, um pico inteiro vai para o mesmo lugar, e um pool com
1 sucesso em 1 tentativa passa na frente de um com 200 em 210. Sortear dentro da faixa
de incerteza resolve os tres.

Sortear sozinho nao resolve o pico inteiro, so o suaviza. Cada request sorteia de forma
independente, entao a fatia que um pool recebe converge para a probabilidade dele vencer,
e essa probabilidade nao sabe nada sobre quantas vagas o pool tem. Dois mil jobs na mesma
janela ainda cabem quase todos num pool com trinta vagas livres, e ai o servico causa o
problema que existe para evitar. O teto por capacidade limita a fatia de cada pool ao que
ele consegue absorver, e o excedente escorre para o proximo melhor.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
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


# A folga que um pool precisa ter, em relacao ao candidato mais folgado, para receber todo
# o trafego. Com 3, um pool com um terco das vagas do maior ainda passa sem limite, e so
# pool pequeno de verdade e apertado.
DEFAULT_CONCENTRATION = 3.0


def capacity_caps(pools: Sequence[PoolEntry], concentration: float) -> dict[str, float]:
    """Fatia maxima do trafego que cada pool pode receber, pela folga relativa dele.

    A comparacao e contra o candidato mais folgado, nao contra a soma. Comparar com a soma
    apertaria todo mundo so por existirem muitos pools: com dez pools identicos, cada um
    ficaria com um decimo, quando capacidade igual significa exatamente que nenhum deles e
    mais fragil que o outro. Contra o maior, capacidade igual nao limita ninguem e o
    comportamento e o mesmo de antes.

    Pool que nao declara teto fica sem limite: sem saber quantas vagas ele tem, qualquer
    numero seria invencao.

    O que isto nao resolve: se todos os candidatos estao quase cheios, todos tem folga
    parecida e nenhum e limitado. Rotear nao cria capacidade, e esse caso pede pool novo.
    O sinal para perceber isso e o `free_slots` que vai na resposta.
    """
    known = {
        pool.pool_id.value: float(pool.capacity.free_slots)
        for pool in pools
        if pool.capacity is not None and pool.capacity.free_slots is not None
    }
    largest = max(known.values(), default=0.0)
    if largest <= 0.0:
        return {}
    return {pool_id: min(1.0, concentration * free / largest) for pool_id, free in known.items()}


def allocate(
    scored: Sequence[ScoredPool], caps: Mapping[str, float]
) -> list[tuple[ScoredPool, float]]:
    """Distribui a probabilidade em ordem de score, respeitando o teto de cada pool.

    O melhor pool leva tudo que couber no teto dele, o resto escorre para o proximo. Sem
    teto que morda, o primeiro leva 1.0 e a escolha e o mesmo argmax de antes.
    """
    allocation: list[tuple[ScoredPool, float]] = []
    remaining = 1.0
    for item in scored:
        if remaining <= 0.0:
            break
        share = min(remaining, caps.get(item.pool_id, 1.0))
        if share > 0.0:
            allocation.append((item, share))
            remaining -= share

    # Todo mundo no teto e ainda sobrou massa: os tetos sao menores que a demanda. Devolve
    # o resto na mesma proporcao, porque recusar o pedido seria pior que apertar um pool.
    if remaining > 1e-9 and allocation:
        assigned = 1.0 - remaining
        for index, (item, share) in enumerate(allocation):
            extra = (
                remaining * (share / assigned) if assigned > 0.0 else remaining / len(allocation)
            )
            allocation[index] = (item, share + extra)
    return allocation


def draw(allocation: Sequence[tuple[ScoredPool, float]], rng: random.Random) -> ScoredPool:
    """Um sorteio uniforme contra a distribuicao acumulada."""
    target = rng.random()
    cumulative = 0.0
    for item, share in allocation:
        cumulative += share
        if target < cumulative:
            return item
    return allocation[-1][0]


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
    concentration: float = DEFAULT_CONCENTRATION,
) -> Selection:
    """Escolhe um pool e devolve reservas junto.

    As alternativas saem do mesmo sorteio, ordenadas pelo que ele produziu. Quem perder
    instancia no pool sugerido faz fallback sem uma segunda chamada.

    `greedy` ignora o teto de capacidade de proposito: quem pede resposta deterministica
    quer o melhor pool, nao uma distribuicao. E o caminho de quem precisa reproduzir.
    """
    if not pools:
        raise NoCandidatesError("nenhum pool atende aos filtros informados")

    generator = rng or random.Random()
    scored = score_all(snapshot, pools, job_id, strategy, generator)
    # Desempate estavel pelo pool_id: duas chamadas com a mesma semente e o mesmo
    # snapshot precisam devolver a mesma coisa, inclusive quando dois scores empatam.
    scored.sort(key=lambda item: (-item.score, item.pool_id))

    if strategy is Strategy.GREEDY:
        chosen = scored[0]
    else:
        chosen = draw(allocate(scored, capacity_caps(pools, concentration)), generator)

    rest = [item for item in scored if item.pool_id != chosen.pool_id]
    return Selection(chosen=chosen, alternatives=tuple(rest[: max(0, alternatives)]))
