"""Contrato do armazenamento do snapshot."""

from __future__ import annotations

from typing import Protocol

from pool_selection.domain.snapshot import Snapshot


class SnapshotUnavailableError(RuntimeError):
    """Nao foi possivel ler o snapshot."""


class SnapshotStore(Protocol):
    def load(self) -> Snapshot:
        """Devolve o snapshot publicado ou levanta `SnapshotUnavailableError`."""
        ...

    def save(self, snapshot: Snapshot) -> None: ...
