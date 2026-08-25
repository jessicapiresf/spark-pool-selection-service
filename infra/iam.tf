data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

locals {
  functions = toset(["api", "ingestor", "aggregator"])
}

resource "aws_iam_role" "lambda" {
  for_each           = local.functions
  name               = "pool-selection-${each.key}-${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "logs" {
  for_each   = local.functions
  role       = aws_iam_role.lambda[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# A API so le o snapshot. Nao escreve nada, nao le contador, nao chama fonte externa.
data "aws_iam_policy_document" "api" {
  statement {
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.snapshot.arn}/*"]
  }
}

# A ingestora le os eventos, consome a fila e soma contadores. Nao le o snapshot.
data "aws_iam_policy_document" "ingestor" {
  statement {
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.events_bucket}/${var.events_prefix}*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["dynamodb:UpdateItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.counters.arn]
  }

  statement {
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.events.arn]
  }
}

# A agregadora e a unica que fala com as fontes externas e a unica que escreve o snapshot.
data "aws_iam_policy_document" "aggregator" {
  statement {
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.snapshot.arn}/*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["dynamodb:Query"]
    resources = [aws_dynamodb_table.counters.arn]
  }

  # Nenhuma das duas aceita restricao por recurso: sao chamadas de leitura da conta.
  statement {
    effect = "Allow"
    actions = [
      "ec2:GetSpotPlacementScores",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeAvailabilityZones",
    ]
    resources = ["*"]
  }

  dynamic "statement" {
    for_each = var.databricks_token_secret_arn == "" ? [] : [1]

    content {
      effect    = "Allow"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [var.databricks_token_secret_arn]
    }
  }
}

resource "aws_iam_role_policy" "api" {
  role   = aws_iam_role.lambda["api"].id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_iam_role_policy" "ingestor" {
  role   = aws_iam_role.lambda["ingestor"].id
  policy = data.aws_iam_policy_document.ingestor.json
}

resource "aws_iam_role_policy" "aggregator" {
  role   = aws_iam_role.lambda["aggregator"].id
  policy = data.aws_iam_policy_document.aggregator.json
}

# O agendador so pode invocar a agregadora.
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "pool-selection-scheduler-${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = [aws_lambda_function.aggregator.arn, "${aws_lambda_function.aggregator.arn}:*"]
    }]
  })
}
