"""Spot placement score da AWS, a fonte preditiva.

Tres restricoes da API decidem este adapter, e ignorar qualquer uma faz o sinal chegar
errado em vez de nao chegar:

1. Menos de tres tipos de instancia diferentes devolve score baixo por definicao, nao
   erro. Um pool tem um tipo so, entao a consulta e por perfil.
2. O score e relativo a capacidade alvo perguntada: 10 para 10 instancias nao e 10 para
   1.000.
3. A resposta traz `AvailabilityZoneId` (`use1-az1`), nao o nome da AZ (`us-east-1c`), e
   os nomes sao embaralhados por conta. Sem traduzir, nenhuma AZ casaria e o fator sumiria
   em silencio.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

from pool_selection.domain.scoring import PlacementForecast

logger = logging.getLogger(__name__)

# A AWS devolve score baixo de proposito abaixo disso. Consultar assim envenenaria o
# modelo, entao o perfil e pulado.
MINIMUM_INSTANCE_TYPES = 3
MAX_INSTANCE_TYPES = 1000


class EC2PlacementScoreProvider:
    def __init__(self, region: str | None = None, client: Any | None = None) -> None:
        self._client = client or boto3.client("ec2", region_name=region)
        self._region = region or self._client.meta.region_name
        self._zone_names: dict[str, str] | None = None

    def fetch(
        self, instance_types_by_profile: Mapping[str, Sequence[str]], targets: Mapping[str, int]
    ) -> Mapping[tuple[str, str], PlacementForecast]:
        forecasts: dict[tuple[str, str], PlacementForecast] = {}
        scored_at = datetime.now(UTC)

        for profile, instance_types in instance_types_by_profile.items():
            unique = sorted(set(instance_types))[:MAX_INSTANCE_TYPES]
            if len(unique) < MINIMUM_INSTANCE_TYPES:
                logger.info(
                    "perfil %s tem %d tipos, abaixo do minimo de %d da API; "
                    "sem previsao para ele nesta rodada",
                    profile,
                    len(unique),
                    MINIMUM_INSTANCE_TYPES,
                )
                continue

            target = max(1, targets.get(profile, 1))
            for zone_name, score in self._score(unique, target):
                forecasts[(zone_name, profile)] = PlacementForecast(
                    score=score, target_capacity=target, scored_at=scored_at
                )
        return forecasts

    def _score(self, instance_types: Sequence[str], target: int) -> list[tuple[str, int]]:
        try:
            response = self._client.get_spot_placement_scores(
                InstanceTypes=list(instance_types),
                TargetCapacity=target,
                SingleAvailabilityZone=True,
                RegionNames=[self._region],
            )
        except ClientError as error:
            # Sem previsao o servico volta a ser reativo, que e degradacao e nao falha.
            logger.warning("placement score indisponivel: %s", error)
            return []

        scored = []
        for entry in response.get("SpotPlacementScores", ()):
            zone_id = entry.get("AvailabilityZoneId")
            score = entry.get("Score")
            if zone_id is None or score is None:
                continue
            zone_name = self._zone_name(str(zone_id))
            if zone_name is not None:
                scored.append((zone_name, int(score)))
        return scored

    def _zone_name(self, zone_id: str) -> str | None:
        if self._zone_names is None:
            self._zone_names = self._load_zone_names()
        name = self._zone_names.get(zone_id)
        if name is None:
            logger.warning("AZ id %s sem nome correspondente na conta", zone_id)
        return name

    def _load_zone_names(self) -> dict[str, str]:
        try:
            response = self._client.describe_availability_zones()
        except ClientError as error:
            logger.warning("nao foi possivel mapear ids de AZ: %s", error)
            return {}
        return {
            zone["ZoneId"]: zone["ZoneName"]
            for zone in response.get("AvailabilityZones", ())
            if zone.get("ZoneId") and zone.get("ZoneName")
        }
