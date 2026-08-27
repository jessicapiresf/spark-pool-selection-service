from __future__ import annotations

import random
from collections import Counter
from dataclasses import replace
from datetime import datetime

import pytest

from conftest import entry
from pool_selection.domain.filters import PoolFilter, candidates
from pool_selection.domain.scoring import Capacity, Factors
from pool_selection.domain.selection import (
    NoCandidatesError,
    ScoredPool,
    Strategy,
    allocate,
    capacity_caps,
    select,
)
from pool_selection.domain.snapshot import PoolEntry, Snapshot
from pool_selection.domain.statistics import Posterior

# Os fatores nao importam nos testes de alocacao: quem manda ali e o score ja calculado.
FACTORS = Factors(availability=Posterior(9.0, 1.0), fit=Posterior(9.0, 1.0))


def test_sem_candidato_e_erro_explicito(snapshot: Snapshot) -> None:
    with pytest.raises(NoCandidatesError):
        select(snapshot, [])


def test_greedy_e_deterministico(snapshot: Snapshot) -> None:
    escolhas = {
        select(snapshot, list(snapshot.pools), strategy=Strategy.GREEDY).chosen.pool_id
        for _ in range(20)
    }
    assert len(escolhas) == 1


def test_mesma_semente_devolve_a_mesma_resposta(snapshot: Snapshot) -> None:
    pools = list(snapshot.pools)
    primeira = select(snapshot, pools, rng=random.Random(42))
    segunda = select(snapshot, pools, rng=random.Random(42))
    assert primeira.chosen.pool_id == segunda.chosen.pool_id
    assert primeira.chosen.score == segunda.chosen.score


def test_sorteio_espalha_sem_estado_compartilhado(snapshot: Snapshot) -> None:
    """Um pico de duzentos jobs nao pode ir todo para o mesmo pool."""
    pools = candidates(snapshot, PoolFilter(family="r6"))
    escolhas = Counter(
        select(snapshot, pools, rng=random.Random(seed)).chosen.pool_id for seed in range(200)
    )
    assert len(escolhas) > 1


def test_pool_com_pouca_evidencia_volta_de_vez_em_quando(snapshot: Snapshot) -> None:
    """A recomendacao de hoje determina os dados de amanha; a faixa larga quebra o ciclo."""
    pools = candidates(snapshot, PoolFilter(family="r6"))
    escolhas = Counter(
        select(snapshot, pools, rng=random.Random(seed)).chosen.pool_id for seed in range(500)
    )
    assert escolhas["pool-r6.xlarge-us-east-1c"] > 0


def test_pool_ruim_com_evidencia_e_evitado_com_confianca(snapshot: Snapshot) -> None:
    pools = candidates(snapshot, PoolFilter(family="r6"))
    escolhas = Counter(
        select(snapshot, pools, rng=random.Random(seed)).chosen.pool_id for seed in range(500)
    )
    assert escolhas["pool-r6.xlarge-us-east-1b"] < 25  # 60/150 de taxa de sucesso


def test_alternativas_saem_do_mesmo_sorteio(snapshot: Snapshot) -> None:
    """Quem perder instancia faz fallback sem uma segunda chamada."""
    selection = select(snapshot, list(snapshot.pools), rng=random.Random(3), alternatives=2)
    assert len(selection.alternatives) == 2
    assert selection.chosen.pool_id not in {alt.pool_id for alt in selection.alternatives}
    assert selection.chosen.score >= selection.alternatives[0].score


def test_pode_pedir_zero_alternativas(snapshot: Snapshot) -> None:
    assert select(snapshot, list(snapshot.pools), alternatives=0).alternatives == ()


def test_historico_do_job_muda_a_recomendacao(snapshot: Snapshot) -> None:
    """`etl-pesado` morre em r6.xlarge, e o historico dele conta isso sozinho."""
    pools = candidates(snapshot, PoolFilter(family="r6"))
    generico = select(snapshot, pools, job_id="etl-vendas", strategy=Strategy.GREEDY)
    pesado = select(snapshot, pools, job_id="etl-pesado", strategy=Strategy.GREEDY)
    assert pesado.chosen.score < generico.chosen.score


def test_empate_desempata_de_forma_estavel(now: datetime) -> None:
    iguais = Snapshot(
        generated_at=now,
        pools=(
            entry("pool-r6.xlarge-us-east-1b", 10, 1, moment=now),
            entry("pool-r6.xlarge-us-east-1a", 10, 1, moment=now),
        ),
    )
    escolhas = {
        select(iguais, list(iguais.pools), strategy=Strategy.GREEDY).chosen.pool_id
        for _ in range(10)
    }
    assert escolhas == {"pool-r6.xlarge-us-east-1a"}


def caps_de(*pares: tuple[str, int | None]) -> list[PoolEntry]:
    """Candidatos com capacidade livre declarada. `None` significa pool sem teto."""
    return [
        entry(
            pool_id,
            100,
            5,
            capacity=Capacity(max_capacity=None if livre is None else livre, used_count=0),
        )
        for pool_id, livre in pares
    ]


def test_capacidade_igual_nao_limita_ninguem() -> None:
    """Se todos tem a mesma folga, nenhum e mais fragil, e nada muda."""
    pools = caps_de(
        ("pool-r6.xlarge-us-east-1a", 50),
        ("pool-r6.xlarge-us-east-1b", 50),
        ("pool-r6.xlarge-us-east-1c", 50),
    )
    assert set(capacity_caps(pools, 3.0).values()) == {1.0}


