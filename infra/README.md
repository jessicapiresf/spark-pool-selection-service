# Infra

Terraform descreve tudo: fila, tabela, agendamento, alarme e as tres funcoes. O SAM so
descreveria a parte serverless, e o CDK exigiria infra em outra linguagem.

## Estados por ambiente

```bash
terraform init -backend-config=backends/staging.hcl
terraform plan  -var-file=envs/staging.tfvars
terraform apply -var-file=envs/staging.tfvars
```

O `artifacts_path` aponta para o zip que o CI produz. Rodando na mao, gere antes com
`make -C .. package`.

## O que exige atencao antes do primeiro pico

- `api_reserved_concurrency` reserva a cota. O limite padrao da conta barra pico subito
  por cota, nao por capacidade tecnica, e o cenario de referencia sao 2.000 requests
  simultaneos.
- A tabela nasce sob demanda. Ela dobra a capacidade conforme cresce, mas barra pico bem
  acima do anterior. Para pico no mesmo horario todo dia, o certo e capacidade
  provisionada com auto scaling agendado.
- O limite de capacidade alvo do placement score sai do uso recente de spot da conta.
  Conta com pouco historico recebe limite baixo por padrao e o score volta rebaixado sem
  erro nenhum.
