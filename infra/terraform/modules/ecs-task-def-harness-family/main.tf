/**
 * modules/ecs-task-def-harness-family — one Fargate task definition per env image (ADR-0030 H2)
 *
 * ECS cannot override a task's image at run-task time, so the harness pool is
 * dispatched as one Fargate task per instance against a PRE-REGISTERED
 * task-definition family per env image. The families are derived from the
 * warm-cache manifest by scripts/gen_harness_task_families.py (the image the
 * task must run) — this module only registers what it is told, with a shared
 * execution/task role. The dispatcher service that RUNS these families is H3;
 * until then the families are inert (task definitions, no service), so they
 * cannot double-process the queue.
 *
 * Container shape mirrors the (soon-to-be-replaced) ecs-service-harness-worker:
 * the per-env `-hw` image carries the env + prebuilt `testbed` + the six CLIs,
 * and the image's own ENTRYPOINT runs the harness worker. NO EFS mount: under
 * ADR-0029 the git mirror is the git-daemon service (git://), not EFS — the
 * empty EFS filesystem was removed (review U1). The job payload arrives via the
 * dispatcher's containerOverrides
 * (H3); this container def is the base the override is layered on.
 */
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# --- TWO roles, not one (A1-8 / review finding) -----------------------------
# The single role that served as BOTH execution and task role gave the untrusted
# agent container direct `secretsmanager:GetSecretValue` on LITELLM_MASTER_KEY
# (and OPENROUTER_API_KEY) via the credentials endpoint — the injection was not
# the only path. Split:
#   * execution role — image pull + secret RESOLUTION (the Fargate agent reads
#     `valueFrom` over the task ENI, A1-1/ADR-0033) + awslogs delivery.
#   * task role — the harness's own AWS calls (SQS poll/ack, S3 artifacts and
#     dataset mirror). No secrets, no logs.
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-harness-family-exec"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags = {
    Name    = "${var.name_prefix}-harness-family-exec"
    purpose = var.purpose
  }
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid       = "EcrPull"
    actions   = ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer"]
    resources = ["*"]
  }
  # G2 (2026-09-05): no SecretsGet statement any more — the harness task
  # injects NO secrets (see `secrets = []` below); the per-run gateway key
  # arrives as a plain containerOverride from the dispatcher.  `var.secrets`
  # is kept declared so the root's module call needs no edit.
  # The awslogs driver (which on Fargate PV1.4.0 delivers over the task ENI)
  # needs log permissions on the EXECUTION role.
  statement {
    sid       = "CloudWatchLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "${var.name_prefix}-harness-family-exec"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

resource "aws_iam_role" "task" {
  name               = "${var.name_prefix}-harness-family-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags = {
    Name    = "${var.name_prefix}-harness-family-task"
    purpose = var.purpose
  }
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "SqsScoped"
    actions   = ["sqs:ReceiveMessage", "sqs:GetQueueUrl", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:SendMessage", "sqs:ChangeMessageVisibility"]
    resources = [var.queue_arns.harness, var.queue_arns.eval, var.queue_arns.results, var.queue_arns.llm_calls]
  }
  statement {
    sid     = "S3Artifacts"
    actions = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
    resources = [
      var.artifact_bucket_arn,
      "${var.artifact_bucket_arn}/*",
    ]
  }
  # B1 (dataset-mirror-test-patch-exposure): scope to the PUBLIC mirror object
  # ONLY — never bucket/*.  The full-gold mirror (.jsonl, which carries test_patch
  # + gold patch) lives in the SAME bucket, and the old bucket/* grant let the
  # harness task role read it.  C4: the loader only ever GetObject's a known key
  # (grep confirms no ListBucket call), so no bucket-wide ListBucket is granted.
  # V2 (switch-to-swebench-verified §4): the dataset segment is now
  # `princeton-nlp/*` (dataset-agnostic) so switching to Verified never breaks
  # every harness task with AccessDenied; the `*.public.jsonl` suffix still
  # keeps the gold `<rev>.jsonl` unreadable in ANY dataset directory.
  statement {
    sid     = "S3DatasetReadPublicOnly"
    actions = ["s3:GetObject"]
    resources = [
      # ADR-0043: the datasets moved to the SWE-bench HF org; the princeton-nlp
      # prefix stays readable for the pre-upgrade checkpoint's mirror.
      "arn:aws:s3:::${var.dataset_bucket_name}/SWE-bench/*/*/*.public.jsonl",
      "arn:aws:s3:::${var.dataset_bucket_name}/princeton-nlp/*/*/*.public.jsonl",
    ]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${var.name_prefix}-harness-family-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# --- One task definition per env image (the family set) ----------------------
resource "aws_ecs_task_definition" "family" {
  for_each = var.families

  family                   = each.key
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  # The -hw image is a ~2.4 GB env image + agent CLIs; the conda env + an agent
  # need headroom. H3 measures the real peak and adjusts.
  # 2026-09-08 (owner): 2048 -> 3072. One OOM kill in ~2,500 attempts across five
  # 500-runs (opencode / matplotlib-26208: ECS stoppedReason "OutOfMemoryError:
  # container killed due to memory usage" — the harness log just ends). 3 GB is
  # the next Fargate step for 1 vCPU (~+15% memory cost per task-hour).
  cpu                = "1024"
  memory             = "3072"
  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn      = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      # The image's OWN ENTRYPOINT runs the harness worker (default CMD
      # "harness-worker"); the dispatcher (H3) overrides command+env with the
      # per-job payload. `essential = true` so the family can never be a
      # no-op task.
      name      = "harness-worker"
      image     = each.value.image
      essential = true
      # ADR-0034 M1.4: stopTimeout is the SIGTERM->SIGKILL grace the harness
      # worker uses to capture the partial patch + trajectory on Abort.  Fargate
      # default is 30s — not enough to upload a large trajectory.  120 is the
      # maximum.
      stop_timeout = 120
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = var.log_group
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = var.harness_name
        }
      }
      environment = [
        { name = "HARNESS", value = var.harness_name },
        # The -hw image has `testbed` PREBUILT — repo-prep runs SWE-bench's own
        # install_repo_script against it, never a runtime env build.
        { name = "REPO_PREP", value = "install_repo_script" },
        { name = "REDIS_URL", value = var.redis_endpoint },
        { name = "QUEUE_URL", value = var.queue_urls.harness },
        { name = "EVAL_QUEUE_URL", value = var.queue_urls.eval },
        { name = "RESULTS_QUEUE_URL", value = var.queue_urls.results },
        { name = "LITELLM_BASE_URL", value = var.gateway_base_url },
        { name = "ARTIFACTS_BUCKET", value = var.artifact_bucket_name },
        { name = "DATASET_BUCKET", value = var.dataset_bucket_name },
        # G1 (HARNESS-ISOLATION-AUDIT-2026-09-05 §3): GIT_MIRROR_URL is gone —
        # the pre-baked -inst never clones at runtime, and the mirror (a
        # full-history copy of every repo, gold fix included) must not be
        # nameable, let alone reachable, from the harness. The mirror SG no
        # longer admits the harness SG (envs/dev/eval) and routing's agent
        # denylist strips the name as well.
        { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name },
        { name = "EVAL_ENV_PREFIX", value = var.name_prefix },
        { name = "SQS_QUEUE_PREFIX", value = "${var.name_prefix}-" },
        # master-handover 3b (2026-08-24): BLAS thread limits. OpenBLAS reads the
        # HOST core count, not the cgroup's, and sizes its workspace for a machine
        # the task does not have — dsyevr saw liwork=1 on a 1 vCPU task (a real
        # infra failure at turn 165, reported as an unresolved). Pin to 1 thread
        # so the LAPACK workspace query is stable. Task-def env, no -hw rebuild.
        { name = "OMP_NUM_THREADS", value = "1" },
        { name = "OPENBLAS_NUM_THREADS", value = "1" },
        { name = "MKL_NUM_THREADS", value = "1" },
        # M1 (review 2026-08-25): opencode's inotify file watcher keeps its
        # handle open after the instance is disposed (only in a git repo like
        # /testbed), so `opencode run` never exits → wall-clock timeout →
        # FAILED_HARNESS → zero graded instances. Belt-and-braces with the
        # adapter's own env var (opencode/harness.py). agent_environment()
        # forwards OPENCODE_* (not denied), so it takes effect without a rebuild.
        { name = "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER", value = "true" },
      ]
      # G2 (HARNESS-ISOLATION-AUDIT-2026-09-05 §3): NO secrets at all. The
      # harness calls the gateway with the PER-RUN key the dispatcher hands
      # it (LITELLM_API_KEY via containerOverrides; ENFORCE_PER_RUN_KEY=1 on
      # the dispatcher refuses a launch without one), so the admin master key
      # has no business in a container running untrusted code — it was the
      # one credential a root agent could recover from /proc/1/environ.
      # (A1-8 had already dropped OPENROUTER_API_KEY for the same reason.)
      secrets = []
    },
  ])

  tags = {
    Name    = each.key
    purpose = var.purpose
  }
}

output "family_arns" {
  description = "family name -> task-definition ARN (the dispatcher's run_task targets, H3)."
  value = {
    for family, def in aws_ecs_task_definition.family : family => def.arn
  }
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}
output "execution_role_arn" {
  value = aws_iam_role.execution.arn
}

# Adoption Phase 1a: the region comes from the provider, never a literal.
data "aws_region" "current" {}
