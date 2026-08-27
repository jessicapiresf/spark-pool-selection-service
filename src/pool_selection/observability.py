"""Log estruturado e metricas em Embedded Metric Format.

EMF vai junto do log, entao nao gasta uma chamada de API por metrica: o CloudWatch extrai
a metrica do proprio log. Em uma Lambda que roda 1.440 vezes ao dia mais o trafego da API,
a diferenca aparece na conta.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Mapping
from typing import Any

NAMESPACE = os.environ.get("METRICS_NAMESPACE", "PoolSelection")

logger = logging.getLogger("pool_selection")


def configure_logging(level: int = logging.INFO) -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def log(event: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": event, **fields}, default=str))


def emit_metrics(
    metrics: Mapping[str, float],
    dimensions: Mapping[str, str] | None = None,
    unit: str = "Count",
    dimension_sets: list[list[str]] | None = None,
    **context: Any,
) -> None:
    """Uma linha de log que o CloudWatch le como metrica.

    `dimension_sets` permite publicar a mesma metrica em mais de um recorte. O painel de
    efeito manada precisa do total e da quebra por pool, e sem os dois conjuntos so o
    total existiria. Cada nome citado precisa estar em `dimensions`.
    """
    dimensions = dimensions or {}
    if dimension_sets is None:
        dimension_sets = [list(dimensions)] if dimensions else [[]]
    else:
        unknown = {name for group in dimension_sets for name in group} - set(dimensions)
        if unknown:
            raise ValueError(f"dimensao sem valor: {sorted(unknown)}")

    payload: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": dimension_sets,
                    "Metrics": [{"Name": name, "Unit": unit} for name in metrics],
                }
            ],
        },
        **dimensions,
        **dict(metrics),
        **context,
    }
    logger.info(json.dumps(payload, default=str))
