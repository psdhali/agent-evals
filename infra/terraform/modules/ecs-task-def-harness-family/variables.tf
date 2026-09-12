variable "name_prefix" {
  type        = string
  description = "Resource name prefix."
}

variable "purpose" {
  type        = string
  description = "'dev' or 'scale'."
}

variable "families" {
  type        = map(object({ image = string }))
  description = <<EOT
    Per-env task-definition families, family name -> container image. DERIVED by
    scripts/gen_harness_task_families.py from the warm-cache manifest (ADR-0030
    H2) — one family per env image, each pointing at its `-hw` tag. Do not edit
    by hand: regenerate with `make gen-harness-families`.
  EOT
}

variable "harness_name" {
  type        = string
  default     = "harness"
  description = "Harness id for the log stream prefix / HARNESS env (the job's harness arrives via the dispatcher's containerOverrides)."
}

variable "gateway_base_url" {
  type        = string
  description = "LiteLLM gateway ALB base URL the agent routes through (LITELLM_BASE_URL)."
}

variable "redis_endpoint" {
  type        = string
  description = "Valkey endpoint for instance_progress (cache module)."
}

variable "artifact_bucket_name" {
  type        = string
  description = "Durable artifacts S3 bucket name (patch/trajectory/raw-log uploads)."
}

variable "artifact_bucket_arn" {
  type        = string
  description = "ARN of the artifact bucket, for the scoped s3:PutObject IAM statement."
}

variable "dataset_bucket_name" {
  type        = string
  description = "Durable dataset S3 bucket name — the pinned SWE-bench mirror this worker reads for repo prep (PUBLIC schema, gold excluded)."
}

variable "log_group" {
  type        = string
  description = "CloudWatch log group for the harness family tasks."
}

variable "secrets" {
  type = object({
    master = string
  })
  description = "Secret ARN for the harness task. LITELLM_MASTER_KEY only — OPENROUTER_API_KEY was dropped (A1-8: nothing in the framework reads it; the harness calls the gateway, never OpenRouter)."
}

variable "queue_arns" {
  type = object({
    harness   = string
    eval      = string
    results   = string
    llm_calls = string # ADR-0037 M0 §3: worker SENDs llm_calls.jsonl pointers
  })
  description = "SQS queue ARNs — used in the IAM policy (URLs are not IAM resources)."
}

variable "queue_urls" {
  type = object({
    harness = string
    eval    = string
    results = string
  })
  description = "SQS queue URLs (sqs module)."
}
