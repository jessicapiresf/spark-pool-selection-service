"""Adaptador de Lambda para o app ASGI.

O mesmo app roda local com Uvicorn e em producao atras de uma Function URL. Nao ter dois
caminhos diferentes e o que faz o teste de contrato valer para producao.
"""

from __future__ import annotations

from mangum import Mangum

from pool_selection.entrypoints.api.app import app

handler = Mangum(app, lifespan="off")
