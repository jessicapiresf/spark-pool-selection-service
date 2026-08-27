"""Fumaca contra um ambiente de verdade, com SigV4.

Roda no CI depois do apply em staging. Chamar o endpoint e o que separa "o Terraform
aplicou" de "o servico funciona".
"""

from __future__ import annotations

import json
import sys
from urllib.parse import urljoin

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import URLLib3Session


def signed_get(base_url: str, path: str) -> tuple[int, dict]:
    session = boto3.Session()
    request = AWSRequest(method="GET", url=urljoin(base_url, path))
    SigV4Auth(session.get_credentials(), "lambda", session.region_name).add_auth(request)
    response = URLLib3Session().send(request.prepare())
    body = response.content.decode("utf-8")
    return response.status_code, json.loads(body) if body else {}


def main(base_url: str) -> int:
    status, body = signed_get(base_url, "/health")
    assert status == 200, f"/health devolveu {status}: {body}"
    print("health ok")

    status, body = signed_get(base_url, "/get-pools?job_id=smoke-test&alternatives=1")
    if status == 503:
        print("sem snapshot ainda: a agregadora roda a cada minuto, tente de novo depois")
        return 1

    assert status == 200, f"/get-pools devolveu {status}: {body}"
    assert body["pool_id"].startswith("pool-"), body
    assert 0.0 <= body["score"] <= 1.0, body
    print(
        f"get-pools ok: {body['pool_id']} score={body['score']} "
        f"fonte={body['evidence']['source']} degradado={body['degraded']}"
    )

    if body["degraded"]:
        print("respondendo degradado: ha snapshot? a agregadora rodou?")
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("uso: smoke.py <base-url>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
