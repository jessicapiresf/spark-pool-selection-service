# O snapshot vive separado dos eventos: sao ciclos de vida diferentes. O evento e dado
# bruto que se guarda; o snapshot e derivado e reconstruivel a cada minuto.
resource "aws_s3_bucket" "snapshot" {
  bucket = "pool-selection-snapshot-${var.environment}-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "snapshot" {
  bucket                  = aws_s3_bucket.snapshot.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "snapshot" {
  bucket = aws_s3_bucket.snapshot.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "snapshot" {
  bucket = aws_s3_bucket.snapshot.id

  # Versionar permite voltar para o snapshot anterior se uma agregacao publicar lixo.
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "snapshot" {
  bucket = aws_s3_bucket.snapshot.id

  rule {
    id     = "expira-versoes-antigas"
    status = "Enabled"

    filter {}

    # Uma versao por minuto acumula rapido, e um snapshot de ontem nao serve para nada.
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }
}

# Contadores por minuto. A chave e MIN#<minuto> com o pool ou o job na sort key, para a
# agregadora ler um minuto inteiro em uma Query so.
resource "aws_dynamodb_table" "counters" {
  name         = "pool-selection-counters-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  # A janela deslizante se implementa aqui, sem job de limpeza.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = var.environment == "production"
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
