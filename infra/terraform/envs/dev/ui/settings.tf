/**
 * Adoption Phase 1a — the deployment's identity, in ONE place per root.
 *
 * Region, credential profile, name prefix and the state bucket used to be
 * literals in every providers.tf / backend.tf / main.tf; a second region, a
 * second environment in one account, or a fresh account meant a tree-wide
 * edit.  They are variables now, with the original values as defaults so an
 * existing deployment plans to zero changes with no tfvars beyond
 * `tfstate_bucket` (written by `make bootstrap` into terraform.tfvars).
 *
 * The same values reach the containers as environment (AWS_DEFAULT_REGION,
 * EVAL_ENV_PREFIX — see swebench_eval/aws_names.py), so code and infra agree by
 * construction.  Identical in every envs/dev/* root on purpose (roots cannot
 * share files); keep them in sync.
 */

variable "region" {
  type        = string
  default     = "us-west-2"
  description = "AWS region for every resource in this deployment."
}

variable "aws_profile" {
  type        = string
  default     = "eval-framework"
  description = "AWS CLI profile the provider and the remote-state reads use. Set to \"\" to use the ambient credential chain (environment variables, an exported SSO session, an instance role)."
}

variable "name_prefix" {
  type        = string
  default     = "eval-dev"
  description = "Prefix of every resource name (cluster, queues, secrets, log groups, ECR repos, task families). Lower-case letters, digits and dashes."
}

variable "purpose" {
  type        = string
  default     = "dev"
  description = "Cost-allocation tag value (the `purpose` and `Env` tags)."
}

variable "tfstate_bucket" {
  type        = string
  description = "The Terraform state bucket every root shares — the same value as backend.hcl. Written by `make bootstrap`."
}

# Adoption Phase 2 finding (fresh account, 2026-09-11): terraform_remote_state ERRORS when the
# other tier's state OBJECT does not exist yet ("Unable to find remote state") — try() only
# guards missing OUTPUTS. On the originating account every state object existed, so the
# cross-tier reads always worked; on a fresh account the first persistent/ui applies run before
# eval/ has any state. The reads are therefore switchable; `make up` / `make up-persistent`
# set them from whether the state object exists, so nobody has to think about it.
variable "read_eval_state" {
  type        = bool
  default     = true
  description = "Read envs/dev/eval's remote state (its Valkey endpoint / gateway URL). false until eval/ has been applied at least once in this account."
}

variable "read_ui_state" {
  type        = bool
  default     = true
  description = "Read envs/dev/ui's remote state (its API ALB). false until ui/ has been applied at least once in this account."
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  aws_profile = var.aws_profile != "" ? var.aws_profile : null
  account_id  = data.aws_caller_identity.current.account_id
  # The first three zones of the region, by name — in us-west-2 that is a, b, c,
  # exactly the list the live VPC was built with.
  azs = slice(sort(data.aws_availability_zones.available.names), 0, 3)
  # Shared config for every terraform_remote_state read (the key is added per read).
  remote_state = merge(
    { bucket = var.tfstate_bucket, region = var.region, encrypt = true },
    var.aws_profile != "" ? { profile = var.aws_profile } : {},
  )
}
