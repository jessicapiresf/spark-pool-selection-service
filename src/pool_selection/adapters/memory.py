"""Implementacoes em memoria, para teste e para o ambiente local.

O dominio nunca importa AWS, entao trocar os adapters por estes deixa a suite inteira
rodar em milissegundos e o `make dev` subir sem credencial nenhuma.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from pool_selection.domain.catalog import InstanceSpec
from pool_selection.domain.scoring import Capacity, PlacementForecast
from pool_selection.domain.snapshot import Snapshot
from pool_selection.ports.counters import CounterDelta, MinuteCounters, Scope
from pool_selection.ports.snapshots import SnapshotUnavailableError


class InMemoryCounterStore:
    def __init__(self) -> None:
        self._pools: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
        self._jobs: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
        self._claimed: set[str] = set()

    def add(self, deltas: Iterable[CounterDelta]) -> None:
        for delta in deltas:
            if delta.key.scope is Scope.POOL:
                bucket = self._pools[(delta.key.minute, delta.key.key)]
            else:
                job_id, _, instance_type = delta.key.key.partition("#")
                bucket = self._jobs[(delta.key.minute, job_id, instance_type)]
            bucket[0] += delta.successes
            bucket[1] += delta.failures

    def read_minute(self, minute: str) -> MinuteCounters:
        return MinuteCounters(
            minute=minute,
            pools={
                pool_id: (values[0], values[1])
                for (bucket, pool_id), values in self._pools.items()
                if bucket == minute
            },
            jobs={
                (job_id, instance_type): (values[0], values[1])
                for (bucket, job_id, instance_type), values in self._jobs.items()
                if bucket == minute
            },
        )

    def claim(self, identity: str) -> bool:
        if identity in self._claimed:
            return False
        self._claimed.add(identity)
        return True


class InMemorySnapshotStore:
    def __init__(self, snapshot: Snapshot | None = None) -> None:
        self._snapshot = snapshot
        self.saves = 0

    def load(self) -> Snapshot:
        if self._snapshot is None:
            raise SnapshotUnavailableError("nenhum snapshot publicado")
        return self._snapshot

    def save(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot
        self.saves += 1


class StaticCapacityProvider:
    def __init__(self, capacities: Mapping[str, Capacity]) -> None:
        self._capacities = dict(capacities)

    def fetch(self) -> Mapping[str, Capacity]:
        return dict(self._capacities)


class StaticPlacementScoreProvider:
    def __init__(self, forecasts: Mapping[tuple[str, str], PlacementForecast]) -> None:
        self._forecasts = dict(forecasts)

    def fetch(
        self,
        instance_types_by_profile: Mapping[str, Sequence[str]],
        _targets: Mapping[str, int],
    ) -> Mapping[tuple[str, str], PlacementForecast]:
        return {
            key: forecast
            for key, forecast in self._forecasts.items()
            if key[1] in instance_types_by_profile
        }


class StaticInstanceCatalogProvider:
    def __init__(self, specs: Sequence[InstanceSpec]) -> None:
        self._specs = {spec.instance_type: spec for spec in specs}

    def describe(self, instance_types: Sequence[str]) -> Sequence[InstanceSpec]:
        return [self._specs[name] for name in instance_types if name in self._specs]
