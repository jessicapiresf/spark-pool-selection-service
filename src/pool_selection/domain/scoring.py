"""Os dois fatores, o decaimento e as fontes que ajustam o score.

Escassez da AZ e adequacao do job ao tipo sao perguntas diferentes, aprendem de fontes
diferentes e esquecem em ritmos diferentes. Com a mesma meia-vida nos dois, um job diario
esqueceria de si mesmo entre uma execucao e outra.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from pool_selection.domain.statistics import Posterior

AZ_HALF_LIFE = timedelta(minutes=20)
JOB_FIT_HALF_LIFE = timedelta(days=14)

# Prior do fator de adequacao: comeca otimista e cede rapido, porque falha e sinal forte
# e sucesso e sinal fraco. Com media 0,9 e forca 2, tres falhas levam a estimativa para
# 0,36, que e o numero que a arquitetura promete.
JOB_FIT_PRIOR_MEAN = 0.9
JOB_FIT_PRIOR_STRENGTH = 2.0

# Prior do fator de AZ quando existe placement score. Forca 4 deixa a previsao mandar
# enquanto nao ha falha recente e sair do caminho depois de ~10 observacoes reais.
PLACEMENT_PRIOR_STRENGTH = 4.0
PLACEMENT_PRIOR_MEAN_FLOOR = 0.35
PLACEMENT_PRIOR_MEAN_CEILING = 0.97

# Sem placement score o fator de AZ nasce sem opiniao.
NEUTRAL_AZ_PRIOR_MEAN = 0.75
NEUTRAL_AZ_PRIOR_STRENGTH = 1.0

# Quantas observacoes de um perfil bastam para ele determinar o prior de um job novo.
PROFILE_PRIOR_SATURATION = 50.0

# Pool que cai para on-demand quase nao falha por escassez, mas a economia do spot some
# junto. O bonus nasce pequeno de proposito.
ON_DEMAND_FALLBACK_BONUS = 0.15

# Quantas instancias um job tipicamente pede, quando nao ha previsao dizendo outra coisa.
DEFAULT_TARGET_CAPACITY = 10


class PoolState(StrEnum):
    ACTIVE = "ACTIVE"
    STOPPED = "STOPPED"
    DELETED = "DELETED"


class SpotAvailability(StrEnum):
    SPOT = "SPOT"
    ON_DEMAND = "ON_DEMAND"
    SPOT_WITH_FALLBACK = "SPOT_WITH_FALLBACK"


@dataclass(frozen=True, slots=True)
class Evidence:
    """Contadores com decaimento exponencial aplicado ate `updated_at`.

    Decair sucesso e falha juntos preserva a estimativa e alarga a faixa, que e o
    comportamento certo: evidencia velha vale menos, mas nao vira evidencia contraria.
    """

    successes: float = 0.0
    failures: float = 0.0
    updated_at: datetime | None = None

    @property
    def trials(self) -> float:
        return self.successes + self.failures

    def decayed_to(self, moment: datetime, half_life: timedelta) -> Evidence:
        if self.updated_at is None:
            return replace(self, updated_at=moment)
        elapsed = (moment - self.updated_at).total_seconds()
        if elapsed <= 0.0:
            return self
        factor = 0.5 ** (elapsed / half_life.total_seconds())
        return Evidence(
            successes=self.successes * factor,
            failures=self.failures * factor,
            updated_at=moment,
        )

    def accumulate(
        self, successes: float, failures: float, moment: datetime, half_life: timedelta
    ) -> Evidence:
        """`novo = anterior x decaimento + delta`. E o que torna a agregadora O(1)."""
        base = self.decayed_to(moment, half_life)
        return Evidence(
            successes=base.successes + successes,
            failures=base.failures + failures,
            updated_at=moment,
        )


@dataclass(frozen=True, slots=True)
class Capacity:
    """Recorte do que a API de pools da Databricks responde sobre um pool."""

    state: PoolState = PoolState.ACTIVE
    max_capacity: int | None = None
    used_count: int = 0
    idle_count: int = 0
    pending_used_count: int = 0
    pending_idle_count: int = 0
    availability: SpotAvailability = SpotAvailability.SPOT

    @property
    def free_slots(self) -> int | None:
        """`None` quando o pool nao declara teto, que e legal na Databricks."""
        if self.max_capacity is None:
            return None
        used = self.used_count + self.pending_used_count
        return max(0, self.max_capacity - used)

    @property
    def warm_instances(self) -> int:
        """Instancias ja adquiridas e ociosas: nao passam pelo mercado spot de novo."""
        return self.idle_count + self.pending_idle_count

    @property
    def is_selectable(self) -> bool:
        """Pool parado ou lotado sai da lista, por melhor que seja o historico dele."""
        if self.state is not PoolState.ACTIVE:
            return False
        return self.free_slots != 0


@dataclass(frozen=True, slots=True)
class PlacementForecast:
    """Spot placement score da AWS, de 1 a 10, por AZ e perfil."""

    score: int
    target_capacity: int
    scored_at: datetime

    def __post_init__(self) -> None:
        if not 1 <= self.score <= 10:
            raise ValueError("placement score da AWS vai de 1 a 10")

    @property
    def implied_success_rate(self) -> float:
        span = PLACEMENT_PRIOR_MEAN_CEILING - PLACEMENT_PRIOR_MEAN_FLOOR
        return PLACEMENT_PRIOR_MEAN_FLOOR + (self.score - 1) / 9.0 * span


def az_prior(forecast: PlacementForecast | None) -> Posterior:
    if forecast is None:
        return Posterior.from_prior(NEUTRAL_AZ_PRIOR_MEAN, NEUTRAL_AZ_PRIOR_STRENGTH)
    return Posterior.from_prior(forecast.implied_success_rate, PLACEMENT_PRIOR_STRENGTH)


def job_fit_prior(profile_evidence: Evidence | None) -> Posterior:
    """Job novo herda o comportamento medio dos outros jobs do mesmo perfil.

    O perfil desloca a media do prior, nunca a forca dele. Se o perfil aumentasse a
    forca, um job novo precisaria de muito mais falhas para se corrigir, que e o oposto
    do que se quer de quem ainda nao tem historico proprio.
    """
    if profile_evidence is None or profile_evidence.trials <= 0.0:
        return Posterior.from_prior(JOB_FIT_PRIOR_MEAN, JOB_FIT_PRIOR_STRENGTH)

    confidence = min(1.0, profile_evidence.trials / PROFILE_PRIOR_SATURATION)
    profile_rate = profile_evidence.successes / profile_evidence.trials
    mean = (1.0 - confidence) * JOB_FIT_PRIOR_MEAN + confidence * profile_rate
    return Posterior.from_prior(_clamp_open(mean), JOB_FIT_PRIOR_STRENGTH)


def posterior_for(prior: Posterior, evidence: Evidence | None) -> Posterior:
    if evidence is None:
        return prior
    return prior.updated(evidence.successes, evidence.failures)


@dataclass(frozen=True, slots=True)
class Factors:
    """As duas faixas que compoem o score de um pool para um job."""

    availability: Posterior
    fit: Posterior
    capacity: Capacity | None = None
    forecast: PlacementForecast | None = None
    target_capacity: int = DEFAULT_TARGET_CAPACITY

    @property
    def expected(self) -> float:
        return _clamp(self.adjusted_availability(self.availability.mean) * self.fit.mean)

    def sample(self, rng: random.Random) -> float:
        """Sorteia um ponto na faixa de cada fator e combina. Thompson Sampling."""
        availability = self.adjusted_availability(self.availability.sample(rng))
        return _clamp(availability * self.fit.sample(rng))

    def adjusted_availability(self, availability: float) -> float:
        """Aplica o que as fontes ao vivo sabem e o historico nao.

        O ajuste toca so o fator de disponibilidade. Instancia ociosa e queda para
        on-demand mudam o risco de nao conseguir maquina; nenhuma das duas muda se o job
        cabe no tipo de instancia, e por isso o fator de adequacao passa intacto.
        """
        if self.capacity is None:
            return _clamp(availability)

        adjusted = availability
        warm = self.capacity.warm_instances
        if warm > 0:
            covered = min(1.0, warm / max(1, self.target_capacity))
            adjusted = covered + (1.0 - covered) * adjusted

        if self.capacity.availability is SpotAvailability.ON_DEMAND:
            adjusted = 1.0
        elif self.capacity.availability is SpotAvailability.SPOT_WITH_FALLBACK:
            adjusted = ON_DEMAND_FALLBACK_BONUS + (1.0 - ON_DEMAND_FALLBACK_BONUS) * adjusted
        return _clamp(adjusted)

    @property
    def evidence_strength(self) -> tuple[float, float]:
        return (self.availability.strength, self.fit.strength)


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _clamp_open(value: float) -> float:
    return min(1.0 - 1e-9, max(1e-9, value))
