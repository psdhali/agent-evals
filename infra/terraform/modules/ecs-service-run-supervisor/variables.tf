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
  description = "Framework image (orchestrator) — command ['run-supervisor'] runs the control-plane singleton (heartbeat + reaper rules 2/3)."
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

variable "secrets" {
  type = object({
    database_url = string
    # 2026-09-08: open_run_recovery (3c7cd3c) re-mints per-run LiteLLM keys at
    # supervisor startup through the gateway admin API — which needs the master
    # key. Without it the task fell back to routing.py's "sk-local" and every
    # recovery failed ("connection refused", both bring-ups of 2026-09-08).
    master = string
  })
  description = "Secret ARNs: Aurora DATABASE_URL, LiteLLM master key."
}

# Same value the orchestrator module receives (LITELLM_BASE_URL); without it
# gateway_base_url() defaults to http://localhost:4000/v1, unreachable from a
# deployed task — the actual cause of open-run recovery never re-minting a key.
variable "gateway_base_url" {
  type        = string
  description = "LiteLLM gateway base URL (LITELLM_BASE_URL) for open-run recovery's key re-mint."
}

variable "queue_urls" {
  type = object({
    harness = string
    results = string
  })
  description = "SQS queue URLs — harness-jobs (queue-depth read for rule 3), results (rule 2/3 reap emission)."
}

variable "queue_arns" {
  type = object({
    harness = string
    results = string
    eval    = string
  })
  description = "SQS queue ARNs — the IAM resources for queue_urls above, plus eval-jobs (depth reads only: the eval autoscaler's control law and the capacity observer; never ReceiveMessage)."
}

# ── Eval autoscaler + capacity observer (§2.3 of the 2026-09-01 wiring review) ──
# Both run as daemon threads INSIDE this service, so their knobs and IAM belong on this
# task definition. The observe->live flip must be a tfvars change + apply, never a code
# edit — which also means the (scoped) live-actuation IAM is granted now, not at flip time.

variable "eval_autoscaler_mode" {
  type        = string
  default     = "observe"
  description = "Eval autoscaler mode (off|observe|live). The flip to live is the owner's decision — never default to live."
  validation {
    condition     = contains(["off", "observe", "live"], var.eval_autoscaler_mode)
    error_message = "eval_autoscaler_mode must be one of: off, observe, live."
  }
}

variable "eval_max_workers" {
  type        = string
  default     = ""
  description = "REQUIRED when live (the code refuses to start live without it — ADR-0006: never inferred). Empty = observe-mode default applies."
}

variable "eval_asg_name" {
  type        = string
  default     = ""
  description = "The eval host ASG's name (persistent's eval_asg_name output). A live scaler with no ASG name cannot scale hosts — the code refuses live with it empty, same rule as eval_max_workers."
}

variable "eval_service_name" {
  type        = string
  default     = "eval-worker"
  description = "The eval ECS service name (task-level actuation + observer reads)."
}

variable "eval_min_workers" {
  type        = string
  default     = "0"
  description = "Floor for desired eval tasks."
}

variable "eval_tasks_per_host" {
  type        = string
  default     = "1"
  description = "Tasks per eval host — the constant most likely to be wrong (re-derive from real peak RSS before live); settable without a code change for exactly that reason."
}

variable "eval_task_scale_in_s" {
  type        = string
  default     = "300"
  description = "F7 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): seconds of consecutive lower ticks before eval TASKS scale in (hosts keep ADR-0006's two-tick rule). 60 s churned idle workers three times in 30 min on the qwen x mini e2e."
}

variable "eval_feedforward_beta" {
  type        = string
  default     = "0.35"
  description = "R1 feed-forward: fraction of live harness instances expected to complete within the host-boot horizon."
}

variable "redis_endpoint" {
  type        = string
  description = "Valkey endpoint (control:flags heartbeat/reconcile, ADR-0039). Empty degrades to all-paused reads elsewhere — this service is the one that PUBLISHES the key."
}
