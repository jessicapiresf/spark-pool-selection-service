output "api_url" {
  description = "Function URL da API. Autenticacao IAM: assinar com SigV4."
  value       = aws_lambda_function_url.api.function_url
}

output "snapshot_bucket" {
  value = aws_s3_bucket.snapshot.id
}

output "counters_table" {
  value = aws_dynamodb_table.counters.name
}

output "events_queue_url" {
  value = aws_sqs_queue.events.url
}

output "dlq_url" {
  description = "Mensagem aqui significa evento perdido: o historico fica com buraco."
  value       = aws_sqs_queue.events_dlq.url
}

output "api_alias_arn" {
  description = "Alias versionado. E o alvo do shift gradual no deploy."
  value       = aws_lambda_alias.api_live.arn
}
