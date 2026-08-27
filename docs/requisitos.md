# Requisitos

Reduzir falha de job Spark por indisponibilidade de instâncias spot, indicando o pool com
maior chance de o job terminar. O contexto do problema está na
[arquitetura](arquitetura.md#1-o-que-o-serviço-faz).

A coluna Origem separa o que foi pedido de forma direta, o que é derivado do contexto e do
formato dos dados, e o que é premissa assumida. As premissas ficam em seção própria,
porque é onde mora o risco de eu ter entendido algo errado.

## Entrada

Arquivos `.json` em bucket S3, atualizados em tempo real. Cada arquivo traz um evento por
linha, então a leitura é linha a linha e não um `json.load` do arquivo inteiro.

| Campo | Tipo | Descrição |
|---|---|---|
| `finished_at` | string | Fim do job. Timestamp UTC em ISO. |
| `job_id` | string | Nome do job, escolhido pelo dono. |
| `pool_id` | string | `pool-<instance-type>-<az>`. Carrega tipo de instância e AZ. |
| `status` | string | Sucesso ou falha. |
| `reason` | string | `SPOT_INSTANCE_TERMINATION`, `TIMED_OUT` ou `SPARK_EXECUTION_ERROR`. |

## Funcionais

| ID | Requisito | Origem | Onde |
|---|---|---|---|
| RF-01 | Endpoint que devolve um ID de pool. O enunciado cita `/get-pool` e `/get-pools`, e o serviço responde nos dois. | Pedido | [Contrato](arquitetura.md#5-contrato-do-endpoint) |
| RF-02 | O pool devolvido deve ter alta probabilidade de o job rodar sem perder instâncias spot. | Pedido | [Seleção](arquitetura.md#3-como-o-pool-é-escolhido) |
| RF-03 | Aceitar parâmetros que restrinjam os tipos de instância (ex. só memória, só CPU). | Pedido | [Contrato](arquitetura.md#5-contrato-do-endpoint) e [perfil](arquitetura.md#de-onde-sai-o-perfil-de-um-tipo-de-instância) |
| RF-04 | Resposta válida a qualquer momento do dia, acompanhando a variação de disponibilidade. | Derivado | [Como funciona](arquitetura.md#2-como-funciona) |
| RF-05 | Recomendar para um job específico, não em geral. | Derivado | Parâmetro `job_id` na [seleção](arquitetura.md#3-como-o-pool-é-escolhido) |
| RF-06 | Consumir os arquivos `.json` do S3, que trazem um evento por linha. | Derivado | [Como funciona](arquitetura.md#2-como-funciona) |
| RF-07 | Distinguir os motivos de falha: só `SPOT_INSTANCE_TERMINATION` fala sobre a AZ. | Derivado | [Seleção](arquitetura.md#3-como-o-pool-é-escolhido) |
| RF-08 | Reagir à queda de uma AZ antes de o primeiro job morrer nela, e não só depois. | Derivado | [Sinal preditivo](arquitetura.md#o-sinal-que-chega-antes-da-falha) |

## Não funcionais

| ID | Requisito | Origem | Onde |
|---|---|---|---|
| RNF-01 | Python acima de 3.9. | Pedido | Python 3.13, com o [motivo](arquitetura.md#5-contrato-do-endpoint) |
| RNF-02 | Alta disponibilidade. | Pedido | [Disponibilidade](arquitetura.md#4-disponibilidade-e-degradação) |
| RNF-03 | Escalar para picos imprevisíveis, sem falhar na hora de obter um pool. | Pedido | [Escala](arquitetura.md#7-escala) e [teto por capacidade](arquitetura.md#o-sorteio-sozinho-não-segura-um-pico) |
| RNF-04 | Pronta para produção. | Pedido | [CI/CD e testes](arquitetura.md#10-cicd-e-testes) |
| RNF-05 | Ambiente de dev em um comando, isolado, respondendo em `http://localhost:5050/get-pools`. | Pedido | `make dev`, sem Docker, na [fase 3](arquitetura.md#9-o-que-foi-construído) |

## Documentação e processo

| ID | Entregável | Origem | Onde |
|---|---|---|---|
| RD-01 | Racional do framework. | Pedido | [6.1](arquitetura.md#61-framework-web) |
| RD-02 | Racional do banco de dados. | Pedido | [6.4](arquitetura.md#64-banco-de-dados) |
| RD-03 | Decisões arquiteturais. | Pedido | [arquitetura.md](arquitetura.md) |
| RD-04 | Documentação do endpoint. | Pedido | [Contrato](arquitetura.md#5-contrato-do-endpoint) e OpenAPI em `/docs` |
| RD-05 | Estratégia de CI/CD. | Pedido | [CI/CD e testes](arquitetura.md#10-cicd-e-testes) |
| RD-06 | Testes unitários. | Pedido | [CI/CD e testes](arquitetura.md#10-cicd-e-testes) |

## Escolhas delegadas

Deixadas em aberto de propósito. O racional de cada uma está na arquitetura.

| Assunto | Decisão |
|---|---|
| Framework | FastAPI |
| Banco de dados | DynamoDB para contadores, S3 para o snapshot |
| Formato da resposta | JSON com pool, score, evidência, capacidade e alternativas |
| Espalhamento no pico | Teto por capacidade sobre o sorteio, sem estado compartilhado |
| Classificação de perfil | Derivada de `DescribeInstanceTypes`, não tabela fixa no código |
| Fonte preditiva | Spot placement score da AWS, por perfil, a cada 5 minutos |
| Autenticação | IAM na Function URL |
| Onde roda | AWS Lambda |

## Premissas

| ID | Premissa | Se estiver errada |
|---|---|---|
| P-01 | "Ambiente isolado" significa ambiente virtual por projeto na máquina do desenvolvedor, criado pelo `uv`, que instala o próprio Python. | Se a exigência for container obrigatório, `docker compose up` cobre o mesmo caminho. |
| P-02 | O job informa seu `job_id` na chamada. | Sem ele, some o fator de adequação e job leve e pesado recebem a mesma resposta. |
| P-03 | A plataforma roda Databricks, porque "pool de instâncias" é vocabulário dela. | Some a fonte de capacidade ao vivo e o serviço fica só com o histórico de falhas. |
| P-04 | Só existe evento de job que terminou. Job que nunca subiu pode não gerar registro. | Pool ruim demais apareceria saudável por ausência de dado. |
| P-05 | `TIMED_OUT` é ambíguo e entra com peso parcial. | Se for sempre escassez, o peso deveria ser cheio. É configuração, não código. |
| P-06 | Região única. | Multi-região multiplica os pools e exige decidir se a comparação atravessa regiões. |
| P-07 | Consumidor interno, o scheduler de jobs. | Se for exposto para fora, entra API Gateway com throttling. |
| P-08 | A conta tem uso recente de spot suficiente para o limite de capacidade alvo do placement score não cair no default baixo. | O score volta rebaixado sem erro, e o fator preditivo precisa ser desligado até o limite ser ajustado. |

## Metas adotadas

Disponibilidade e escala foram pedidas sem número. Sem número não dá para testar, então
estas são as metas adotadas.

| Meta | Valor |
|---|---|
| Latência p99 | abaixo de 50 ms, fora cold start |
| Disponibilidade | responde algo mesmo com o snapshot indisponível |
| Frescor do dado | snapshot com no máximo 90 segundos |
| Pico suportado | 2.000 requests simultâneos sem throttling |
| Concentração no pico | nenhum pool recebe fatia maior do que a folga dele comporta |
| Reação a queda de AZ, com previsão | recomendação migra em até 5 minutos, sem esperar falha |
| Reação a queda de AZ, só pelo histórico | recomendação migra em até 20 minutos |

## Fora de escopo

Criar ou redimensionar pools, submeter o job, verificar se a recomendação foi seguida, e
decidir entre spot e on-demand. Construir modelo próprio de previsão também está fora: o
serviço consome o placement score que a AWS já publica, não tenta prever por conta.

## Referências

Consultar antes de afirmar qualquer coisa sobre instância, spot ou pool.

| Fonte | Para quê |
|---|---|
| [Apache Spark](https://spark.apache.org/docs/latest/) | Uso de executores e memória. |
| [AWS EC2](https://docs.aws.amazon.com/ec2/) | Catálogo de tipos e famílias, e o `DescribeInstanceTypes` que alimenta a classificação de perfil. |
| [Vantage](https://instances.vantage.sh/) | vCPU, memória e preço por tipo. Confere a classificação sem precisar chamar a API. |
| [EC2 Spot](https://aws.amazon.com/pt/ec2/spot/) | Como funciona a interrupção. |
| [Spot placement score](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-placement-score.html) e [a API](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_GetSpotPlacementScores.html) | Fonte preditiva por AZ. O mínimo de três tipos de instância e o limite de capacidade alvo são o que moldam a integração. |
| [Regiões e AZs](https://aws.amazon.com/about-aws/global-infrastructure/regions_az/) | Validar o parse do `pool_id`. |
| [Runtimes do Lambda](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html) | Prazo de depreciação de cada versão de Python. Base do RNF-01. |
| [Pools Databricks](https://docs.databricks.com/en/compute/pool-index.html) e [API](https://docs.databricks.com/api/workspace/instancepools) | `max_capacity`, `used_count`, `idle_count`, `state` e `aws_attributes`. É a fonte de capacidade ao vivo, detalhada na [seleção](arquitetura.md#3-como-o-pool-é-escolhido). |
