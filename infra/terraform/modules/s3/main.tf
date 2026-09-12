/**
 * modules/s3 — dataset cache, transcripts/artifacts, results (arch §6.6)
 *
 * PA-3/PA-4 (persistence): ALL three buckets are durable inputs/outputs of a
 * published result, so this module is instantiated in envs/dev/persistent/ (the
 * state never routinely destroyed) and every bucket is VERSIONED.
 *
 * force_destroy is a PER-BUCKET input, deliberately NOT uniform:
 *   - results, artifacts: force_destroy = FALSE (PA-3). These hold irreplaceable
 *     eval output. A destroy hitting a non-empty bucket FAILS LOUDLY — "bucket
 *     not empty" is a five-second annoyance; a silent wipe is unrecoverable.
 *     This deliberately inverts the earlier F-9/A10 "force_destroy=true" guidance,
 *     which was written when these buckets were ephemeral disposable.
 *   - dataset: force_destroy = TRUE stays correct (PA-3). It is re-downloadable
 *     from HuggingFace at a pinned revision; no silent-loss risk.
 *
 * Versioning (PA-3/PA-4): enables object versioning on all three, so an
 * overwrite/delete is recoverable and a POINT-IN-TIME record survives.
 *
 * Lifecycle: expire stale dev-run S3 prefixes after a short window (N7/ADR-0014)
 * on artifacts/results; dataset is the permanent input and does not expire.
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

variable "region" {
  type        = string
  description = "Region for bucket names."
}

variable "account_id" {
  type        = string
  description = "AWS account ID (for globally-unique bucket names)."
}

# bucket name -> force_destroy. durable buckets MUST be false; dataset true.
variable "bucket_force_destroy" {
  type = map(bool)
  default = {
    "artifacts" = false # durable eval output (PA-3) — destroy must fail loudly
    "results"   = false # durable eval output (PA-3)
    "dataset"   = true  # re-downloadable from HF pinned revision (PA-3)
  }
  description = "Per-bucket force_destroy. Durable buckets are false (PA-3): a destroy failing 'bucket not empty' beats a silent wipe."
}

output "bucket_names" {
  value = { for k, b in aws_s3_bucket.this : k => b.id }
}

resource "aws_s3_bucket" "this" {
  for_each = toset(keys(var.bucket_force_destroy))

  bucket        = "${var.name_prefix}-${each.key}-${var.account_id}-${var.region}"
  force_destroy = var.bucket_force_destroy[each.key]
  tags = {
    Name       = "${var.name_prefix}-${each.key}"
    purpose    = var.purpose
    durability = "persistent"
  }
}

# PA-3/PA-4: versioning on every bucket (recoverable overwrites; POINT-IN-TIME).
resource "aws_s3_bucket_versioning" "this" {
  for_each = aws_s3_bucket.this

  bucket = each.value.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = aws_s3_bucket.this

  bucket = each.value.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# N7 / ADR-0014: expire stale dev-run prefixes on artifacts/results after a
# short window; dataset (the permanent input) has NO expiration.
resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = {
    for k, b in aws_s3_bucket.this : k => b if k != "dataset"
  }

  bucket = each.value.id

  rule {
    id     = "expire-stale-runs"
    status = "Enabled"

    filter {
      prefix = "runs/"
    }

    expiration {
      days = 90
    }
  }
}

output "bucket_ids" {
  value = { for k, b in aws_s3_bucket.this : k => b.id }
}
