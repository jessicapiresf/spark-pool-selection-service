# O requisito e um comando so, isolado, com o endpoint respondendo. `make dev` faz isso
# sem Docker: o uv instala o proprio Python, o seed roda o pipeline de verdade em memoria e
# grava o snapshot num arquivo, e a API sobe lendo dele. Nao ha passo manual entre clonar e
# ver o endpoint responder, e nao ha pre-requisito alem do uv.
#
# `make dev-aws` e o mesmo fluxo contra o LocalStack, para exercitar os adapters de AWS.

SHELL := /bin/bash
UV := $(shell command -v uv 2>/dev/null || echo "$$HOME/.local/bin/uv")
RUN := $(UV) run
PORT ?= 5050
export FALLBACK_POOLS ?= pool-r6.xlarge-us-east-1a,pool-c6.xlarge-us-east-1a

# `SNAPSHOT_PATH` e o que decide entre ler do disco e ler do S3, entao ele nao pode ser
# exportado no topo: os alvos de LocalStack precisam dele vazio, e um valor global vazaria
# para eles e faria a API ler o arquivo em vez do bucket.
LOCAL_SNAPSHOT := .local/snapshot/pools.json.gz
LOCALSTACK_ENV := \
	AWS_ENDPOINT_URL=http://localhost:4566 \
	AWS_ACCESS_KEY_ID=local \
	AWS_SECRET_ACCESS_KEY=local \
	AWS_DEFAULT_REGION=us-east-1 \
	SNAPSHOT_BUCKET=pool-selection-local \
	COUNTERS_TABLE=pool-selection-counters

.DEFAULT_GOAL := help
.PHONY: help dev dev-aws install localstack seed seed-aws serve serve-aws test test-fast lint fmt typecheck audit check clean down demo load package tf-validate

help:  ## Lista os alvos
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

dev: install seed serve  ## Ambiente completo em um comando, sem Docker

dev-aws: install localstack seed-aws serve-aws  ## O mesmo fluxo contra o LocalStack

install:  ## Cria o ambiente virtual isolado e instala as dependencias travadas
	@command -v $(UV) >/dev/null || { echo "uv nao encontrado: https://docs.astral.sh/uv/"; exit 1; }
	$(UV) sync --all-extras

localstack:  ## Sobe o LocalStack e espera ficar saudavel
	docker compose up -d localstack
	@echo "aguardando o LocalStack..."
	@for i in $$(seq 1 40); do \
		curl -sf http://localhost:4566/_localstack/health >/dev/null && exit 0; \
		sleep 1; \
	done; echo "LocalStack nao respondeu a tempo"; exit 1

seed:  ## Gera eventos sinteticos e publica um snapshot num arquivo local
	SNAPSHOT_PATH=$(LOCAL_SNAPSHOT) $(RUN) python tools/seed_local.py --mode file

seed-aws:  ## O mesmo, criando os recursos no LocalStack
	$(LOCALSTACK_ENV) $(RUN) python tools/seed_local.py --mode aws

serve:  ## Liga a API lendo o snapshot do disco, com reload
	@echo "API em http://localhost:$(PORT)/get-pools"
	@echo "OpenAPI em http://localhost:$(PORT)/docs"
	SNAPSHOT_PATH=$(LOCAL_SNAPSHOT) \
	$(RUN) uvicorn pool_selection.entrypoints.api.app:app --reload --port $(PORT)

serve-aws:  ## Liga a API lendo o snapshot do LocalStack
	@echo "API em http://localhost:$(PORT)/get-pools  (snapshot vindo do LocalStack)"
	$(LOCALSTACK_ENV) \
	$(RUN) uvicorn pool_selection.entrypoints.api.app:app --reload --port $(PORT)

demo:  ## Sobe tudo em container, sem precisar de Python na maquina
	docker compose up --build

test:  ## Suite inteira com o gate de cobertura
	$(RUN) pytest --cov --cov-report=term-missing

test-fast:  ## So o dominio e a API, sem simular AWS. Roda em milissegundos.
	$(RUN) pytest tests/unit tests/properties tests/contract -q --no-cov

load:  ## Pico de 2.000 requests simultaneos contra a API local
	@if command -v k6 >/dev/null 2>&1; then \
		k6 run tests/load/get_pool.js; \
	elif command -v docker >/dev/null 2>&1; then \
		echo "k6 nao encontrado localmente. Executando via Docker..."; \
		docker run --rm -i --network=host grafana/k6 run - < tests/load/get_pool.js; \
	else \
		echo "Erro: k6 ou Docker sao necessarios para rodar o teste de carga."; \
		echo "Instale o k6 (https://k6.io/docs/getting-started/installation/) ou Docker."; \
		exit 1; \
	fi

package:  ## Monta o zip de deploy com as dependencias de producao
	rm -rf dist/build dist/pool_selection.zip
	mkdir -p dist/build
	$(UV) export --frozen --no-dev --no-emit-project -o dist/requirements.txt
	$(UV) pip install --python-platform aarch64-manylinux_2_17 --python-version 3.13 \
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
	$(RUN) pip-audit --strict --disable-pip -r dist/requirements-audit.txt

check: lint typecheck test audit  ## Tudo que o CI roda

down:  ## Derruba os containers
	docker compose down -v

clean: down  ## Remove ambiente virtual e caches
	rm -rf .venv .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov .local
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
