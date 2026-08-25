from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pool_selection.domain.events import (
    JobEvent,
    MalformedEventError,
    Verdict,
    Weights,
    classify,
    observe,
    parse_timestamp,
)


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("SUCCESS", None, Verdict.CAPACITY_HELD),
        ("FAILED", "SPOT_INSTANCE_TERMINATION", Verdict.CAPACITY_LOST),
        ("FAILED", "TIMED_OUT", Verdict.AMBIGUOUS),
        ("FAILED", "SPARK_EXECUTION_ERROR", Verdict.IRRELEVANT),
        ("failed", "spot_instance_termination", Verdict.CAPACITY_LOST),
    ],
)
def test_classifica_status_e_motivo(status: str, reason: str | None, expected: Verdict) -> None:
    assert classify(status, reason) is expected


def test_bug_de_spark_nao_penaliza_o_pool() -> None:
    """Senao pools usados por times com codigo instavel afundariam sem culpa."""
    assert observe(classify("FAILED", "SPARK_EXECUTION_ERROR")).trials == 0.0


@pytest.mark.parametrize(
    ("status", "reason"),
    [("PENDING", None), ("FAILED", "MOTIVO_QUE_AINDA_NAO_EXISTE"), ("", None)],
)
def test_valor_desconhecido_e_ignorado_e_nao_derruba(status: str, reason: str | None) -> None:
    """Um valor novo que a plataforma passe a emitir nao pode virar falha por engano."""
    assert classify(status, reason) is Verdict.IRRELEVANT


def test_timed_out_entra_com_peso_parcial() -> None:
    observation = observe(Verdict.AMBIGUOUS, Weights(timed_out=0.5))
    assert observation.successes == 0.5
    assert observation.failures == 0.5


def test_peso_de_timed_out_e_configuracao_nao_codigo() -> None:
    assert observe(Verdict.AMBIGUOUS, Weights(timed_out=1.0)).failures == 1.0
    assert observe(Verdict.AMBIGUOUS, Weights(timed_out=0.0)).successes == 1.0


def test_peso_invalido_e_recusado() -> None:
    with pytest.raises(ValueError, match="entre 0 e 1"):
        Weights(timed_out=1.5)


def test_timestamp_sem_fuso_e_lido_como_utc() -> None:
    """O evento chega em UTC sem offset; a idade nao pode depender do fuso da maquina."""
    parsed = parse_timestamp("2024-08-07T00:04:52.767830")
    assert parsed == datetime(2024, 8, 7, 0, 4, 52, 767830, tzinfo=UTC)


def test_timestamp_com_offset_e_convertido() -> None:
    assert parse_timestamp("2024-08-07T02:04:52+02:00").hour == 0


def test_parse_do_evento_do_enunciado() -> None:
    event = JobEvent.parse(
        {
            "finished_at": "2024-08-07T00:04:52.767830",
            "job_id": "my-job",
            "pool_id": "pool-r6.xlarge-us-east-1c",
            "status": "FAILED",
            "reason": "SPOT_INSTANCE_TERMINATION",
        }
    )
    assert event.job_id == "my-job"
    assert event.pool_id.availability_zone.value == "us-east-1c"
    assert event.verdict is Verdict.CAPACITY_LOST
    assert event.minute == "2024-08-07T00:04"


@pytest.mark.parametrize(
    "payload",
    [
        {"job_id": "j", "pool_id": "pool-r6.xlarge-us-east-1c"},
        {"finished_at": "2024-08-07T00:04:52", "pool_id": "pool-r6.xlarge-us-east-1c"},
        {"finished_at": "ontem", "job_id": "j", "pool_id": "pool-r6.xlarge-us-east-1c"},
        {"finished_at": "2024-08-07T00:04:52", "job_id": "j", "pool_id": "invalido"},
        {
            "finished_at": "2024-08-07T00:04:52",
            "job_id": "",
            "pool_id": "pool-r6.xlarge-us-east-1c",
        },
    ],
)
def test_evento_incompleto_ou_torto_e_recusado(payload: dict[str, object]) -> None:
    with pytest.raises(MalformedEventError):
        JobEvent.parse(payload)
