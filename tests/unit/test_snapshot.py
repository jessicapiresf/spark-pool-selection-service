from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from conftest import entry
from pool_selection.domain.pool import Profile
from pool_selection.domain.scoring import Capacity, Evidence, PlacementForecast, SpotAvailability
from pool_selection.domain.snapshot import Snapshot, fallback_snapshot


def test_ida_e_volta_por_json_preserva_tudo(snapshot: Snapshot, now: datetime) -> None:
    rich = Snapshot(
        generated_at=now,
        through_minute=snapshot.through_minute,
        pools=(
            entry(
                "pool-r6.xlarge-us-east-1a",
                12.5,
                3.25,
                moment=now,
                capacity=Capacity(
                    max_capacity=40,
                    used_count=7,
                    idle_count=3,
                    availability=SpotAvailability.SPOT_WITH_FALLBACK,
                ),
                forecast=PlacementForecast(8, 20, now),
            ),
        ),
        job_fit=snapshot.job_fit,
        profile_fit=snapshot.profile_fit,
        catalog=snapshot.catalog,
        catalog_refreshed_at=now,
    )
    restored = Snapshot.from_dict(json.loads(json.dumps(rich.to_dict())))

    assert restored.pools[0].capacity == rich.pools[0].capacity
    assert restored.pools[0].forecast == rich.pools[0].forecast
    assert restored.pools[0].evidence.successes == pytest.approx(12.5)
    assert restored.job_fit["etl-pesado"]["r6.xlarge"].failures == pytest.approx(8.0)
    assert restored.catalog == rich.catalog


def test_snapshot_de_outra_versao_e_recusado(snapshot: Snapshot) -> None:
    payload = snapshot.to_dict() | {"version": 99}
    with pytest.raises(ValueError, match="versao"):
        Snapshot.from_dict(payload)


def test_ranking_cabe_em_poucos_kilobytes(snapshot: Snapshot) -> None:
    """E o que permite ele caber na memoria da API."""
    assert len(json.dumps(snapshot.to_dict())) < 8000


def test_idade_nunca_e_negativa(snapshot: Snapshot, now: datetime) -> None:
    from datetime import timedelta

    assert snapshot.age_seconds(now - timedelta(minutes=5)) == 0.0
    assert snapshot.age_seconds(now + timedelta(seconds=45)) == pytest.approx(45.0)


def test_sem_job_id_o_fator_de_adequacao_fica_no_prior(snapshot: Snapshot) -> None:
    pool = snapshot.entry("pool-r6.xlarge-us-east-1a")
    assert pool is not None
    anonimo = snapshot.factors_for(pool, None)
    conhecido = snapshot.factors_for(pool, "etl-pesado")
    assert anonimo.fit.mean > conhecido.fit.mean


def test_job_desconhecido_cai_no_prior_do_perfil(snapshot: Snapshot) -> None:
    pool = snapshot.entry("pool-r6.xlarge-us-east-1a")
    assert pool is not None
    novo = snapshot.factors_for(pool, "job-que-nunca-rodou")
    assert novo.fit.strength == pytest.approx(2.0)


def test_fallback_monta_lista_estatica_sem_evidencia() -> None:
    snapshot = fallback_snapshot(
        ["pool-r6.xlarge-us-east-1a", "pool-c6.xlarge-us-east-1b"], datetime.now(UTC)
    )
    assert len(snapshot.pools) == 2
    assert all(pool.evidence.trials == 0.0 for pool in snapshot.pools)
    assert all(pool.profile is Profile.UNKNOWN for pool in snapshot.pools)


def test_fallback_ignora_pool_id_torto_em_vez_de_quebrar() -> None:
    snapshot = fallback_snapshot(["pool-r6.xlarge-us-east-1a", "lixo"], datetime.now(UTC))
    assert len(snapshot.pools) == 1


def test_pool_sem_capacidade_conhecida_continua_elegivel() -> None:
    assert entry("pool-r6.xlarge-us-east-1a").is_selectable


def test_evidencia_vazia_serializa_sem_relogio() -> None:
    payload = Snapshot(
        generated_at=datetime.now(UTC), pools=(entry("pool-r6.xlarge-us-east-1a"),)
    ).to_dict()
    assert payload["pools"][0]["evidence"]["updated_at"] is None
    assert Snapshot.from_dict(payload).pools[0].evidence == Evidence()
