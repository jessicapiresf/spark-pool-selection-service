"""Consome a fila, classifica os eventos e soma contadores.

Duas coisas nao sao detalhe aqui. A pre-agregacao em memoria transforma milhares de
escritas em poucas, uma por par de pool e minuto, sem a qual o ingestor vira o gargalo. E
a reivindicacao por objeto do S3 impede que um lote reentregue conte a mesma falha de
novo: a entrega do SQS e at-least-once.
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote_plus

import boto3

from pool_selection.adapters.dynamodb_counters import DynamoDBCounterStore
from pool_selection.config import Settings, settings
from pool_selection.domain.events import JobEvent, MalformedEventError, Weights
from pool_selection.observability import configure_logging, emit_metrics, log
from pool_selection.ports.counters import CounterDelta, CounterKey, CounterStore, Scope


@dataclass
class IngestionReport:
    objects: int = 0
    skipped_objects: int = 0
    events: int = 0
    malformed: int = 0
    irrelevant: int = 0
    writes: int = 0

    def as_metrics(self) -> dict[str, float]:
        return {
            "ObjectsIngested": self.objects,
            "ObjectsSkippedAsDuplicate": self.skipped_objects,
            "EventsIngested": self.events,
            "EventsMalformed": self.malformed,
            "CounterWrites": self.writes,
        }


def ingest(
    records: list[dict[str, Any]],
    store: CounterStore,
    read_object: Any,
    weights: Weights,
) -> IngestionReport:
    """Nucleo testavel: recebe notificacoes do S3 e devolve o que fez."""
    report = IngestionReport()
    pool_totals: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    job_totals: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])

    for record in records:
        bucket = record["bucket"]
        key = record["key"]
        identity = f"{bucket}/{key}#{record.get('etag', '')}"

        if not store.claim(identity):
            report.skipped_objects += 1
            log("objeto_ja_processado", bucket=bucket, key=key)
            continue

        report.objects += 1
        for line in read_object(bucket, key):
            try:
                event = JobEvent.parse(json.loads(line))
            except (MalformedEventError, json.JSONDecodeError) as error:
                report.malformed += 1
                log("evento_invalido", bucket=bucket, key=key, error=str(error))
                continue

            report.events += 1
            observation = event.observation(weights)
            if observation.trials == 0.0:
                report.irrelevant += 1
                continue

            pool_bucket = pool_totals[(event.minute, event.pool_id.value)]
            pool_bucket[0] += observation.successes
            pool_bucket[1] += observation.failures

            fit_key = f"{event.job_id}#{event.pool_id.instance_type.value}"
            job_bucket = job_totals[(event.minute, fit_key)]
            job_bucket[0] += observation.successes
            job_bucket[1] += observation.failures

    deltas = [
        CounterDelta(CounterKey(Scope.POOL, key, minute), values[0], values[1])
        for (minute, key), values in pool_totals.items()
    ] + [
        CounterDelta(CounterKey(Scope.JOB, key, minute), values[0], values[1])
        for (minute, key), values in job_totals.items()
    ]
    store.add(deltas)
    report.writes = len(deltas)
    return report


def s3_notifications(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Extrai as notificacoes do S3 de dentro das mensagens do SQS."""
    for message in event.get("Records", ()):
        try:
            body = json.loads(message["body"])
        except (KeyError, json.JSONDecodeError):
            log("mensagem_sqs_invalida", message_id=message.get("messageId"))
            continue
        # Uma notificacao de teste do S3 nao tem Records e nao e erro.
        for notification in body.get("Records", ()):
            s3 = notification.get("s3", {})
            key = s3.get("object", {}).get("key")
            if not key:
                continue
            yield {
                "bucket": s3.get("bucket", {}).get("name", ""),
                "key": unquote_plus(key),
                "etag": s3.get("object", {}).get("eTag", ""),
            }


def _reader(client: Any) -> Any:
    def read(bucket: str, key: str) -> Iterator[str]:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        if key.endswith(".gz"):
            body = gzip.decompress(body)
        for line in body.decode("utf-8").splitlines():
            if line.strip():
                yield line

    return read


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    configure_logging()
    config: Settings = settings()
    store = DynamoDBCounterStore(config.counters_table)
    report = ingest(
        records=list(s3_notifications(event)),
        store=store,
        read_object=_reader(boto3.client("s3")),
        weights=Weights(timed_out=config.timed_out_weight),
    )
    emit_metrics(report.as_metrics(), dimensions={"Component": "Ingestor"})
    return {"ingested": report.events, "objects": report.objects}
