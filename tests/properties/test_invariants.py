"""Invariantes que teste de exemplo nao pega.

Um componente estatistico tem espaco de entrada grande demais para exemplo cobrir. O que
importa aqui nao e um caso, e a propriedade valer para qualquer semente e qualquer volume
de evidencia.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from pool_selection.domain.events import Verdict, Weights, classify, observe
from pool_selection.domain.filters import PoolFilter, candidates
from pool_selection.domain.pool import PoolId, Profile
from pool_selection.domain.scoring import (
    AZ_HALF_LIFE,
    Capacity,
    Evidence,
    Factors,
    PlacementForecast,
    SpotAvailability,
)
from pool_selection.domain.selection import Strategy, select
from pool_selection.domain.snapshot import PoolEntry, Snapshot
from pool_selection.domain.statistics import Posterior, beta_quantile, regularized_incomplete_beta

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)

counts = st.floats(min_value=0.0, max_value=5000.0, allow_nan=False, allow_infinity=False)
positive = st.floats(min_value=0.01, max_value=5000.0, allow_nan=False, allow_infinity=False)
instance_types = st.sampled_from(["r6.xlarge", "r6.2xlarge", "c6.xlarge", "m6.xlarge", "i3.xlarge"])
zones = st.sampled_from(["us-east-1a", "us-east-1b", "us-east-1c", "us-east-1d"])
profiles = st.sampled_from(list(Profile.selectable()))


@st.composite
def pool_entries(draw: st.DrawFn) -> PoolEntry:
    instance_type = draw(instance_types)
    zone = draw(zones)
    forecast = draw(
        st.one_of(st.none(), st.integers(1, 10).map(lambda s: PlacementForecast(s, 10, NOW)))
    )
    capacity = draw(
        st.one_of(
            st.none(),
            st.builds(
                Capacity,
                max_capacity=st.one_of(st.none(), st.integers(1, 200)),
                used_count=st.integers(0, 50),
                idle_count=st.integers(0, 50),
                availability=st.sampled_from(list(SpotAvailability)),
            ),
        )
    )
    return PoolEntry(
        pool_id=PoolId.build(instance_type, zone),
        profile=draw(profiles),
        evidence=Evidence(draw(counts), draw(counts), NOW),
        capacity=capacity,
        forecast=forecast,
    )


@given(alpha=positive, beta=positive, x=st.floats(0.0, 1.0))
def test_acumulada_fica_entre_zero_e_um(alpha: float, beta: float, x: float) -> None:
    assert 0.0 <= regularized_incomplete_beta(alpha, beta, x) <= 1.0


@given(alpha=positive, beta=positive, p=st.floats(0.001, 0.999))
def test_quantil_fica_no_suporte_da_beta(alpha: float, beta: float, p: float) -> None:
    assert 0.0 <= beta_quantile(alpha, beta, p) <= 1.0


# Menor parametro que os priores do servico conseguem produzir. Abaixo disso a Beta nao e
# alcancavel por nenhum caminho do codigo.
REACHABLE = st.floats(min_value=0.12, max_value=5000.0, allow_nan=False, allow_infinity=False)
WELL_CONDITIONED = st.floats(min_value=0.5, max_value=5000.0, allow_nan=False, allow_infinity=False)


@given(alpha=REACHABLE, beta=REACHABLE, level=st.floats(0.5, 0.99))
def test_intervalo_credivel_cobre_a_massa_que_promete(
    alpha: float, beta: float, level: float
) -> None:
    """A definicao do intervalo, nao um sintoma dela.

    Testar "a media cai dentro" seria errado: numa Beta bem assimetrica, como a que sai de
    um placement score 10 sem nenhuma falha ainda, a cauda longa puxa a media para fora do
    intervalo central sem que nada esteja quebrado.

    A folga de 1e-2 e o pior caso medido, e acontece so no canto em U, com os dois
    parametros perto de 0,12. Ali os dois quantis caem a menos de 1e-16 das bordas, que e
    mais perto do que um double distingue, e a biseccao para de ganhar precisao. O
    intervalo e valor de exibicao: o score usa `sample` e `mean`, entao meio ponto
    percentual de cobertura nao muda recomendacao nenhuma.
    """
    posterior = Posterior(alpha, beta)
    low, high = posterior.credible_interval(level)
    assert 0.0 <= low <= high <= 1.0
    massa = regularized_incomplete_beta(alpha, beta, high) - regularized_incomplete_beta(
        alpha, beta, low
    )
    assert massa == pytest.approx(level, abs=1e-2)


@given(alpha=WELL_CONDITIONED, beta=WELL_CONDITIONED, level=st.floats(0.5, 0.99))
def test_intervalo_e_exato_para_pool_com_evidencia(alpha: float, beta: float, level: float) -> None:
    """Qualquer pool que ja viu um evento cai aqui, e aqui a conta e exata."""
    low, high = Posterior(alpha, beta).credible_interval(level)
    massa = regularized_incomplete_beta(alpha, beta, high) - regularized_incomplete_beta(
        alpha, beta, low
    )
    assert massa == pytest.approx(level, abs=1e-9)


@given(alpha=WELL_CONDITIONED, beta=WELL_CONDITIONED)
def test_intervalo_contem_a_media_quando_a_beta_nao_e_degenerada(alpha: float, beta: float) -> None:
    posterior = Posterior(alpha, beta)
    low, high = posterior.credible_interval()
    assert low - 1e-6 <= posterior.mean <= high + 1e-6


@given(
    entry=pool_entries(),
    job=st.one_of(st.none(), st.text(min_size=1, max_size=12)),
    seed=st.integers(),
)
def test_score_sempre_entre_zero_e_um(entry: PoolEntry, job: str | None, seed: int) -> None:
    snapshot = Snapshot(generated_at=NOW, pools=(entry,))
    factors = snapshot.factors_for(entry, job)
    assert 0.0 <= factors.expected <= 1.0
    assert 0.0 <= factors.sample(random.Random(seed)) <= 1.0


@given(
    pools=st.lists(pool_entries(), min_size=1, max_size=12, unique_by=lambda p: p.pool_id.value),
    family=st.one_of(st.none(), st.sampled_from(["r6", "c6", "m6", "i3"])),
    profile=st.one_of(st.none(), profiles),
    zone=st.one_of(st.none(), zones),
    min_samples=st.floats(0.0, 500.0),
)
def test_filtro_nunca_devolve_pool_fora_do_filtro(
    pools: list[PoolEntry],
    family: str | None,
    profile: Profile | None,
    zone: str | None,
    min_samples: float,
) -> None:
    pool_filter = PoolFilter(
        family=family,
        profile=profile,
        availability_zones=frozenset({zone}) if zone else None,
        min_samples=min_samples,
    )
    for pool in candidates(Snapshot(generated_at=NOW, pools=tuple(pools)), pool_filter):
        assert pool.is_selectable
        if family:
            assert pool.pool_id.instance_type.in_family(family)
        if profile:
            assert pool.profile is profile
        if zone:
            assert pool.pool_id.availability_zone.value == zone
        assert pool.evidence.trials >= min_samples


@given(seed=st.integers(), trials=st.floats(150.0, 4000.0))
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_pool_so_com_termino_de_spot_nunca_vence_pool_bom_com_amostra_grande(
    seed: int, trials: float
) -> None:
    """O invariante central: exploracao nao pode virar recomendar o que ja se sabe ruim."""
    bom = PoolEntry(
        pool_id=PoolId.build("r6.xlarge", "us-east-1a"),
        profile=Profile.MEMORY,
        evidence=Evidence(successes=trials * 0.95, failures=trials * 0.05, updated_at=NOW),
    )
    pessimo = PoolEntry(
        pool_id=PoolId.build("r6.xlarge", "us-east-1b"),
        profile=Profile.MEMORY,
        evidence=Evidence(successes=0.0, failures=trials, updated_at=NOW),
    )
    snapshot = Snapshot(generated_at=NOW, pools=(bom, pessimo))
    escolhido = select(snapshot, [bom, pessimo], rng=random.Random(seed)).chosen
    assert escolhido.pool_id == bom.pool_id.value


@given(
    pools=st.lists(pool_entries(), min_size=2, max_size=8, unique_by=lambda p: p.pool_id.value),
    seed=st.integers(),
    alternatives=st.integers(0, 5),
)
def test_alternativas_nunca_repetem_o_escolhido_nem_ultrapassam_o_pedido(
    pools: list[PoolEntry], seed: int, alternatives: int
) -> None:
    selecionaveis = [p for p in pools if p.is_selectable]
    assume(selecionaveis)
    snapshot = Snapshot(generated_at=NOW, pools=tuple(pools))
    selection = select(snapshot, selecionaveis, rng=random.Random(seed), alternatives=alternatives)
    ids = [alt.pool_id for alt in selection.alternatives]
    assert selection.chosen.pool_id not in ids
    assert len(ids) == len(set(ids))
    assert len(ids) <= min(alternatives, len(selecionaveis) - 1)


@given(
    pools=st.lists(pool_entries(), min_size=1, max_size=8, unique_by=lambda p: p.pool_id.value),
    seed=st.integers(),
)
def test_greedy_escolhe_o_maior_valor_esperado(pools: list[PoolEntry], seed: int) -> None:
    selecionaveis = [p for p in pools if p.is_selectable]
    assume(selecionaveis)
    snapshot = Snapshot(generated_at=NOW, pools=tuple(pools))
    selection = select(snapshot, selecionaveis, strategy=Strategy.GREEDY, rng=random.Random(seed))
    melhor = max(snapshot.factors_for(p, None).expected for p in selecionaveis)
    assert selection.chosen.score >= melhor - 1e-9


@given(successes=counts, failures=counts, minutes=st.floats(0.0, 20000.0))
def test_decaimento_nunca_cria_evidencia(successes: float, failures: float, minutes: float) -> None:
    evidence = Evidence(successes, failures, NOW)
    decayed = evidence.decayed_to(NOW + timedelta(minutes=minutes), AZ_HALF_LIFE)
    assert decayed.successes <= successes + 1e-9
    assert decayed.failures <= failures + 1e-9
    assert decayed.trials >= 0.0


@given(successes=counts, failures=counts, minutes=st.floats(-5000.0, 0.0))
def test_relogio_para_tras_nao_ressuscita_evidencia(
    successes: float, failures: float, minutes: float
) -> None:
    evidence = Evidence(successes, failures, NOW)
    decayed = evidence.decayed_to(NOW + timedelta(minutes=minutes), AZ_HALF_LIFE)
    assert decayed.successes == successes
    assert decayed.failures == failures


@given(
    status=st.sampled_from(["SUCCESS", "FAILED", "PENDING", "", "qualquer"]),
    reason=st.one_of(st.none(), st.text(max_size=30)),
    weight=st.floats(0.0, 1.0),
)
def test_observacao_nunca_soma_mais_de_um_ensaio(
    status: str, reason: str | None, weight: float
) -> None:
    observation = observe(classify(status, reason), Weights(timed_out=weight))
    assert 0.0 <= observation.trials <= 1.0
    assert observation.successes >= 0.0
    assert observation.failures >= 0.0


@given(reason=st.text(max_size=40))
def test_motivo_desconhecido_nunca_vira_falha_de_capacidade(reason: str) -> None:
    """Um motivo novo que a plataforma emita nao pode penalizar pool por engano."""
    assume(reason.strip().upper() not in {"SPOT_INSTANCE_TERMINATION", "TIMED_OUT"})
    assert classify("FAILED", reason) is Verdict.IRRELEVANT


@given(entry=pool_entries(), availability=st.floats(0.0, 1.0))
def test_ajuste_de_capacidade_nunca_piora_o_pool(entry: PoolEntry, availability: float) -> None:
    """Instancia quente e fallback so podem reduzir risco, nunca aumentar."""
    factors = Factors(
        availability=Posterior(1, 1),
        fit=Posterior(1, 1),
        capacity=entry.capacity,
        forecast=entry.forecast,
        target_capacity=entry.forecast.target_capacity if entry.forecast else 10,
    )
    assert factors.adjusted_availability(availability) >= availability - 1e-9
