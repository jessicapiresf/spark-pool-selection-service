"""O ranking pre-calculado que a API serve.

O snapshot e ao mesmo tempo o resultado publicado e o estado da agregadora. Como o
decaimento exponencial e incremental, `anterior x decaimento + delta` reconstroi o
proximo a partir deste, e nao ha um segundo lugar guardando estado que possa divergir.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pool_selection.domain.events import parse_timestamp
from pool_selection.domain.pool import PoolId, Profile
from pool_selection.domain.scoring import (
    DEFAULT_TARGET_CAPACITY,
    Capacity,
    Evidence,
    Factors,
    PlacementForecast,
    PoolState,
    SpotAvailability,
    az_prior,
    job_fit_prior,
    posterior_for,
)

SNAPSHOT_VERSION = 1


@dataclass(frozen=True, slots=True)
class PoolEntry:
    """Um pool no ranking, com tudo que as tres fontes sabem sobre ele."""

    pool_id: PoolId
    profile: Profile = Profile.UNKNOWN
    evidence: Evidence = field(default_factory=Evidence)
    capacity: Capacity | None = None
    forecast: PlacementForecast | None = None

    @property
    def is_selectable(self) -> bool:
        """Sem informacao de capacidade o pool continua elegivel: o historico basta."""
        return True if self.capacity is None else self.capacity.is_selectable

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pool_id": self.pool_id.value,
            "profile": self.profile.value,
            "evidence": _evidence_to_dict(self.evidence),
        }
        if self.capacity is not None:
            payload["capacity"] = {
                "state": self.capacity.state.value,
                "max_capacity": self.capacity.max_capacity,
                "used_count": self.capacity.used_count,
                "idle_count": self.capacity.idle_count,
                "pending_used_count": self.capacity.pending_used_count,
                "pending_idle_count": self.capacity.pending_idle_count,
                "availability": self.capacity.availability.value,
            }
        if self.forecast is not None:
            payload["forecast"] = {
                "score": self.forecast.score,
                "target_capacity": self.forecast.target_capacity,
                "scored_at": self.forecast.scored_at.isoformat(),
            }
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PoolEntry:
        capacity = None
        if (raw := payload.get("capacity")) is not None:
            capacity = Capacity(
                state=PoolState(raw["state"]),
                max_capacity=raw.get("max_capacity"),
                used_count=raw.get("used_count", 0),
                idle_count=raw.get("idle_count", 0),
                pending_used_count=raw.get("pending_used_count", 0),
                pending_idle_count=raw.get("pending_idle_count", 0),
                availability=SpotAvailability(raw.get("availability", "SPOT")),
            )
        forecast = None
        if (raw := payload.get("forecast")) is not None:
            forecast = PlacementForecast(
                score=raw["score"],
                target_capacity=raw["target_capacity"],
                scored_at=parse_timestamp(raw["scored_at"]),
            )
        return cls(
            pool_id=PoolId.parse(payload["pool_id"]),
            profile=Profile(payload.get("profile", Profile.UNKNOWN.value)),
            evidence=_evidence_from_dict(payload.get("evidence")),
            capacity=capacity,
            forecast=forecast,
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Tudo que a API precisa para responder sem tocar na rede."""

    generated_at: datetime
    through_minute: str | None = None
    pools: tuple[PoolEntry, ...] = ()
    job_fit: Mapping[str, Mapping[str, Evidence]] = field(default_factory=dict)
    profile_fit: Mapping[str, Evidence] = field(default_factory=dict)
    catalog: Mapping[str, str] = field(default_factory=dict)
    catalog_refreshed_at: datetime | None = None

    @classmethod
    def empty(cls) -> Snapshot:
        return cls(generated_at=datetime.now(UTC))

    def age_seconds(self, now: datetime) -> float:
        return max(0.0, (now - self.generated_at).total_seconds())

    def entry(self, pool_id: str) -> PoolEntry | None:
        return next((pool for pool in self.pools if pool.pool_id.value == pool_id), None)

    def factors_for(self, pool: PoolEntry, job_id: str | None) -> Factors:
        """Combina as duas faixas para um pool e um job.

        Sem `job_id` o fator de adequacao fica no prior do perfil: a resposta continua
        valida, mas deixa de distinguir job leve de job pesado.
        """
        availability = posterior_for(az_prior(pool.forecast), pool.evidence)
        profile_evidence = self.profile_fit.get(pool.profile.value)
        prior = job_fit_prior(profile_evidence)
        fit_evidence = None
        if job_id is not None:
            fit_evidence = self.job_fit.get(job_id, {}).get(pool.pool_id.instance_type.value)
        return Factors(
            availability=availability,
            fit=posterior_for(prior, fit_evidence),
            capacity=pool.capacity,
            forecast=pool.forecast,
            target_capacity=(
                pool.forecast.target_capacity if pool.forecast else DEFAULT_TARGET_CAPACITY
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SNAPSHOT_VERSION,
            "generated_at": self.generated_at.isoformat(),
            "through_minute": self.through_minute,
            "pools": [pool.to_dict() for pool in self.pools],
            "job_fit": {
                job_id: {
                    instance_type: _evidence_to_dict(evidence)
                    for instance_type, evidence in per_type.items()
                }
                for job_id, per_type in self.job_fit.items()
            },
            "profile_fit": {
                profile: _evidence_to_dict(evidence)
                for profile, evidence in self.profile_fit.items()
            },
            "catalog": dict(self.catalog),
            "catalog_refreshed_at": (
                self.catalog_refreshed_at.isoformat() if self.catalog_refreshed_at else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Snapshot:
        version = payload.get("version", SNAPSHOT_VERSION)
        if version != SNAPSHOT_VERSION:
            raise ValueError(f"snapshot na versao {version}, esperada {SNAPSHOT_VERSION}")
        refreshed = payload.get("catalog_refreshed_at")
        return cls(
            generated_at=parse_timestamp(payload["generated_at"]),
            through_minute=payload.get("through_minute"),
            pools=tuple(PoolEntry.from_dict(entry) for entry in payload.get("pools", ())),
            job_fit={
                job_id: {
                    instance_type: _evidence_from_dict(raw)
                    for instance_type, raw in per_type.items()
                }
                for job_id, per_type in payload.get("job_fit", {}).items()
            },
            profile_fit={
                profile: _evidence_from_dict(raw)
                for profile, raw in payload.get("profile_fit", {}).items()
            },
            catalog=dict(payload.get("catalog", {})),
            catalog_refreshed_at=parse_timestamp(refreshed) if refreshed else None,
        )


def fallback_snapshot(pool_ids: Iterable[str], now: datetime) -> Snapshot:
    """Ultimo recurso: lista estatica de pools conhecidos, sem evidencia nenhuma.

    Serve para o servico responder algo em vez de 503 quando nao ha snapshot nenhum. A
    resposta sai marcada como degradada para ninguem confundir isso com recomendacao.
    """
    entries = []
    for pool_id in pool_ids:
        try:
            entries.append(PoolEntry(pool_id=PoolId.parse(pool_id)))
        except ValueError:
            continue
    return Snapshot(generated_at=now, pools=tuple(entries))


def _evidence_to_dict(evidence: Evidence) -> dict[str, Any]:
    return {
        "successes": round(evidence.successes, 6),
        "failures": round(evidence.failures, 6),
        "updated_at": evidence.updated_at.isoformat() if evidence.updated_at else None,
    }


def _evidence_from_dict(payload: Mapping[str, Any] | None) -> Evidence:
    if not payload:
        return Evidence()
    updated = payload.get("updated_at")
    return Evidence(
        successes=float(payload.get("successes", 0.0)),
        failures=float(payload.get("failures", 0.0)),
        updated_at=parse_timestamp(updated) if updated else None,
    )
