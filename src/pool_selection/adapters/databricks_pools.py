"""Capacidade atual, via API de instance pools da Databricks.

O historico de falhas responde "esse pool costuma aguentar?". Esta fonte responde "esse
pool tem espaco agora?", e e ela que tambem define quem sao os candidatos: pool novo, que
nunca apareceu em nenhum evento, seria invisivel se a lista viesse do historico.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from pool_selection.domain.pool import MalformedPoolIdError, PoolId
from pool_selection.domain.scoring import Capacity, PoolState, SpotAvailability

logger = logging.getLogger(__name__)

LIST_PATH = "/api/2.0/instance-pools/list"
DEFAULT_TIMEOUT_SECONDS = 5.0

# `zone_id` pode vir como "auto", que significa que a Databricks escolhe a AZ. Nesse caso
# nao da para dizer em que AZ o pool esta, e o pool fica fora da lista de candidatos.
AUTO_ZONE = "auto"


class DatabricksCapacityProvider:
    def __init__(
        self,
        host: str,
        token: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Any | None = None,
    ) -> None:
        self._host = host.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def fetch(self) -> Mapping[str, Capacity]:
        payload = self._list_pools()
        capacities: dict[str, Capacity] = {}
        for raw in payload.get("instance_pools", ()):
            parsed = self._parse(raw)
            if parsed is not None:
                capacities[parsed[0]] = parsed[1]
        return capacities

    def _list_pools(self) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._host}{LIST_PATH}",
            headers={"Authorization": f"Bearer {self._token}"},
            method="GET",
        )
        with self._opener(request, timeout=self._timeout) as response:
            return json.loads(response.read())

    def _parse(self, raw: Mapping[str, Any]) -> tuple[str, Capacity] | None:
        node_type = raw.get("node_type_id")
        zone = (raw.get("aws_attributes") or {}).get("zone_id")
        if not node_type or not zone or zone == AUTO_ZONE:
            logger.debug(
                "pool sem tipo ou AZ autoritativos, ignorado: %s", raw.get("instance_pool_id")
            )
            return None

        try:
            # Tipo e AZ vem da API, nao parseados do nome do pool.
            pool_id = PoolId.build(str(node_type), str(zone))
        except MalformedPoolIdError:
            logger.warning("pool com tipo ou AZ fora do formato esperado: %s / %s", node_type, zone)
            return None

        stats = raw.get("stats") or raw
        return pool_id.value, Capacity(
            state=_state(raw.get("state")),
            max_capacity=raw.get("max_capacity"),
            used_count=int(stats.get("used_count", 0)),
            idle_count=int(stats.get("idle_count", 0)),
            pending_used_count=int(stats.get("pending_used_count", 0)),
            pending_idle_count=int(stats.get("pending_idle_count", 0)),
            availability=_availability((raw.get("aws_attributes") or {}).get("availability")),
        )


def _state(raw: object) -> PoolState:
    try:
        return PoolState(str(raw).upper())
    except ValueError:
        # Estado desconhecido nao pode virar ACTIVE por acidente.
        return PoolState.STOPPED


def _availability(raw: object) -> SpotAvailability:
    try:
        return SpotAvailability(str(raw).upper())
    except ValueError:
        return SpotAvailability.SPOT
