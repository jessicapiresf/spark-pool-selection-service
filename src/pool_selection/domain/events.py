"""Classificacao dos eventos de termino de job.

Nem toda falha fala sobre o pool. Um bug de Spark penalizaria pools so porque times com
codigo instavel os usam, entao ele nao entra na conta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pool_selection.domain.pool import MalformedPoolIdError, PoolId


class Verdict(StrEnum):
    """O que o evento diz sobre a capacidade do pool."""

    CAPACITY_HELD = "capacity_held"
    CAPACITY_LOST = "capacity_lost"
    AMBIGUOUS = "ambiguous"
    IRRELEVANT = "irrelevant"


class MalformedEventError(ValueError):
    """Linha que nao da para interpretar como evento."""


@dataclass(frozen=True, slots=True)
class Weights:
    """`TIMED_OUT` pode ser escassez ou job lento, entao entra com peso parcial.

    E configuracao, nao codigo: se a plataforma confirmar que timeout e sempre escassez,
    `timed_out` vira 1.0 sem tocar no algoritmo.
    """

    timed_out: float = 0.5

    def __post_init__(self) -> None:
        if not 0.0 <= self.timed_out <= 1.0:
            raise ValueError("peso de TIMED_OUT precisa estar entre 0 e 1")


DEFAULT_WEIGHTS = Weights()


@dataclass(frozen=True, slots=True)
class Observation:
    """Quanto um evento soma de sucesso e de falha, ja ponderado."""

    successes: float = 0.0
    failures: float = 0.0

    @property
    def trials(self) -> float:
        return self.successes + self.failures

    def __add__(self, other: Observation) -> Observation:
        return Observation(self.successes + other.successes, self.failures + other.failures)


def classify(status: str, reason: str | None) -> Verdict:
    """Traduz `status` e `reason` crus para o que eles significam sobre capacidade.

    Status ou reason desconhecido cai em `IRRELEVANT` de proposito: um valor novo que a
    plataforma passe a emitir nao pode derrubar a ingestao nem virar falha por engano.
    """
    normalized_status = (status or "").strip().upper()
    if normalized_status == "SUCCESS":
        return Verdict.CAPACITY_HELD
    if normalized_status != "FAILED":
        return Verdict.IRRELEVANT

    match (reason or "").strip().upper():
        case "SPOT_INSTANCE_TERMINATION":
            return Verdict.CAPACITY_LOST
        case "TIMED_OUT":
            return Verdict.AMBIGUOUS
        case _:
            # SPARK_EXECUTION_ERROR e qualquer motivo novo: bug do job, nao do pool.
            return Verdict.IRRELEVANT


def observe(verdict: Verdict, weights: Weights = DEFAULT_WEIGHTS) -> Observation:
    match verdict:
        case Verdict.CAPACITY_HELD:
            return Observation(successes=1.0)
        case Verdict.CAPACITY_LOST:
            return Observation(failures=1.0)
        case Verdict.AMBIGUOUS:
            return Observation(successes=1.0 - weights.timed_out, failures=weights.timed_out)
        case Verdict.IRRELEVANT:
            return Observation()


@dataclass(frozen=True, slots=True)
class JobEvent:
    finished_at: datetime
    job_id: str
    pool_id: PoolId
    verdict: Verdict

    @property
    def minute(self) -> str:
        """Balde de agregacao. Minuto e a granularidade mais fina que o modelo usa."""
        return self.finished_at.strftime("%Y-%m-%dT%H:%M")

    def observation(self, weights: Weights = DEFAULT_WEIGHTS) -> Observation:
        return observe(self.verdict, weights)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> JobEvent:
        try:
            finished_at = parse_timestamp(payload["finished_at"])
            job_id = str(payload["job_id"])
            pool_id = PoolId.parse(str(payload["pool_id"]))
        except KeyError as exc:
            raise MalformedEventError(f"campo obrigatorio ausente: {exc.args[0]}") from exc
        except (MalformedPoolIdError, ValueError) as exc:
            raise MalformedEventError(str(exc)) from exc

        if not job_id:
            raise MalformedEventError("job_id vazio")

        return cls(
            finished_at=finished_at,
            job_id=job_id,
            pool_id=pool_id,
            verdict=classify(str(payload.get("status", "")), payload.get("reason")),
        )


def parse_timestamp(raw: object) -> datetime:
    """O `finished_at` e o relogio do modelo, entao normalizar fuso aqui nao e detalhe.

    O evento chega em UTC sem offset. Assumir naive como UTC evita que a idade de uma
    observacao dependa do fuso de quem esta rodando o codigo.
    """
    if isinstance(raw, datetime):
        parsed = raw
    else:
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError as exc:
            raise MalformedEventError(f"finished_at nao e ISO 8601: {raw!r}") from exc

    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
