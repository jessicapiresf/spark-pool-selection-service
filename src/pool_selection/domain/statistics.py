"""Beta em Python puro, sem scipy.

O miolo estatistico precisa rodar dentro de uma Lambda com cold start curto, entao
carregar scipy por causa de duas funcoes nao se paga. Sao ~80 linhas e o resultado e
exato o bastante para o que o servico faz com ele.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from math import exp, lgamma, log, log1p

_MAX_ITERATIONS = 200
_EPSILON = 3.0e-16
_TINY = 1.0e-300


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Fracao continuada de Lentz para a beta incompleta."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _TINY:
        d = _TINY
    d = 1.0 / d
    h = d

    for m in range(1, _MAX_ITERATIONS + 1):
        m2 = 2 * m
        numerator = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + numerator * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + numerator / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        h *= d * c

        numerator = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + numerator * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + numerator / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPSILON:
            break

    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """P(X <= x) para X ~ Beta(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0

    log_beta = lgamma(a + b) - lgamma(a) - lgamma(b)
    if x < (a + 1.0) / (a + b + 2.0):
        front = exp(log_beta + a * log(x) + b * log1p(-x))
        return front * _beta_continued_fraction(a, b, x) / a

    front = exp(log_beta + b * log1p(-x) + a * log(x))
    return 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b


def beta_quantile(a: float, b: float, probability: float) -> float:
    """Inversa por biseccao. Monotona e sem derivada, entao nao diverge."""
    if probability <= 0.0:
        return 0.0
    if probability >= 1.0:
        return 1.0

    low, high = 0.0, 1.0
    for _ in range(80):
        middle = (low + high) / 2.0
        if regularized_incomplete_beta(a, b, middle) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


@dataclass(frozen=True, slots=True)
class Posterior:
    """Beta(alpha, beta). O centro e a taxa de sucesso estimada, a largura e a incerteza."""

    alpha: float
    beta: float

    def __post_init__(self) -> None:
        if self.alpha <= 0.0 or self.beta <= 0.0:
            raise ValueError("alpha e beta precisam ser positivos")

    @classmethod
    def from_prior(cls, mean: float, strength: float) -> Posterior:
        """Prior expresso como "vale por `strength` observacoes centradas em `mean`"."""
        if not 0.0 < mean < 1.0:
            raise ValueError("media do prior precisa estar entre 0 e 1, exclusivo")
        if strength <= 0.0:
            raise ValueError("forca do prior precisa ser positiva")
        return cls(alpha=mean * strength, beta=(1.0 - mean) * strength)

    def updated(self, successes: float, failures: float) -> Posterior:
        return Posterior(alpha=self.alpha + successes, beta=self.beta + failures)

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def strength(self) -> float:
        """Quanta evidencia sustenta a estimativa, prior incluido."""
        return self.alpha + self.beta

    def credible_interval(self, level: float = 0.90) -> tuple[float, float]:
        tail = (1.0 - level) / 2.0
        return (
            beta_quantile(self.alpha, self.beta, tail),
            beta_quantile(self.alpha, self.beta, 1.0 - tail),
        )

    def sample(self, rng: random.Random) -> float:
        """Um ponto sorteado dentro da faixa. E o passo de Thompson Sampling."""
        return rng.betavariate(self.alpha, self.beta)
