"""Junta as tres fontes e publica o ranking.

Roda uma vez por minuto, fora do caminho de request. O decaimento exponencial e
incremental por natureza, entao `anterior x decaimento + delta do ultimo minuto` evita
reler a janela inteira a cada execucao, o que com trezentos pools seriam cento e oito mil
itens por rodada, 1.440 vezes ao dia.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pool_selection.adapters.databricks_pools import DatabricksCapacityProvider
from pool_selection.adapters.dynamodb_counters import DynamoDBCounterStore
from pool_selection.adapters.ec2_catalog import EC2InstanceCatalogProvider
from pool_selection.adapters.ec2_placement import EC2PlacementScoreProvider
from pool_selection.adapters.s3_snapshots import S3SnapshotStore
from pool_selection.config import Settings, settings
from pool_selection.domain.catalog import build_catalog
from pool_selection.domain.pool import MalformedPoolIdError, PoolId, Profile
from pool_selection.domain.scoring import (
    AZ_HALF_LIFE,
    DEFAULT_TARGET_CAPACITY,
    JOB_FIT_HALF_LIFE,
    Capacity,
    Evidence,
    PlacementForecast,
)
from pool_selection.domain.snapshot import PoolEntry, Snapshot
from pool_selection.observability import configure_logging, emit_metrics, log
from pool_selection.ports.counters import CounterStore
from pool_selection.ports.snapshots import SnapshotStore, SnapshotUnavailableError
from pool_selection.ports.sources import (
    CapacityProvider,
    InstanceCatalogProvider,
    PlacementScoreProvider,
)

MINUTE_FORMAT = "%Y-%m-%dT%H:%M"

# Na primeira execucao nao ha marco anterior. Uma janela de uma meia-vida de AZ ja deixa o
# fator de escassez utilizavel sem varrer o dia inteiro.
BOOTSTRAP_MINUTES = 20

# Evidencia abaixo disso nao muda recomendacao nenhuma e so engorda o snapshot.
JOB_FIT_PRUNE_FLOOR = 0.01

TARGET_CAPACITY_PERCENTILE = 0.90


@dataclass
class AggregationReport:
    minutes: int = 0
    pools: int = 0
    tracked_jobs: int = 0
    pruned_jobs: int = 0
    skipped_minutes: int = 0
    placement_refreshed: bool = False
    catalog_refreshed: bool = False

    def as_metrics(self) -> dict[str, float]:
        return {
            "MinutesProcessed": self.minutes,
            "MinutesSkipped": self.skipped_minutes,
            "PoolsRanked": self.pools,
            "JobsTracked": self.tracked_jobs,
            "JobsPruned": self.pruned_jobs,
        }


def minutes_to_process(through: str | None, now: datetime, cap: int) -> tuple[list[str], int]:
    """Minutos fechados que faltam consumir, e quantos ficaram para tras.

    O corte e explicito: se a agregadora ficou parada tempo demais, os minutos mais
    antigos ja decairam a quase nada e reprocessa-los custaria mais do que valem. O que
    foi descartado sai em metrica, para nao virar buraco silencioso.
    """
    last_closed = (now - timedelta(minutes=1)).replace(second=0, microsecond=0)
    if through is None:
        start = last_closed - timedelta(minutes=BOOTSTRAP_MINUTES - 1)
    else:
        start = datetime.strptime(through, MINUTE_FORMAT).replace(tzinfo=UTC) + timedelta(minutes=1)

    if start > last_closed:
        return [], 0

    total = int((last_closed - start).total_seconds() // 60) + 1
    skipped = max(0, total - cap)
    start += timedelta(minutes=skipped)
    stamps = []
    cursor = start
    while cursor <= last_closed:
        stamps.append(cursor.strftime(MINUTE_FORMAT))
        cursor += timedelta(minutes=1)
    return stamps, skipped


def percentile(values: Sequence[int], fraction: float) -> int:
    if not values:
        return DEFAULT_TARGET_CAPACITY
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return max(1, ordered[index])


def target_capacities(
    capacities: Mapping[str, Capacity], catalog: Mapping[str, str]
) -> dict[str, int]:
    """Quantas instancias um job de cada perfil costuma pedir.

    O evento nao diz quantas instancias um job usou, entao a conta sai do tamanho real
    dos pools daquele perfil. Sem isso o placement score seria pedido para uma capacidade
    inventada, e o numero que voltasse nao diria nada.
    """
    by_profile: dict[str, list[int]] = defaultdict(list)
    for pool_id, capacity in capacities.items():
        try:
            parsed = PoolId.parse(pool_id)
        except MalformedPoolIdError:
            continue
        profile = catalog.get(parsed.instance_type.value, Profile.UNKNOWN.value)
        in_use = capacity.used_count + capacity.pending_used_count + capacity.idle_count
        if in_use > 0:
            by_profile[profile].append(in_use)
    return {
        profile: percentile(values, TARGET_CAPACITY_PERCENTILE)
        for profile, values in by_profile.items()
    }


def instance_types_by_profile(
    instance_types: Sequence[str], catalog: Mapping[str, str]
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for instance_type in instance_types:
        profile = catalog.get(instance_type)
        if profile and profile != Profile.UNKNOWN.value:
            grouped[profile].append(instance_type)
    return grouped


def aggregate(
    previous: Snapshot,
    store: CounterStore,
    capacities: Mapping[str, Capacity],
    forecasts: Mapping[tuple[str, str], PlacementForecast],
    catalog: Mapping[str, str],
    catalog_refreshed_at: datetime | None,
    now: datetime,
    max_minutes: int,
) -> tuple[Snapshot, AggregationReport]:
    report = AggregationReport()
    stamps, report.skipped_minutes = minutes_to_process(previous.through_minute, now, max_minutes)

    pool_evidence = {pool.pool_id.value: pool.evidence for pool in previous.pools}
    job_evidence: dict[str, dict[str, Evidence]] = {
        job_id: dict(per_type) for job_id, per_type in previous.job_fit.items()
    }

    for stamp in stamps:
        counters = store.read_minute(stamp)
        moment = datetime.strptime(stamp, MINUTE_FORMAT).replace(tzinfo=UTC)
        for pool_id, (successes, failures) in counters.pools.items():
            current = pool_evidence.get(pool_id, Evidence())
            pool_evidence[pool_id] = current.accumulate(successes, failures, moment, AZ_HALF_LIFE)
        for (job_id, instance_type), (successes, failures) in counters.jobs.items():
            per_type = job_evidence.setdefault(job_id, {})
            current = per_type.get(instance_type, Evidence())
            per_type[instance_type] = current.accumulate(
                successes, failures, moment, JOB_FIT_HALF_LIFE
            )
    report.minutes = len(stamps)

    # Tudo precisa chegar ao mesmo relogio, inclusive quem nao recebeu evento nenhum:
    # senao um pool parado pareceria mais fresco do que e.
    pool_evidence = {
        pool_id: evidence.decayed_to(now, AZ_HALF_LIFE)
        for pool_id, evidence in pool_evidence.items()
    }

    pruned_job_fit: dict[str, dict[str, Evidence]] = {}
    for job_id, per_type in job_evidence.items():
        kept = {}
        for instance_type, evidence in per_type.items():
            decayed = evidence.decayed_to(now, JOB_FIT_HALF_LIFE)
            if decayed.trials >= JOB_FIT_PRUNE_FLOOR:
                kept[instance_type] = decayed
            else:
                report.pruned_jobs += 1
        if kept:
            pruned_job_fit[job_id] = kept
    report.tracked_jobs = len(pruned_job_fit)

    profile_fit = _profile_fit(pruned_job_fit, catalog)
    entries = _build_entries(pool_evidence, capacities, forecasts, catalog)
    report.pools = len(entries)

    snapshot = Snapshot(
        generated_at=now,
        through_minute=stamps[-1] if stamps else previous.through_minute,
        pools=entries,
        job_fit=pruned_job_fit,
        profile_fit=profile_fit,
        catalog=dict(catalog),
        catalog_refreshed_at=catalog_refreshed_at,
    )
    return snapshot, report


def _profile_fit(
    job_fit: Mapping[str, Mapping[str, Evidence]], catalog: Mapping[str, str]
) -> dict[str, Evidence]:
    """Comportamento medio por perfil, que vira o prior de um job que nunca rodou."""
    totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for per_type in job_fit.values():
        for instance_type, evidence in per_type.items():
            profile = catalog.get(instance_type, Profile.UNKNOWN.value)
            totals[profile][0] += evidence.successes
            totals[profile][1] += evidence.failures
    return {
        profile: Evidence(successes=values[0], failures=values[1])
        for profile, values in totals.items()
    }


def _build_entries(
    pool_evidence: Mapping[str, Evidence],
    capacities: Mapping[str, Capacity],
    forecasts: Mapping[tuple[str, str], PlacementForecast],
    catalog: Mapping[str, str],
) -> tuple[PoolEntry, ...]:
    """A lista de candidatos vem da API de pools quando ela existe, nao do historico.

    Pool novo, que nunca apareceu em nenhum evento, seria invisivel se a lista saisse dos
    contadores. Sem a API, o historico volta a ser a unica fonte, que era o desenho
    original.
    """
    pool_ids = set(capacities) | set(pool_evidence) if capacities else set(pool_evidence)

    entries = []
    for pool_id in sorted(pool_ids):
        try:
            parsed = PoolId.parse(pool_id)
        except MalformedPoolIdError:
            log("pool_id_invalido_ignorado", pool_id=pool_id)
            continue
        profile = Profile(catalog.get(parsed.instance_type.value, Profile.UNKNOWN.value))
        entries.append(
            PoolEntry(
                pool_id=parsed,
                profile=profile,
                evidence=pool_evidence.get(pool_id, Evidence()),
                capacity=capacities.get(pool_id),
                forecast=forecasts.get((parsed.availability_zone.value, profile.value)),
            )
        )
    return tuple(entries)


def _needs_refresh(moment: datetime | None, now: datetime, ttl_seconds: int) -> bool:
    return moment is None or (now - moment).total_seconds() >= ttl_seconds


def run(
    snapshots: SnapshotStore,
    store: CounterStore,
    capacity_provider: CapacityProvider | None,
    placement_provider: PlacementScoreProvider | None,
    catalog_provider: InstanceCatalogProvider | None,
    config: Settings,
    now: datetime | None = None,
) -> AggregationReport:
    now = now or datetime.now(UTC)
    try:
        previous = snapshots.load()
    except SnapshotUnavailableError:
        log("sem_snapshot_anterior_reconstruindo")
        previous = Snapshot.empty()

    capacities: Mapping[str, Capacity] = {}
    if capacity_provider is not None:
        try:
            capacities = capacity_provider.fetch()
        except Exception as error:
            log("capacidade_indisponivel", error=str(error))

    catalog = dict(previous.catalog)
    catalog_refreshed_at = previous.catalog_refreshed_at
    known_types = sorted(
        {PoolId.parse(p).instance_type.value for p in capacities if _parseable(p)}
        | {pool.pool_id.instance_type.value for pool in previous.pools}
    )
    report_catalog = False
    if catalog_provider is not None and known_types:
        missing = [t for t in known_types if t not in catalog]
        if missing or _needs_refresh(catalog_refreshed_at, now, config.catalog_refresh_seconds):
            try:
                catalog.update(build_catalog(list(catalog_provider.describe(known_types))))
                catalog_refreshed_at = now
                report_catalog = True
            except Exception as error:
                log("catalogo_indisponivel", error=str(error))

    forecasts = {
        key: forecast
        for pool in previous.pools
        if pool.forecast is not None
        for key in ((pool.pool_id.availability_zone.value, pool.profile.value),)
        for forecast in (pool.forecast,)
    }
    stalest = min((f.scored_at for f in forecasts.values()), default=None)
    report_placement = False
    if placement_provider is not None and _needs_refresh(
        stalest, now, config.placement_refresh_seconds
    ):
        grouped = instance_types_by_profile(known_types, catalog)
        if grouped:
            try:
                refreshed = placement_provider.fetch(
                    grouped, target_capacities(capacities, catalog)
                )
                if refreshed:
                    forecasts = dict(refreshed)
                    report_placement = True
            except Exception as error:
                log("placement_score_indisponivel", error=str(error))

    snapshot, report = aggregate(
        previous=previous,
        store=store,
        capacities=capacities,
        forecasts=forecasts,
        catalog=catalog,
        catalog_refreshed_at=catalog_refreshed_at,
        now=now,
        max_minutes=config.aggregator_max_minutes,
    )
    report.catalog_refreshed = report_catalog
    report.placement_refreshed = report_placement
    snapshots.save(snapshot)
    return report


def _parseable(pool_id: str) -> bool:
    try:
        PoolId.parse(pool_id)
    except MalformedPoolIdError:
        return False
    return True


def handler(_event: dict[str, Any] | None = None, _context: Any = None) -> dict[str, Any]:
    configure_logging()
    config = settings()
    capacity_provider = (
        DatabricksCapacityProvider(config.databricks_host, config.databricks_token)
        if config.databricks_host and config.databricks_token
        else None
    )
    report = run(
        snapshots=S3SnapshotStore(config.snapshot_bucket, config.snapshot_key),
        store=DynamoDBCounterStore(config.counters_table),
        capacity_provider=capacity_provider,
        placement_provider=EC2PlacementScoreProvider(),
        catalog_provider=EC2InstanceCatalogProvider(),
        config=config,
    )
    emit_metrics(report.as_metrics(), dimensions={"Component": "Aggregator"})
    return {"pools": report.pools, "minutes": report.minutes}
