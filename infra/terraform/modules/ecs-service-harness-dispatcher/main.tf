/**
 * modules/ecs-service-harness-dispatcher — the H3 task-per-instance dispatcher (ADR-0030/0032)
 *
 * Replaces the long-running harness-worker SERVICE (which could not run per-env
 * images) with a small Fargate dispatcher that reads `harness-jobs` and calls
 * RunTask once per job against the H2 per-env family `eval-dev-harness-<hash>`.
 * It does NOT delete the message — the launched task owns the receipt handle,
 * extends visibility via its heartbeat and deletes on success, so at-least-once
 * delivery is unchanged. The dispatcher is the only piece that RUNS the
 * families (H2's task definitions are inert until this service exists).
 *
 * IAM is the H3-specific surface: the dispatcher must be able to RunTask (on
 * the cluster + the family task definitions) and PassRole (the family's shared
 * task/execution role) — permissions no other service has.
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

# --- IAM ---------------------------------------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dispatcher" {
  name               = "${var.name_prefix}-harness-dispatcher-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags = {
    Name    = "${var.name_prefix}-harness-dispatcher-task"
    purpose = var.purpose
  }
}

data "aws_iam_policy_document" "dispatcher" {
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
  # H3: poll harness-jobs. A LAUNCHED job's message is deleted by the task, not
  # the dispatcher. DeleteMessage is for the one case the dispatcher discards a
  # message itself: a job whose run is already aborted (abort reconciliation,
  # harness_dispatcher._discard_aborted). 2026-09-04: found live — that discard
  # shipped without this grant, the AccessDenied escaped the poll loop and the
  # dispatcher crash-looped on the aborted run's queued restarts.
  statement {
    sid = "SqsReceive"
    actions = [
      "sqs:ReceiveMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes",
      "sqs:DeleteMessage",
    ]
    resources = [var.harness_queue_arn]
  }
  # run-launch §6.2 / D6: after a successful RunTask, _emit_dispatched sends
  # the DISPATCHED ledger notice to results (owns instance_results.dispatched_at
  # / dispatch_count — no other component sets them, §6.3a). 2026-08-29: found
  # live — this grant was never added when that feature was built, so every
  # emit 403'd (QueueDoesNotExist masking AccessDenied, SQS's deliberately
  # ambiguous error for "missing" vs "no permission") and dispatched_at stayed
  # permanently NULL, the exact gap that let the reaper's rule 3 false-positive
  # on a live, real dispatch.
  statement {
    sid       = "SqsSendResults"
    actions   = ["sqs:SendMessage", "sqs:GetQueueUrl"]
    resources = [var.results_queue_arn]
  }
  # §6.6: the autoscaler's observation events (reconciliation_peak / overload /
  # recovery_stabilized) — send-only; the results-writer consumes and INSERTs.
  statement {
    sid       = "SqsSendModelObservations"
    actions   = ["sqs:SendMessage", "sqs:GetQueueUrl"]
    resources = [var.model_observations_queue_arn]
  }
  # H3: launch one Fargate task per job against the H2 families.
  statement {
    sid     = "RunTask"
    actions = ["ecs:RunTask"]
    resources = [
      "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}",
      "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task-definition/${var.name_prefix}-harness-*",
    ]
  }
  # H3: the launched task runs under the family's roles (execution AND task —
  # A1-8 split; RunTask must be able to pass whichever the task definition names).
  statement {
    sid       = "PassRole"
    actions   = ["iam:PassRole"]
    resources = var.family_role_arns
  }
  # Per-instance family selection (builder1-per-instance-images QA1 option (a)):
  # _family_for_job probes describe_task_definition to prefer the registered
  # per-instance family eval-dev-harness-<instance_id> over the env-hash family.
  # Without this the dispatcher's IAM role cannot read the families and the
  # selection fails closed (AccessDenied → DispatchRefusedError → job retries).
  #
  # NOTE: ecs:DescribeTaskDefinition is NOT scopeable by family — AWS evaluates
  # the action against a literal resource of "*" (the API has no ARN for a
  # describe call). A scoped task-definition ARN in resources would NEVER match
  # and the dispatcher would keep failing with "on resource: *". Read-only and
  # harmless vs the RunTask grant above (verified live: the scoped form rendered
  # and still AccessDenied on resource "*").
  statement {
    sid       = "DescribeTaskDefinition"
    actions   = ["ecs:DescribeTaskDefinition"]
    resources = ["*"]
  }
  statement {
    sid       = "CloudWatchLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }
  # A1-7 / ADR-0033 decision 4: the gate reads LIVE state, not a variable file.
  # At startup the dispatcher asks EC2 what routes the configured harness subnets
  # carry and what egress the configured SG has, and REFUSES to dispatch if either
  # has a 0.0.0.0/0. Without ec2:Describe* the assertion couldn't run.
  statement {
    sid       = "Ec2DescribeIsolation"
    actions   = ["ec2:DescribeRouteTables", "ec2:DescribeSecurityGroups", "ec2:DescribeSubnets"]
    resources = ["*"]
  }
  # H4 §5: the dispatcher's admission ceiling is enforced against ListTasks
  # ground truth (the authoritative running count).  Without ecs:ListTasks the
  # count can't be computed and the dispatcher fails closed.
  # Per the 5a review: prefer the tighter form — keep resources = ["*"]
  # and constrain with a condition on the ecs:cluster key. If the condition does
  # not evaluate against live IAM after the owner applies (this is the
  # documented fallback), revert to the bare ["*"] form which is already deployed
  # and working.
  statement {
    sid     = "EcsActionsGroundTruth"
    actions = ["ecs:ListTasks"]
    # ListTasks is a LIST verb scoped by service-name; "*" is acceptable.
    resources = ["*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = ["arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}"]
    }
  }
  # Bring-up 2026-09-03: ListTasks cannot filter by family prefix, so it returns
  # every task in the cluster — the six services included. With the L2 planner
  # live those read as booting harness tasks and gated all launches (ceiling ==
  # in_flight == 8 at an idle bring-up). running_count now DescribeTasks the
  # listed ARNs and keeps only the harness families. DescribeTasks is a
  # task-resource action: scope it by the task ARN, not an ecs:cluster condition
  # (that key never matches for cluster-resource actions — the trap recorded at
  # the 2026-09-02 bring-up).
  statement {
    sid       = "EcsDescribeTasksGroundTruth"
    actions   = ["ecs:DescribeTasks"]
    resources = ["arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task/${var.cluster_name}/*"]
  }
}

resource "aws_iam_role_policy" "dispatcher" {
  name   = "${var.name_prefix}-harness-dispatcher"
  role   = aws_iam_role.dispatcher.id
  policy = data.aws_iam_policy_document.dispatcher.json
}

# --- Task definition + service ----------------------------------------------
resource "aws_ecs_task_definition" "dispatcher" {
  family                   = "${var.name_prefix}-harness-dispatcher"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  cpu                = "256"
  memory             = "512"
  execution_role_arn = aws_iam_role.dispatcher.arn
  task_role_arn      = aws_iam_role.dispatcher.arn

  container_definitions = jsonencode([
    {
      name      = "harness-dispatcher"
      image     = var.image
      command   = ["harness-dispatcher"]
      essential = true
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = var.log_group
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "harness-dispatcher"
        }
      }
      environment = [
        # A1/ADR-0033: the launched HARNESS task goes into the ISOLATED network
        # (dedicated subnets + the harness SG with no 0.0.0.0/0). There is no
        # code path that launches a harness task into the NAT-routed subnets —
        # the posture is structural, not a remembered switch. The dispatcher
        # ITSELF stays on the normal network (var.private_subnet_ids below).
        # ADR-0039: the dispatcher reads/writes the control state (pour/abort flags)
        # in the gateway's Valkey endpoint, so Valkey joins the harness-tier reads.
        { name = "CLUSTER", value = var.cluster_name },
        { name = "HARNESS_SUBNET_IDS", value = jsonencode(var.harness_subnet_ids) },
        { name = "HARNESS_SECURITY_GROUP_IDS", value = jsonencode(var.harness_security_group_ids) },
        { name = "SQS_QUEUE_PREFIX", value = "${var.name_prefix}-" },
        { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name },
        { name = "EVAL_ENV_PREFIX", value = var.name_prefix },
        # ADR-0039: the dispatcher ALSO reads operator control state from the
        # Valkey that lives HERE in this tier; the endpoint is a module variable and
        # renders even when "" (local/dev — where the read fails closed to all-paused).
        { name = "REDIS_URL", value = var.redis_endpoint },
        # H4 §4.1: the admission ceiling is a Terraform variable (ADR-0018
        # "keep capacity a variable", ADR-0030 "don't hardcode").  The dispatcher
        # REFUSES to start without it.  Initial value from the gateway-RPM term.
        { name = "MAX_CONCURRENT_HARNESS_TASKS", value = tostring(var.max_concurrent_harness_tasks) },
        # 2026-08-29 owner decision: a job with no cached per-run LiteLLM key
        # (run predates launch_run(), lost cache entry, or — the live case
        # that surfaced this — a stale SQS redelivery of an already-finalised
        # run) must never silently fall back to LITELLM_MASTER_KEY
        # (routing.gateway_api_key). ENFORCE_PER_RUN_KEY=1 makes
        # _reference_for refuse the launch closed (DispatchRefusedError,
        # caught cleanly in run_harness_dispatcher's loop — message stays
        # queued and eventually DLQs, no crash) instead of launching with a
        # shared, unmetered, unbounded-budget credential. Was left off
        # pending "every launch path confirmed to go through launch_run()"
        # (harness_dispatcher.py's own comment) — now confirmed live.
        { name = "ENFORCE_PER_RUN_KEY", value = "1" },
        # §2.2 of the 2026-09-01 wiring review: the observe->live flip must be a tfvars
        # change + apply, not a code edit under time pressure. Mode default observe;
        # the alias env is the static FALLBACK only — the launched run's overrides hash
        # carries the authoritative alias.
        { name = "AUTOSCALER_MODE", value = var.autoscaler_mode },
        { name = "AUTOSCALER_MODEL_ALIAS", value = var.autoscaler_model_alias },
      ]
    },
  ])

  tags = {
    Name    = "${var.name_prefix}-harness-dispatcher"
    purpose = var.purpose
  }
}

resource "aws_ecs_service" "dispatcher" {
  name            = "harness-dispatcher"
  cluster         = var.cluster_name
  task_definition = aws_ecs_task_definition.dispatcher.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = false
  }

  tags = {
    Name    = "${var.name_prefix}-harness-dispatcher"
    purpose = var.purpose
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}
