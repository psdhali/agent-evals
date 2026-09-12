/**
 * modules/gateway — LiteLLM proxy service (arch §4, §6.4)
 *
 * Stateless behind the ALB with its rpm/tpm counters in Valkey (ADR-0018 §4:
 * the gateway's shared rate-limit state lives in the same serverless cluster as
 * instance_progress:*). replica_count is a variable — one in dev, N in scale —
 * which is the "stateless, horizontally-scalable from day one" shape.
 *
 * The ALB is the gateway's front door (service discovery would also work for
 * one instance; §5.4 specifies an ALB and dev destroys it per session).
 * enable_deletion_protection = false (F-9) so destroy doesn't refuse.
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

variable "vpc_id" {
  type        = string
  description = "VPC ID."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnets for the ECS tasks."
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "Public subnets for the ALB."
}

variable "cluster_name" {
  type        = string
  description = "ECS cluster (ecs-cluster module)."
}

variable "replica_count" {
  type        = number
  default     = 1
  description = "Gateway replicas (dev: 1; scale: N). Stateless behind ALB."
}

variable "security_group_ids" {
  type = object({
    alb  = list(string)
    task = list(string)
  })
  description = "Security groups for the ALB and the gateway task."
}

variable "litellm_image" {
  type        = string
  description = "LiteLLM image/repo (ECR repository URL for the gateway)."
}

variable "secrets" {
  type = object({
    master       = string
    openrouter   = string
    database_url = string
  })
  description = "Secret ARNs: LiteLLM master key, OpenRouter provider key, Aurora DATABASE_URL."
}

variable "redis_endpoint" {
  type        = string
  description = "Valkey serverless endpoint (cache module)."
}

variable "log_group" {
  type        = string
  description = "CloudWatch log group for the gateway."
}

# --- IAM role for the gateway task -------------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "gateway" {
  name               = "${var.name_prefix}-gateway-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags = {
    Name    = "${var.name_prefix}-gateway-task"
    purpose = var.purpose
  }
}

data "aws_iam_policy_document" "gateway_secrets" {
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
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.secrets.master, var.secrets.openrouter, var.secrets.database_url]
  }
  # 5a-ii: without a logs statement the task cannot create its CloudWatch log
  # stream and every placement fails ("logs:CreateLogStream ... is not
  # authorized"). Every other service module has this statement; the gateway was
  # the one that didn't.
  statement {
    sid       = "CloudWatchLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "gateway" {
  name   = "${var.name_prefix}-gateway-secrets"
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.gateway_secrets.json
}

# --- ALB ----------------------------------------------------------------------
# A1/ADR-0033: the gateway ALB MUST be internal, and that is load-bearing, not
# hardening. An internet-facing ALB resolves to PUBLIC IPs even from inside the
# VPC, and an isolated harness task (no default route) cannot reach a public IP —
# so without the flip the harness could not call the model at all after A1. It
# also closes the project's only genuinely public surface (the earlier
# `internal = false` + a 0.0.0.0/0 "albs-sg"). The gateway TASK keeps NAT egress
# to OpenRouter; `internal` governs inbound only and never touches that.
resource "aws_alb" "gateway" {
  name                       = "${var.name_prefix}-gateway-alb"
  internal                   = true # A1/ADR-0033: only reachable from inside the VPC
  load_balancer_type         = "application"
  subnets                    = var.private_subnet_ids # internal ALBs need private subnets
  security_groups            = var.security_group_ids.alb
  enable_deletion_protection = false # F-9: destroy must not refuse
  # 2026-09-02 (llm-judge 504s): the AWS default of 60s killed every
  # NON-STREAMING completion that thought for longer than a minute — the ALB
  # returned an HTML 504 mid-generation (the judge's deepseek calls with
  # reasoning_effort=high routinely exceed 60s). Harness traffic never hit
  # this because the shim streams, so bytes keep the connection non-idle.
  # 2026-09-08: 600 -> 1800. A judgment over a long trajectory can take the
  # reasoning judge more than 10 min on a slow provider day — 51 of 503
  # claude_code candidates 504'd at exactly 600 s, twice. The judge's OpenAI
  # client timeout is raised to the same 30 min (judge_llm.JUDGE_CALL_TIMEOUT_S).
  idle_timeout = 1800

  tags = {
    Name    = "${var.name_prefix}-gateway-alb"
    purpose = var.purpose
  }
}

resource "aws_alb_target_group" "gateway" {
  name        = "${var.name_prefix}-gateway-tg"
  port        = 4000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/health/liveliness"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = {
    Name    = "${var.name_prefix}-gateway-tg"
    purpose = var.purpose
  }
}

resource "aws_alb_listener" "gateway" {
  load_balancer_arn = aws_alb.gateway.arn
  port              = 4000
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_alb_target_group.gateway.arn
  }
}

# ADR-0040 — NOT the live gateway-pause path (corrected 2026-08-31; this
# comment previously claimed it was — see ADR-0040 for the full account).
# Gateway pause is enforced today via LiteLLM key-block
# (control_plane/gateway_pause.py), not this rule: no code anywhere calls
# elbv2 ModifyRule against it, its ARN was never exposed as a Terraform
# output, and it carries no lifecycle guard against a routine apply
# reverting a live toggle — all discovered when the pause/resume surface was
# actually audited.
#
# The rule is kept, unused, as addressable defense-in-depth for the day a
# harness bypasses the per-worker shim (today none does —
# SHIM_ROUTED_HARNESSES == frozenset(HARNESS_ADAPTERS)), at which point
# wiring elbv2 ModifyRule against this rule becomes real work again, not a
# design decision to make from scratch. It starts as a pass-through FORWARD
# so an un-paused gateway is unaffected either way.
resource "aws_alb_listener_rule" "gateway_framework_pause" {
  listener_arn = aws_alb_listener.gateway.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_alb_target_group.gateway.arn
  }

  condition {
    path_pattern {
      values = ["/*"]
    }
  }
}

# --- ECS service ---------------------------------------------------------------
# Item 16 (BUILDER4-RESUME-2026-09-04-FIXBATCH): LiteLLM's Redis-backed counters (router
# cooldown reads, ModelRateLimitingCheck, the v3 parallel-request limiter) batch-read their
# keys with a plain MGET. ElastiCache Serverless is cluster-mode; the keys hash to different
# slots, so every such read logged `redis_cache.py: async batch get cache — CROSSSLOT`.
# Verified in the v1.99.1 image: router.py:891-898 and proxy_server._build_redis_usage_cache
# both build a RedisClusterCache (whose _async_run_redis_mget_operation is
# RedisCluster.mget_nonatomic — a per-slot split) when REDIS_CLUSTER_NODES is set, and
# _redis.py:341 turns REDIS_SSL=true into the cluster client's TLS flag. Both are derived
# from the same rediss://host:port the REDIS_URL already carries — one place knows the
# endpoint. Empty endpoint (no eval tier yet) = no cluster env, same as today.
locals {
  redis_endpoint_parts = try(regex("^(rediss?)://([^:/]+)(?::([0-9]+))?", var.redis_endpoint), null)
  redis_cluster_env = local.redis_endpoint_parts == null ? [] : [
    {
      name = "REDIS_CLUSTER_NODES"
      value = jsonencode([{
        host = local.redis_endpoint_parts[1]
        port = tonumber(coalesce(local.redis_endpoint_parts[2], "6379"))
      }])
    },
    { name = "REDIS_SSL", value = local.redis_endpoint_parts[0] == "rediss" ? "true" : "false" },
  ]
}

resource "aws_ecs_task_definition" "gateway" {
  family                   = "${var.name_prefix}-gateway"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  # 5a-ii: 256/512 AND 512/1024 both OOM-killed the LiteLLM container (exit 137,
  # "OutOfMemoryError: container killed due to memory usage") on the ALB health
  # check. LiteLLM's main-stable image (python + prisma + config load) exceeds
  # 1GB RSS during startup on Fargate. 1024/2048 gives real headroom; re-sized
  # from two consecutive live OOMs, not guessed. The ALB health check path is
  # /health/liveliness.
  # Autoscaler spec §1.4 (2026-09-01): 1024/2048 -> 2048/8192. LiteLLM's spend-log buffer is
  # UNBOUNDED (spend_log_transactions, no cap) and grows at the full arrival rate if a flush
  # stalls: ~1 GB headroom = OOM in ~9 min at fleet load; ~6 GB survives ~50 min. Sized for the
  # paced fleet, not idle.
  cpu                = "2048"
  memory             = "8192"
  execution_role_arn = aws_iam_role.gateway.arn
  task_role_arn      = aws_iam_role.gateway.arn

  container_definitions = jsonencode([{
    name         = "gateway"
    image        = var.litellm_image
    portMappings = [{ containerPort = 4000, protocol = "tcp" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.log_group
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "gateway"
      }
    }
    environment = concat([
      { name = "REDIS_URL", value = var.redis_endpoint },
      { name = "LITELLM_CONFIG", value = "/app/config.yaml" },
      ], local.redis_cluster_env, [
      # Spec §1.4: smaller, more frequent spend-log flushes — the flush backlog
      # (remaining_count, logged by LiteLLM itself) is the leading OOM indicator.
      { name = "PROXY_BATCH_WRITE_AT", value = "5" },
    ])
    secrets = [
      { name = "LITELLM_MASTER_KEY", valueFrom = var.secrets.master },
      { name = "OPENROUTER_API_KEY", valueFrom = var.secrets.openrouter },
      { name = "DATABASE_URL", valueFrom = var.secrets.database_url },
    ]
  }])

  tags = {
    Name    = "${var.name_prefix}-gateway"
    purpose = var.purpose
  }
}

resource "aws_ecs_service" "gateway" {
  name            = "gateway"
  cluster         = var.cluster_name
  task_definition = aws_ecs_task_definition.gateway.arn
  desired_count   = var.replica_count
  launch_type     = "FARGATE" # baseline, On-Demand (gateway is not a harness worker)

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = var.security_group_ids.task
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_alb_target_group.gateway.arn
    container_name   = "gateway"
    container_port   = 4000
  }

  tags = {
    Name    = "${var.name_prefix}-gateway"
    purpose = var.purpose
  }

  lifecycle {
    ignore_changes = [task_definition]
  }
}

output "alb_dns" {
  value = aws_alb.gateway.dns_name
}
output "alb_arn" {
  value = aws_alb.gateway.arn
}
# The ALB's hosted zone id — required for an alias record to this ALB (the
# stable-name indirection, builder1-gateway-stable-dns). A Route53 alias record
# needs BOTH the ALB's dns_name and its zone_id.
output "alb_zone_id" {
  value = aws_alb.gateway.zone_id
}

# Adoption Phase 1a: the region comes from the provider, never a literal.
data "aws_region" "current" {}
