"""Classificacao de perfil a partir dos atributos reais do tipo de instancia.

Uma tabela fixa no codigo resolveria e envelheceria: cada familia nova da AWS viraria um
deploy. A razao memoria por vCPU ja separa as familias, e funciona para familia que ainda
nao existe.
"""

from __future__ import annotations

from dataclasses import dataclass

from pool_selection.domain.pool import Profile

MIB_PER_GIB = 1024.0

# Fronteiras em GiB de memoria por vCPU. A familia `c` fica em 2, a `m` em 4 e a `r` em 8,
# entao os cortes em 3 e 6 caem no meio das faixas em vez de na borda de alguma delas.
COMPUTE_CEILING = 3.0
GENERAL_CEILING = 6.0

# GB de disco local por vCPU acima do qual o tipo e de armazenamento. Pega i3 (~237) e d3,
# e deixa de fora as variantes `d` de proposito, que sao familias gerais com disco (~37).
STORAGE_FLOOR = 100.0


@dataclass(frozen=True, slots=True)
class InstanceSpec:
    """O que o `DescribeInstanceTypes` responde e que importa para classificar."""

    instance_type: str
    vcpus: int
    memory_mib: int
    instance_storage_gb: int = 0

    @property
    def memory_gib_per_vcpu(self) -> float:
        return (self.memory_mib / MIB_PER_GIB) / self.vcpus

    @property
    def storage_gb_per_vcpu(self) -> float:
        return self.instance_storage_gb / self.vcpus


def classify_profile(spec: InstanceSpec) -> Profile:
    """Tipo sem vCPU declarada nao da para classificar, e vira `UNKNOWN` em vez de chute."""
    if spec.vcpus <= 0 or spec.memory_mib <= 0:
        return Profile.UNKNOWN
    if spec.storage_gb_per_vcpu >= STORAGE_FLOOR:
        return Profile.STORAGE
    ratio = spec.memory_gib_per_vcpu
    if ratio < COMPUTE_CEILING:
        return Profile.COMPUTE
    if ratio < GENERAL_CEILING:
        return Profile.GENERAL
    return Profile.MEMORY


def build_catalog(specs: list[InstanceSpec]) -> dict[str, str]:
    return {spec.instance_type: classify_profile(spec).value for spec in specs}
