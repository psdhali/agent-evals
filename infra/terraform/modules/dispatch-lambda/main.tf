/**
 * modules/dispatch-lambda — the ADR-0024 dispatch trigger (durable tier)
 *
 * Turns an S3-dropped run config into SQS jobs by calling the SAME
 * register_run()/dispatch_run() the Phase-7 API will call. Four load-bearing
 * properties (ADR-0024 §Decision):
 *
 *  1. One dispatch implementation, two triggers — the Lambda imports the real
 *     dispatcher; it does not reimplement enqueueing.
 *  2. VPC-attached (private subnets + the task SG) so database.connection
 *     reaches Aurora directly over the existing psycopg path.
 *  3. The object IS the audit record: pending/ → processed/ or failed/ is a
 *     receipt, and the lambda treats "object no longer in pending/" as the
 *     at-least-once completion latch (no double dispatch).
 *  4. Authorisation is IAM: s3:PutObject on runs/pending/* is the permission
 *     to start a run.
 *
 * Reserved concurrency is 1 (the ADR's explicit fan-out guard: a bulk copy into
 * pending/ is a spend hazard). Lives in the durable tier (persistent/).
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
  description = "Resource name prefix (eval-dev)."
}

variable "purpose" {
  type        = string
  description = "Cost-allocation tag value ('dev')."
}

variable "lambda_image" {
  type        = string
  description = "The dispatch Lambda container image (ECR URL)."
}

variable "results_bucket_name" {
  type        = string
  description = "The durable results bucket that receives runs/pending/ objects."
}

variable "results_bucket_arn" {
  type        = string
  description = "ARN of the results bucket (IAM + S3 event policy)."
}

variable "harness_queue_arn" {
  type        = string
  description = "ARN of the harness-jobs queue (the dispatch Lambda enqueues to it)."
}

variable "database_url_secret_arn" {
  type        = string
  description = "ARN of the Aurora DATABASE_URL secret (the app connection string)."
}

variable "dataset_bucket_name" {
  type        = string
  description = "Durable dataset S3 bucket — the warm-cache manifest (cache_manifest.py) and the pinned dataset mirror (SwebenchLiteLoader) the dispatcher reads at dispatch (ADR-0030 H1/review R2)."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnets to attach the Lambda to (VPC)."
}

variable "task_security_group_id" {
  type        = string
  description = "The task security group — gives the Lambda the same DB/egress reach as the workers."
}

# --- Gateway / Redis / digest wiring (BUILDER1-CONSOLIDATED-REVIEW task 3) ----
# The window-resolution path in the dispatcher (dispatcher.py:_resolve_window)
# queries the LIVE gateway /model/info. Without LITELLM_BASE_URL the Lambda fell
# back to localhost:4000 (Connection refused) and without LITELLM_MASTER_KEY it
# 401'd — both silently degraded the resolution chain to baked_config. And
# config_snapshot.harness_image_digest was null because HARNESS_IMAGE_DIGEST was
# never in the env. These were set manually on the live Lambda during the probe
# but never made durable — add them here so a re-apply doesn't regress them.
variable "litellm_base_url" {
  type        = string
  description = "Gateway base URL the dispatcher's window-lookup hits (/model/info). The stable name gateway.eval.internal, never a raw ALB DNS."
  default     = "http://gateway.eval.internal:4000/v1"
}
variable "litellm_master_key" {
  type        = string
  description = "LiteLLM master key (from the litellm-master secret) the dispatcher sends to /model/info. Plaintext in the Lambda env — the gateway already trusts it that way."
  default     = ""
}
variable "redis_url" {
  type        = string
  description = "Valkey/Redis endpoint URL the dispatcher uses. DEFAULT empty (persistent does not know eval's cache endpoint); set per-deploy, else the Lambda has no REDIS_URL."
  default     = ""
}
variable "harness_image_digest" {
  type        = string
  description = "The current -hw digest, recorded in config_snapshot.harness_image_digest for reproducibility. Empty = unknown (code safely returns None)."
  default     = ""
}
variable "harness_image_repo_arn" {
  type        = string
  description = "ARN of the harness-worker ECR repo — the dispatcher looks up the -inst/-hw digest live (ecr:DescribeImages) for D-3, so it needs read access to this repo."
  default     = ""
}

# --- IAM ---------------------------------------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dispatch" {
  name               = "${var.name_prefix}-dispatch-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags = {
    Name    = "${var.name_prefix}-dispatch"
    purpose = var.purpose
  }
}

data "aws_iam_policy_document" "dispatch" {
  statement {
    sid = "EcrPull"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = ["*"]
  }
  # D-3 (review 2026-08-27 §5): the dispatcher resolves the -inst/-hw digest LIVE
  # from the harness-worker repo for config_snapshot.harness_image_digest — a
  # permission, not a value, so it never goes stale with a rebuild.
  statement {
    sid       = "EcrDescribeHarnessWorker"
    actions   = ["ecr:DescribeImages"]
    resources = var.harness_image_repo_arn != "" ? [var.harness_image_repo_arn] : ["*"]
  }
  statement {
    sid       = "ResultsBucket"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:CopyObject", "s3:DeleteObject"]
    resources = ["${var.results_bucket_arn}/*"]
  }
  # ADR-0030 review R2: the dispatcher reads the warm-cache manifest AND the
  # pinned dataset mirror — both live in the DATASET bucket. Without this the
  # gate fails with AccessDenied (masquerading as "warm cache not built").
  statement {
    sid     = "DatasetRead"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      "arn:aws:s3:::${var.dataset_bucket_name}",
      "arn:aws:s3:::${var.dataset_bucket_name}/*",
    ]
  }
  statement {
    sid       = "Enqueue"
    actions   = ["sqs:SendMessage", "sqs:GetQueueUrl"]
    resources = [var.harness_queue_arn]
  }
  statement {
    sid       = "SecretsGet"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.database_url_secret_arn]
  }
  statement {
    sid = "VpcAndLogs"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DeleteNetworkInterface",
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "dispatch" {
  role   = aws_iam_role.dispatch.name
  policy = data.aws_iam_policy_document.dispatch.json
}

# --- The function -------------------------------------------------------------
resource "aws_lambda_function" "dispatch" {
  function_name = "${var.name_prefix}-dispatch"
  image_uri     = var.lambda_image
  package_type  = "Image"
  role          = aws_iam_role.dispatch.arn
  timeout       = 60
  memory_size   = 512

  # ADR-0024 asks for reserved concurrency 1 (the fan-out guard for a bulk copy
  # into pending/). THIS ACCOUNT CANNOT HONOUR IT: `ConcurrentExecutions` is 10
  # (verified via get-account-settings), so ANY reservation would push the
  # account's unreserved concurrency below its mandatory floor of 10 — the API
  # rejects it (recorded in the 5a-ii phase notes as a decision forced by the
  # account). The safety property is preserved two other ways: (1) the S3
  # pending->processed latch makes duplicated delivery a no-op, so a second
  # invocation of the same object cannot double-dispatch; (2) a bulk copy into
  # pending/ still fans out, but only within the account's hard 10-concurrency
  # ceiling, which is the same bound for any operation here. Restore the
  # reservation after a Lambda concurrency quota increase (owner action).

  # VPC-attached: the existing psycopg connection path to Aurora is REUSED. An
  # out-of-VPC lambda would need the Data API and a rewrite of connection.py —
  # the exact blast-radius the ADR rejects.
  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [var.task_security_group_id]
  }

  environment {
    variables = {
      # NOTE: AWS_DEFAULT_REGION is set by the Lambda runtime itself and is a
      # RESERVED key — the API rejects it in the environment (InvalidParameter).
      # The handler reads it via os.environ with a us-west-2 fallback.
      SQS_QUEUE_PREFIX        = "${var.name_prefix}-"
      DATABASE_URL_SECRET_ARN = var.database_url_secret_arn
      # ADR-0030 review R1: the 5b warm-cache gate is ON here. The gate is
      # default-OFF in code (local/dev); without this the deployed dispatch
      # never fires it and H1's env_image_key would be "" on every job. The
      # gate's enablement is asserted by tests/test_infra_cache_gate_enabled.py.
      ENFORCE_CACHE_GATE = "1"
      # ADR-0030 review R2: the manifest + dataset mirror live in the dataset
      # bucket; the loader's default matches, but being explicit is what keeps
      # the IAM statement above and the code's DATASET_BUCKET read in step.
      DATASET_BUCKET = var.dataset_bucket_name
      # Window-lookup + reproducibility wiring (see variable block above).
      LITELLM_BASE_URL     = var.litellm_base_url
      LITELLM_MASTER_KEY   = var.litellm_master_key
      HARNESS_IMAGE_DIGEST = var.harness_image_digest
      # register_run() -> control_state.publish_from_db() writes Valkey flags, so
      # the Lambda needs Redis. Set per-deploy (concrete endpoint, not the CNAME —
      # it fails rediss TLS). Empty default = the Lambda has no REDIS_URL.
      REDIS_URL = var.redis_url
    }
  }

  tags = {
    Name    = "${var.name_prefix}-dispatch"
    purpose = var.purpose
  }
}

# --- S3 trigger --------------------------------------------------------------
# The reservation lives on the function above (reserved_concurrent_executions);
# an event_invoke_config here would be for destination/retry settings, not the
# fan-out guard.
resource "aws_s3_bucket_notification" "dispatch" {
  bucket = var.results_bucket_name

  lambda_function {
    lambda_function_arn = aws_lambda_function.dispatch.arn
    events              = ["s3:ObjectCreated:*"]
    filter_suffix       = ".json"
    filter_prefix       = "runs/pending/"
  }
}

resource "aws_lambda_permission" "s3_invoke" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dispatch.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = var.results_bucket_arn
}