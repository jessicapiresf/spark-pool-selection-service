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

    def processed(self, identities: Iterable[str]) -> set[str]:
        """Quais desses objetos ja foram contados antes.

        Consulta sem efeito colateral. E o que impede um lote reentregue de contar a mesma
        falha de novo e afundar um pool que estava bem.
        """
        ...

    def mark_processed(self, identities: Iterable[str]) -> None:
        """Marca os objetos como contados.

        Chamado **depois** de os contadores terem sido gravados, nunca antes. Marcar antes
        parece mais seguro e nao e: se a funcao morre entre a marca e a escrita, a
        reentrega encontra o objeto marcado, pula, e aquele evento some para sempre. Na
        ordem certa, o mesmo acidente causa contagem dobrada, que decai junto com o resto
        e nao abre buraco no historico.
        """
        ...
