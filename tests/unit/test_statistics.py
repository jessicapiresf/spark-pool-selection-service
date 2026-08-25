from __future__ import annotations

import pytest

from pool_selection.domain.statistics import (
    Posterior,
    beta_quantile,
    regularized_incomplete_beta,
)


@pytest.mark.parametrize(
    ("a", "b", "x", "expected"),
    [
        (1.0, 1.0, 0.25, 0.25),  # uniforme
        (1.0, 1.0, 0.80, 0.80),
        (2.0, 1.0, 0.50, 0.25),  # x^2
        (1.0, 2.0, 0.50, 0.75),
        (5.0, 5.0, 0.50, 0.50),  # simetrica
    ],
)
def test_beta_incompleta_bate_com_o_valor_fechado(
    a: float, b: float, x: float, expected: float
) -> None:
    assert regularized_incomplete_beta(a, b, x) == pytest.approx(expected, abs=1e-9)


def test_beta_incompleta_nas_bordas() -> None:
    assert regularized_incomplete_beta(3.0, 4.0, 0.0) == 0.0
    assert regularized_incomplete_beta(3.0, 4.0, 1.0) == 1.0


@pytest.mark.parametrize(("a", "b"), [(1.0, 1.0), (2.0, 5.0), (30.0, 3.0), (0.5, 0.5)])
def test_quantil_e_a_inversa_da_acumulada(a: float, b: float) -> None:
    for probability in (0.05, 0.25, 0.5, 0.95):
        x = beta_quantile(a, b, probability)
        assert regularized_incomplete_beta(a, b, x) == pytest.approx(probability, abs=1e-6)


def test_faixa_estreita_quando_ha_muita_evidencia() -> None:
    narrow = Posterior(200, 20).credible_interval()
    wide = Posterior(4, 1).credible_interval()
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_prior_expressa_forca_em_observacoes() -> None:
    prior = Posterior.from_prior(mean=0.75, strength=8)
    assert prior.mean == pytest.approx(0.75)
    assert prior.strength == pytest.approx(8.0)


@pytest.mark.parametrize(("alpha", "beta"), [(0.0, 1.0), (1.0, 0.0), (-1.0, 2.0)])
def test_parametros_nao_positivos_sao_recusados(alpha: float, beta: float) -> None:
    with pytest.raises(ValueError, match="positivos"):
        Posterior(alpha, beta)


@pytest.mark.parametrize(("mean", "strength"), [(0.0, 2.0), (1.0, 2.0), (0.5, 0.0)])
def test_prior_invalido_e_recusado(mean: float, strength: float) -> None:
    with pytest.raises(ValueError):
        Posterior.from_prior(mean, strength)
