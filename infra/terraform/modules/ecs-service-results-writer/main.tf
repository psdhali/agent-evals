/**
 * modules/ecs-service-results-writer — the results consume loop, N replicas
 * (CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md /
 * BUILDER6-SPLIT-SUPERVISOR-AND-RESULTS-WRITER-2026-08-31.md).
 *
 * The other half of the retired orchestrator-control-plane — the bulk data
 * path (results consume loop, the llm_calls bulk-insert daemon, and reaper
 * rule 1's two DLQ consumer threads). ADR-0018 already made the consume loop
 * safe under competing consumers (idempotent upserts; the eval-job enqueue
 * gates on the state-rank guard advancing, never on a fresh INSERT), so
 * unlike run-supervisor this one scales — desired_count = 2 in dev so that
 * claim is continuously tested against a real contended queue, not just
 * theoretical.
 *
 * run-supervisor (modules/ecs-service-run-supervisor) is the sibling that
 * took the heartbeat + reaper rules 2/3 out of this process.
 */
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "writer" {
  name               = "${var.name_prefix}-results-writer-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags = {
    Name    = "${var.name_prefix}-results-writer-task"
    purpose = var.purpose
  }
}

data "aws_iam_policy_document" "writer" {
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
  statement {
    sid       = "SecretsGet"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.secrets.database_url]
  }
  statement {
    sid       = "CloudWatchLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }
  # The results consume loop: receive+delete on results, send onto eval-jobs
  # (_enqueue_eval_job), receive+delete on llm-calls (run_llm_calls_writer).
  statement {
    sid = "SqsMainQueues"
    actions = [
      "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:SendMessage",
      "sqs:GetQueueAttributes", "sqs:GetQueueUrl",
    ]
    resources = [
      var.queue_arns.results,
      var.queue_arns.eval,
      var.queue_arns.llm_calls,
      # §6.6: the autoscaler-observations daemon (run_model_observations_writer).
      var.queue_arns.model_observations,
    ]
  }
  # Reaper rule 1's two daemon threads (_run_dlq_reaper): consume-only, never
  # send — the DLQ reaper deletes after persisting the body, it does not
  # re-enqueue.
  statement {
    sid       = "SqsDlqConsume"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"]
    resources = [var.queue_dlq_arns.harness, var.queue_dlq_arns.eval]
  }
  # llm_calls.jsonl bulk reads (run_llm_calls_writer) and DLQ body evidence
  # (_persist_dlq_body, rule 1) — both against the artifacts bucket.
  statement {
    sid     = "ArtifactsReadWrite"
    actions = ["s3:GetObject", "s3:ListBucket", "s3:PutObject"]
    resources = [
      "arn:aws:s3:::${var.artifact_bucket_name}",
      "arn:aws:s3:::${var.artifact_bucket_name}/*",
    ]
  }
  # _log_missing_run_id (R4.3): an FK violation on run_id emits a
  # ResultsWriterMissingRunId metric so a bad dispatch is never silent —
  # not in the work order's needs list; found reading _log_missing_run_id.
  statement {
    sid       = "CloudWatchMetricsPut"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "writer" {
  name   = "${var.name_prefix}-results-writer"
  role   = aws_iam_role.writer.id
  policy = data.aws_iam_policy_document.writer.json
}

resource "aws_ecs_task_definition" "writer" {
  family                   = "${var.name_prefix}-results-writer"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  cpu                = "512"
  memory             = "1024"
  execution_role_arn = aws_iam_role.writer.arn
  task_role_arn      = aws_iam_role.writer.arn

  container_definitions = jsonencode([{
    name    = "results-writer"
    image   = var.image
    command = ["results-writer"]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.log_group
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "results-writer"
      }
    }
    environment = [
      # control_state.mark_runs_active() — see variables.tf's redis_endpoint
      # description for why this is not optional.
      { name = "REDIS_URL", value = var.redis_endpoint },
      { name = "HARNESS_QUEUE_URL", value = var.queue_urls.harness },
      { name = "EVAL_QUEUE_URL", value = var.queue_urls.eval },
      { name = "RESULTS_QUEUE_URL", value = var.queue_urls.results },
      { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name },
      { name = "EVAL_ENV_PREFIX", value = var.name_prefix },
      { name = "SQS_QUEUE_PREFIX", value = "${var.name_prefix}-" },
      { name = "ARTIFACTS_BUCKET", value = var.artifact_bucket_name },
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = var.secrets.database_url },
    ]
  }])
  tags = {
    Name    = "${var.name_prefix}-results-writer"
    purpose = var.purpose
  }
}

resource "aws_ecs_service" "writer" {
  name            = "results-writer"
  cluster         = var.cluster_name
  task_definition = aws_ecs_task_definition.writer.arn
  desired_count   = var.replicas
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = false
  }
  tags = {
    Name    = "${var.name_prefix}-results-writer"
    purpose = var.purpose
  }
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

output "service_name" {
  value = aws_ecs_service.writer.name
}

# Adoption Phase 1a: the region comes from the provider, never a literal.
data "aws_region" "current" {}
