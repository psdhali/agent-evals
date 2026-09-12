/**
 * modules/ecs-service-orchestrator — orchestrator-api (arch §5.4)
 *
 * Stateless FastAPI, N replicas behind the ALB, mints run_id (ULID) at
 * POST /runs.
 *
 * CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md: this module used to also
 * bundle orchestrator-control-plane (the results_writer singleton) — that
 * service is RETIRED and replaced by modules/ecs-service-run-supervisor
 * (the heartbeat + reaper rules 2/3, still a singleton) and
 * modules/ecs-service-results-writer (the results consume loop + reaper
 * rule 1, now N=2 replicas — ADR-0018 already made that seam safe).
 *
 * Needs Aurora connection-retry / pool-recycle behaviour because
 * min_capacity=0 auto-pause drops open connections after 300s idle (ADR-0022 /
 * review C-1 caveat). That behaviour lives in the APPLICATION code (Phase 4
 * package), not Terraform — but this module is where the DATABASE_URL is
 * injected so the retry loop has the right target.
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

variable "cluster_name" {
  type        = string
  description = "ECS cluster."
}

variable "image" {
  type        = string
  description = "Orchestrator image (ECR)."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnets."
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "Public subnets for the API ALB."
}

variable "security_group_ids" {
  type = object({
    alb  = list(string)
    task = list(string)
  })
  description = "Security groups."
}

variable "vpc_id" {
  type        = string
  description = "VPC id."
}

variable "api_replicas" {
  type        = number
  default     = 1
  description = "orchestrator-api replicas (dev: 1; scale: N)."
}

variable "log_group_api" {
  type        = string
  description = "CloudWatch log group for the API."
}

variable "secrets" {
  type = object({
    master       = string
    database_url = string
    # 2026-09-02 live-LLM-calls view: the API reads LiteLLM's spend-log rows
    # (litellm_spend DB) for GET /runs/{id}/llm-live — read-only SELECTs.
    litellm_spend_database_url = string
  })
  description = "Secret ARNs: LiteLLM master, Aurora DATABASE_URL, litellm_spend DATABASE_URL."
}

variable "queue_urls" {
  type = object({
    harness = string
    eval    = string
    results = string
  })
  description = "SQS queue URLs."
}
variable "queue_arns" {
  type = object({
    harness   = string
    eval      = string
    results   = string
    llm_calls = string # ADR-0037 M0 §3: results writer RECEIVEs llm_calls.jsonl pointers
  })
  description = "SQS queue ARNs — used in the IAM policy (URLs are not IAM resources; the first ui apply failed with MalformedPolicyDocument 'arn://sqs...')."
}
variable "queue_dlq_arns" {
  type = object({
    harness   = string
    eval      = string
    results   = string
    llm_calls = string # the queue panel's get_dlq_depth resolves <queue>-dlq (ADR-0037 M2.6)
  })
  description = "SQS DLQ ARNs — the operator queue panel reads DLQ depths (get_dlq_depth -> <queue>-dlq)."
}

variable "redis_endpoint" {
  type        = string
  description = "Valkey endpoint (instance_progress)."
}

variable "dataset_bucket_name" {
  type        = string
  description = "Durable dataset S3 bucket — the warm-cache manifest and the pinned dataset mirror the dispatcher reads at dispatch (ADR-0030 H1/review R2)."
}

# ADR-0037 M0 §3: the results writer (inside the control-plane) fetches
# llm_calls.jsonl objects from the artifact bucket the harness worker wrote.
variable "artifact_bucket_name" {
  type        = string
  description = "Durable artifacts S3 bucket — the llm_calls.jsonl objects the results writer bulk-reads (ADR-0037 M0 §3)."
}

# run-launch (BUILDER4-RUN-LAUNCH-ORCHESTRATOR-2026-08-26 §3.1): the ADR-0024
# S3-dispatch trigger's "results" bucket — where POST /runs also drops
# runs/processed/<run_id>.json, "the two triggers must produce the same
# artifact."  Not previously wired to the orchestrator (only the dispatch
# Lambda module had it, via results_bucket_name/results_bucket_arn).
variable "results_bucket_name" {
  type        = string
  description = "Durable results S3 bucket (ADR-0024) — where runs/pending|processed|failed/ live."
}
variable "results_bucket_arn" {
  type        = string
  description = "ARN of the results bucket, for the IAM policy."
}

# run-launch §5.2: the orchestrator mints/rotates gateway keys at PROVISION —
# it needs the gateway's own base URL, same as the harness-worker module
# already carries (ecs-service-harness-worker/main.tf's gateway_base_url).
# Not previously wired here; without it gateway_base_url() defaults to
# http://localhost:4000/v1, unreachable from a deployed task.
variable "gateway_base_url" {
  type        = string
  description = "LiteLLM gateway ALB base URL (LITELLM_BASE_URL) — same value the harness-worker module receives."
}

# run-launch §5.2 point 3 / D5: the out-of-band OpenRouter provisioning key.
# Created 2026-08-19, NOT in Terraform at all (grepped) — reference it with a
# data source, never a resource, exactly like the Docker Hub PAT precedent
# (execution-plan.md: "Terraform must reference it with a data source, never
# create it"). Parametrized on name_prefix rather than hardcoding
# "eval-dev-openrouter-management" so the module stays reusable across envs.
data "aws_secretsmanager_secret" "openrouter_management" {
  name = "${var.name_prefix}-openrouter-management"
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

data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

resource "aws_iam_role" "orch" {
  name               = "${var.name_prefix}-orchestrator-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags = {
    Name    = "${var.name_prefix}-orchestrator-task"
    purpose = var.purpose
  }
}

data "aws_iam_policy_document" "orch" {
  statement {
    sid = "EcrPull"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      # BUILDER4-LAUNCHABLE-FALSE-ALL-ISSUE-2026-08-28: _launchable_instance_ids()
      # (run_launch_routes.py) calls ecr.describe_images to list built -inst
      # tags for GET /dataset/instances. This statement was written for image
      # PULLING only (copied from the harness family's grant) — nobody added
      # the read/list verb when the launchable check was built on top of it,
      # so every describe_images call 403'd and the fail-open path reported
      # zero launchable instances for all 500 rows, even the ones with a real
      # built image.
      "ecr:DescribeImages",
    ]
    resources = ["*"]
  }
  statement {
    sid     = "SecretsGet"
    actions = ["secretsmanager:GetSecretValue"]
    # run-launch D5: the OpenRouter provisioning key is granted to the
    # ORCHESTRATOR task role ONLY — it must never reach the harness family
    # role, which runs untrusted agent code (ecs-task-def-harness-family's
    # SecretsGet statement carries only var.secrets.master and is untouched
    # by this change).
    resources = [
      var.secrets.master,
      var.secrets.database_url,
      var.secrets.litellm_spend_database_url,
      data.aws_secretsmanager_secret.openrouter_management.arn,
    ]
  }
  # ADR-0030 review R2: the control plane hosts the dispatcher, which reads the
  # warm-cache manifest + the pinned dataset mirror from the DATASET bucket.
  statement {
    sid     = "DatasetRead"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      "arn:aws:s3:::${var.dataset_bucket_name}",
      "arn:aws:s3:::${var.dataset_bucket_name}/*",
    ]
  }
  statement {
    sid = "SqsScoped"
    actions = [
      "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:SendMessage",
      "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility",
      # 5a-ii: the client resolves every queue by GetQueueUrl (queue/client.py
      # get_queue_url). Without the action AWS returns NonExistentQueue even
      # though the queue exists — the 'or you do not have access' half of the
      # error. Never visible against local ElasticMQ (no IAM).
      "sqs:GetQueueUrl",
    ]
    resources = [var.queue_arns.harness, var.queue_arns.eval, var.queue_arns.results, var.queue_arns.llm_calls]
  }
  # ADR-0037 M2.6/ADR-0034 §3: the queue panel reads DLQ depths — get_dlq_depth
  # resolves <queue>-dlq and reads its ApproximateNumberOfMessages. The DLQ ARNs
  # are the dead-letter companions to the main queues; without them the panel
  # 500s on AccessDenied instead of showing a depth.
  statement {
    sid     = "SqsDlqRead"
    actions = ["sqs:GetQueueUrl", "sqs:GetQueueAttributes", "sqs:ReceiveMessage"]
    resources = [
      var.queue_dlq_arns.harness,
      var.queue_dlq_arns.eval,
      var.queue_dlq_arns.results,
      var.queue_dlq_arns.llm_calls,
    ]
  }
  # ADR-0037 M0 §3: the results writer reads llm_calls.jsonl back from the
  # artifact bucket (never touches results in a shared transaction).
  # ADR-0034 M1.7: the abort drain manifest is WRITTEN here too — the results
  # writer and the abort drain both need PutObject on the artifact bucket.
  statement {
    sid     = "ArtifactsRead"
    actions = ["s3:GetObject", "s3:ListBucket", "s3:PutObject"]
    resources = [
      "arn:aws:s3:::${var.artifact_bucket_name}",
      "arn:aws:s3:::${var.artifact_bucket_name}/*",
    ]
  }
  # run-launch §3.1: POST /runs writes runs/processed/<run_id>.json to the
  # results bucket, the same one the ADR-0024 S3-dispatch Lambda triggers on
  # (dispatch-lambda/main.tf's results_bucket_name/arn) — "the two triggers
  # must produce the same artifact."
  statement {
    sid       = "ResultsBucketWrite"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${var.results_bucket_arn}/*"]
  }
  statement {
    sid       = "CloudWatchLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }
  # ADR-0034 M1.6: abort lists this run's tasks (startedBy=run_id) and stops
  # them. ecs:ListTasks is a LIST verb (scoped by serviceName) and StopTask
  # targets the task ARN — both constrained to this cluster via ecs:cluster.
  statement {
    sid       = "EcsAbortListStop"
    actions   = ["ecs:ListTasks", "ecs:StopTask"]
    resources = ["*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = ["arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}"]
    }
  }
  # 2026-09-05: the launch screen's `launchable` flag requires the per-instance
  # task family as well as the -inst image (28 matplotlib jobs crash-looped on
  # the -hw fallback when only the image existed). ListTaskDefinitionFamilies
  # is a LIST verb with no resource or ecs:cluster scoping — "*" with no
  # condition (see the ecs:cluster condition-key trap note in memory).
  statement {
    sid       = "EcsListTaskFamilies"
    actions   = ["ecs:ListTaskDefinitionFamilies"]
    resources = ["*"]
  }
  # ADR-0037 M2.6: the queue panel's oldest_age_s reads CloudWatch metrics.
  # Metric reads are not resource-scopable — resources must be "*".
  statement {
    sid       = "CloudWatchMetricsRead"
    actions   = ["cloudwatch:GetMetricStatistics"]
    resources = ["*"]
  }
  # Phase-7 gateway pause (builder 3, parked): the orchestrator rewrites the
  # gateway listener rule to pause/resume routing. Deliberately granted now so
  # the pause needs no second apply cycle; inert until the caller lands.
  statement {
    sid       = "ElbGatewayPause"
    actions   = ["elasticloadbalancing:ModifyRule", "elasticloadbalancing:DescribeRules"]
    resources = ["*"]
  }
  # Ceiling discovery (exact-design §6, 2026-09-01): POST /model-ceilings/{alias}/discover
  # launches the one-shot discovery task via RunTask — a bursty, ~150-connection, real-money
  # probe that must NOT run inside the API process (owner's decision). Scoped to the discovery
  # family in this cluster; PassRole only for the orchestrator's own role (the task reuses it).
  statement {
    sid       = "EcsRunDiscoveryTask"
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task-definition/${var.name_prefix}-ceiling-discovery:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = ["arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}"]
    }
  }
  # offline-analysis-design.md §9.5/§10.4: POST /runs/{id}/judge launches the one-shot
  # llm-judge task via RunTask — same shape as ceiling-discovery (§6), same reason: a
  # real-money analysis pass must not share the API process or the harness run's control
  # plane. Scoped to the judge task family in this cluster; PassRole is the SAME
  # PassOwnRoleToDiscovery statement below (unscoped by family — any RunTask using this
  # role needs it, not a per-family grant).
  statement {
    sid       = "EcsRunJudgeTask"
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task-definition/${var.name_prefix}-llm-judge:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = ["arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}"]
    }
  }
  statement {
    sid       = "PassOwnRoleToDiscovery"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.orch.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "orch" {
  name   = "${var.name_prefix}-orchestrator"
  role   = aws_iam_role.orch.id
  policy = data.aws_iam_policy_document.orch.json
}

resource "aws_alb" "api" {
  name                       = "${var.name_prefix}-api-alb"
  internal                   = true # orchestrator-api is internal (UI-only, behind auth in Phase 7)
  load_balancer_type         = "application"
  subnets                    = var.public_subnet_ids
  security_groups            = var.security_group_ids.alb
  enable_deletion_protection = false # F-9
  # 2026-09-06 (first 500-run): POST /runs with 500 ids and POST /images/validate
  # with 500 ids both take longer than the 60 s default, so the UI saw
  # "Gateway Time-out" while the API finished the launch behind it (the run was
  # created and dispatched — an operator who relaunched would have doubled the
  # spend). 600 s matches the gateway ALB (17656ac); the API's own work is
  # bounded by the dispatcher, not by this timer.
  idle_timeout = 600

  tags = {
    Name    = "${var.name_prefix}-api-alb"
    purpose = var.purpose
  }
}

resource "aws_alb_target_group" "api" {
  name        = "${var.name_prefix}-api-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"
  health_check { path = "/health" }
}

resource "aws_alb_listener" "api" {
  load_balancer_arn = aws_alb.api.arn
  port              = 8000
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_alb_target_group.api.arn
  }
}

# --- orchestrator-api ---------------------------------------------------------
resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name_prefix}-orchestrator-api"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  cpu                = "256"
  memory             = "512"
  execution_role_arn = aws_iam_role.orch.arn
  task_role_arn      = aws_iam_role.orch.arn

  container_definitions = jsonencode([{
    name         = "orchestrator-api"
    image        = var.image
    command      = ["orchestrator-api"]
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.log_group_api
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "api"
      }
    }
    environment = [
      { name = "REDIS_URL", value = var.redis_endpoint },
      { name = "HARNESS_QUEUE_URL", value = var.queue_urls.harness },
      # 5a-ii transport seam (queue/client.py): deployed containers must point
      # at real SQS with the eval-dev- prefix and the right region.
      { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name },
      { name = "EVAL_ENV_PREFIX", value = var.name_prefix },
      { name = "SQS_QUEUE_PREFIX", value = "${var.name_prefix}-" },
      # ADR-0034 M1.6: abort enumerates this run's tasks by startedBy=run_id
      # against this cluster — it needs the cluster name at runtime.
      { name = "CLUSTER", value = var.cluster_name },
      # ADR-0030 review R1: the 5b warm-cache gate is ON for every dispatch
      # path (the future POST /runs lives here too). Enablement is asserted by
      # tests/test_infra_cache_gate_enabled.py.
      { name = "ENFORCE_CACHE_GATE", value = "1" },
      # ADR-0030 review R2: where the manifest + dataset mirror live (kept
      # explicit alongside the DatasetRead IAM statement).
      { name = "DATASET_BUCKET", value = var.dataset_bucket_name },
      # ADR-0037 M0 §3: results writer reads llm_calls.jsonl from this bucket.
      { name = "ARTIFACTS_BUCKET", value = var.artifact_bucket_name },
      # run-launch §3.1: runs/processed/<run_id>.json (POST /runs' copy of
      # the ADR-0024 artifact).
      { name = "RESULTS_BUCKET", value = var.results_bucket_name },
      # run-launch §5.2: /key/generate, /key/delete, /model/new, /model/update
      # — the orchestrator's own admin calls to the gateway.
      { name = "LITELLM_BASE_URL", value = var.gateway_base_url },
      # run-launch D5: non-secret — the ARN, not the key. The orchestrator
      # fetches the secret's VALUE at PROVISION time via the SecretsGet grant
      # above; only the orchestrator role can read it (D5: never the harness
      # family role).
      { name = "OPENROUTER_MANAGEMENT_SECRET_ARN", value = data.aws_secretsmanager_secret.openrouter_management.arn },
      # Ceiling discovery (exact-design §6): the discover route RunTasks the one-shot probe
      # into the NORMAL private network (it talks gateway/Redis/Aurora — never the isolated
      # harness subnets). Family name self-references the task def below.
      { name = "DISCOVERY_TASK_FAMILY", value = "${var.name_prefix}-ceiling-discovery" },
      { name = "DISCOVERY_SUBNET_IDS", value = jsonencode(var.private_subnet_ids) },
      { name = "DISCOVERY_SECURITY_GROUP_IDS", value = jsonencode(var.security_group_ids.task) },
      # offline-analysis-design.md §9.5: POST /runs/{id}/judge RunTasks the one-shot llm-judge
      # task into the NORMAL private network (it talks gateway/Aurora/S3 — never the isolated
      # harness subnets, same as discovery). Family name self-references the task def below.
      { name = "JUDGE_TASK_FAMILY", value = "${var.name_prefix}-llm-judge" },
      { name = "JUDGE_SUBNET_IDS", value = jsonencode(var.private_subnet_ids) },
      { name = "JUDGE_SECURITY_GROUP_IDS", value = jsonencode(var.security_group_ids.task) },
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = var.secrets.database_url },
      { name = "LITELLM_MASTER_KEY", valueFrom = var.secrets.master },
      { name = "LITELLM_SPEND_DATABASE_URL", valueFrom = var.secrets.litellm_spend_database_url },
    ]
  }])
  tags = {
    Name    = "${var.name_prefix}-orchestrator-api"
    purpose = var.purpose
  }
}

# --- ceiling-discovery (one-shot, RunTask-launched — NO service) --------------
# exact-design §6/§8: the discovery probe holds ~150+ concurrent connections with ~150-200MB of
# request bodies in flight and deliberately drives a shared provider pool to its admission edge.
# It runs as its own task so that burst never competes with the API's steady-state duties.
# Manual-only: the ONLY caller is the API's discover route, itself operator-confirm-gated.
resource "aws_ecs_task_definition" "ceiling_discovery" {
  family                   = "${var.name_prefix}-ceiling-discovery"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  cpu                = "1024"
  memory             = "4096" # ~200MB of in-flight bodies + 150+ sockets needs real headroom
  execution_role_arn = aws_iam_role.orch.arn
  task_role_arn      = aws_iam_role.orch.arn

  container_definitions = jsonencode([{
    name    = "ceiling-discovery"
    image   = var.image
    command = ["ceiling-discovery"] # entrypoint dispatches to python -m ...ceiling_discovery
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.log_group_api
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "ceiling-discovery"
      }
    }
    environment = [
      { name = "REDIS_URL", value = var.redis_endpoint },
      { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name },
      { name = "EVAL_ENV_PREFIX", value = var.name_prefix },
      { name = "LITELLM_BASE_URL", value = var.gateway_base_url },
      { name = "OPENROUTER_MANAGEMENT_SECRET_ARN", value = data.aws_secretsmanager_secret.openrouter_management.arn },
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = var.secrets.database_url },
      { name = "LITELLM_MASTER_KEY", valueFrom = var.secrets.master },
    ]
  }])
  tags = {
    Name    = "${var.name_prefix}-ceiling-discovery"
    purpose = var.purpose
  }
}

# --- llm-judge (one-shot, RunTask-launched — NO service) ---------------------
# offline-analysis-design.md §9.5/§10.4/§10.5: Pass A (leak backfill) then Pass B (the
# judge) in one invocation, triggered by POST /runs/{id}/judge. Same "own task, no shared
# process" shape as ceiling-discovery and for the same reason — this must not compete with
# or couple to the API process, the harness dispatcher, the reaper, or any in-flight run's
# control plane (§10.4: zero coupling — no SQS, no reaper, no instance_results.state writes
# beyond what Pass A itself already owns). REDIS_URL since 2026-09-07 (parallel judge): the
# pass publishes its live progress (judge:live:{run_id}, TTL'd, best-effort — the run
# screen's "live judge progress") and seeds pacer:cfg:{judge-model} from the deepseek pool
# at start (without it the gateway pacer's DEFAULTS cap a parallel pass at ~10 in flight).
# Still nothing here touches control_state or any run's control plane.
resource "aws_ecs_task_definition" "llm_judge" {
  family                   = "${var.name_prefix}-llm-judge"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  cpu                = "1024"
  memory             = "2048" # JUDGE_WORKERS (default 24, max 100) concurrent calls, each holding one assembled trajectory
  execution_role_arn = aws_iam_role.orch.arn
  task_role_arn      = aws_iam_role.orch.arn

  container_definitions = jsonencode([{
    name    = "llm-judge"
    image   = var.image
    command = ["llm-judge"] # entrypoint dispatches to python -m ...llm_judge_task
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.log_group_api
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "llm-judge"
      }
    }
    environment = [
      { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name },
      { name = "EVAL_ENV_PREFIX", value = var.name_prefix },
      { name = "ARTIFACTS_BUCKET", value = var.artifact_bucket_name },
      { name = "LITELLM_BASE_URL", value = var.gateway_base_url },
      { name = "OPENROUTER_MANAGEMENT_SECRET_ARN", value = data.aws_secretsmanager_secret.openrouter_management.arn },
      { name = "REDIS_URL", value = var.redis_endpoint },
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = var.secrets.database_url },
      { name = "LITELLM_MASTER_KEY", valueFrom = var.secrets.master },
    ]
  }])
  tags = {
    Name    = "${var.name_prefix}-llm-judge"
    purpose = var.purpose
  }
}

resource "aws_ecs_service" "api" {
  name            = "orchestrator-api"
  cluster         = var.cluster_name
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_replicas
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = var.security_group_ids.task
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_alb_target_group.api.arn
    container_name   = "orchestrator-api"
    container_port   = 8000
  }
  tags = {
    Name    = "${var.name_prefix}-orchestrator-api"
    purpose = var.purpose
  }
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

output "api_alb_dns" {
  value = aws_alb.api.dns_name
}
output "api_service" {
  value = aws_ecs_service.api.name
}
