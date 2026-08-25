from __future__ import annotations

import pytest

from pool_selection.domain.catalog import InstanceSpec, build_catalog, classify_profile
from pool_selection.domain.pool import Profile


@pytest.mark.parametrize(
    ("instance_type", "vcpus", "memory_mib", "storage_gb", "expected"),
    [
        ("c6.xlarge", 4, 8192, 0, Profile.COMPUTE),  # 2 GiB/vCPU
        ("m6.xlarge", 4, 16384, 0, Profile.GENERAL),  # 4 GiB/vCPU
        ("r6.xlarge", 4, 32768, 0, Profile.MEMORY),  # 8 GiB/vCPU
        ("x2.xlarge", 4, 131072, 0, Profile.MEMORY),  # 32 GiB/vCPU
        ("i3.xlarge", 4, 31232, 950, Profile.STORAGE),  # 237 GB/vCPU de disco
        ("m5d.xlarge", 4, 16384, 150, Profile.GENERAL),  # disco pequeno nao vira storage
    ],
)
def test_classifica_pela_razao_memoria_por_vcpu(
    instance_type: str, vcpus: int, memory_mib: int, storage_gb: int, expected: Profile
) -> None:
    spec = InstanceSpec(instance_type, vcpus, memory_mib, storage_gb)
    assert classify_profile(spec) is expected


def test_familia_que_ainda_nao_existe_e_classificada_do_mesmo_jeito() -> None:
    """E o ponto de nao usar tabela fixa: familia nova nao exige deploy."""
    futura = InstanceSpec("r9zz.4xlarge", vcpus=16, memory_mib=131072)
    assert classify_profile(futura) is Profile.MEMORY


@pytest.mark.parametrize(("vcpus", "memory_mib"), [(0, 8192), (4, 0), (-1, 1024)])
def test_tipo_sem_atributo_vira_unknown_em_vez_de_chute(vcpus: int, memory_mib: int) -> None:
    assert classify_profile(InstanceSpec("misterio.xlarge", vcpus, memory_mib)) is Profile.UNKNOWN


def test_catalogo_mapeia_tipo_para_nome_do_perfil() -> None:
    catalog = build_catalog(
        [InstanceSpec("r6.xlarge", 4, 32768), InstanceSpec("c6.xlarge", 4, 8192)]
    )
    assert catalog == {"r6.xlarge": "memory", "c6.xlarge": "compute"}
