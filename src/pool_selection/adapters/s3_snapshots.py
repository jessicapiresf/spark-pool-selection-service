"""Snapshot em S3, comprimido.

O snapshot nao e banco: e um arquivo reconstruido do zero a cada minuto, escrito uma vez
e lido por toda instancia da API que sobe fria. O S3 escala por prefixo e aguenta esse
padrao; um item unico no DynamoDB concentraria as leituras em uma particao so.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import boto3
from botocore.exceptions import ClientError

from pool_selection.domain.snapshot import Snapshot
from pool_selection.ports.snapshots import SnapshotUnavailableError


class S3SnapshotStore:
    def __init__(self, bucket: str, key: str = "snapshot/pools.json.gz", client: Any | None = None):
        self._bucket = bucket
        self._key = key
        self._client = client or boto3.client("s3")

    def load(self) -> Snapshot:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._key)
            raw = gzip.decompress(response["Body"].read())
        except ClientError as error:
            raise SnapshotUnavailableError(
                f"nao foi possivel ler s3://{self._bucket}/{self._key}"
            ) from error
        except (OSError, ValueError) as error:
            raise SnapshotUnavailableError("snapshot corrompido") from error

        try:
            return Snapshot.from_dict(json.loads(raw))
        except (ValueError, KeyError) as error:
            raise SnapshotUnavailableError(f"snapshot ilegivel: {error}") from error

    def save(self, snapshot: Snapshot) -> None:
        body = gzip.compress(
            json.dumps(snapshot.to_dict(), separators=(",", ":")).encode("utf-8"), mtime=0
        )
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._key,
            Body=body,
            ContentType="application/json",
            CacheControl="max-age=30",
        )

    @property
    def location(self) -> str:
        return f"s3://{self._bucket}/{self._key}"
