from __future__ import annotations

import pytest

from pool_selection.domain.pool import InstanceType, MalformedPoolIdError, PoolId, Profile


@pytest.mark.parametrize(
    ("raw", "instance_type", "az"),
    [
        ("pool-r6.xlarge-us-east-1c", "r6.xlarge", "us-east-1c"),
        ("pool-i3.xlarge-us-east-1a", "i3.xlarge", "us-east-1a"),
        ("pool-c6gn.16xlarge-ap-southeast-2b", "c6gn.16xlarge", "ap-southeast-2b"),
        ("pool-m5a.8xlarge-sa-east-1a", "m5a.8xlarge", "sa-east-1a"),
    ],
)
def test_separa_tipo_de_instancia_e_az(raw: str, instance_type: str, az: str) -> None:
    parsed = PoolId.parse(raw)
    assert parsed.instance_type.value == instance_type
    assert parsed.availability_zone.value == az


def test_reconhece_zona_local() -> None:
    parsed = PoolId.parse("pool-r6.xlarge-us-west-2-lax-1a")
    assert parsed.availability_zone.value == "us-west-2-lax-1a"
    assert parsed.instance_type.value == "r6.xlarge"


@pytest.mark.parametrize(
    "raw",
    [
        "r6.xlarge-us-east-1c",  # sem prefixo
        "pool-r6.xlarge",  # sem AZ
        "pool-r6xlarge-us-east-1c",  # tipo sem ponto
        "pool--us-east-1c",  # tipo vazio
        "",
    ],
)
def test_recusa_pool_id_fora_do_formato(raw: str) -> None:
    with pytest.raises(MalformedPoolIdError):
        PoolId.parse(raw)


def test_regiao_sai_da_az() -> None:
    assert PoolId.parse("pool-r6.xlarge-us-east-1c").availability_zone.region == "us-east-1"


@pytest.mark.parametrize(
    ("instance_type", "prefix", "matches"),
    [
        ("r6.xlarge", "r6", True),
        ("r6i.xlarge", "r6", True),  # variacoes da mesma geracao contam
        ("r6a.2xlarge", "r6", True),
        ("c6.xlarge", "r6", False),
        ("r5.xlarge", "r6", False),
        ("r6.xlarge", "R6", True),  # filtro nao deve depender de caixa
    ],
)
def test_filtro_de_familia_por_prefixo(instance_type: str, prefix: str, matches: bool) -> None:
    assert InstanceType.parse(instance_type).in_family(prefix) is matches


def test_unknown_nao_e_perfil_selecionavel() -> None:
    assert Profile.UNKNOWN not in Profile.selectable()
    assert len(Profile.selectable()) == 4
