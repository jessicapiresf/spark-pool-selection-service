from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from pool_selection.domain.scoring import (
    AZ_HALF_LIFE,
    JOB_FIT_HALF_LIFE,
    JOB_FIT_PRIOR_MEAN,
    JOB_FIT_PRIOR_STRENGTH,
    Capacity,
    Evidence,
    Factors,
    PlacementForecast,
    PoolState,
    SpotAvailability,
    az_prior,
    job_fit_prior,
    posterior_for,
)
from pool_selection.domain.statistics import Posterior

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def test_evidencia_cai_pela_metade_a_cada_meia_vida() -> None:
    evidence = Evidence(100.0, 20.0, NOW)
    decayed = evidence.decayed_to(NOW + AZ_HALF_LIFE, AZ_HALF_LIFE)
    assert decayed.successes == pytest.approx(50.0)
    assert decayed.failures == pytest.approx(10.0)


def test_decaimento_preserva_a_estimativa_e_alarga_a_faixa() -> None:
    """Evidencia velha vale menos, mas nao vira evidencia contraria."""

    def rate(evidence: Evidence) -> float:
        return evidence.successes / evidence.trials

    def width(evidence: Evidence) -> float:
        low, high = Posterior(1 + evidence.successes, 1 + evidence.failures).credible_interval()
        return high - low

    fresh = Evidence(90.0, 10.0, NOW)
    old = fresh.decayed_to(NOW + 3 * AZ_HALF_LIFE, AZ_HALF_LIFE)

    assert rate(old) == pytest.approx(rate(fresh))
    assert width(old) > width(fresh)


def test_acumulo_e_incremental() -> None:
    """`anterior x decaimento + delta` e o que torna a agregadora O(1)."""
    step = Evidence().accumulate(10, 0, NOW, AZ_HALF_LIFE)
    step = step.accumulate(10, 0, NOW + AZ_HALF_LIFE, AZ_HALF_LIFE)
    assert step.successes == pytest.approx(15.0)


def test_meias_vidas_diferentes_sao_essenciais() -> None:
    """Com 20 minutos nos dois, um job diario esqueceria de si entre execucoes."""
    a_day = timedelta(days=1)
    az = Evidence(100.0, 0.0, NOW).decayed_to(NOW + a_day, AZ_HALF_LIFE)
    fit = Evidence(100.0, 0.0, NOW).decayed_to(NOW + a_day, JOB_FIT_HALF_LIFE)
    assert az.trials < 1e-15
    assert fit.trials > 90.0


def test_tres_falhas_derrubam_o_score_para_perto_de_036() -> None:
    """Numero que a arquitetura promete: falha e sinal forte, sucesso e sinal fraco."""
    prior = Posterior.from_prior(JOB_FIT_PRIOR_MEAN, JOB_FIT_PRIOR_STRENGTH)
    assert prior.updated(successes=0, failures=3).mean == pytest.approx(0.36, abs=0.005)


def test_prior_de_perfil_desloca_a_media_sem_engessar_o_job() -> None:
    """Se o perfil aumentasse a forca, o job novo demoraria mais para se corrigir."""
    pessimistic = job_fit_prior(Evidence(successes=10.0, failures=90.0))
    assert pessimistic.mean < JOB_FIT_PRIOR_MEAN
    assert pessimistic.strength == pytest.approx(JOB_FIT_PRIOR_STRENGTH)


def test_sem_evidencia_de_perfil_o_prior_e_o_padrao() -> None:
    assert job_fit_prior(None).mean == pytest.approx(JOB_FIT_PRIOR_MEAN)
    assert job_fit_prior(Evidence()).mean == pytest.approx(JOB_FIT_PRIOR_MEAN)


@pytest.mark.parametrize(("score", "ordered"), [(1, 0), (5, 1), (10, 2)])
def test_placement_score_vira_prior_monotono(score: int, ordered: int) -> None:
    rates = [PlacementForecast(s, 10, NOW).implied_success_rate for s in (1, 5, 10)]
    assert rates == sorted(rates)
    assert PlacementForecast(score, 10, NOW).implied_success_rate == rates[ordered]


def test_placement_score_fora_da_escala_e_recusado() -> None:
    for invalid in (0, 11, -1):
        with pytest.raises(ValueError, match="1 a 10"):
            PlacementForecast(invalid, 10, NOW)


def test_previsao_manda_enquanto_nao_ha_falha_e_sai_do_caminho_depois() -> None:
    optimistic = az_prior(PlacementForecast(10, 10, NOW))
    assert optimistic.mean > 0.9

    contradicted = posterior_for(optimistic, Evidence(successes=2.0, failures=40.0))
    assert contradicted.mean < 0.2


def test_pool_parado_ou_apagado_sai_da_lista() -> None:
    for state in (PoolState.STOPPED, PoolState.DELETED):
        assert not Capacity(state=state).is_selectable


def test_pool_lotado_nao_e_recomendado_por_melhor_que_seja_o_historico() -> None:
    assert not Capacity(max_capacity=10, used_count=8, pending_used_count=2).is_selectable
    assert Capacity(max_capacity=10, used_count=8).is_selectable


def test_pool_sem_teto_declarado_continua_elegivel() -> None:
    """`max_capacity` e opcional na Databricks."""
    capacity = Capacity(max_capacity=None, used_count=999)
    assert capacity.free_slots is None
    assert capacity.is_selectable


def test_instancia_ociosa_cobre_a_parte_do_risco_que_ela_atende() -> None:
    """Instancia ja quente nao passa pelo mercado spot de novo."""
    half = Factors(Posterior(1, 1), Posterior(1, 1), Capacity(idle_count=5), target_capacity=10)
    assert half.adjusted_availability(0.0) == pytest.approx(0.5)

    full = Factors(Posterior(1, 1), Posterior(1, 1), Capacity(idle_count=10), target_capacity=10)
    assert full.adjusted_availability(0.0) == pytest.approx(1.0)


def test_ajuste_de_capacidade_nao_apaga_o_fator_de_adequacao() -> None:
    """Capacidade muda o risco de conseguir maquina, nao se o job cabe no tipo."""
    factors = Factors(
        availability=Posterior(100, 1),
        fit=Posterior(1, 19),
        capacity=Capacity(availability=SpotAvailability.ON_DEMAND),
    )
    assert factors.expected < 0.15


def test_queda_para_on_demand_ajuda_pouco_de_proposito() -> None:
    """A economia do spot some quando o fallback e acionado, entao o bonus nasce pequeno."""
    base = Factors(Posterior(1, 1), Posterior(1, 1), Capacity())
    fallback = Factors(
        Posterior(1, 1), Posterior(1, 1), Capacity(availability=SpotAvailability.SPOT_WITH_FALLBACK)
    )
    gain = fallback.adjusted_availability(0.5) - base.adjusted_availability(0.5)
    assert 0.0 < gain < 0.1


def test_sorteio_com_a_mesma_semente_devolve_o_mesmo_ponto() -> None:
    factors = Factors(Posterior(20, 3), Posterior(9, 1))
    assert factors.sample(random.Random(7)) == factors.sample(random.Random(7))
