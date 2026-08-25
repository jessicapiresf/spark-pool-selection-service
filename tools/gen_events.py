"""Gerador de eventos sinteticos.

Serve para dois usos: popular o ambiente local com dado que parece real, e alimentar o
teste de simulacao, que derruba a disponibilidade de uma AZ no meio do fluxo e verifica
que a recomendacao migra. Por isso ele aceita uma agenda de mudanca de saude, e nao so um
volume.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pool_selection.domain.pool import PoolId

DEFAULT_POOLS = (
    "pool-r6.xlarge-us-east-1a",
    "pool-r6.xlarge-us-east-1b",
    "pool-r6.xlarge-us-east-1c",
    "pool-r6.2xlarge-us-east-1a",
    "pool-r6.2xlarge-us-east-1c",
    "pool-c6.xlarge-us-east-1a",
    "pool-c6.xlarge-us-east-1b",
    "pool-m6.xlarge-us-east-1b",
    "pool-i3.xlarge-us-east-1a",
)

DEFAULT_JOBS = ("etl-vendas", "etl-pesado", "agg-diario", "ml-features", "limpeza-noturna")

# Quando um job falha sem ser por spot, e por um destes.
OTHER_FAILURES = ("SPARK_EXECUTION_ERROR", "TIMED_OUT")


@dataclass
class HealthSchedule:
    """Saude de cada pool ao longo do tempo.

    `base` e a chance de um job terminar bem. `changes` sao viradas: a partir do minuto
    indicado, o pool passa a ter outra saude. E o que permite simular uma AZ apertando.
    """

    base: Mapping[str, float]
    changes: Sequence[tuple[int, str, float]] = field(default_factory=tuple)

    def health_at(self, pool_id: str, minute_offset: int) -> float:
        health = self.base.get(pool_id, 0.95)
        for at_minute, target, new_health in sorted(self.changes, key=lambda c: c[0]):
            if minute_offset >= at_minute and target in (pool_id, _az_of(pool_id)):
                health = new_health
        return health


def _az_of(pool_id: str) -> str:
    """Usa o parse do dominio em vez de repetir a regra aqui."""
    return PoolId.parse(pool_id).availability_zone.value


def generate(
    *,
    pools: Sequence[str] = DEFAULT_POOLS,
    jobs: Sequence[str] = DEFAULT_JOBS,
    minutes: int = 120,
    per_minute: int = 8,
    start: datetime | None = None,
    schedule: HealthSchedule | None = None,
    seed: int = 0,
) -> Iterator[dict[str, object]]:
    rng = random.Random(seed)
    start = start or datetime.now(UTC) - timedelta(minutes=minutes)
    schedule = schedule or HealthSchedule(base=dict.fromkeys(pools, 0.95))

    for offset in range(minutes):
        moment = start + timedelta(minutes=offset)
        for _ in range(per_minute):
            pool_id = rng.choice(list(pools))
            job_id = rng.choice(list(jobs))
            health = schedule.health_at(pool_id, offset)

            # `etl-pesado` nao cabe em maquina pequena, e o historico dele conta isso
            # sozinho, sem nenhum campo novo no evento.
            if job_id == "etl-pesado" and pool_id.split("-")[1].endswith(".xlarge"):
                health *= 0.35

            finished_at = moment + timedelta(seconds=rng.randint(0, 59))
            if rng.random() < health:
                event = {"status": "SUCCESS", "reason": None}
            elif rng.random() < 0.8:
                event = {"status": "FAILED", "reason": "SPOT_INSTANCE_TERMINATION"}
            else:
                event = {"status": "FAILED", "reason": rng.choice(OTHER_FAILURES)}

            yield {
                "finished_at": finished_at.isoformat(),
                "job_id": job_id,
                "pool_id": pool_id,
                **event,
            }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gera eventos sinteticos, um por linha")
    parser.add_argument("--minutes", type=int, default=120)
    parser.add_argument("--per-minute", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--unhealthy-az", help="AZ que aperta no meio da janela, ex. us-east-1c")
    parser.add_argument("--output", help="Arquivo de saida. Sem isso, escreve no stdout.")
    args = parser.parse_args(argv)

    changes: list[tuple[int, str, float]] = []
    if args.unhealthy_az:
        changes.append((args.minutes // 2, args.unhealthy_az, 0.10))

    events = generate(
        minutes=args.minutes,
        per_minute=args.per_minute,
        seed=args.seed,
        schedule=HealthSchedule(base=dict.fromkeys(DEFAULT_POOLS, 0.95), changes=changes),
    )
    lines = "\n".join(json.dumps(event) for event in events)
    if args.output:
        from pathlib import Path

        Path(args.output).write_text(lines + "\n", encoding="utf-8")
    else:
        sys.stdout.write(lines + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
