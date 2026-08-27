"""Snapshot em arquivo local.

Existe por causa do requisito de ambiente de desenvolvimento: um comando so, isolado, sem
passo manual. Com este adapter o `make dev` nao precisa de Docker nem de AWS simulada, e
quem clonar o repositorio ve o endpoint respondendo com o que o uv instalou e mais nada.

O formato e o mesmo do S3, gzip por cima do JSON, entao o arquivo gerado localmente e byte
a byte o que a agregadora publicaria. Nao ha um caminho de dado para desenvolvimento e
outro para producao: muda o lugar de onde se le, nao o que se le.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from pool_selection.domain.snapshot import Snapshot
from pool_selection.ports.snapshots import SnapshotUnavailableError


class FileSnapshotStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> Snapshot:
        try:
            raw = gzip.decompress(self._path.read_bytes())
        except FileNotFoundError as error:
            raise SnapshotUnavailableError(f"sem snapshot em {self._path}") from error
        except (OSError, ValueError) as error:
            raise SnapshotUnavailableError("snapshot corrompido") from error

        try:
            return Snapshot.from_dict(json.loads(raw))
        except (ValueError, KeyError) as error:
            raise SnapshotUnavailableError(f"snapshot ilegivel: {error}") from error

    def save(self, snapshot: Snapshot) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = gzip.compress(
            json.dumps(snapshot.to_dict(), separators=(",", ":")).encode("utf-8"), mtime=0
        )
        # Escrita em arquivo temporario e troca atomica: a API recarrega a cada 30
        # segundos e nao pode pegar o arquivo pela metade.
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_bytes(body)
        temporary.replace(self._path)

    @property
    def location(self) -> str:
        return str(self._path)
