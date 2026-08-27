from __future__ import annotations

from datetime import datetime

from conftest import entry
from pool_selection.domain.filters import PoolFilter, candidates
from pool_selection.domain.pool import Profile
from pool_selection.domain.scoring import Capacity, PoolState
from pool_selection.domain.snapshot import Snapshot


def ids(pools: list) -> set[str]:
    return {pool.pool_id.value for pool in pools}


def test_sem_filtro_todos_os_pools_competem(snapshot: Snapshot) -> None:
    assert len(candidates(snapshot, PoolFilter())) == 4


def test_lista_explicita_de_tipos(snapshot: Snapshot) -> None:
    found = candidates(snapshot, PoolFilter(instance_types=frozenset({"c6.xlarge"})))
    assert ids(found) == {"pool-c6.xlarge-us-east-1a"}


def test_familia_casa_variacoes_da_geracao(snapshot: Snapshot) -> None:
    assert len(candidates(snapshot, PoolFilter(family="r6"))) == 3


def test_perfil_separa_memoria_de_cpu(snapshot: Snapshot) -> None:
    found = candidates(snapshot, PoolFilter(profile=Profile.COMPUTE))
    assert ids(found) == {"pool-c6.xlarge-us-east-1a"}


def test_restringe_azs_para_localidade_de_dados(snapshot: Snapshot) -> None:
    found = candidates(snapshot, PoolFilter(availability_zones=frozenset({"us-east-1b"})))
    assert ids(found) == {"pool-r6.xlarge-us-east-1b"}


def test_exclui_pool_para_retry(snapshot: Snapshot) -> None:
    found = candidates(snapshot, PoolFilter(exclude_pools=frozenset({"pool-r6.xlarge-us-east-1a"})))
    assert "pool-r6.xlarge-us-east-1a" not in ids(found)


def test_min_samples_tira_quem_nao_tem_evidencia(snapshot: Snapshot) -> None:
    """E assim que job critico desliga a exploracao."""
    found = candidates(snapshot, PoolFilter(min_samples=50))
    assert "pool-r6.xlarge-us-east-1c" not in ids(found)  # so tem 4 amostras
    assert "pool-r6.xlarge-us-east-1a" in ids(found)


def test_filtros_se_combinam(snapshot: Snapshot) -> None:
    found = candidates(
        snapshot,
        PoolFilter(
            family="r6", availability_zones=frozenset({"us-east-1a", "us-east-1b"}), min_samples=100
        ),
    )
    assert ids(found) == {"pool-r6.xlarge-us-east-1a", "pool-r6.xlarge-us-east-1b"}


def test_pool_parado_nao_compete_mesmo_com_historico_otimo(now: datetime) -> None:
    """O historico de eventos nao sabe que o pool morreu; a API de pools sabe."""
    snapshot = Snapshot(
        generated_at=now,
        pools=(
            entry(
                "pool-r6.xlarge-us-east-1a",
                500,
                1,
                moment=now,
                capacity=Capacity(state=PoolState.DELETED),
            ),
            entry("pool-r6.xlarge-us-east-1b", 10, 5, moment=now, capacity=Capacity()),
        ),
    )
    assert ids(candidates(snapshot, PoolFilter())) == {"pool-r6.xlarge-us-east-1b"}


def test_pool_lotado_nao_compete(now: datetime) -> None:
    cheio = Capacity(max_capacity=10, used_count=10)
    snapshot = Snapshot(
        generated_at=now,
        pools=(entry("pool-r6.xlarge-us-east-1a", 500, 1, moment=now, capacity=cheio),),
    )
    assert candidates(snapshot, PoolFilter()) == []
