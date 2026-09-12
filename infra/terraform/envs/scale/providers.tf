# envs/scale — same modules as dev, capacity turned up (ADR-0018).
#
# PLAN ONLY — UNAPPLIABLE BY MECHANISM (M-3, N-1, P-1).
#
# The profile currently defaults to `eval-framework-ro`, the READ-ONLY role
# (OPS-6, created 2026-08-12: arn:aws:iam::<account-id>:role/eval-framework-scale-ro,
# AWS managed ReadOnlyAccess). Under it, `plan` renders and `apply` FAILS CLOSED
# with AccessDenied — the DoD-3 artifact. Do NOT run scale/ under the admin
# `eval-framework` profile.
#
# Intended missing-profile behaviour (note it, don't treat it as a bug): a
# machine without `eval-framework-ro` in ~/.aws/config fails `plan` loudly with
# "failed to get shared config profile" — that failure is the correct trade over
# silently planning AND applying the 20k-shaped env under AdministratorAccess.
# Override with `-var aws_profile=...` only intentionally.
variable "region" {
  type        = string
  default     = "us-west-2"
  description = "AWS region (adoption Phase 1a)."
}

variable "aws_profile" {
  type        = string
  default     = "eval-framework-ro"
  description = "AWS profile for envs/scale. Defaults to the read-only eval-framework-ro role (OPS-6) so apply fails closed; override with -var only deliberately. A missing profile fails plan loudly — that is intended."
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags {
    tags = {
      App       = "swe-bench-eval-framework"
      Env       = "scale"
      purpose   = "scale"
      ManagedBy = "terraform"
    }
  }
}

provider "aws" {
  alias   = "us_east_1"
  region  = "us-east-1"
  profile = var.aws_profile
  default_tags {
    tags = {
      App       = "swe-bench-eval-framework"
      Env       = "scale"
      purpose   = "scale"
      ManagedBy = "terraform"
    }
  }
}
