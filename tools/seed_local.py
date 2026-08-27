"""Prepara o ambiente local: gera eventos e roda o pipeline uma vez.

Depois disso a API tem um snapshot de verdade para servir, entao `make dev` sobe algo que
responde com dado que parece real em vez de um 503.

Dois modos. Em `file`, que e o padrao, nada de AWS: contadores em memoria e o snapshot num
arquivo. E o que faz o requisito de "um unico comando" nao arrastar Docker junto. Em
`aws`, o mesmo pipeline contra o LocalStack, para exercitar os adapters de verdade.

O que muda entre os dois e de onde se le e onde se grava, nunca o que roda no meio: a
ingestora e a agregadora sao as mesmas em qualquer modo, e o snapshot gerado e byte a byte
o que a producao publicaria.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(__file__.rsplit("/", 1)[0]))

from gen_events import DEFAULT_POOLS, HealthSchedule, generate
from pool_selection.adapters.dynamodb_counters import DynamoDBCounterStore
from pool_selection.adapters.file_snapshots import FileSnapshotStore
from pool_selection.adapters.memory import (
    InMemoryCounterStore,
    StaticCapacityProvider,
    StaticInstanceCatalogProvider,
    StaticPlacementScoreProvider,
)
from pool_selection.adapters.s3_snapshots import S3SnapshotStore
from pool_selection.config import Settings
from pool_selection.domain.catalog import InstanceSpec
from pool_selection.domain.events import Weights
from pool_selection.domain.scoring import Capacity, PlacementForecast
from pool_selection.entrypoints.aggregator.handler import run
from pool_selection.entrypoints.ingestor.handler import ingest

BUCKET = "pool-selection-local"
TABLE = "pool-selection-counters"
EVENTS_KEY = "eventos/local.json"
DEFAULT_SNAPSHOT_PATH = ".local/snapshot/pools.json.gz"

# Sem Databricks e sem AWS de verdade no ambiente local, estas duas fontes sao encenadas.
# A terceira, o historico, e real: sai dos eventos gerados.
SPECS = [
    InstanceSpec("r6.xlarge", 4, 32768),
    InstanceSpec("r6.2xlarge", 8, 65536),
    InstanceSpec("c6.xlarge", 4, 8192),
    InstanceSpec("m6.xlarge", 4, 16384),
    InstanceSpec("i3.xlarge", 4, 31232, 950),
]


def ensure_resources(s3, dynamodb) -> None:
    try:
        s3.create_bucket(Bucket=BUCKET)
    except ClientError as error:
        if error.response["Error"]["Code"] not in (
            "BucketAlreadyOwnedByYou",
            "BucketAlreadyExists",
        ):
            raise

    try:
        dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.get_waiter("table_exists").wait(TableName=TABLE)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceInUseException":
            raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Popula o ambiente local")
    parser.add_argument("--minutes", type=int, default=90)
    parser.add_argument("--per-minute", type=int, default=12)
    parser.add_argument("--unhealthy-az", default="us-east-1c")
    parser.add_argument(
        "--mode",
        choices=("file", "aws"),
        default="aws" if os.environ.get("AWS_ENDPOINT_URL") else "file",
        help="`file` nao usa AWS nenhuma. `aws` fala com o LocalStack.",
    )
    args = parser.parse_args(argv)

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    events = list(
        generate(
            minutes=args.minutes,
            per_minute=args.per_minute,
            start=now - timedelta(minutes=args.minutes + 1),
            schedule=HealthSchedule(
                base=dict.fromkeys(DEFAULT_POOLS, 0.95),
                changes=[(args.minutes // 2, args.unhealthy_az, 0.08)],
            ),
        )
    )
    lines = [json.dumps(event) for event in events]

    counters: Any
    snapshots: Any
    if args.mode == "aws":
        s3 = boto3.client("s3")
        dynamodb = boto3.client("dynamodb")
        ensure_resources(s3, dynamodb)
        s3.put_object(Bucket=BUCKET, Key=EVENTS_KEY, Body=("\n".join(lines)).encode("utf-8"))
        print(f"{len(events)} eventos em s3://{BUCKET}/{EVENTS_KEY}")
        counters = DynamoDBCounterStore(TABLE, dynamodb)
        snapshots = S3SnapshotStore(BUCKET, "snapshot/pools.json.gz", s3)
    else:
        events_path = Path(os.environ.get("EVENTS_PATH", ".local/eventos.jsonl"))
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"{len(events)} eventos em {events_path}")
        counters = InMemoryCounterStore()
        snapshots = FileSnapshotStore(os.environ.get("SNAPSHOT_PATH", DEFAULT_SNAPSHOT_PATH))

    report = ingest(
        [{"bucket": BUCKET, "key": EVENTS_KEY, "etag": str(now.timestamp())}],
        counters,
        lambda _bucket, _key: iter(lines),
        Weights(),
    )
    print(f"ingestao: {report.events} eventos em {report.writes} escritas")

    capacities = {
        pool: Capacity(max_capacity=60, used_count=8, idle_count=2) for pool in DEFAULT_POOLS
    }
    forecasts = {
        (az, "memory"): PlacementForecast(3 if az == args.unhealthy_az else 9, 20, now)
        for az in ("us-east-1a", "us-east-1b", "us-east-1c")
    }
    aggregation = run(
        snapshots,
        counters,
        StaticCapacityProvider(capacities),
        StaticPlacementScoreProvider(forecasts),
        StaticInstanceCatalogProvider(SPECS),
        # Sem folga: o seed acabou de gravar os contadores, nao ha lote em transito.
        Settings(aggregator_max_minutes=360, aggregator_lag_minutes=0),
        now=now,
    )
    print(f"snapshot publicado em {snapshots.location}")
    print(f"{aggregation.pools} pools, {aggregation.tracked_jobs} jobs")
    print(f"AZ encenada como apertada: {args.unhealthy_az}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
