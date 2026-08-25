variable "region" {
  description = "Regiao unica. Multi-regiao exigiria decidir se a comparacao atravessa regioes."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  type = string

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment precisa ser staging ou production."
  }
}

variable "events_bucket" {
  description = "Bucket onde a plataforma escreve os eventos de termino de job."
  type        = string
}

variable "events_prefix" {
  description = "Prefixo dos eventos. Restringe a notificacao ao que interessa."
  type        = string
  default     = "events/"
}

variable "artifacts_path" {
  description = "Caminho do zip com o codigo, produzido pelo CI."
  type        = string
  default     = "../dist/pool_selection.zip"
}

variable "databricks_host" {
  type    = string
  default = ""
}

variable "databricks_token_secret_arn" {
  description = "Secret no Secrets Manager. O token nunca entra em variavel de ambiente."
  type        = string
  default     = ""
}

variable "fallback_pools" {
  description = "Lista estatica usada quando nao ha snapshot nenhum. Ultimo recurso."
  type        = list(string)
  default     = []
}

variable "api_reserved_concurrency" {
  description = <<-EOT
    Concorrencia reservada da API. O limite padrao da conta barra pico subito por cota, nao
    por capacidade tecnica, e o cenario de referencia sao 2.000 requests simultaneos.
  EOT
  type        = number
  default     = 200
}

variable "aggregator_interval_minutes" {
  type    = number
  default = 1
}

variable "alarm_topic_arn" {
  description = "SNS para onde vao os alarmes. Vazio desliga a notificacao."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  type    = number
  default = 30
}
