# spark-pool-selection-service

API REST que responde, a qualquer hora do dia, em qual pool de instâncias spot um Spark
job tem mais chance de rodar até o fim.

Job que sobe em uma AZ sem capacidade spot morre no meio da execução. Ninguém publica
quanta capacidade existe em cada pool, então o serviço junta o que dá para saber: o
histórico de término dos jobs, a capacidade atual dos pools e a previsão de spot da AWS.

## Comece por aqui

```bash
make dev
```

Um comando, sem Docker. Cria o ambiente virtual isolado com `uv`, gera eventos sintéticos,
roda o pipeline de verdade sobre eles, publica um snapshot e liga a API em
<http://localhost:5050/get-pools>, com a documentação interativa em
<http://localhost:5050/docs>.

O único pré-requisito é o [uv](https://docs.astral.sh/uv/). Ele instala o próprio Python
3.13, então não importa qual versão está na máquina.

Quem preferir container, ou não quiser nem o `uv`:

```bash
docker compose up
```

E quem quiser exercitar os adapters de AWS contra o LocalStack, o que precisa de Docker:

```bash
make dev-aws
```

## O endpoint

```bash
curl 'localhost:5050/get-pools?job_id=etl-vendas&profile=memory'
```

O enunciado cita os dois nomes, então `/get-pool` também responde, como alias.

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

Todos os parâmetros são opcionais e combináveis: `job_id`, `instance_types`, `family`,
`profile`, `availability_zones`, `exclude_pools`, `min_samples`, `strategy`,
`alternatives`, `seed`. A referência completa está no OpenAPI em `/docs` e no
[contrato](docs/arquitetura.md#5-contrato-do-endpoint).

## Como funciona

Dois caminhos que nunca se cruzam em tempo de request. Um ranking pré-calculado uma vez
por minuto, e uma API que só consulta o resultado, já em memória. Na maior parte das
chamadas nenhuma requisição de rede acontece.

O score de cada pool combina duas faixas de incerteza, e a escolha é um sorteio dentro
delas, não o máximo. Isso resolve dois problemas: um pool que parou de ser recomendado
ainda gera evidência de vez em quando, e um pool com 1 sucesso em 1 tentativa não passa na
frente de um com 200 em 210.

Sortear sozinho não segura um pico, só o suaviza, porque a chance de um pool vencer não
sabe quantas vagas ele tem. Por isso a fatia de cada candidato tem teto derivado da
capacidade livre: um pool com duas vagas nunca recebe a enxurrada que um com sessenta
receberia, e o excedente escorre para o próximo melhor.

O detalhamento, com as alternativas descartadas em cada decisão, está na
[arquitetura](docs/arquitetura.md).

## Documentação

- [Arquitetura](docs/arquitetura.md): o desenho, cada escolha de ferramenta com o que foi
  descartado, o algoritmo de seleção e os limites conhecidos.
- [Requisitos](docs/requisitos.md): o que o serviço precisa fazer, separado entre pedido,
  derivado e premissa assumida, com rastreabilidade até a arquitetura.
- [Infra](infra/README.md): o que exige atenção antes do primeiro pico.

## Comandos

| Comando | O que faz |
|---|---|
| `make dev` | Ambiente completo em um comando, sem Docker |
| `make dev-aws` | O mesmo fluxo contra o LocalStack |
| `make test` | Suíte inteira com o gate de cobertura de 85% no domínio |
| `make test-fast` | Só domínio, propriedades e contrato. Roda em milissegundos, sem simular AWS. |
| `make check` | Tudo que o CI roda: lint, tipos, testes e auditoria de dependência |
| `make load` | Pico de 2.000 requests simultâneos com k6 |
| `make tf-validate` | Formata e valida o Terraform |
| `make package` | Monta o zip de deploy |
| `make demo` | Sobe tudo em container |

`make help` lista o resto.

## Estrutura

```
src/pool_selection/
├── domain/          # Python puro: sem boto3, sem FastAPI, sem AWS
├── ports/           # os contratos com o mundo externo
├── adapters/        # dynamodb, s3, databricks, ec2, memória
└── entrypoints/     # api, ingestor, aggregator
infra/               # Terraform
tools/               # gerador de eventos e seed do ambiente local
tests/               # unit, propriedades, integração, contrato, simulação, carga
```

O miolo estatístico não importa nada de AWS, então a maior parte da suíte roda em
milissegundos e sem simular nada. As bordas ficam finas o bastante para poucos testes de
integração.

## Testes

| Nível | O que cobre |
|---|---|
| Unitário | Classificação de evento, decaimento, os dois fatores, sorteio e filtros |
| Propriedade | Invariantes que exemplo não pega, com Hypothesis |
| Integração | Adapters contra AWS simulada em processo, com moto |
| Contrato | O endpoint inteiro, incluindo validação de parâmetro e formato da resposta |
| Simulação | Derruba a disponibilidade de uma AZ e verifica que a recomendação migra |
| Carga | Pico de 2.000 requests simultâneos, com k6 |

O teste de simulação é o único que prova que o algoritmo funciona. Os outros verificam
peças.
