# Sem a fila nao ha DLQ nem controle de concorrencia, e uma rajada de arquivos no S3
# viraria uma rajada de invocacoes.
resource "aws_sqs_queue" "events" {
  name                       = "pool-selection-events-${var.environment}"
  visibility_timeout_seconds = 180 # seis vezes o timeout da ingestora
  message_retention_seconds  = 345600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.events_dlq.arn
    maxReceiveCount     = 5
  })
}

# Mensagem que falhou varias vezes fica aqui, para investigar sem travar o resto.
resource "aws_sqs_queue" "events_dlq" {
  name                      = "pool-selection-events-dlq-${var.environment}"
  message_retention_seconds = 1209600
}

data "aws_iam_policy_document" "queue_from_s3" {
  statement {
    effect  = "Allow"
    actions = ["sqs:SendMessage"]

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }

    resources = [aws_sqs_queue.events.arn]

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.events.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "events" {
  queue_url = aws_sqs_queue.events.id
  policy    = data.aws_iam_policy_document.queue_from_s3.json
}

resource "aws_s3_bucket_notification" "events" {
  bucket = aws_s3_bucket.events.id

  queue {
    queue_arn     = aws_sqs_queue.events.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = var.events_prefix
  }

  depends_on = [aws_sqs_queue_policy.events]
}
