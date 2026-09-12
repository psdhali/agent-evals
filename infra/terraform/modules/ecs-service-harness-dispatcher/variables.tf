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
  description = "Framework image (orchestrator) — command ['harness-dispatcher'] runs the H3 dispatcher loop."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnets for the DISPATCHER'S OWN Fargate service (it keeps normal egress — it runs our code, not the agent's)."
}

variable "security_group_ids" {
  type        = list(string)
  description = "Task security groups for the DISPATCHER'S OWN service."
}

variable "log_group" {
  type        = string
  description = "CloudWatch log group."
}

variable "harness_queue_url" {
  type        = string
  description = "harness-jobs queue URL the dispatcher polls."
}

variable "harness_queue_arn" {
  type        = string
  description = "harness-jobs queue ARN (IAM resource)."
}

variable "results_queue_arn" {
  type        = string
  description = "results queue ARN (IAM resource) — _emit_dispatched sends the DISPATCHED ledger notice here after a successful RunTask (run-launch §6.2)."
}

variable "model_observations_queue_arn" {
  type        = string
  description = "model-observations queue ARN (IAM resource) — the autoscaler's §6.6 observation events (peaks/overloads/recoveries), send-only from the dispatcher."
}

variable "autoscaler_mode" {
  type        = string
  default     = "observe"
  description = "L2 planner mode (off|observe|live). The observe->live flip is the OWNER'S decision, made after comparing decision records against a real run — never default to live."
  validation {
    condition     = contains(["off", "observe", "live"], var.autoscaler_mode)
    error_message = "autoscaler_mode must be one of: off, observe, live."
  }
}

variable "autoscaler_model_alias" {
  type        = string
  default     = ""
  description = "STATIC fallback alias for the L2 planner's pacer:cfg lookup + §6.6 emission. The live run's launch publishes its own alias via the overrides hash and WINS; empty here is a visible degraded state (budgets.source='defaults' in every record) only until a run launches."
}

variable "redis_endpoint" {
  type        = string
  description = "Valkey endpoint this dispatcher reads control state from (ADR-0039). Empty means \"no Valkey\" — the read fails closed to all-paused, never an unknown."
}

variable "family_role_arns" {
  type        = list(string)
  description = "Harness-family execution + task role ARNs (A1-8 split) — the RunTask PassRole targets."
}

variable "harness_subnet_ids" {
  type        = list(string)
  description = "HARNESS-ISOLATED subnets the launched harness task runs in (ADR-0033). No default route."
}

variable "harness_security_group_ids" {
  type        = list(string)
  description = "HARNESS security groups for the launched task (no 0.0.0.0/0 egress, A1)."
}

variable "max_concurrent_harness_tasks" {
  type        = number
  description = "H4 admission ceiling (ADR-0037) — the max concurrent harness tasks the dispatcher may launch. NO DEFAULT: the dispatcher refuses to start without it. Set from the gateway-RPM term (~50 today), raised on Stage C evidence."
}
