/**
 * modules/ec2-task-warm-job — the warm-job TASK (5b), on the privileged EC2 pool
 *
 * The warm job (scripts/warm_image_cache.py) BUILDS the env images and the
 * harness-worker layers, so it needs the host docker daemon (like the eval
 * worker) plus push IAM (env/harness images → one collocated ECR repo) plus the
 * dataset-bucket S3 for the cache manifest.
 *
 * The git-mirror half was REMOVED (ADR-0025/0029 review S1): the 12 repos are
 * baked into the git-mirror image, so the warm job no longer refreshes EFS
 * mirrors. The mirror half of `CacheManifest` no longer gates dispatch.
 *
 * It is deliberately a TASK DEFINITION, not a service: the warm cache is a
 * one-shot build, invoked on demand via `aws ecs run-task` (ADR-0020/5b — the
 * EventBridge schedule stays dormant until the precondition gate exists). A
 * service would keep one forever-idle task costing a host 24/7.
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
  description = "Warm-job image (ECR), WITHOUT a tag (the repo URL)."
}

variable "image_digest" {
  type        = string
  default     = ""
  description = "Optional pinned ECR sha256 digest for the warm-job image. When set, the task-definition image becomes '<image>@<digest>' so ECS always pulls exactly that digest — a reused EC2 host can never run a stale cached 'warm-job:latest' (the 2026-08-27 refresh-not-carrying-code root cause; same durable pattern as the dispatcher's harness-image digest resolution). Empty (default) keeps the legacy '<image>:latest' floating reference for callers that do not pin."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnets for the task."
}

variable "security_group_ids" {
  type        = list(string)
  description = "Task security groups."
}

variable "log_group" {
  type        = string
  description = "CloudWatch log group."
}

variable "image_repo_name" {
  type        = string
  description = "Name of the collocated env+harness ECR repo the warm job pushes to."
}

variable "dataset_bucket_name" {
  type        = string
  description = "Durable dataset bucket name (cache-manifest/<version>.json lives here)."
}

variable "dockerhub_secret_arn" {
  type        = string
  default     = ""
  description = "Optional: ARN of the Secrets Manager secret holding Docker Hub credentials ({username, accessToken}). When set, the task role may read it and the task gets DOCKERHUB_SECRET_ID so build_phase0_instances_v2.py --base official logs in to Docker Hub before pulling SWE-bench's published images (anonymous pulls are rate-limited per NAT IP; 500 pulls need an account). Empty (default) = anonymous pulls, unchanged behaviour."
}

variable "gateway_config_hash" {
  type        = string
  description = "sha256 of infra/docker/litellm_config.yaml at build time — the -hw image attests which gateway config it was built against (smoke-readiness-check.md). Passed through to the -hw build (warm_image_cache._env('GATEWAY_CONFIG_HASH'))."
}

variable "task_family_suffix" {
  type        = string
  default     = "warm-job"
  description = "Task-definition family suffix: family = name_prefix + '-' + task_family_suffix. Default keeps the ORIGINAL family (eval-dev-warm-job) unchanged. builder5-image-build-tier Stage 2: envs/dev/build passes a DIFFERENT suffix (e.g. 'image-build') so the two task definitions coexist under different families and can never fight over the same one (brief §4 Stage 2 'the family name must differ')."
}

variable "task_cpu" {
  type        = string
  default     = "1024"
  description = "Task cpu units. Default (1024) is the ORIGINAL value, sized to fit a c5d.large — unchanged for the existing envs/dev/eval caller. builder5-image-build-tier Stage 2's reservation fix: the build tier passes a value sized to ~the whole build host so ECS can place only ONE shard per host (the 512 MB memory reservation below is fiction today — docker build runs on the host daemon outside this task's cgroup, which is exactly how two shards got packed onto one host and OOM'd each other)."
}

variable "task_memory" {
  type        = string
  default     = "512"
  description = "Task memory (MiB), hard limit. Default (512) is the ORIGINAL value — see task_cpu. The build tier passes a value sized to ~the whole build host's RAM (minus headroom for the ECS agent/daemon) instead of relying on a distinctInstance placement constraint, per the brief's preference: 'the constraint hides the lie, the reservation corrects it.'"
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

resource "aws_iam_role" "warm" {
  # task_family_suffix in the name (not just the family above): IAM role names
  # are unique per ACCOUNT, not per module instantiation. Without this, a
  # second instantiation of this module with the same name_prefix (envs/dev/build
  # alongside envs/dev/eval, builder5-image-build-tier Stage 2) would collide on
  # "${name_prefix}-warm-job-task" and fail (or worse, silently adopt the
  # other root's role).
  name               = "${var.name_prefix}-${var.task_family_suffix}-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags = {
    Name    = "${var.name_prefix}-${var.task_family_suffix}-task"
    purpose = var.purpose
  }
}

data "aws_iam_policy_document" "warm" {
  statement {
    sid = "EcrFullForWarm"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:ListImages",
      "ecr:DescribeImages",
    ]
    resources = ["*"] # GetAuthorizationToken + ListImages are registry-level
  }
  dynamic "statement" {
    for_each = var.dockerhub_secret_arn != "" ? [var.dockerhub_secret_arn] : []
    content {
      sid       = "DockerHubSecret"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [statement.value]
    }
  }
  statement {
    sid     = "S3Dataset"
    actions = ["s3:GetObject", "s3:PutObject"]
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
  # ECS Exec (operator diagnostics): the warm task's IAM must allow the SSM
  # session actions so the managed exec agent can serve `execute-command`.
  # Scoped by which entity holds this role — the warm task only (workers do not).
  statement {
    sid = "EcsExec"
    actions = [
      "ssm:StartSession",
      "ssm:TerminateSession",
      "ssm:ResumeSession",
      "ssm:SendCommand",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "warm" {
  # Inline policy, scoped to aws_iam_role.warm.id (already unique per
  # instantiation above) — this name only needs to be unique WITHIN that role,
  # but matches task_family_suffix for the same clarity reason.
  name   = "${var.name_prefix}-${var.task_family_suffix}"
  role   = aws_iam_role.warm.id
  policy = data.aws_iam_policy_document.warm.json
}

resource "aws_ecs_task_definition" "warm" {
  family                   = "${var.name_prefix}-${var.task_family_suffix}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["EC2"] # docker for the image builds
  # ECS Exec (2026-08-17): operator diagnostics via `aws ecs execute-command`
  # (read build_image.log / `docker images` on the host daemon — stop flying
  # blind on long solves). Exec is not a task-definition field; enable it per
  # LAUNCH with `aws ecs run-task ... --enable-execute-command` and the SSM IAM
  # below. The warm task only — NOT the workers (they execute untrusted model
  # output; exec there is where a vuln would live).
  runtime_platform { # C-1: pin X86_64, then READ the manifest back
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  # Sized to fit the c5d.large eval host (2 vCPU / 4 GiB): the eval-worker lock
  # this host at 1024/2048 and the agent + daemon reserve ~300 MB, leaving
  # ~3.7 GiB — the warm task's first 2048/4096 run placed "RESOURCE:MEMORY"
  # (4096 requested, 3780 available).  The warm container itself only runs
  # Python + the docker CLI; the actual env/harness BUILD happens in the host
  # daemon's build containers — and THOSE are what must get the memory.
  #
  # Live finding (2026-08-17): with memory=2048 the daemon was left ~1.7 GiB
  # and every env build died "returned a non-zero code: 137" (SIGKILL — kernel
  # OOM). Shrunk to 512 (the container only coordinates); env builds run
  # serially (max_workers=1, ENV_WORKERS) so each conda solve gets the host.
  #
  # builder5-image-build-tier Stage 2 reservation fix: the 512 MiB above is
  # fiction for what actually needs the memory — docker build runs on the HOST
  # daemon, outside this task's cgroup entirely, so ECS sees a tiny task while
  # the host does the real work. That's exactly how two shards got packed onto
  # one host and OOM'd each other. task_cpu/task_memory default to the
  # ORIGINAL values (unchanged for envs/dev/eval's caller); envs/dev/build
  # passes values sized to ~the whole build host so ECS can place only ONE
  # such task per host — a reservation that tells the truth, preferred over a
  # distinctInstance placement constraint (which would hide the lie instead of
  # correcting it).
  cpu                = var.task_cpu
  memory             = var.task_memory
  execution_role_arn = aws_iam_role.warm.arn
  task_role_arn      = aws_iam_role.warm.arn

  volume {
    name      = "docker-sock"
    host_path = "/var/run/docker.sock"
  }

  container_definitions = jsonencode([{
    name       = "warm-job"
    image      = var.image_digest != "" ? "${var.image}@${var.image_digest}" : var.image
    privileged = true # docker daemon access (image builds)
    mountPoints = [
      { sourceVolume = "docker-sock", containerPath = "/var/run/docker.sock", readOnly = true },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.log_group
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "warm-job"
      }
    }
    environment = [
      { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name },
      { name = "EVAL_ENV_PREFIX", value = var.name_prefix },
      { name = "AWS_ACCOUNT_ID", value = data.aws_caller_identity.current.account_id },
      { name = "ECR_REGISTRY", value = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.name}.amazonaws.com" },
      { name = "AWS_REGION", value = data.aws_region.current.name },
      { name = "HARNESS_IMAGE_REPO", value = var.image_repo_name },
      { name = "DATASET_BUCKET", value = var.dataset_bucket_name },
      # Only the secret's NAME — the build script reads it at run time with the
      # task role (DockerHubSecret statement); empty = anonymous pulls.
      { name = "DOCKERHUB_SECRET_ID", value = var.dockerhub_secret_arn },
      { name = "HARNESS_BUILD_ROOT", value = "/app" },
      # smoke-readiness-check.md: the -hw image must attest which gateway config
      # it was built against. warm_image_cache reads this env and passes it as the
      # GATEWAY_CONFIG_HASH build-arg — the control-plane images carry the same
      # sha (858054540ba... currently), so the attestation is comparable.
      { name = "GATEWAY_CONFIG_HASH", value = var.gateway_config_hash },
      # ADR-0030 review S1: GIT_MIRROR_ROOT removed — the 12 repos are baked
      # into the git-mirror image; the empty EFS filesystem was removed too
      # (review U1).
      # warm-job-verified-option-handover (§5): the warm job must build the
      # ACTIVE dataset's env surface — VERIFIED (40 envs), not Lite (35) and
      # not full (61, ~49 GB it never runs).  Verified's env set is a strict
      # subset of full's (40 ⊆ 61), so the smaller build covers every instance
      # the dispatch keys derive from.  _dataset_rows() binds each name to its
      # own pin (the 2026-08-21 Lite@Verified 404 fix); the explicit 'verified'
      # here is what the deployed task actually runs.
      { name = "ENV_DATASET", value = "verified" },
    ]
  }])

  tags = {
    Name    = "${var.name_prefix}-${var.task_family_suffix}"
    purpose = var.purpose
  }
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.warm.arn
}

# Adoption Phase 1a: the region comes from the provider, never a literal.
data "aws_region" "current" {}

data "aws_caller_identity" "current" {}
