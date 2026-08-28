locals {
  common_environment = {
    SNAPSHOT_BUCKET   = aws_s3_bucket.snapshot.id
    SNAPSHOT_KEY      = "snapshot/pools.json.gz"
    COUNTERS_TABLE    = aws_dynamodb_table.counters.name
    METRICS_NAMESPACE = "PoolSelection"
    POWERTOOLS_ENV    = var.environment
  }
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = local.functions
  name              = "/aws/lambda/pool-selection-${each.key}-${var.environment}"
  retention_in_days = var.log_retention_days
}

# O FastAPI pesa no cold start, e memoria a mais compra CPU proporcional na Lambda, entao
# 1024 MB sai mais barato que 512 no tempo total mesmo custando mais por ms.
resource "aws_lambda_function" "api" {
  function_name = "pool-selection-api-${var.environment}"
  role          = aws_iam_role.lambda["api"].arn
  handler       = "pool_selection.entrypoints.api.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 1024
  timeout       = 10
  publish       = true

  filename         = var.artifacts_path
  source_code_hash = filebase64sha256(var.artifacts_path)

  reserved_concurrent_executions = var.api_reserved_concurrency > 0 ? var.api_reserved_concurrency : -1

  environment {
    variables = merge(local.common_environment, {
      FALLBACK_POOLS         = join(",", var.fallback_pools)
      SNAPSHOT_TTL_SECONDS   = "30"
      STALE_AFTER_SECONDS    = "300"
      CAPACITY_CONCENTRATION = tostring(var.capacity_concentration)
    })
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_alias" "api_live" {
  name             = "live"
  function_name    = aws_lambda_function.api.function_name
  function_version = aws_lambda_function.api.version

  # O CodeDeploy move o peso entre as versoes durante o shift gradual.
  lifecycle {
    ignore_changes = [routing_config]
  }
}

# Nao cobra por request e a autenticacao IAM basta para um servico interno. Uma peca a
# menos que um API Gateway.
resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.api.function_name
  qualifier          = aws_lambda_alias.api_live.name
  authorization_type = "AWS_IAM"
}

resource "aws_lambda_function" "ingestor" {
  function_name = "pool-selection-ingestor-${var.environment}"
  role          = aws_iam_role.lambda["ingestor"].arn
  handler       = "pool_selection.entrypoints.ingestor.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 30

  filename         = var.artifacts_path
  source_code_hash = filebase64sha256(var.artifacts_path)

  environment {
    variables = local.common_environment
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_event_source_mapping" "events" {
  event_source_arn = aws_sqs_queue.events.arn
  function_name    = aws_lambda_function.ingestor.arn

  # Lote grande e o que faz a pre-agregacao valer: milhares de eventos viram poucas
  # escritas, uma por par de pool e minuto.
  batch_size                         = 1000
  maximum_batching_window_in_seconds = 20

  scaling_config {
    # A ingestao nao esta no caminho critico, e limitar a concorrencia protege a particao
    # de escrita do DynamoDB numa rajada de arquivos.
    maximum_concurrency = 10
  }

  # Sem `ReportBatchItemFailures`: a ingestora pre-agrega o lote inteiro antes de escrever,
  # entao nao existe "esta mensagem falhou e aquela nao". O lote e uma unidade. Declarar o
  # recurso sem devolver `batchItemFailures` faria a AWS tratar toda resposta como sucesso
  # total, o que e pior que nao declarar: parece protecao e nao e.
}

resource "aws_lambda_function" "aggregator" {
  function_name = "pool-selection-aggregator-${var.environment}"
  role          = aws_iam_role.lambda["aggregator"].arn
  handler       = "pool_selection.entrypoints.aggregator.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 1024

  # Roda a cada 60s, entao precisa terminar bem antes da proxima disparar.
  timeout = 45

  filename         = var.artifacts_path
  source_code_hash = filebase64sha256(var.artifacts_path)

  environment {
    variables = merge(local.common_environment, {
      DATABRICKS_HOST           = var.databricks_host
      DATABRICKS_TOKEN_SECRET   = var.databricks_token_secret_arn
      PLACEMENT_REFRESH_SECONDS = "300"
      CATALOG_REFRESH_SECONDS   = "86400"
      AGGREGATOR_MAX_MINUTES    = "360"
      AGGREGATOR_LAG_MINUTES    = tostring(var.aggregator_lag_minutes)
    })
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

# Cron em container exigiria container ligado o tempo todo, e o Airflow e peca grande
# demais para um job de um minuto.
resource "aws_scheduler_schedule" "aggregator" {
  name                = "pool-selection-aggregator-${var.environment}"
  schedule_expression = "rate(${var.aggregator_interval_minutes} minute)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.aggregator.arn
    role_arn = aws_iam_role.scheduler.arn

    retry_policy {
      # Se uma rodada falhar, a proxima ja acontece em 60s e recupera os minutos pendentes.
      maximum_retry_attempts = 1
    }
  }
}
