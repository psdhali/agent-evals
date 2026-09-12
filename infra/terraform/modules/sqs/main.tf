/**
 * modules/sqs — harness-jobs, eval-jobs, results queues + DLQs (arch §5.1)
 *
 * Visibility timeouts are the calibration standing-start numbers from
 * calibration-note §2 / ADR-0015: harness ~235s, eval ~300s, results 30s.
 * These are standing-start, not finished — re-derive against queue-through runs
 * (the SQS base timeouts are NOT blue-ticket; calibration-note §2 caveat).
 *
 * IAM note: `sqs:ChangeMessageVisibility` is the easy-to-forget permission —
 * not needed locally against ElasticMQ (Phase 3), mandatory on real SQS for
 * ADR-0015's heartbeat/visibility-extension. Worker task roles (worker modules)
 * get a scoped statement for their own in-flight message from the queue ARNs.
 */
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "name_prefix" {
  type        = string
  description = "Resource name prefix."
}

variable "purpose" {
  type        = string
  description = "'dev' or 'scale'."
}

# (name, base_visibility_seconds) — standing start per calibration-note §2.
# ADR-0037 / M0 §3: `llm-calls` is a SEPARATE queue from `results`, never the
# same one — M2 uses results depth as a scaling/UI signal, and ~40 call-rows per
# result-row would ruin it.  It also carries its own failure policy: a lost
# result row is a lost outcome; a lost call row is a lost data point (M0 §3.2).
locals {
  queues = {
    "harness-jobs" = { visibility = 235 }
    "eval-jobs"    = { visibility = 300 }
    "results"      = { visibility = 30 }
    "llm-calls"    = { visibility = 60 }
    # §6.6 (BUILDER4-AUTOSCALER-TPM-CEILING-DISCOVERY-DESIGN-2026-08-31.md): the live
    # dispatcher's autoscaler observations (reconciliation_peak / overload /
    # recovery_stabilized) — the dispatcher never writes Aurora from its hot path; the
    # results-writer drains this and only ever INSERTs into model_tpm_observations.
    # Separate from results for the same reason llm-calls is: results depth is a
    # scaling/UI signal and must not be polluted.
    "model-observations" = { visibility = 60 }
  }
}

resource "aws_sqs_queue" "this" {
  for_each = local.queues

  name                       = "${var.name_prefix}-${each.key}"
  visibility_timeout_seconds = each.value.visibility
  receive_wait_time_seconds  = 20 # long-poll (cheap, reduces empty receives)
  # ADR-0034 A-9: 14-day retention, not the AWS 4-day default.  Pause is meant
  # to hold a backlog for an arbitrarily long operator investigation; 4 days
  # would silently drop that backlog mid-pause, violating pause's no-data-loss
  # property.  The queues live in persistent/, so nightly eval-down does not
  # destroy a paused run's backlog.
  message_retention_seconds = 1209600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = 5
  })

  tags = {
    Name    = "${var.name_prefix}-${each.key}"
    purpose = var.purpose
  }
}

resource "aws_sqs_queue" "dlq" {
  for_each = local.queues

  name = "${var.name_prefix}-${each.key}-dlq"

  tags = {
    Name    = "${var.name_prefix}-${each.key}-dlq"
    purpose = var.purpose
  }
}

output "queue_urls" {
  value = { for k, q in aws_sqs_queue.this : k => q.id }
}
output "queue_arns" {
  value = { for k, q in aws_sqs_queue.this : k => q.arn }
}
output "dlq_arns" {
  value = { for k, q in aws_sqs_queue.dlq : k => q.arn }
}
