"""Leitura de segredo no Secrets Manager.

O token da Databricks nao pode viver em variavel de ambiente da Lambda: variavel aparece
no console, no `get-function-configuration` e em qualquer dump de configuracao. O
Terraform entrega o ARN do segredo, e quem precisa do valor resolve na hora de usar.

Uma leitura por execucao da agregadora, que roda uma vez por minuto. O cache em processo
evita repetir a chamada quando a mesma instancia atende varias execucoes seguidas.
"""

from __future__ import annotations

import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class SecretUnavailableError(RuntimeError):
    """Nao foi possivel ler o segredo."""


class SecretsManagerResolver:
    def __init__(self, client: Any | None = None) -> None:
        self._client = client
        self._cache: dict[str, str] = {}

    def resolve(self, secret_id: str) -> str:
        if secret_id in self._cache:
            return self._cache[secret_id]

        if self._client is None:
            self._client = boto3.client("secretsmanager")
        try:
            response = self._client.get_secret_value(SecretId=secret_id)
        except ClientError as error:
            raise SecretUnavailableError(f"nao foi possivel ler {secret_id}") from error

        value = response.get("SecretString")
        if not value:
            raise SecretUnavailableError(f"segredo {secret_id} nao tem SecretString")

        self._cache[secret_id] = value
        return value