def test_muitos_pools_iguais_nao_apertam_uns_aos_outros() -> None:
    """Comparar com a soma faria o teto cair so por existirem mais candidatos."""
    poucos = caps_de(*[(f"pool-r6.xlarge-us-east-1{c}", 40) for c in "abc"])
    muitos = caps_de(*[(f"pool-r6.xlarge-us-east-1{c}", 40) for c in "abcdefghij"])
    assert capacity_caps(poucos, 3.0) == {p.pool_id.value: 1.0 for p in poucos}
    assert capacity_caps(muitos, 3.0) == {p.pool_id.value: 1.0 for p in muitos}


def test_pool_pequeno_recebe_teto_proporcional_a_folga() -> None:
    pools = caps_de(("pool-r6.xlarge-us-east-1a", 60), ("pool-r6.xlarge-us-east-1b", 2))
    caps = capacity_caps(pools, 3.0)
    assert caps["pool-r6.xlarge-us-east-1a"] == 1.0
    assert caps["pool-r6.xlarge-us-east-1b"] == pytest.approx(0.1)


def test_pool_sem_teto_declarado_fica_sem_limite() -> None:
    pools = caps_de(("pool-r6.xlarge-us-east-1a", 60), ("pool-r6.xlarge-us-east-1b", None))
    assert "pool-r6.xlarge-us-east-1b" not in capacity_caps(pools, 3.0)


def test_sem_capacidade_conhecida_nao_ha_teto() -> None:
    assert capacity_caps([entry("pool-r6.xlarge-us-east-1a", 10, 1)], 3.0) == {}


def test_excedente_escorre_para_o_proximo_melhor() -> None:
    pools = caps_de(("pool-r6.xlarge-us-east-1a", 6), ("pool-r6.xlarge-us-east-1b", 60))
    scored = [
        ScoredPool(pool=pools[0], factors=FACTORS, score=0.99),
        ScoredPool(pool=pools[1], factors=FACTORS, score=0.50),
    ]
    allocation = allocate(scored, capacity_caps(pools, 3.0))

    # O melhor tem folga para 30% do trafego; os outros 70% vao para o segundo.
    assert allocation[0][0].pool_id == "pool-r6.xlarge-us-east-1a"
    assert allocation[0][1] == pytest.approx(0.3)
    assert allocation[1][1] == pytest.approx(0.7)
    assert sum(share for _, share in allocation) == pytest.approx(1.0)


def test_alocacao_sempre_soma_um_mesmo_com_todos_apertados() -> None:
    """Recusar o pedido seria pior que apertar um pool: o job vai subir de qualquer jeito."""
    pools = caps_de(("pool-r6.xlarge-us-east-1a", 1), ("pool-r6.xlarge-us-east-1b", 60))
    scored = [ScoredPool(pool=p, factors=FACTORS, score=0.9) for p in pools]
    caps = {p.pool_id.value: 0.2 for p in pools}
    allocation = allocate(scored, caps)
    assert sum(share for _, share in allocation) == pytest.approx(1.0)


def test_pico_nao_afunda_o_pool_pequeno(snapshot: Snapshot) -> None:
    """O requisito e conseguir obter um pool no pico, nao so obter uma resposta.

    O pool pequeno tem o melhor historico e venceria quase sempre no sorteio puro. Com o
    teto, ele leva uma fatia compativel com as vagas que tem.
    """
    pequeno = entry(
        "pool-r6.xlarge-us-east-1a", 400, 5, capacity=Capacity(max_capacity=4, used_count=0)
    )
    grande = entry(
        "pool-r6.xlarge-us-east-1b", 40, 40, capacity=Capacity(max_capacity=120, used_count=0)
    )
    pools = [pequeno, grande]
    snapshot = replace(snapshot, pools=tuple(pools))

    escolhas = Counter(
        select(snapshot, pools, rng=random.Random(seed)).chosen.pool_id for seed in range(600)
    )
    fatia_do_pequeno = escolhas["pool-r6.xlarge-us-east-1a"] / sum(escolhas.values())

    assert fatia_do_pequeno < 0.20, "o pool pequeno absorveu mais do que aguenta"
    assert escolhas["pool-r6.xlarge-us-east-1b"] > 0


def test_greedy_ignora_o_teto_e_continua_deterministico(snapshot: Snapshot) -> None:
    """Quem pede resposta reproduzivel quer o melhor pool, nao uma distribuicao."""
    pequeno = entry(
        "pool-r6.xlarge-us-east-1a", 400, 5, capacity=Capacity(max_capacity=4, used_count=0)
    )
    grande = entry(
        "pool-r6.xlarge-us-east-1b", 40, 40, capacity=Capacity(max_capacity=120, used_count=0)
    )
    pools = [pequeno, grande]
    snapshot = replace(snapshot, pools=tuple(pools))

    escolhas = {
        select(snapshot, pools, strategy=Strategy.GREEDY, rng=random.Random(s)).chosen.pool_id
        for s in range(20)
    }
    assert escolhas == {"pool-r6.xlarge-us-east-1a"}


def test_escolhido_nunca_aparece_nas_alternativas(snapshot: Snapshot) -> None:
    pools = caps_de(
        ("pool-r6.xlarge-us-east-1a", 6),
        ("pool-r6.xlarge-us-east-1b", 60),
        ("pool-r6.xlarge-us-east-1c", 60),
    )
    snapshot = replace(snapshot, pools=tuple(pools))
    for seed in range(50):
        chosen = select(snapshot, pools, rng=random.Random(seed), alternatives=3)
        assert chosen.chosen.pool_id not in {alt.pool_id for alt in chosen.alternatives}
