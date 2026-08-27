"""Contadores no DynamoDB.

A chave e `MIN#<minuto>` com o pool ou o job na sort key, e nao o contrario. Assim a
agregadora le um minuto inteiro em uma Query so, em vez de uma por pool. O custo e que as
escritas de um minuto caem na mesma particao, o que a pre-agregacao no ingestor resolve.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

import boto3

from pool_selection.ports.counters import CounterDelta, MinuteCounters, Scope

MINUTE_PARTITION = "MIN#{minute}"
CLAIM_PREFIX = "OBJ#"
CLAIM_PARTITION = CLAIM_PREFIX + "{identity}"
CLAIM_SORT_KEY = "CLAIM"

# Limite do proprio BatchGetItem.
BATCH_GET_LIMIT = 100

# Os contadores sao delta consumido uma vez: o estado com decaimento mora no snapshot.
# Dois dias cobrem uma parada longa da agregadora com folga.
COUNTER_TTL_SECONDS = 2 * 24 * 3600
CLAIM_TTL_SECONDS = 7 * 24 * 3600


class DynamoDBCounterStore:
    def __init__(self, table_name: str, client: Any | None = None) -> None:
        self._table = table_name
        self._client = client or boto3.client("dynamodb")

    def add(self, deltas: Iterable[CounterDelta]) -> None:
        expires_at = int(time.time()) + COUNTER_TTL_SECONDS
        for delta in deltas:
            if delta.successes == 0.0 and delta.failures == 0.0:
                continue
            self._client.update_item(
                TableName=self._table,
                Key={
                    "pk": {"S": MINUTE_PARTITION.format(minute=delta.key.minute)},
                    "sk": {"S": f"{delta.key.scope.value}#{delta.key.key}"},
                },
                UpdateExpression=(
                    "ADD successes :s, failures :f SET expires_at = if_not_exists(expires_at, :t)"
                ),
                ExpressionAttributeValues={
                    ":s": {"N": repr(delta.successes)},
                    ":f": {"N": repr(delta.failures)},
                    ":t": {"N": str(expires_at)},
                },
            )

    def read_minute(self, minute: str) -> MinuteCounters:
        pools: dict[str, tuple[float, float]] = {}
        jobs: dict[tuple[str, str], tuple[float, float]] = {}
        paginator = self._client.get_paginator("query")
        pages = paginator.paginate(
            TableName=self._table,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": {"S": MINUTE_PARTITION.format(minute=minute)}},
        )
        for page in pages:
            for item in page.get("Items", ()):
                scope, _, key = item["sk"]["S"].partition("#")
                counts = (
                    float(item.get("successes", {}).get("N", "0")),
                    float(item.get("failures", {}).get("N", "0")),
                )
                if scope == Scope.POOL.value:
                    pools[key] = counts
                elif scope == Scope.JOB.value:
                    job_id, _, instance_type = key.partition("#")
                    jobs[(job_id, instance_type)] = counts
        return MinuteCounters(minute=minute, pools=pools, jobs=jobs)

    def processed(self, identities: Iterable[str]) -> set[str]:
        """Consulta em lote. O BatchGetItem aceita cem chaves por chamada."""
        wanted = list(dict.fromkeys(identities))
        found: set[str] = set()
        for start in range(0, len(wanted), BATCH_GET_LIMIT):
            page = wanted[start : start + BATCH_GET_LIMIT]
            keys = [
                {"pk": {"S": CLAIM_PARTITION.format(identity=item)}, "sk": {"S": CLAIM_SORT_KEY}}
                for item in page
            ]
            pending: dict[str, Any] = {self._table: {"Keys": keys}}
            while pending:
                response = self._client.batch_get_item(RequestItems=pending)
                for item in response.get("Responses", {}).get(self._table, ()):
                    found.add(item["pk"]["S"].removeprefix(CLAIM_PREFIX))
                # O DynamoDB pode devolver parte do lote e pedir para tentar o resto.
                pending = response.get("UnprocessedKeys") or {}
        return found

    def mark_processed(self, identities: Iterable[str]) -> None:
        expires_at = str(int(time.time()) + CLAIM_TTL_SECONDS)
        for identity in dict.fromkeys(identities):
            self._client.put_item(
                TableName=self._table,
                Item={
                    "pk": {"S": CLAIM_PARTITION.format(identity=identity)},
                    "sk": {"S": CLAIM_SORT_KEY},
                    "expires_at": {"N": expires_at},
                },
            )
