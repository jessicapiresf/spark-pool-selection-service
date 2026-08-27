"""Atributos reais dos tipos de instancia, via `DescribeInstanceTypes`.

E o que evita uma tabela fixa de familia para perfil, que envelheceria a cada familia
nova da AWS. Tipo que a API nao reconhece fica sem perfil em vez de receber um chute.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import boto3
from botocore.exceptions import ClientError

from pool_selection.domain.catalog import InstanceSpec

logger = logging.getLogger(__name__)

# Limite do proprio DescribeInstanceTypes por chamada.
PAGE_SIZE = 100


class EC2InstanceCatalogProvider:
    def __init__(self, region: str | None = None, client: Any | None = None) -> None:
        self._client = client or boto3.client("ec2", region_name=region)

    def describe(self, instance_types: Sequence[str]) -> Sequence[InstanceSpec]:
        wanted = sorted(set(instance_types))
        specs: list[InstanceSpec] = []
        for start in range(0, len(wanted), PAGE_SIZE):
            specs.extend(self._describe_page(wanted[start : start + PAGE_SIZE]))
        return specs

    def _describe_page(self, page: Sequence[str]) -> list[InstanceSpec]:
        try:
            response = self._client.describe_instance_types(InstanceTypes=list(page))
        except ClientError as error:
            # Um tipo inexistente derruba a pagina inteira, entao vale registrar quais.
            logger.warning("describe_instance_types falhou para %s: %s", list(page), error)
            return []

        specs = []
        for raw in response.get("InstanceTypes", ()):
            storage = raw.get("InstanceStorageInfo") or {}
            specs.append(
                InstanceSpec(
                    instance_type=raw["InstanceType"],
                    vcpus=int((raw.get("VCpuInfo") or {}).get("DefaultVCpus", 0)),
                    memory_mib=int((raw.get("MemoryInfo") or {}).get("SizeInMiB", 0)),
                    instance_storage_gb=int(storage.get("TotalSizeInGB", 0)),
                )
            )
        return specs
