"""Cache do snapshot em memoria, com degradacao.

O caminho de leitura nao pode fazer chamada de rede na maior parte das vezes: e isso que
faz 2.000 req/s caberem em cerca de dez execucoes simultaneas. O cache tambem e o que
sustenta a promessa de nunca devolver erro se der para evitar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pool_selection.domain.snapshot import Snapshot, fallback_snapshot
from pool_selection.observability import log
from pool_selection.ports.snapshots import SnapshotStore, SnapshotUnavailableError


class NoSnapshotAvailableError(RuntimeError):
    """Nem snapshot, nem cache, nem lista de fallback: nao ha o que responder."""


@dataclass(frozen=True, slots=True)
class SnapshotView:
    snapshot: Snapshot
    age_seconds: float
    stale: bool
    degraded: bool


class SnapshotCache:
    def __init__(
        self,
        store: SnapshotStore,
        ttl_seconds: int = 30,
        stale_after_seconds: int = 300,
        fallback_pools: tuple[str, ...] = (),
    ) -> None:
        self._store = store
        self._ttl = ttl_seconds
        self._stale_after = stale_after_seconds
        self._fallback_pools = fallback_pools
        self._cached: Snapshot | None = None
        self._loaded_at: datetime | None = None
        self._last_error: str | None = None

    def get(self, now: datetime | None = None) -> SnapshotView:
        now = now or datetime.now(UTC)
        if self._should_reload(now):
            self._reload(now)

        if self._cached is None:
            if not self._fallback_pools:
                raise NoSnapshotAvailableError(self._last_error or "sem snapshot publicado")
            # Lista estatica com escolha uniforme. E palpite, e sai marcado como tal.
            return SnapshotView(
                snapshot=fallback_snapshot(self._fallback_pools, now),
                age_seconds=0.0,
                stale=True,
                degraded=True,
            )

        age = self._cached.age_seconds(now)
        return SnapshotView(
            snapshot=self._cached,
            age_seconds=age,
            stale=age >= self._stale_after,
            degraded=False,
        )

    def _should_reload(self, now: datetime) -> bool:
        if self._loaded_at is None:
            return True
        return (now - self._loaded_at).total_seconds() >= self._ttl

    def _reload(self, now: datetime) -> None:
        try:
            self._cached = self._store.load()
            self._loaded_at = now
            self._last_error = None
        except SnapshotUnavailableError as error:
            # Serve o ultimo que tem em memoria. Um palpite informado e melhor que um 503.
            self._last_error = str(error)
            self._loaded_at = now
            log(
                "snapshot_indisponivel_servindo_cache",
                error=str(error),
                has_cache=self._cached is not None,
            )

    @property
    def is_healthy(self) -> bool:
        return self._cached is not None and self._last_error is None
