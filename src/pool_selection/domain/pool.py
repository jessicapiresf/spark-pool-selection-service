"""Vocabulario de pool, tipo de instancia e zona de disponibilidade.

Um `pool_id` carrega duas informacoes independentes: onde (a AZ) e o que (o tipo de
instancia). O modelo inteiro depende de separar as duas, entao o parse mora aqui.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# AZ padrao (us-east-1c) e zona local (us-west-2-lax-1a).
_AZ_PATTERN = r"[a-z]{2}-[a-z]+-\d+(?:-[a-z]+-\d+)?[a-z]"

_POOL_ID = re.compile(rf"^pool-(?P<instance_type>.+?)-(?P<availability_zone>{_AZ_PATTERN})$")
_INSTANCE_TYPE = re.compile(r"^(?P<family>[a-z][a-z0-9-]*)\.(?P<size>[a-z0-9]+)$")
_AZ = re.compile(rf"^{_AZ_PATTERN}$")


class Profile(StrEnum):
    """Perfil de uso de recurso de um tipo de instancia.

    `UNKNOWN` nao e um valor que o cliente possa pedir. Ele existe para tipo que o
    catalogo da AWS nao resolveu, que continua acessivel por `instance_types` e
    `family` em vez de sumir do catalogo.
    """

    MEMORY = "memory"
    COMPUTE = "compute"
    GENERAL = "general"
    STORAGE = "storage"
    UNKNOWN = "unknown"

    @classmethod
    def selectable(cls) -> tuple[Profile, ...]:
        return (cls.MEMORY, cls.COMPUTE, cls.GENERAL, cls.STORAGE)


class MalformedPoolIdError(ValueError):
    """`pool_id` que nao segue `pool-<instance-type>-<az>`."""


@dataclass(frozen=True, slots=True)
class InstanceType:
    """Ex. `r6.xlarge`: familia `r6`, tamanho `xlarge`."""

    value: str
    family: str
    size: str

    @classmethod
    def parse(cls, value: str) -> InstanceType:
        match = _INSTANCE_TYPE.match(value)
        if match is None:
            raise MalformedPoolIdError(f"tipo de instancia invalido: {value!r}")
        return cls(value=value, family=match["family"], size=match["size"])

    def in_family(self, prefix: str) -> bool:
        """`r6` casa com `r6`, `r6i` e `r6a`, que e como a AWS nomeia variacoes."""
        return self.family.startswith(prefix.lower())


@dataclass(frozen=True, slots=True)
class AvailabilityZone:
    value: str

    @classmethod
    def parse(cls, value: str) -> AvailabilityZone:
        if _AZ.match(value) is None:
            raise MalformedPoolIdError(f"zona de disponibilidade invalida: {value!r}")
        return cls(value=value)

    @property
    def region(self) -> str:
        """`us-east-1c` vive em `us-east-1`."""
        return self.value[:-1]


@dataclass(frozen=True, slots=True)
class PoolId:
    value: str
    instance_type: InstanceType
    availability_zone: AvailabilityZone

    @classmethod
    def parse(cls, value: str) -> PoolId:
        match = _POOL_ID.match(value)
        if match is None:
            raise MalformedPoolIdError(f"pool_id fora do formato pool-<tipo>-<az>: {value!r}")
        return cls(
            value=value,
            instance_type=InstanceType.parse(match["instance_type"]),
            availability_zone=AvailabilityZone.parse(match["availability_zone"]),
        )

    @classmethod
    def build(cls, instance_type: str, availability_zone: str) -> PoolId:
        """Monta a partir do tipo e da AZ autoritativos da API de pools."""
        return cls.parse(f"pool-{instance_type}-{availability_zone}")

    def __str__(self) -> str:
        return self.value
