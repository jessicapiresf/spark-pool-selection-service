"""Contrato do armazenamento de contadores.

Os contadores precisam sobreviver entre execucoes de funcoes que nao compartilham
memoria, e a entrega do SQS e at-least-once, entao o mesmo lote pode chegar duas vezes.
Por isso o contrato tem incremento atomico e reivindicacao de objeto, nao um `put`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class Scope(StrEnum):
    POOL = "POOL"
    JOB = "JOB"


@dataclass(frozen=True, slots=True)
class CounterKey:
    scope: Scope
    key: str
    minute: str


@dataclass(frozen=True, slots=True)
class CounterDelta:
    key: CounterKey
    successes: float
    failures: float


@dataclass(frozen=True, slots=True)
class MinuteCounters:
    """Tudo que aconteceu em um minuto, ja somado."""

    minute: str
    pools: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    jobs: Mapping[tuple[str, str], tuple[float, float]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.pools and not self.jobs


class CounterStore(Protocol):
    def add(self, deltas: Iterable[CounterDelta]) -> None:
        """Soma os deltas de forma atomica. Chamadas concorrentes nao se perdem."""
        ...

    def read_minute(self, minute: str) -> MinuteCounters:
        """Le tudo de um minuto em uma consulta so."""
        ...

    def claim(self, identity: str) -> bool:
        """Reivindica um objeto do S3. `False` se ja foi processado antes.

        E o que impede um lote reentregue de contar a mesma falha de novo e afundar um
        pool que estava bem.
        """
        ...
