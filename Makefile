# O requisito e um comando so. `make dev` instala tudo isolado, sobe as dependencias,
# popula dado e liga a API. Nao ha passo manual entre clonar e ver o endpoint responder.

SHELL := /bin/bash
UV := $(shell command -v uv 2>/dev/null || echo "$$HOME/.local/bin/uv")
RUN := $(UV) run
export AWS_ENDPOINT_URL ?= http://localhost:4566
export AWS_ACCESS_KEY_ID ?= local
export AWS_SECRET_ACCESS_KEY ?= local
export AWS_DEFAULT_REGION ?= us-east-1
export SNAPSHOT_BUCKET ?= pool-selection-local
export COUNTERS_TABLE ?= pool-selection-counters
export FALLBACK_POOLS ?= pool-r6.xlarge-us-east-1a,pool-c6.xlarge-us-east-1a

.DEFAULT_GOAL := help
.PHONY: help dev install localstack seed serve test test-fast lint fmt typecheck audit check clean down demo load package tf-validate

help:  ## Lista os alvos
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

dev: install localstack seed serve  ## Ambiente completo em um comando

install:  ## Cria o ambiente virtual isolado e instala as dependencias travadas
	@command -v $(UV) >/dev/null || { echo "uv nao encontrado: https://docs.astral.sh/uv/"; exit 1; }
	$(UV) sync --all-extras

localstack:  ## Sobe o LocalStack e espera ficar saudavel
	docker compose up -d localstack
	@echo "aguardando o LocalStack..."
	@for i in $$(seq 1 40); do \
		curl -sf $$AWS_ENDPOINT_URL/_localstack/health >/dev/null && exit 0; \
		sleep 1; \
	done; echo "LocalStack nao respondeu a tempo"; exit 1

seed:  ## Cria os recursos, gera eventos sinteticos e publica um snapshot
	$(RUN) python tools/seed_local.py

serve:  ## Liga a API com reload
	@echo "API em http://localhost:8000  |  OpenAPI em http://localhost:8000/docs"
	$(RUN) uvicorn pool_selection.entrypoints.api.app:app --reload --port 8000

demo:  ## Sobe tudo em container, sem precisar de Python na maquina
	docker compose up --build

test:  ## Suite inteira com o gate de cobertura
	$(RUN) pytest --cov --cov-report=term-missing

test-fast:  ## So o dominio e a API, sem simular AWS. Roda em milissegundos.
	$(RUN) pytest tests/unit tests/properties tests/contract -q --no-cov

load:  ## Pico de 2.000 requests simultaneos contra a API local
	k6 run tests/load/get_pool.js

package:  ## Monta o zip de deploy com as dependencias de producao
	rm -rf dist/build dist/pool_selection.zip
	mkdir -p dist/build
	$(UV) export --frozen --no-dev --no-emit-project -o dist/requirements.txt
	$(UV) pip install --python-platform aarch64-manylinux2014 --python-version 3.13 \
		--target dist/build --no-deps --requirement dist/requirements.txt
	cp -r src/pool_selection dist/build/
	# O runtime do Lambda ja traz boto3 e botocore, e botocore sozinho e 24 MB de um
	# pacote de 34. Levar copia propria so aumentaria o cold start de um caminho onde a
	# latencia e o requisito. Em troca, a versao passa a ser a do runtime: se algum dia o
	# codigo depender de um recurso novo do SDK, ele volta para ca com pin explicito.
	rm -rf dist/build/boto3* dist/build/botocore* dist/build/s3transfer*
	find dist/build -name '__pycache__' -type d -prune -exec rm -rf {} +
	# Os .dist-info ficam: varias bibliotecas leem a propria versao via importlib.metadata
	# em tempo de import, e sem eles a falha aparece so em producao.
	cd dist/build && zip -qr ../pool_selection.zip .
	@echo "dist/pool_selection.zip: $$(du -h dist/pool_selection.zip | cut -f1)"

tf-validate:  ## Formata e valida o Terraform
	cd infra && terraform fmt -recursive && terraform init -backend=false -input=false >/dev/null && terraform validate

lint:  ## Lint e formatacao
	$(RUN) ruff check src tests tools
	$(RUN) ruff format --check src tests tools

fmt:  ## Corrige o que der automaticamente
	$(RUN) ruff check --fix src tests tools
	$(RUN) ruff format src tests tools

typecheck:  ## Tipagem estrita no dominio
	$(RUN) mypy

audit:  ## Vulnerabilidade conhecida nas dependencias
	@mkdir -p dist
	$(UV) export --frozen --all-extras --no-emit-project -o dist/requirements-audit.txt
	$(RUN) pip-audit --strict -r dist/requirements-audit.txt

check: lint typecheck test audit  ## Tudo que o CI roda

down:  ## Derruba os containers
	docker compose down -v

clean: down  ## Remove ambiente virtual e caches
	rm -rf .venv .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
