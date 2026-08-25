from __future__ import annotations

import random
from collections import Counter
from datetime import datetime

import pytest

from conftest import entry
from pool_selection.domain.filters import PoolFilter, candidates
from pool_selection.domain.selection import NoCandidatesError, Strategy, select
from pool_selection.domain.snapshot import Snapshot


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
