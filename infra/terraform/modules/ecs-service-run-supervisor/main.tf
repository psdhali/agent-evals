/**
 * modules/ecs-service-run-supervisor — the control-plane SINGLETON
 * (CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md /
 * BUILDER6-SPLIT-SUPERVISOR-AND-RESULTS-WRITER-2026-08-31.md).
 *
 * Relocated out of orchestrator-control-plane (formerly bundled with
 * orchestrator-api in modules/ecs-service-orchestrator) because the 90-second
 * Aurora->Valkey heartbeat that everything else's liveness depends on
 * (control/state.py's STALE_AFTER_S — a stale key fails EVERY reader closed
 * to paused) used to share a process with the busiest consumer in the
 * system. This service does almost nothing on purpose: the heartbeat +
 * reaper rules 2 (deadline) and 3 (never-dispatched) — both DB-scan-driven,
 * in-process timers that need exactly ONE view of their own state, hence
 * desired_count fixed at 1 (never surfaced as a variable — a second replica
 * would let two copies of rule 3's "consecutive empty" tracking each reach
 * the threshold independently).
 *
 * results-writer (modules/ecs-service-results-writer) is the sibling that
 * replaces the other half of the retired orchestrator-control-plane.
 */
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "supervisor" {
  name               = "${var.name_prefix}-run-supervisor-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags = {
    Name    = "${var.name_prefix}-run-supervisor-task"
    purpose = var.purpose
  }
}

data "aws_iam_policy_document" "supervisor" {
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
    resources = [var.secrets.database_url, var.secrets.master]
  }
  statement {
    sid       = "CloudWatchLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }
  # _maybe_run_reaper's rule 2/3: send onto the results queue
  # (_emit_reap_result — "every reap is recorded, never silent").
  statement {
    sid       = "SqsSendResults"
    actions   = ["sqs:SendMessage", "sqs:GetQueueUrl"]
    resources = [var.queue_arns.results]
  }
  # Rule 3 (_reap_never_dispatched): queue-depth read on harness-jobs, never
  # ReceiveMessage — the reaper does not consume this queue, only observes it.
  statement {
    sid       = "SqsHarnessDepthRead"
    actions   = ["sqs:GetQueueUrl", "sqs:GetQueueAttributes"]
    resources = [var.queue_arns.harness]
  }
  # Rules 2/3's live-signal check (_running_instance_ids_for_run, ADR-0032):
  # list this run's RUNNING tasks then describe them for the INSTANCE_ID
  # override. DescribeTasks is not resource-scopable (same AWS limitation
  # noted in ecs-service-harness-dispatcher's DescribeTaskDefinition grant).
  statement {
    sid       = "EcsListTasks"
    actions   = ["ecs:ListTasks"]
    resources = ["*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = ["arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}"]
    }
  }
  statement {
    sid       = "EcsDescribeTasks"
    actions   = ["ecs:DescribeTasks"]
    resources = ["*"]
  }
  # ── Eval autoscaler + capacity observer (both daemon threads in THIS process) ──
  # Reads are needed in EVERY mode — without them, observe mode's AWS reads silently
  # degrade to None/hold (the "check that runs, passes, and cannot fail" defect: a
  # blank chart instead of a denied call). eval-jobs depth: attributes only, never
  # ReceiveMessage (this service does not consume it — matching the harness-jobs
  # depth-read discipline above).
  statement {
    sid       = "SqsEvalDepthRead"
    actions   = ["sqs:GetQueueUrl", "sqs:GetQueueAttributes"]
    resources = [var.queue_arns.eval]
  }
  statement {
    sid       = "EcsEvalServiceObserve"
    actions   = ["ecs:DescribeServices"]
    resources = ["arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:service/${var.cluster_name}/${var.eval_service_name}"]
  }
  # ListContainerInstances acts ON the cluster resource itself, and AWS does
  # not populate the ecs:cluster condition key for it (unlike ListTasks above)
  # — an ArnEquals condition on that key can never match, so the statement
  # silently never applies (proven live 2026-09-02: AccessDenied at first
  # boot). Scope by resource instead; equally tight.
  statement {
    sid       = "EcsEvalHosts"
    actions   = ["ecs:ListContainerInstances"]
    resources = ["arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}"]
  }
  statement {
    sid       = "EcsDescribeContainerInstances"
    actions   = ["ecs:DescribeContainerInstances"]
    resources = ["*"]
  }
  statement {
    sid       = "AsgDescribe"
    actions   = ["autoscaling:DescribeAutoScalingGroups", "autoscaling:DescribeLifecycleHooks"]
    resources = ["*"] # Describe* is not resource-scopable
  }
  # Live-mode actuation, scoped to the ONE service and (when named) the ONE ASG —
  # granted now so the observe->live flip is a tfvars change with no IAM edit.
  statement {
    sid       = "EcsEvalServiceActuate"
    actions   = ["ecs:UpdateService"]
    resources = ["arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:service/${var.cluster_name}/${var.eval_service_name}"]
  }
  dynamic "statement" {
    for_each = var.eval_asg_name == "" ? [] : [1]
    content {
      sid = "AsgEvalActuate"
      actions = [
        "autoscaling:SetDesiredCapacity",
        "autoscaling:TerminateInstanceInAutoScalingGroup",
        # finding 17: release drained hosts stuck in the termination hook before scaling up
        "autoscaling:CompleteLifecycleAction",
      ]
      # ASG ARNs embed a UUID between the region/account and the name — wildcard it.
      resources = ["arn:aws:autoscaling:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:autoScalingGroup:*:autoScalingGroupName/${var.eval_asg_name}"]
    }
  }
}

