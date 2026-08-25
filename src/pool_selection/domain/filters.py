"""Restricoes que o cliente pode impor sobre quais pools podem ser devolvidos.

Jobs tem caracteristicas diferentes de uso de recurso, entao quem chama precisa poder
dizer "so memoria" ou "so essa familia" antes de o sorteio acontecer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pool_selection.domain.pool import Profile
from pool_selection.domain.snapshot import PoolEntry, Snapshot


@dataclass(frozen=True, slots=True)
class PoolFilter:
    instance_types: frozenset[str] | None = None
    family: str | None = None
    profile: Profile | None = None
    availability_zones: frozenset[str] | None = None
    exclude_pools: frozenset[str] = frozenset()
    min_samples: float = 0.0

    def matches(self, pool: PoolEntry) -> bool:
        if pool.pool_id.value in self.exclude_pools:
            return False
        if (
            self.instance_types is not None
            and pool.pool_id.instance_type.value not in self.instance_types
        ):
            return False
        if self.family is not None and not pool.pool_id.instance_type.in_family(self.family):
            return False
        if self.profile is not None and pool.profile is not self.profile:
            return False
        if (
            self.availability_zones is not None
            and pool.pool_id.availability_zone.value not in self.availability_zones
        ):
            return False
        return not (self.min_samples > 0.0 and pool.evidence.trials < self.min_samples)


def candidates(snapshot: Snapshot, pool_filter: PoolFilter) -> list[PoolEntry]:
    """Pools que passam no filtro do cliente e nas portas que a capacidade fecha.

    Pool parado, apagado ou lotado sai daqui, nao do sorteio. Um pool que nao existe mais
    nao deveria nem competir, por melhor que o historico dele tenha sido.
    """
    return [pool for pool in snapshot.pools if pool.is_selectable and pool_filter.matches(pool)]


def selectable_profiles(pools: Iterable[PoolEntry]) -> set[Profile]:
    return {pool.profile for pool in pools if pool.profile is not Profile.UNKNOWN}
