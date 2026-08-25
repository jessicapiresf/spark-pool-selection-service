environment   = "production"
events_bucket = "dados-plataforma-producao"
events_prefix = "spark-job-events/"

api_reserved_concurrency = 400

# Ultimo recurso, usado so quando nao ha snapshot nenhum. Um por AZ, para a escolha
# uniforme nao concentrar tudo em uma zona.
fallback_pools = [
  "pool-r6.xlarge-us-east-1a",
  "pool-r6.xlarge-us-east-1b",
  "pool-r6.xlarge-us-east-1c",
  "pool-c6.xlarge-us-east-1a",
  "pool-c6.xlarge-us-east-1b",
]
