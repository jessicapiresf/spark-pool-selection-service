from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from pool_selection.domain.catalog import InstanceSpec
from pool_selection.domain.pool import PoolId, Profile
from pool_selection.domain.scoring import Capacity, Evidence, PlacementForecast
from pool_selection.domain.snapshot import PoolEntry, Snapshot


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def specs() -> list[InstanceSpec]:
    return [
        InstanceSpec("r6.xlarge", vcpus=4, memory_mib=32768),
        InstanceSpec("r6.2xlarge", vcpus=8, memory_mib=65536),
        InstanceSpec("c6.xlarge", vcpus=4, memory_mib=8192),
        InstanceSpec("m6.xlarge", vcpus=4, memory_mib=16384),
        InstanceSpec("i3.xlarge", vcpus=4, memory_mib=31232, instance_storage_gb=950),
    ]


def entry(
    pool_id: str,
    successes: float = 0.0,
    failures: float = 0.0,
    *,
    profile: Profile = Profile.MEMORY,
    moment: datetime | None = None,
    capacity: Capacity | None = None,
    forecast: PlacementForecast | None = None,
) -> PoolEntry:
    return PoolEntry(
        pool_id=PoolId.parse(pool_id),
        profile=profile,
        evidence=Evidence(successes, failures, moment),
        capacity=capacity,
        forecast=forecast,
    )


@pytest.fixture
def snapshot(now: datetime) -> Snapshot:
    return Snapshot(
        generated_at=now,
        through_minute="2026-08-25T11:59",
        pools=(
            entry("pool-r6.xlarge-us-east-1a", 200, 10, moment=now),
            entry("pool-r6.xlarge-us-east-1b", 60, 90, moment=now),
            entry("pool-r6.xlarge-us-east-1c", 3, 1, moment=now),
            entry("pool-c6.xlarge-us-east-1a", 100, 5, profile=Profile.COMPUTE, moment=now),
        ),
        job_fit={"etl-pesado": {"r6.xlarge": Evidence(1.0, 8.0, now)}},
        profile_fit={"memory": Evidence(300.0, 40.0, now)},
        catalog={"r6.xlarge": "memory", "c6.xlarge": "compute"},
        catalog_refreshed_at=now,
    )
