"""Contrato de entrada e saida do `/get-pool`.

Os filtros de query sao a parte chata deste endpoint, e e o Pydantic que resolve
validacao e mensagem de erro sem codigo manual. O OpenAPI em `/docs` sai daqui.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ProfileName = Literal["memory", "compute", "general", "storage"]
StrategyName = Literal["sampling", "greedy"]


def split_csv(raw: str | None) -> frozenset[str] | None:
    if raw is None:
        return None
    values = frozenset(item.strip() for item in raw.split(",") if item.strip())
    return values or None


class Evidence(BaseModel):
    az_samples: float = Field(description="Observacoes ponderadas do pool, ja decaidas.")
    job_samples: float = Field(description="Observacoes deste job neste tipo de instancia.")
    source: Literal["job_history", "profile_prior", "none"] = Field(
        description="De onde veio a recomendacao. `none` significa palpite."
    )


class CapacityView(BaseModel):
    free_slots: int | None = Field(description="`null` quando o pool nao declara teto.")
    idle_instances: int
    falls_back_to_on_demand: bool


class AzOutlook(BaseModel):
    spot_placement_score: int = Field(ge=1, le=10)
    target_capacity: int = Field(
        description="Capacidade perguntada a AWS. O score nao quer dizer nada sem ela."
    )
    age_seconds: float


class Alternative(BaseModel):
    pool_id: str
    score: float


class SnapshotView(BaseModel):
    age_seconds: float
    stale: bool


class PoolRecommendation(BaseModel):
    pool_id: str
    instance_type: str
    availability_zone: str
    score: float
    credible_interval: tuple[float, float]
    evidence: Evidence
    capacity: CapacityView | None = None
    az_outlook: AzOutlook | None = None
    alternatives: list[Alternative] = Field(default_factory=list)
    snapshot: SnapshotView
    degraded: bool


class ErrorDetail(BaseModel):
    detail: str
