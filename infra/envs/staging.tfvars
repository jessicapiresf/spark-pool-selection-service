environment   = "staging"
events_bucket = "dados-plataforma-staging"
events_prefix = "spark-job-events/"

api_reserved_concurrency = -1

fallback_pools = [
  "pool-r6.xlarge-us-east-1a",
  "pool-c6.xlarge-us-east-1a",
]
