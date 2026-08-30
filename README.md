# Spark Pool Selection Service

> Service REST de alta performance para recomendação preditiva de **Spark Instance Pools** em instâncias Spot da AWS, prevenindo quedas por escassez de capacidade.

---

## ⚡ Quickstart (1 Comando)

Você pode subir o serviço completo (API + Geração de Eventos + Pipeline de Agregação) em **1 comando**:

### Opção 1: Via Docker (Recomendado se não tiver `uv`)
```bash
docker compose up
# Ou via Makefile:
make demo
```

### Opção 2: Via `uv` (Nativo em Python)
```bash
make dev
```
*Requisito: [uv](https://docs.astral.sh/uv/) instalado (o `uv` gerencia o Python 3.13 e as dependências automaticamente).*

A API estará disponível em `http://localhost:5050/get-pools`, com documentação Swagger interativa em `http://localhost:5050/docs`.

---

## 📌 Testando o Endpoint

```bash
curl 'http://localhost:5050/get-pools?job_id=etl-vendas&profile=memory'
```
*(Alias `/get-pool` também suportado).*

### Exemplo de Resposta JSON

```json
{
  "pool_id": "pool-r6.2xlarge-us-east-1a",
  "instance_type": "r6.2xlarge",
  "availability_zone": "us-east-1a",
  "score": 0.87,
  "credible_interval": [0.84, 0.99],
  "evidence": { "az_samples": 210.4, "job_samples": 12.0, "source": "job_history" },
  "capacity": { "free_slots": 52, "idle_instances": 2, "falls_back_to_on_demand": false },
  "az_outlook": { "spot_placement_score": 9, "target_capacity": 20, "age_seconds": 142.0 },
  "alternatives": [{ "pool_id": "pool-r6.xlarge-us-east-1a", "score": 0.81 }],
  "snapshot": { "age_seconds": 23.4, "stale": false },
  "degraded": false
}
```

Parâmetros suportados: `job_id`, `instance_types`, `family`, `profile`, `availability_zones`, `exclude_pools`, `min_samples`, `strategy`, `alternatives`, `seed`. Veja o [Contrato da API](docs/arquitetura.md#5-contrato-da-api-get-get-pools).

---

## 🧠 Como Funciona

1. **Desperdício Zero no Request (Latência < 5ms):** Ranking é pré-calculado a cada 1 minuto por um Worker assíncrono e armazenado no S3 em Gzip. A API de leitura apenas lê o snapshot da memória RAM.
2. **Aprendizado Estatístico Duplo (Thompson Sampling):**
   - **Escassez da AZ (Meia-vida de 20 min):** Adaptação ultra-rápida a quedas no mercado Spot da AWS.
3. **Prevenção de Efeito Manada (Capacity Caps):** Aplica limites dinâmicos de alocação de tráfego baseados na capacidade livre real dos pools de instâncias (integrando com APIs de plataformas como Databricks ou telemetria de clusters EMR/Spark autogeridos).

---

## 🛠️ Comandos Principais

| Comando | Descrição |
|---|---|
| `make demo` / `docker compose up` | Sobe ambiente completo em 1 comando via Docker |
| `make dev` | Sobe ambiente local via `uv` sem necessidade de Docker |
| `make dev-aws` | Executa fluxo completo integrado ao LocalStack |
| `make test` | Roda toda a suíte de testes (gate de 85% de cobertura) |
| `make test-fast` | Testes rápidos de domínio/contrato (sem mocks AWS, em ms) |
| `make check` | Executa linter (`ruff`), checagem de tipos (`mypy`) e auditoria |
| `make load` | Teste de carga simulando pico de 2.000 req/s via `k6` |
| `make package` | Gera o pacote ZIP otimizado para deploy Serverless na AWS Lambda |

---

## 📂 Estrutura do Repositório

```
src/pool_selection/
├── domain/          # Python puro: regras de negócio, Thompson Sampling, sem AWS/FastAPI
├── ports/           # Interfaces e contratos abstratos
├── adapters/        # Implementações concretas (DynamoDB, S3, Databricks, EC2)
└── entrypoints/     # Lambda Handlers (API REST, Ingestor SQS, Agregador EventBridge)
infra/               # Módulos Terraform (IaC Serverless completo)
tools/               # Geradores de eventos sintéticos e scripts dev
tests/               # Unitários, Propriedades (Hypothesis), Integração (moto), Carga (k6)
```

---

## 📄 Documentação Técnica

- [📐 Arquitetura Completa](docs/arquitetura.md): Decisões de design, modelos de decaimento, resiliência e comparação de alternativas.
- [📋 Requisitos e Premissas](docs/requisitos.md): Mapeamento de requisitos funcionais e não-funcionais.
- [🚀 Guia de Infraestrutura](infra/README.md): Detalhes de provisionamento Terraform e pipeline CI/CD Serverless.

