# Os alarmes seguem os sinais que aparecem antes de o usuario perceber. Idade do snapshot
# e taxa de degradacao avisam antes de a recomendacao apodrecer; erro na API avisa depois.

locals {
  alarm_actions = var.alarm_topic_arn == "" ? [] : [var.alarm_topic_arn]
}

# A agregadora roda a cada minuto. Se o snapshot passa de cinco, ela parou.
resource "aws_cloudwatch_metric_alarm" "snapshot_age" {
  alarm_name          = "pool-selection-snapshot-velho-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  threshold           = 300
  period              = 60
  statistic           = "Maximum"
  namespace           = "PoolSelection"
  metric_name         = "SnapshotAgeSeconds"
  treat_missing_data  = "breaching"

  alarm_description = "Snapshot com mais de 5 minutos: a agregadora parou ou esta falhando."
  alarm_actions     = local.alarm_actions
  ok_actions        = local.alarm_actions

  dimensions = {
    Component = "Api"
  }
}

resource "aws_cloudwatch_metric_alarm" "aggregator_failures" {
  alarm_name          = "pool-selection-agregadora-falhando-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  threshold           = 0
  period              = 60
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.aggregator.function_name
  }
}

# O servico esta no caminho de submissao de job: erro aqui trava pipeline de gente.
resource "aws_cloudwatch_metric_alarm" "api_errors" {
  alarm_name          = "pool-selection-api-com-erro-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  threshold           = 5
  period              = 60
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.api.function_name
  }
}

# Throttle significa que a cota de concorrencia barrou o pico, e nao a capacidade tecnica.
resource "aws_cloudwatch_metric_alarm" "api_throttles" {
  alarm_name          = "pool-selection-api-com-throttle-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  period              = 60
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.api.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "pool-selection-dlq-com-mensagem-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  period              = 300
  statistic           = "Maximum"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  treat_missing_data  = "notBreaching"

  alarm_description = "Evento que nao conseguiu ser ingerido. O historico fica com buraco."
  alarm_actions     = local.alarm_actions

  dimensions = {
    QueueName = aws_sqs_queue.events_dlq.name
  }
}

# Nao dispara pager: e o sinal de que o modelo esta cego, nao de que caiu.
resource "aws_cloudwatch_metric_alarm" "degraded_rate" {
  alarm_name          = "pool-selection-respondendo-degradado-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  threshold           = 0
  period              = 300
  statistic           = "Sum"
  namespace           = "PoolSelection"
  metric_name         = "DegradedResponses"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "pool-selection-${var.environment}"

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", width = 12, height = 6, x = 0, y = 0
        properties = {
          title  = "Latencia da API"
          region = var.region
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.api.function_name, { stat = "p50" }],
            ["...", { stat = "p99" }],
          ]
        }
      },
      {
        type = "metric", width = 12, height = 6, x = 12, y = 0
        properties = {
          title   = "Idade do snapshot"
          region  = var.region
          metrics = [["PoolSelection", "SnapshotAgeSeconds", "Component", "Api", { stat = "Maximum" }]]
        }
      },
      {
        type = "metric", width = 12, height = 6, x = 0, y = 6
        properties = {
          title  = "Ingestao"
          region = var.region
          metrics = [
            ["PoolSelection", "EventsIngested", "Component", "Ingestor", { stat = "Sum" }],
            [".", "EventsMalformed", ".", ".", { stat = "Sum" }],
            [".", "ObjectsSkippedAsDuplicate", ".", ".", { stat = "Sum" }],
          ]
        }
      },
      {
        type = "metric", width = 12, height = 6, x = 12, y = 6
        properties = {
          title   = "Distribuicao dos pools recomendados (efeito manada)"
          region  = var.region
          metrics = [["PoolSelection", "Recommendations", "Component", "Api", { stat = "Sum" }]]
        }
      },
    ]
  })
}
