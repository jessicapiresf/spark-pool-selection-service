FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# As dependencias mudam bem menos que o codigo, entao instalar antes de copiar o resto
# aproveita o cache de camada na maioria das reconstrucoes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project

COPY src/ src/
COPY tools/ tools/
RUN uv sync --frozen

ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/src:/app/tools"

EXPOSE 8000
CMD ["uvicorn", "pool_selection.entrypoints.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