resource "aws_iam_role_policy" "supervisor" {
  name   = "${var.name_prefix}-run-supervisor"
  role   = aws_iam_role.supervisor.id
  policy = data.aws_iam_policy_document.supervisor.json
}

resource "aws_ecs_task_definition" "supervisor" {
  family                   = "${var.name_prefix}-run-supervisor"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  # §5 of the work order: "256/512 is enough (it does almost nothing)."
  cpu                = "256"
  memory             = "512"
  execution_role_arn = aws_iam_role.supervisor.arn
  task_role_arn      = aws_iam_role.supervisor.arn

  container_definitions = jsonencode([{
    name    = "run-supervisor"
    image   = var.image
    command = ["run-supervisor"]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.log_group
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "run-supervisor"
      }
    }
    environment = [
      { name = "REDIS_URL", value = var.redis_endpoint },
      { name = "HARNESS_QUEUE_URL", value = var.queue_urls.harness },
      { name = "RESULTS_QUEUE_URL", value = var.queue_urls.results },
      { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name },
      { name = "EVAL_ENV_PREFIX", value = var.name_prefix },
      { name = "SQS_QUEUE_PREFIX", value = "${var.name_prefix}-" },
      # ADR-0034 M1.6 / rule 2-3's live-ECS-task check: startedBy=run_id
      # DescribeTasks against this cluster.
      { name = "CLUSTER", value = var.cluster_name },
      # §2.3 (wiring review 2026-09-01): the eval autoscaler's and capacity observer's
      # knobs — the observe->live flip is a tfvars change + apply, never a code edit.
      { name = "EVAL_AUTOSCALER_MODE", value = var.eval_autoscaler_mode },
      { name = "EVAL_MAX_WORKERS", value = var.eval_max_workers },
      { name = "EVAL_ASG_NAME", value = var.eval_asg_name },
      { name = "EVAL_SERVICE_NAME", value = var.eval_service_name },
      { name = "EVAL_MIN_WORKERS", value = var.eval_min_workers },
      { name = "EVAL_TASKS_PER_HOST", value = var.eval_tasks_per_host },
      { name = "EVAL_FEEDFORWARD_BETA", value = var.eval_feedforward_beta },
      { name = "EVAL_TASK_SCALE_IN_S", value = var.eval_task_scale_in_s },
      # 2026-09-08: open-run recovery talks to the gateway admin API at startup
      # (re-mint per-run keys after a Valkey/gateway recreate). Unset, the
      # client defaulted to localhost:4000 and every recovery failed.
      { name = "LITELLM_BASE_URL", value = var.gateway_base_url },
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = var.secrets.database_url },
      { name = "LITELLM_MASTER_KEY", valueFrom = var.secrets.master },
    ]
  }])
  tags = {
    Name    = "${var.name_prefix}-run-supervisor"
    purpose = var.purpose
  }
}

resource "aws_ecs_service" "supervisor" {
  name            = "run-supervisor"
  cluster         = var.cluster_name
  task_definition = aws_ecs_task_definition.supervisor.arn
  # SINGLETON, not a variable: two copies would let rule 3's "consecutive
  # empty" tracking (process-local dict) each reach the threshold
  # independently — the whole reason this service exists on its own.
  desired_count = 1
  launch_type   = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = false
  }
  tags = {
    Name    = "${var.name_prefix}-run-supervisor"
    purpose = var.purpose
  }
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

output "service_name" {
  value = aws_ecs_service.supervisor.name
}
