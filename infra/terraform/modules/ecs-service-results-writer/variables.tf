variable "name_prefix" {
  type        = string
  description = "Resource name prefix."
}

variable "purpose" {
  type        = string
  description = "'dev' or 'scale'."
}

variable "cluster_name" {
  type        = string
  description = "ECS cluster."
}

variable "image" {
  type        = string
  description = "Framework image (orchestrator) — command ['results-writer'] runs the results consume loop + llm_calls daemon + DLQ consumers (reaper rule 1)."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnets."
}

variable "security_group_ids" {
  type        = list(string)
  description = "Task security groups."
}

variable "log_group" {
  type        = string
  description = "CloudWatch log group."
}

variable "replicas" {
  type        = number
  default     = 2
  description = "results-writer replicas. ADR-0018 made this seam safe (idempotent upserts, eval-enqueue gated on the state-rank guard, not on a fresh INSERT) — dev runs 2 so that claim is continuously tested, not theoretical."
}

variable "secrets" {
  type = object({
    database_url = string
  })
  description = "Secret ARN: Aurora DATABASE_URL."
}

variable "queue_urls" {
  type = object({
    harness   = string
    eval      = string
    results   = string
    llm_calls = string
  })
  description = "SQS queue URLs — results (consume), eval-jobs (send), llm-calls (consume), harness-jobs (only for resolving its -dlq companion)."
}

variable "queue_arns" {
  type = object({
    eval               = string
    results            = string
    llm_calls          = string
    model_observations = string
  })
  description = "SQS queue ARNs for the main queues this service touches directly (send eval-jobs, receive results + llm-calls + model-observations)."
}

variable "queue_dlq_arns" {
  type = object({
    harness = string
    eval    = string
  })
  description = "harness-jobs-dlq / eval-jobs-dlq ARNs — reaper rule 1's two DLQ consumer threads (_run_dlq_reaper)."
}

variable "redis_endpoint" {
  type        = string
  description = "Valkey endpoint — NOT in the work order's needs list, added after reading the code: the results consume loop calls control_state.mark_runs_active() (an HSET) on every message it receives, unconditionally (not wrapped in try/except), so a missing/wrong REDIS_URL crashes the process, not just degrades a read."
}

variable "artifact_bucket_name" {
  type        = string
  description = "Durable artifacts S3 bucket — llm_calls.jsonl bulk reads (run_llm_calls_writer) and DLQ body persistence (_persist_dlq_body, rule 1) both live here."
}
