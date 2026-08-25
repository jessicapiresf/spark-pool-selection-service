"""O endpoint.

Nenhuma chamada de rede acontece aqui na maior parte das vezes: o snapshot ja esta em
memoria, recarregado no maximo trinta segundos atras. A resposta filtra, sorteia e sai.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Response

from pool_selection.adapters.s3_snapshots import S3SnapshotStore
from pool_selection.config import Settings, settings
from pool_selection.domain.filters import PoolFilter, candidates
from pool_selection.domain.pool import Profile
from pool_selection.domain.selection import NoCandidatesError, ScoredPool, Strategy, select
from pool_selection.domain.snapshot import Snapshot
from pool_selection.entrypoints.api.schemas import (
    Alternative,
    AzOutlook,
    CapacityView,
    ErrorDetail,
    Evidence,
    PoolRecommendation,
    ProfileName,
    SnapshotView,
    StrategyName,
    split_csv,
)
from pool_selection.entrypoints.api.snapshot_cache import NoSnapshotAvailableError, SnapshotCache
from pool_selection.observability import configure_logging, emit_metrics

DESCRIPTION = """
Indica em qual pool de instancias spot um Spark job tem mais chance de rodar ate o fim.

O ranking e pre-calculado uma vez por minuto a partir de tres fontes: o historico de
termino dos jobs, a capacidade atual dos pools e o Spot placement score da AWS. Este
endpoint so consulta o resultado, ja em memoria.
""".strip()

_cache: SnapshotCache | None = None


def build_cache(config: Settings) -> SnapshotCache:
    return SnapshotCache(
        store=S3SnapshotStore(config.snapshot_bucket, config.snapshot_key),
        ttl_seconds=config.snapshot_ttl_seconds,
        stale_after_seconds=config.stale_after_seconds,
        fallback_pools=config.fallback_pools,
    )


def get_cache() -> SnapshotCache:
    global _cache
    if _cache is None:
        _cache = build_cache(settings())
    return _cache


def set_cache(cache: SnapshotCache | None) -> None:
    """Ponto de injecao para teste e para o ambiente local."""
    global _cache
    _cache = cache


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="Seletor de pools Spark",
        description=DESCRIPTION,
        version="0.1.0",
        docs_url="/docs",
    )

    @app.get(
        "/get-pool",
        response_model=PoolRecommendation,
        responses={
            404: {"model": ErrorDetail, "description": "Nenhum pool atende aos filtros."},
            503: {"model": ErrorDetail, "description": "Sem snapshot e sem lista de fallback."},
        },
        summary="Devolve o pool com maior chance de o job terminar",
    )
    def get_pool(
        response: Response,
        cache: Annotated[SnapshotCache, Depends(get_cache)],
        job_id: Annotated[
            str | None,
            Query(description="Ativa o historico daquele job. Sem ele, so o fator de AZ e usado."),
        ] = None,
        instance_types: Annotated[
            str | None, Query(description="Lista explicita, ex. `r6.xlarge,r6.2xlarge`.")
        ] = None,
        family: Annotated[
            str | None, Query(description="Prefixo de familia, ex. `r6` casa com `r6i` e `r6a`.")
        ] = None,
        profile: Annotated[
            ProfileName | None, Query(description="Perfil de uso de recurso.")
        ] = None,
        availability_zones: Annotated[
            str | None, Query(description="Restringe AZs, para jobs com localidade de dados.")
        ] = None,
        exclude_pools: Annotated[
            str | None, Query(description="Pools a ignorar, util para retry.")
        ] = None,
        min_samples: Annotated[
            float,
            Query(
                ge=0, description="Exige evidencia minima. Desliga a exploracao para job critico."
            ),
        ] = 0.0,
        strategy: Annotated[
            StrategyName, Query(description="`greedy` para resposta deterministica.")
        ] = "sampling",
        alternatives: Annotated[
            int, Query(ge=0, le=10, description="Quantos pools de reserva retornar.")
        ] = 2,
        seed: Annotated[
            int | None, Query(description="Semente do sorteio. Existe para teste e depuracao.")
        ] = None,
    ) -> PoolRecommendation:
        now = datetime.now(UTC)
        try:
            view = cache.get(now)
        except NoSnapshotAvailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

        pool_filter = PoolFilter(
            instance_types=split_csv(instance_types),
            family=family,
            profile=Profile(profile) if profile else None,
            availability_zones=split_csv(availability_zones),
            exclude_pools=split_csv(exclude_pools) or frozenset(),
            min_samples=min_samples,
        )
        eligible = candidates(view.snapshot, pool_filter)
        try:
            selection = select(
                view.snapshot,
                eligible,
                job_id=job_id,
                strategy=Strategy(strategy),
                rng=random.Random(seed),
                alternatives=alternatives,
            )
        except NoCandidatesError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

        if view.stale or view.degraded:
            response.headers["Cache-Control"] = "no-store"
        emit_metrics(
            {"Recommendations": 1, "SnapshotAgeSeconds": view.age_seconds},
            dimensions={"Component": "Api"},
            pool_id=selection.chosen.pool_id,
            degraded=view.degraded,
        )
        return _render(selection.chosen, selection.alternatives, view.snapshot, view, job_id, now)

    @app.get("/health", summary="Liveness. Responde sempre que o processo esta de pe.")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", summary="Readiness. Falha se nao ha snapshot utilizavel.")
    def ready(cache: Annotated[SnapshotCache, Depends(get_cache)]) -> dict[str, object]:
        try:
            view = cache.get()
        except NoSnapshotAvailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {"status": "ok", "snapshot_age_seconds": view.age_seconds, "stale": view.stale}

    return app


def _render(
    chosen: ScoredPool,
    alternatives: tuple[ScoredPool, ...],
    snapshot: Snapshot,
    view: object,
    job_id: str | None,
    now: datetime,
) -> PoolRecommendation:
    pool = chosen.pool
    job_evidence = (
        snapshot.job_fit.get(job_id, {}).get(pool.pool_id.instance_type.value) if job_id else None
    )
    profile_evidence = snapshot.profile_fit.get(pool.profile.value)
    if job_evidence is not None and job_evidence.trials > 0:
        source = "job_history"
    elif profile_evidence is not None and profile_evidence.trials > 0:
        source = "profile_prior"
    else:
        source = "none"

    capacity = None
    if pool.capacity is not None:
        capacity = CapacityView(
            free_slots=pool.capacity.free_slots,
            idle_instances=pool.capacity.warm_instances,
            falls_back_to_on_demand=pool.capacity.availability.value != "SPOT",
        )

    outlook = None
    if pool.forecast is not None:
        outlook = AzOutlook(
            spot_placement_score=pool.forecast.score,
            target_capacity=pool.forecast.target_capacity,
            age_seconds=max(0.0, (now - pool.forecast.scored_at).total_seconds()),
        )

    return PoolRecommendation(
        pool_id=pool.pool_id.value,
        instance_type=pool.pool_id.instance_type.value,
        availability_zone=pool.pool_id.availability_zone.value,
        score=round(chosen.score, 4),
        credible_interval=tuple(round(bound, 4) for bound in chosen.credible_interval),  # type: ignore[arg-type]
        evidence=Evidence(
            az_samples=round(pool.evidence.trials, 3),
            job_samples=round(job_evidence.trials, 3) if job_evidence else 0.0,
            source=source,
        ),
        capacity=capacity,
        az_outlook=outlook,
        alternatives=[
            Alternative(pool_id=item.pool_id, score=round(item.score, 4)) for item in alternatives
        ],
        snapshot=SnapshotView(
            age_seconds=round(view.age_seconds, 1),  # type: ignore[attr-defined]
            stale=view.stale,  # type: ignore[attr-defined]
        ),
        degraded=view.degraded,  # type: ignore[attr-defined]
    )


app = create_app()
