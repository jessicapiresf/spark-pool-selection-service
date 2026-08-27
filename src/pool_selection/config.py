"""Configuracao por variavel de ambiente.

Tudo que muda entre ambientes fica aqui, incluindo os pesos do modelo. O peso de
`TIMED_OUT` e o bonus de queda para on-demand sao calibracao, nao codigo: mudar qualquer
um dos dois nao deveria exigir deploy de logica.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    snapshot_bucket: str = ""
    snapshot_key: str = "snapshot/pools.json.gz"
    counters_table: str = ""

    # Caminho local para o snapshot. Quando preenchido, a API le do disco em vez do S3, e
    # o ambiente de desenvolvimento sobe sem AWS nenhuma, nem simulada.
    snapshot_path: str = ""

    # A API recarrega o snapshot no maximo a cada 30s. Com a agregadora rodando a cada
    # 60s, o dado servido tem no maximo 90 segundos, que e a meta declarada.
    snapshot_ttl_seconds: int = 30
    stale_after_seconds: int = 300

    databricks_host: str = ""
    databricks_token: str = ""
    # O token nunca entra em variavel de ambiente em producao: aqui vem o ARN do segredo,
    # resolvido pela agregadora na hora de montar o provider.
    databricks_token_secret: str = ""

    placement_refresh_seconds: int = 300
    catalog_refresh_seconds: int = 86400
    aggregator_max_minutes: int = 360

    timed_out_weight: float = 0.5

    # Quanto tempo esperar antes de considerar um minuto fechado. O SQS entrega em lote com
    # janela de 20s e a notificacao do S3 tem latencia propria, entao o contador de um
    # minuto pode chegar depois de a agregadora ja ter passado por ele. Sem essa folga,
    # aquele evento e descartado em silencio.
    aggregator_lag_minutes: int = 2

    # Teto de concentracao do trafego por pool, relativo a capacidade livre dele.
    capacity_concentration: float = 3.0

    default_alternatives: int = 2
    fallback_pools: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> Settings:
        raw_fallback = os.environ.get("FALLBACK_POOLS", "")
        return cls(
            snapshot_bucket=os.environ.get("SNAPSHOT_BUCKET", ""),
            snapshot_key=os.environ.get("SNAPSHOT_KEY", "snapshot/pools.json.gz"),
            snapshot_path=os.environ.get("SNAPSHOT_PATH", ""),
            counters_table=os.environ.get("COUNTERS_TABLE", ""),
            snapshot_ttl_seconds=_int("SNAPSHOT_TTL_SECONDS", 30),
            stale_after_seconds=_int("STALE_AFTER_SECONDS", 300),
            databricks_host=os.environ.get("DATABRICKS_HOST", ""),
            databricks_token=os.environ.get("DATABRICKS_TOKEN", ""),
            databricks_token_secret=os.environ.get("DATABRICKS_TOKEN_SECRET", ""),
            placement_refresh_seconds=_int("PLACEMENT_REFRESH_SECONDS", 300),
            catalog_refresh_seconds=_int("CATALOG_REFRESH_SECONDS", 86400),
            aggregator_max_minutes=_int("AGGREGATOR_MAX_MINUTES", 360),
            timed_out_weight=_float("TIMED_OUT_WEIGHT", 0.5),
            aggregator_lag_minutes=_int("AGGREGATOR_LAG_MINUTES", 2),
            capacity_concentration=_float("CAPACITY_CONCENTRATION", 3.0),
            default_alternatives=_int("DEFAULT_ALTERNATIVES", 2),
            fallback_pools=tuple(p.strip() for p in raw_fallback.split(",") if p.strip()),
        )


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings.from_env()
