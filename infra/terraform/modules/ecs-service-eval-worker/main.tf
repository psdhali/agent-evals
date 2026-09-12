/**
 * modules/ecs-service-eval-worker — privileged eval worker (arch §6.1, ADR-0012)
 *
 * Runs SWE-bench grading with Docker-in-Docker, so it lives on the EC2
 * capacity provider (Bottlerocket + NVMe instance-store, created in
 * ecs-cluster), NOT Fargate. `privileged = true` on the container here — this
 * is the one privileged workload, and it is why eval is on EC2 while harness
 * stays on Fargate (structural no-privilege for untrusted model output).
 *
 * 5b note: eval hosts build instance images from the env images with
 * namespace=None (build locally), per image-environment-pipeline.md — the
 * DinD path is the same; only what it pulls changes.
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

variable "ec2_capacity_provider" {
  type        = string
  description = "EC2 capacity provider name (ecs-cluster module)."
}

variable "image" {
  type        = string
  description = "Eval worker image (ECR)."
}

variable "image_digest" {
  type        = string
  default     = ""
  description = "Optional pinned ECR sha256 digest for the eval-worker image. When set, the task-definition image becomes '<image>@<digest>' so ECS pulls exactly that digest — the eval worker runs on the EC2 pool (DinD grading), so a reused host would otherwise serve a stale cached 'eval-worker:latest' with no error and no drift signal (the 2026-09-02 -inst staleness root cause, generalized). Same durable pattern as the warm-job task's image_digest. Empty (default) keeps the legacy '<image>:latest' floating reference."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnets for the EC2 task."
}

variable "security_group_ids" {
  type        = list(string)
  description = "Task security groups."
}

variable "log_group" {
  type        = string
  description = "CloudWatch log group."
}

variable "secrets" {
  type = object({
    master       = string
    database_url = string
  })
  description = "Secret ARNs."
}

variable "artifact_bucket_name" {
  type        = string
  description = "Durable artifacts S3 bucket name (patch download + eval-report/trajectory upload)."
}

variable "artifact_bucket_arn" {
  type        = string
  description = "ARN of the artifact bucket, for the scoped s3 IAM statement."
}

variable "dataset_bucket_name" {
  type        = string
  description = "Durable dataset S3 bucket name — the pinned SWE-bench mirror this worker reads to build the grading TestSpec (FULL schema, include_gold=True). Without read access here the loader silently falls back to HuggingFace (agreed-architecture-changes §0.3)."
}

variable "env_image_repository" {
  type        = string
  description = "ECR repo URL of the collocated env/harness worker images (e.g. <acct>.dkr.ecr.<region>.amazonaws.com/eval-dev-harness-worker). Drives ENV_IMAGE_REPOSITORY for the env-image consumer (review F2): with EVAL_IMAGE_NAMESPACE='build' the eval worker builds the instance image FROM this ECR env image (local-build path) instead of pulling swebench/<...> from Docker Hub."
}

variable "queue_urls" {
  type = object({
    eval    = string
    results = string
  })
  description = "SQS queue URLs."
}
variable "redis_endpoint" {
  type        = string
  description = "Valkey endpoint this worker reads/writes live progress + control state from (ADR-0039). Empty means no Valkey — fail-closed to all-paused."
  default     = "" # local/dev with no Valkey — the worker fails closed (unreachable)
}
variable "queue_arns" {
  type = object({
    eval    = string
    results = string
  })
  description = "SQS queue ARNs — used in the IAM policy (URLs are not IAM resources; same scoping fix as the orchestrator module)."
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

resource "aws_iam_role" "eval" {
  name               = "${var.name_prefix}-eval-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags = {
    Name    = "${var.name_prefix}-eval-task"
    purpose = var.purpose
  }
}

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "eval" {
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
    resources = [var.secrets.master, var.secrets.database_url]
  }
  statement {
    sid = "SqsScoped"
    actions = [
      "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:SendMessage", "sqs:ChangeMessageVisibility",
    ]
    resources = [var.queue_arns.eval, var.queue_arns.results]
  }
  # BUILDER4-EVAL-PACKING-2026-09-03 §6 (scale-in): the worker marks its OWN
  # task scale-in-protected for the duration of a grade through the agent's
  # task-protection endpoint (swebench_eval/workers/task_protection.py), so a
  # desired-count decrease stops idle workers, never a grade in flight. The
  # agent calls UpdateTaskProtection with this task role. Scoped to tasks of
  # this cluster by resource ARN (the ecs:cluster condition key is the trap
  # the 2026-09-02 bring-up hit; task ARNs carry the cluster name).
  statement {
    sid     = "TaskScaleInProtection"
    actions = ["ecs:UpdateTaskProtection", "ecs:GetTaskProtection"]
    resources = [
      "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task/${var.cluster_name}/*"
    ]
  }
  # 5a-ii: the eval worker downloads the patch from S3 and uploads the eval
  # report/trajectory. Prior to this it had SQS+secrets+logs only — the first
  # graded run would fail AccessDenied on the very first get_object.
  statement {
    sid     = "S3Artifacts"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [
      var.artifact_bucket_arn,
      "${var.artifact_bucket_arn}/*",
    ]
  }
  # Stage 0.3: the eval worker reads the dataset mirror (FULL schema, incl. the
  # gold patch) to build the official TestSpec for grading. Was missing, so the
  # grading path fell back to HuggingFace unauthenticated on every fetch.
  statement {
    sid     = "S3DatasetRead"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      "arn:aws:s3:::${var.dataset_bucket_name}",
      "arn:aws:s3:::${var.dataset_bucket_name}/*",
    ]
  }
  statement {
    sid       = "CloudWatchLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "eval" {
  name   = "${var.name_prefix}-eval"
  role   = aws_iam_role.eval.id
  policy = data.aws_iam_policy_document.eval.json
}

resource "aws_ecs_task_definition" "eval" {
  family                   = "${var.name_prefix}-eval-worker"
  network_mode             = "awsvpc"
  requires_compatibilities = ["EC2"] # NOT Fargate — DinD needs a real host
  runtime_platform {                 # C-1: pin X86_64 for consistency + the day the pool is mixed
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  # 5a-ii: the eval task MUST fit the c5d.large host (2 vCPU / 4 GiB) — the
  # plan's 2048/4096 never placed ("insufficient memory available": the task
  # alone took the whole host before the ECS agent's reservation). Sized to
  # 1024/2048 so the DinD grading container has room while the host has slack.
  # Resized from the live placement failure, not guessed.
  #
  # BUILDER4-EVAL-PACKING-2026-09-03 (owner decision B): the host is a
  # c5d.2xlarge now (8 vCPU / 15.2 GiB registered) and this reservation is
  # what decides how many grades ECS packs onto it — there is NO placement
  # constraint, so 1024/2048 let ECS place SEVEN tasks per host while the
  # scaler assumed one. The reservation now tells the truth for FOUR per
  # host (the build tier's rule: "the constraint hides the lie, the
  # reservation corrects it"): 4 × 2048 CPU = the host's 8192 units, and
  # 4 × 3584 MiB = 14 GiB of 15.2 leaves the agent its slack. 3584 = the
  # worker (~300 MB) + the grading container's 3 GiB cgroup cap
  # (EVAL_GRADE_MEM_LIMIT_MB below), which is a sibling on the host daemon
  # and invisible to ECS — the reservation is the only accounting it gets.
  # Sized from 31 sampled grades (peak RSS median 248 MB, max 1.79 GB;
  # 0.2–1.2 cores average). Must move together with EVAL_TASKS_PER_HOST
  # (run-supervisor) and the grade cap.
  # Eval scaling review (F2/F6): the packing contract is sharp on BOTH axes —
  # 4 x 2048 CPU is EXACTLY the host's 8192 units (zero slack: any future
  # daemon or sidecar on the eval ASG silently drops packing to three), and
  # ECS must REGISTER >= 14,336 MiB (registered = MemTotal minus Bottlerocket's
  # settings.ecs.reserved-memory, unset = 0). Bring-up check: describe-
  # container-instances registeredResources MEMORY >= 14336, not "4 per host".
  cpu                = "2048"
  memory             = "3584"
  execution_role_arn = aws_iam_role.eval.arn
  task_role_arn      = aws_iam_role.eval.arn

  # 5a-ii: DinD actually needs the host docker socket. The image comment claimed
  # "mounts the host docker socket" but the task never did — docker.from_env()
  # inside the task would find no /var/run/docker.sock and grading fails at the
  # first container launch. Bottlerocket exposes the daemon socket on the host;
  # ECS on EC2 mounts it via a host_path volume.
  volume {
    name      = "docker-sock"
    host_path = "/var/run/docker.sock"
  }

  container_definitions = jsonencode([{
    name       = "eval-worker"
    image      = var.image_digest != "" ? "${var.image}@${var.image_digest}" : var.image
    privileged = true # the one privileged workload (grading Dind); eval is on EC2 for exactly this
    mountPoints = [
      # readOnly on a Unix socket bind mount is NOT a safeguard: API access is
      # governed by the socket's own permissions, and the grade needs full
      # daemon access (create/exec/remove). Kept as-is; do not read it as one.
      { sourceVolume = "docker-sock", containerPath = "/var/run/docker.sock", readOnly = true }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.log_group
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "eval-worker"
      }
    }
    environment = [
      { name = "EVAL_QUEUE_URL", value = var.queue_urls.eval },
      { name = "RESULTS_QUEUE_URL", value = var.queue_urls.results },
      # 5a-ii: transport/env seam (queue/client.py) + artifact bucket.
      { name = "ARTIFACTS_BUCKET", value = var.artifact_bucket_name },
      { name = "DATASET_BUCKET", value = var.dataset_bucket_name },
      # Review F2: wire the env-image consumer. EVAL_IMAGE_NAMESPACE=build sends
      # the runner to namespace=None (local-build path) so ensure_env_image runs,
      # pulling the ECR env image and building the instance image from it —
      # NOT the legacy Docker Hub swebench/<...> pull.
      { name = "EVAL_IMAGE_NAMESPACE", value = "build" },
      { name = "ENV_IMAGE_REPOSITORY", value = var.env_image_repository },
      { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name },
      { name = "EVAL_ENV_PREFIX", value = var.name_prefix },
      { name = "SQS_QUEUE_PREFIX", value = "${var.name_prefix}-" },
      # master-handover 3b: BLAS thread limits — the GRADE runs SWE-bench's
      # PASS_TO_PASS list; a P2P test crashing inside LAPACK (dsyevr liwork=1 on a
      # 1-vCPU task) would silently depress the resolve rate. Pin to 1 thread.
      # Task-def env, no -hw rebuild; the eval tier is where a wrong grade costs.
      { name = "OMP_NUM_THREADS", value = "1" },
      { name = "OPENBLAS_NUM_THREADS", value = "1" },
      { name = "MKL_NUM_THREADS", value = "1" },
      # ADR-0039: the eval worker reads operator control state + live progress
      # from Valkey. Renders the endpoint (module variable) — empty means no
      # Valkey and the worker fails closed to all-paused.
      { name = "REDIS_URL", value = var.redis_endpoint },
      # BUILDER3B eval resource instrumentation: per-grade container sampler —
      # ON by default; set "0" to disable mid-run without an image rebuild.
      # Task-def env, no image rebuild (same reasoning as the OMP vars above).
      { name = "EVAL_RESOURCE_SAMPLING", value = "1" },
      # BUILDER4-EVAL-PACKING-2026-09-03: cgroup memory cap on the GRADING
      # container (swebench_eval/evaluation/grade_limits.py — provenance in
      # its docstring: 31 grades, max peak RSS 1.79 GB, ×1.6 → 3 GiB). Four
      # grades share this host; a runaway grade is killed inside its own
      # cgroup (→ EVAL_OOM_KILLED, regradable) instead of OOMing the host and
      # the other three. "0" disables; EVAL_GRADE_CPUS (unset) would add a
      # hard CPU cap — deliberately none, CFS shares the host proportionally.
      { name = "EVAL_GRADE_MEM_LIMIT_MB", value = "3072" },
    ]
    secrets = [
      { name = "LITELLM_MASTER_KEY", valueFrom = var.secrets.master },
      { name = "DATABASE_URL", valueFrom = var.secrets.database_url },
    ]
  }])

  tags = {
    Name    = "${var.name_prefix}-eval-worker"
    purpose = var.purpose
  }
}

resource "aws_ecs_service" "eval" {
  name            = "eval-worker"
  cluster         = var.cluster_name
  task_definition = aws_ecs_task_definition.eval.arn
  desired_count   = 1

  # Eval stays on EC2 (ADR-0012/0021) — the EC2 capacity provider, not FARGATE.
  capacity_provider_strategy {
    capacity_provider = var.ec2_capacity_provider
    weight            = 1
    base              = 1
  }

  # BUILDER4-EVAL-PACKING-2026-09-03 §6 (scale-in): binpack by memory. With
  # four grades per host the scaler can only terminate a host once ECS has
  # emptied it, and ECS chooses scale-in victims with the placement strategy
  # in reverse — binpack means tasks come off the LEAST-packed host first, so
  # hosts empty in order instead of the fleet stranding at 1-of-4 occupancy.
  # Placement itself is unchanged in practice: the 2048/3584 reservation fills
  # every host to exactly four either way. (Task scale-in protection, set by
  # the worker while grading, keeps busy tasks out of the victim set.)
  ordered_placement_strategy {
    type  = "binpack"
    field = "memory"
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = false
  }

  tags = {
    Name    = "${var.name_prefix}-eval-worker"
    purpose = var.purpose
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

output "service_name" {
  value = aws_ecs_service.eval.name
}

# Adoption Phase 1a: the region comes from the provider, never a literal.
data "aws_region" "current" {}
