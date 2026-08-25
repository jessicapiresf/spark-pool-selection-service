from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pool_selection.adapters.memory import InMemorySnapshotStore
from pool_selection.domain.snapshot import Snapshot
from pool_selection.entrypoints.api.snapshot_cache import NoSnapshotAvailableError, SnapshotCache
from pool_selection.ports.snapshots import SnapshotUnavailableError


class BrokenStore:
    def __init__(self, failures: int = 999) -> None:
        self.calls = 0
        self._failures = failures

    def load(self) -> Snapshot:
        self.calls += 1
        raise SnapshotUnavailableError("bucket fora do ar")

    def save(self, snapshot: Snapshot) -> None: ...


def test_nao_bate_no_store_a_cada_request(snapshot: Snapshot, now: datetime) -> None:
    """E isso que faz o caminho de leitura nao ter I/O na maior parte das vezes."""
    store = InMemorySnapshotStore(snapshot)
    contador = {"n": 0}

    def contar() -> Snapshot:
        contador["n"] += 1
        return snapshot

    store.load = contar  # type: ignore[method-assign]
    cache = SnapshotCache(store, ttl_seconds=30)
    for _ in range(50):
        cache.get(now)
    assert contador["n"] == 1


def test_recarrega_depois_do_ttl(snapshot: Snapshot, now: datetime) -> None:
    store = InMemorySnapshotStore(snapshot)
    contador = {"n": 0}

    def contar() -> Snapshot:
        contador["n"] += 1
        return snapshot

    store.load = contar  # type: ignore[method-assign]
    cache = SnapshotCache(store, ttl_seconds=30)
    cache.get(now)
    cache.get(now + timedelta(seconds=31))
    assert contador["n"] == 2


def test_serve_o_ultimo_em_memoria_quando_o_store_cai(snapshot: Snapshot, now: datetime) -> None:
    """Um palpite informado e melhor que um 503."""
    store = InMemorySnapshotStore(snapshot)
    cache = SnapshotCache(store, ttl_seconds=0)
    assert cache.get(now).snapshot is snapshot

    def falhar() -> Snapshot:
        raise SnapshotUnavailableError("caiu")

    store.load = falhar  # type: ignore[method-assign]
    view = cache.get(now + timedelta(seconds=1))
    assert view.snapshot is snapshot
    assert not cache.is_healthy


def test_snapshot_velho_demais_continua_respondendo_mas_marcado(
    snapshot: Snapshot, now: datetime
) -> None:
    cache = SnapshotCache(InMemorySnapshotStore(snapshot), ttl_seconds=30, stale_after_seconds=300)
    assert not cache.get(now).stale
    assert cache.get(now + timedelta(seconds=301)).stale


def test_sem_snapshot_usa_lista_estatica_e_marca_degradado(now: datetime) -> None:
    cache = SnapshotCache(BrokenStore(), fallback_pools=("pool-r6.xlarge-us-east-1a",))
    view = cache.get(now)
    assert view.degraded
    assert len(view.snapshot.pools) == 1


def test_sem_snapshot_e_sem_fallback_nao_ha_o_que_responder(now: datetime) -> None:
    with pytest.raises(NoSnapshotAvailableError):
        SnapshotCache(BrokenStore()).get(now)


def test_store_quebrado_nao_e_consultado_a_cada_request(now: datetime) -> None:
    """Sem isso, uma queda do S3 viraria uma tempestade de chamadas."""
    store = BrokenStore()
    cache = SnapshotCache(store, ttl_seconds=30, fallback_pools=("pool-r6.xlarge-us-east-1a",))
    for _ in range(10):
        cache.get(now)
    assert store.calls == 1
