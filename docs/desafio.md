Uma das ferramentas mais utilizadas no ambiente de dados da empresa é o Apache Spark. Seu princípio de funcionamento, de forma bastante resumida, baseia-se na distribuição dos dados na memória de diferentes nós (que podem coexistir em uma mesma instância de computação ou em diferentes instâncias ligadas a uma mesma rede) para que esses nós processem os dados de forma distribuída e horizontalmente escalável.

Da maneira que o Spark é executado na empresa, essas instâncias são divididas em grupos, chamados pools de instâncias. Esses pools podem conter uma quantidade ilimitada (embora eventualmente restrita) de instâncias do tipo EC2 da AWS (3), mas todas as instâncias só podem pertencer a um único tipo. No entanto, para que possamos executar os fluxos de dados com custo reduzido de computação, as instâncias utilizadas são do tipo spot, de forma que a disponibilidade varia entre as diversas zonas de disponibilidade da AWS (AZs), podendo ser abundante em algumas e raras em outras, variando também ao longo do dia. A restrição eventual surge desse motivo: o máximo de instâncias que um pool pode ter está condicionado à disponibilidade máxima de instâncias EC2 spot de um determinado tipo, em uma determinada AZ.

Sendo assim, é importante que a qualquer momento do dia possamos escolher o pool de instâncias com maior disponibilidade de instâncias possível, uma vez que jobs executando em pools de instâncias em AZs de baixa disponibilidade tendem a falhar durante a execução por falta de poder computacional disponível.

Considere que temos disponíveis dados em tempo real, em um AWS S3 bucket, com o seguinte formato:

```json
{
  "finished_at": "2024-08-07T00:04:52.767830",
  "job_id": "my-job",
  "pool_id": "pool-r6.xlarge-us-east-1c",
  "status": "FAILED",
  "reason": "SPOT_INSTANCE_TERMINATION"
}
```

Em que:

- `finished_at`: momento em que o Spark job foi finalizado (timestamp em UTC, formato ISO)
- `job_id`: nome do Spark job, geralmente escolhido pelo dono do job (string)
- `pool_id`: ID dos pools de instância, no formato `pool-<instance-type>-<az>` (ex. `pool-i3.xlarge-us-east-1a`) (string)
- `status`: estado final do job, se finalizou com sucesso ou falha (string)
- `reason`: motivo da falha, que pode ser `SPOT_INSTANCE_TERMINATION`, `TIMED_OUT` ou `SPARK_EXECUTION_ERROR` (string)

O formato do arquivo é `.json` com um evento por linha, sendo que cada evento possui o formato descrito anteriormente.

Dado o contexto acima, precisamos desenvolver uma API REST capaz de retornar, a qualquer momento do dia, o melhor pool de instâncias para que um determinado Spark job possa executar com baixa probabilidade de falha devido à indisponibilidade de instâncias na AZ.

Os requisitos da API são os seguintes:

1. A API deve ser desenvolvida em Python (> 3.9) e possuir um endpoint `/get-pool` que, ao receber uma chamada, devolva um ID de pool com as características desejadas (alta probabilidade de um job executar sem perder instâncias spot). Esse endpoint deve aceitar parâmetros para restringir os tipos de instâncias que podem ser retornados (ex. apenas instâncias focadas em memória - como as instâncias do tipo `r6.xlarge` - ou apenas instâncias focadas em CPU - tais como as `c6.xlarge`), uma vez que os jobs possuem características diferentes de uso de recursos

2. Nenhum framework específico é exigido, utilize aquele que julgar mais adequado para a sua solução. Explique o racional da escolha nas documentações.

3. Não há nenhum pré-requisito em relação ao uso de bancos de dados (SQL ou NOSQL), utilize aquele que julgar mais adequado para a sua solução, e caso julgue necessário. Explique o racional da escolha nas documentações.

4. O ambiente de spark jobs da empresa é bastante dinâmico. Em certos momentos do dia poucos jobs são executados, enquanto em outros momentos muitos jobs são disparados de uma só vez, e como os times possuem autonomia para criar novos jobs de forma descentralizada em relação ao time de plataforma de dados, é difícil prever se, no dia seguinte, a distribuição de jobs ao longo do dia será a mesma do dia anterior. É importante que a API desenvolvida tenha alta disponibilidade e seja escalável a ponto de garantir que, nos momentos de pico, não haverá problemas para se conseguir obter um pool de instâncias.

5. A API deve estar pronta para ser usada em produção. Dessa forma, documentação das tomadas de decisão arquiteturais, documentação do endpoint em si, estratégia de CI/CD e testes unitários são esperados.

6. A subida do ambiente de desenvolvimento da API deve ser imediata: um único comando deve instalar as dependências de forma isolada dentro da máquina do desenvolvedor e iniciar todo o ambiente necessário para que o endpoint `/get-pools` funcione e retorne a resposta desejada em http://localhost:5050/get-pools
