/**
 * envs/dev/eval — the eval fleet tier (PA-10/PA-11 three-tier split)
 *
 * This is what runs during an evaluation session and is `$0` when down:
 * Valkey (cache), gateway + its ALB, harness workers, eval workers, Prometheus,
 * and the nightly scale-to-zero target for the eval services. It reads the
 * durable backbone (VPC, subnets, SGs, Aurora, S3, ECR, secrets, log groups,
 * SQS, the ECS cluster) from `../persistent` via terraform_remote_state.
 *
 * `terraform destroy` on THIS state brings the eval fleet (and its cost) down
 * to zero while leaving the always-on tier (persistent — Aurora, S3, ECR,
 * results/history) untouched — the PA-10 property: browse history without the
 * eval fleet, and never lose the record.
 *
 * The DB ingress rule lives here (M-4): it is sourced from the task SG and
 * destroyed with this state, which is the correct lifecycle.
 */

terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# --- Durable backbone from persistent/ ---------------------------------------
data "terraform_remote_state" "persistent" {
  backend = "s3"
  config  = merge(local.remote_state, { key = "envs/dev/persistent/terraform.tfstate" })
}

locals {
  vpc_id             = data.terraform_remote_state.persistent.outputs.vpc_id
  private_subnet_ids = data.terraform_remote_state.persistent.outputs.private_subnet_ids
  public_subnet_ids  = data.terraform_remote_state.persistent.outputs.public_subnet_ids
  default_sg_id      = data.terraform_remote_state.persistent.outputs.default_security_group_id
  aurora_endpoint    = data.terraform_remote_state.persistent.outputs.aurora_endpoint
  task_sg_id         = data.terraform_remote_state.persistent.outputs.task_security_group_id
  alb_sg_id          = data.terraform_remote_state.persistent.outputs.alb_security_group_id
  cluster_name       = data.terraform_remote_state.persistent.outputs.cluster_name
  # 5a-ii: the Valkey endpoint is a bare address; redis clients want a URL.
  # rediss:// NOT redis:// (DoD 3, verified 2026-08-16): ElastiCache Serverless
  # MANDATES in-transit TLS. A plaintext redis:// TCP connect succeeds and then
  # hangs — the harness's write_progress died with "Timeout reading from socket"
  # exactly so, despite a correct SG. A probe from the tasks-SG proved it:
  # no-TLS PING/SET Terminated (killed by timeout), --tls PING/SET/GET all rc=0.
  redis_endpoint = "rediss://${module.cache.endpoint}:${module.cache.endpoint_port}"
  # A1 / ADR-0033: the isolated harness network + the endpoint SG. The harness
  # subnets/route-table/SG live in persistent/ (network shape); the five interface
  # endpoints that make isolation usable live HERE, torn down with this state.
  harness_sg_id               = data.terraform_remote_state.persistent.outputs.harness_security_group_id
  harness_isolated_subnet_ids = data.terraform_remote_state.persistent.outputs.harness_isolated_subnet_ids
  endpoint_sg_id              = data.terraform_remote_state.persistent.outputs.endpoint_security_group_id
}

# --- M-4: Aurora DB ingress, authored HERE (eval tier) ------------------------
# Aurora's SG lives in persistent/. This rule is sourced from the task SG and
# created/destroyed with THIS state — the correct lifecycle, no circular dep.
resource "aws_vpc_security_group_ingress_rule" "aurora_db" {
  security_group_id = data.terraform_remote_state.persistent.outputs.db_security_group_id
  description       = "Aurora PostgreSQL (5432) from eval-tier tasks (M-4 rule)"

  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  referenced_security_group_id = local.task_sg_id
}

# --- Step 2b: cache (Valkey) — ephemeral, part of the $0-when-down eval tier ---
module "cache" {
  source = "../../../modules/cache"

  name_prefix        = var.name_prefix
  purpose            = var.purpose
  subnet_ids         = local.private_subnet_ids
  security_group_ids = [local.cache_sg_id]
}

locals {
  cache_sg_id = data.terraform_remote_state.persistent.outputs.cache_security_group_id
}

# --- Step 3: gateway, prometheus ----------------------------------------------
# SQS + ECS cluster + S3 + ECR + SGs live in persistent/ (PA-5/PA-10: durable).

# --- Step 4: eval worker (EC2) + harness worker (per-harness, Fargate) --------
module "eval_worker" {
  source = "../../../modules/ecs-service-eval-worker"

  name_prefix           = var.name_prefix
  purpose               = var.purpose
  cluster_name          = data.terraform_remote_state.persistent.outputs.cluster_name
  ec2_capacity_provider = data.terraform_remote_state.persistent.outputs.ec2_capacity_provider
  image                 = data.terraform_remote_state.persistent.outputs.ecr_repository_urls["eval-worker"]
  image_digest          = data.aws_ecr_image.eval_worker.id
  private_subnet_ids    = local.private_subnet_ids
  security_group_ids    = [local.task_sg_id]
  log_group             = "/aws/ecs/${var.name_prefix}-eval-worker"
  queue_urls = {
    eval    = data.terraform_remote_state.persistent.outputs.queue_urls["eval-jobs"]
    results = data.terraform_remote_state.persistent.outputs.queue_urls["results"]
  }
  queue_arns = {
    eval    = data.terraform_remote_state.persistent.outputs.queue_arns["eval-jobs"]
    results = data.terraform_remote_state.persistent.outputs.queue_arns["results"]
  }
  secrets = {
    master       = data.terraform_remote_state.persistent.outputs.secret_arns["litellm-master"]
    database_url = data.terraform_remote_state.persistent.outputs.database_url_secret_arn
  }
  artifact_bucket_name = data.terraform_remote_state.persistent.outputs.s3_bucket_ids["artifacts"]
  artifact_bucket_arn  = "arn:aws:s3:::${data.terraform_remote_state.persistent.outputs.s3_bucket_ids["artifacts"]}"
  # Stage 0.3: the grading path reads the dataset mirror (FULL schema) to build
  # the TestSpec — was silently falling back to HF after every fetch.
  dataset_bucket_name = data.terraform_remote_state.persistent.outputs.s3_bucket_ids["dataset"]
  # Review F2: eval builds the instance image FROM this ECR env image repo.
  env_image_repository = data.terraform_remote_state.persistent.outputs.ecr_repository_urls["harness-worker"]
  # ADR-0039: eval worker reads operator control state + live progress from the
  # same Valkey the gateway uses (this tier's cache).
  redis_endpoint = local.redis_endpoint
}

# --- 5b: the warm-job ONE-SHOT task (privileged EC2 pool) ---------------------
# TASK DEFINITION only — no service, no schedule (the EventBridge cron stays
# unbuilt until the precondition gate exists). Invoked on demand via
# `aws ecs run-task`. Builds env + harness images against the host docker daemon
# (docker-sock volume), pushes everything to the ONE collocated repo (5b §1
# gate) and writes the cache manifest (cache-manifest/<swebench-version>.json)
# the precondition gate reads. (No EFS mirrors — the git-mirror image bakes
# them; review S1/U1.)
#
# Durable image pin (2026-08-27, hardened 2026-09-02): the refresh once ran a
# STALE cached warm-job:latest on a reused EC2 host, baking old code into the -hw.
# We resolve the image to a DIGEST at apply time (`data.aws_ecr_image` →
# "<repo>@sha256:..."), so ECS pulls exactly that digest and a reused host can
# never serve a different cached image.
#
# `var.framework_sha` (default "latest") is the tag we resolve. The 2026-09-02
# incident showed WHY the default matters: resolving the *mutable* "latest" at
# plan time captures whatever it pointed to when terraform last ran — if the tag
# moved after that apply, the task-def stays pinned to the OLD digest and does
# not self-heal (mistaken at the time for "ECS tag resolution stuck"). Pass the
# per-commit tag (`-var framework_sha=<sha>`, produced by docker_build_push.sh's
# immutable :<sha> tag) and the resolution is deterministic — the tag is
# immutable, so plan time cannot capture the wrong digest. Both warm-job and the
# EC2-pool eval-worker resolve through this.
data "aws_ecr_image" "warm" {
  repository_name = "${var.name_prefix}-warm-job"
  image_tag       = var.framework_sha
}

data "aws_ecr_image" "eval_worker" {
  repository_name = "${var.name_prefix}-eval-worker"
  image_tag       = var.framework_sha
}

module "warm_job" {
  source = "../../../modules/ec2-task-warm-job"

  name_prefix           = var.name_prefix
  purpose               = var.purpose
  cluster_name          = data.terraform_remote_state.persistent.outputs.cluster_name
  ec2_capacity_provider = data.terraform_remote_state.persistent.outputs.ec2_capacity_provider
  image                 = data.terraform_remote_state.persistent.outputs.ecr_repository_urls["warm-job"]
  image_digest          = data.aws_ecr_image.warm.id
  private_subnet_ids    = local.private_subnet_ids
  security_group_ids    = [local.task_sg_id]
  log_group             = "/aws/ecs/${var.name_prefix}-warm-job"
  image_repo_name       = "${var.name_prefix}-harness-worker" # the ONE collocated repo (5b §1)
  dataset_bucket_name   = data.terraform_remote_state.persistent.outputs.s3_bucket_ids["dataset"]
  # smoke-readiness-check.md: the -hw images must attest which gateway config
  # they were built against — the same sha the control-plane images carry.
  gateway_config_hash = filesha256("${path.module}/../../../../../infra/docker/litellm_config.yaml")
}

# --- Step 3 (wired in 5a-ii): the LiteLLM gateway, in the eval tier ----------
# The gateway is what every harness worker routes model calls through. Its ALB
# is public (dev); the harness workers reach it from the private subnets via the
# NAT. REDIS_URL points at the Valkey cache (ADR-0018 §4: gateway rpm/tpm rate
# limit state lives in the same serverless cluster). DATABASE_URL is LiteLLM's
# OWN spend log — ADR-0022's `litellm_spend` database, NOT the app's
# `app_control_plane` (the two differ by database name only).
module "gateway" {
  source = "../../../modules/gateway"

  name_prefix        = var.name_prefix
  purpose            = var.purpose
  vpc_id             = local.vpc_id
  private_subnet_ids = local.private_subnet_ids
  public_subnet_ids  = local.public_subnet_ids
  cluster_name       = local.cluster_name
  # Spec §1.4 (2026-09-01): 2 for blast radius — one gateway dying takes every in-flight
  # instance's accumulated spend with it (no checkpoint; restarts replay from turn 0).
  replica_count = 2
  security_group_ids = {
    alb  = [local.alb_sg_id]
    task = [local.task_sg_id]
  }
  litellm_image = data.terraform_remote_state.persistent.outputs.ecr_repository_urls["gateway"]
  secrets = {
    master       = data.terraform_remote_state.persistent.outputs.secret_arns["litellm-master"]
    openrouter   = data.terraform_remote_state.persistent.outputs.secret_arns["openrouter-api-key"]
    database_url = data.terraform_remote_state.persistent.outputs.litellm_spend_database_url_secret_arn
  }
  redis_endpoint = local.redis_endpoint
  log_group      = "/aws/ecs/${var.name_prefix}-gateway"
}

# --- builder1-gateway-stable-dns: stable name in front of the gateway ALB -----
# `module.gateway.alb_dns` is the raw ALB DNS whose numeric suffix AWS assigns and
# changes on every ALB replacement. Baking it into 40 harness task definitions
# (now ~500 after fan-out) means an ALB replacement silently invalidates them all
# (`Name or service not known` in the agent). Alias the gateway ALB to a stable
# private-DNS name `gateway.eval.internal` (same indirection principle
# `modules/network` applies to git-mirror) and have every consumer use the local.
#
# The stable name must be a CLOUD MAP SERVICE + INSTANCE, not a standalone
# route53 record (BUILDER1-GATEWAY-STABLE-DNS-RESOLUTION §3). The `eval.internal`
# namespace is Cloud Map DNS_PRIVATE — its hosted zone can only be written through
# Cloud Map, so a raw `aws_route53_record` into it is AccessDenied (I hit this).
#
# Register the ALB via a Cloud Map service: the instance's AWS_ALIAS_DNS_NAME makes
# Cloud Map create a Route53 ALIAS to the load balancer, so the ALB stays
# load-bearing (the M1.11 pause rule + ADR-0018 multi-replica both survive — Cloud
# Map does NOT bypass the ALB here; only the register-task-IPs shape does).
#
# RoutingPolicy MUST be WEIGHTED for an ELB alias — git-mirror's MULTIVALUE will
# not work for this (Cloud Map silently won't alias a load balancer under it).
resource "aws_service_discovery_service" "gateway" {
  name = "gateway"

  dns_config {
    namespace_id = data.terraform_remote_state.persistent.outputs.internal_dns_namespace_id
    dns_records {
      ttl  = 60
      type = "A"
    }
    # MANDATORY for an ELB alias — git-mirror's MULTIVALUE will not work here.
    routing_policy = "WEIGHTED"
  }

  tags = {
    Name    = "gateway.eval.internal"
    purpose = var.purpose
  }
}

resource "aws_service_discovery_instance" "gateway_alb" {
  instance_id = "gateway-alb"
  service_id  = aws_service_discovery_service.gateway.id

  attributes = {
    # Cloud Map creates the Route53 ALIAS to the ALB. No AWS_INSTANCE_* alongside.
    AWS_ALIAS_DNS_NAME = module.gateway.alb_dns
  }
}

locals {
  gateway_base_url = "http://gateway.eval.internal:4000/v1"
}

# --- Stage 2.2: the git mirror service (VPC-internal, Cloud Map DNS) ----------
# Serves the baked bare repos read-only at http://git-mirror.eval.internal so
# the harness (runtime gitconfig) and the eval-side instance build (§2.1 layer)
# clone with NO internet. Name resolves via Cloud Map in the network (durable)
# module; this is the on-demand Fargate service behind it. Dev keeps 1 replica
# so the mirror is reachable for any run (256/512 ~ $0.04/hr, torn down with
# this state).
module "git_mirror" {
  source = "../../../modules/ecs-service-git-mirror"

  name_prefix        = var.name_prefix
  purpose            = var.purpose
  cluster_name       = local.cluster_name
  vpc_id             = local.vpc_id
  vpc_cidr           = data.terraform_remote_state.persistent.outputs.vpc_cidr
  image              = data.terraform_remote_state.persistent.outputs.ecr_repository_urls["git-mirror"]
  private_subnet_ids = local.private_subnet_ids
  # Reviewer F4: admit the task SG AND the eval-host SG (the eval-side
  # instance-image build runs on the host bridge from the host ENI, so it must
  # be reachable by the mirror too).
  # G1 (dev/HARNESS-ISOLATION-AUDIT-2026-09-05 §3, 2026-09-05): the HARNESS SG
  # is deliberately NOT admitted any more. A1-2 / ADR-0033 admitted it so the
  # harness could clone at repo-prep; the per-instance -inst images bake the
  # prepared repo, nothing in the harness clones at runtime, and the mirror is
  # a full-history --mirror of every repo — the gold fix one `git log` away
  # from any agent that can reach it. verify_harness_isolation.py now REQUIRES
  # the clone from the harness network to fail.
  security_group_ids = [
    local.task_sg_id,
    data.terraform_remote_state.persistent.outputs.eval_host_sg_id,
  ]
  discovery_arn = data.terraform_remote_state.persistent.outputs.git_mirror_discovery_arn
  log_group     = "/aws/ecs/${var.name_prefix}-git-mirror"
  replica_count = 1
}

# --- A1-5 / review §7: operator access via SSM port-forward through the NAT ----
# Both ALBs are now internal; the owner/reviewer reach them by port-forwarding
# THROUGH the existing NAT instance ($0, no new hosts; SSM instance profile is on
# the NAT in persistent/). Builder/owner use the STANDARD
# AWS-StartPortForwardingSessionToRemoteHost document (unrestricted). The
# REVIEWER role (`eval-framework-ro`) gets CUSTOM documents with host+port
# HARDCODED: IAM cannot condition on document parameters, so the document ARN is
# the only scopeable handle — it pins the tunnel to exactly {gateway:4000,
# api:8000}, never Aurora.
data "terraform_remote_state" "ui" {
  count   = var.read_ui_state ? 1 : 0
  backend = "s3"
  config  = merge(local.remote_state, { key = "envs/dev/ui/terraform.tfstate" })
}

variable "reviewer_role_name" {
  type        = string
  default     = ""
  description = "OPTIONAL: an existing read-only reviewer role to pin to the scoped tunnel documents (review §7). Empty (the default) attaches nothing — a fresh account has no such role (adoption Phase 1b). The originating account sets 'eval-framework-scale-ro' in its tfvars."
}

locals {
  _nat_instance_arn = "arn:aws:ec2:${var.region}:${local.account_id}:instance/${data.terraform_remote_state.persistent.outputs.nat_instance_id}"
  # Adoption Phase 1b: the API ALB lives in ui/, which may not be applied yet (a
  # fresh account's first eval apply, or ui torn down). An unguarded read failed
  # the whole plan; now the api tunnel document simply does not exist until ui/
  # has been applied — re-apply eval/ afterwards to create it.
  api_alb_dns = var.read_ui_state ? try(data.terraform_remote_state.ui[0].outputs.api_alb_dns, "") : ""
}

# §2.2 (wiring review 2026-09-01): root-level passthroughs so the owner's observe->live flip
# is a tfvars change + apply — the NAT-gateway lesson: a module-only variable makes the
# documented procedure fail at the root.
variable "autoscaler_mode" {
  type        = string
  default     = "live"
  description = "Harness L2 planner mode (off|observe|live). Flipped to live by the owner 2026-09-03 for the 500-instance qwen run, after the forecast review (BUILDER4-DISPATCHER-FORECAST-REVIEW-2026-09-03.md) — fitted curves, per-alias budgets, booting-task modelling, timeout/queue back-pressure. Set to observe to publish-only again."
}

variable "autoscaler_model_alias" {
  type        = string
  default     = ""
  description = "Static fallback alias for the planner; the launched run's own alias (published at launch) wins."
}

resource "aws_ssm_document" "gateway_tunnel" {
  name            = "${var.name_prefix}-tunnel-gateway-4000"
  document_type   = "Session"
  document_format = "JSON"
  # Shape verified against the live AWS-StartPortForwardingSessionToRemoteHost
  # (2026-08-19): sessionType "Port", no mainSteps — the port-forward target is
  # top-level `properties`. Host/port are HARDCODED (parameters {}) so the
  # document ARN is the only scopeable handle: the tunnel can reach exactly the
  # gateway ALB, never anything else in the VPC (A1-5 / review §7).
  content = jsonencode({
    schemaVersion = "1.0"
    description   = "eval-framework: port-forward to the internal gateway ALB :4000 — host/port hardcoded, no parameters (A1-5)"
    sessionType   = "Port"
    parameters    = {}
    properties = {
      host            = module.gateway.alb_dns
      portNumber      = "4000"
      type            = "LocalPortForwarding"
      localPortNumber = "4000"
    }
  })
}

resource "aws_ssm_document" "api_tunnel" {
  count = local.api_alb_dns != "" ? 1 : 0

  name            = "${var.name_prefix}-tunnel-api-8000"
  document_type   = "Session"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "1.0"
    description   = "eval-framework: port-forward to the internal API/UI ALB :8000 — host/port hardcoded, no parameters (A1-5)"
    sessionType   = "Port"
    parameters    = {}
    properties = {
      host            = local.api_alb_dns
      portNumber      = "8000"
      type            = "LocalPortForwarding"
      localPortNumber = "8000"
    }
  })
}

moved {
  from = aws_ssm_document.api_tunnel
  to   = aws_ssm_document.api_tunnel[0]
}

# The reviewer-scoped StartSession policy: only these two documents + the NAT
# instance. Host/port are baked into the document ARNs, so `ssm:StartSession`
# here cannot reach Aurora or anything else in the VPC.
resource "aws_iam_policy" "reviewer_tunnel" {
  name = "${var.name_prefix}-reviewer-tunnel"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["ssm:StartSession"]
      Resource = concat(
        [aws_ssm_document.gateway_tunnel.arn],
        aws_ssm_document.api_tunnel[*].arn,
        [local._nat_instance_arn],
      )
    }]
  })
}

resource "aws_iam_role_policy_attachment" "reviewer_tunnel" {
  count = var.reviewer_role_name != "" ? 1 : 0

  role       = var.reviewer_role_name
  policy_arn = aws_iam_policy.reviewer_tunnel.arn
}

moved {
  from = aws_iam_role_policy_attachment.reviewer_tunnel
  to   = aws_iam_role_policy_attachment.reviewer_tunnel[0]
}
# Created and destroyed WITH THIS STATE — `make eval-down` (or nightly teardown)
# removes them, so their ~$0.05/hr is billed only while a harness can be
# dispatched, and persistent/ gains no standing cost (ADR-0023 §3's property
# survives; ADR-0033 §2 is what changes WHERE the endpoints live). Attached to
# the module endpoint SG (VPC-scoped 443; A1-9 — the old `aws_security_group.default`
# had no ingress and made envs/scale's endpoints decorative).
#
# The isolated harness subnets reach these via implicit `local` routing + the
# harness SG's VPC-egress allowlist; the NAT-routed subnets share them via private
# DNS. That sharing constrains the endpoint policies: a principal-scoped policy
# naming only the harness role would break every other framework service that
# resolves the same service DNS to these ENIs. ECR / Logs / Secrets Manager all
# require SIGNED IAM calls (no anonymous channel), so they are left open; SQS is
# the one worth bounding below, restricted to the framework's own queues.
locals {
  isolated_endpoint_services = toset(["ecr.api", "ecr.dkr", "logs", "sqs", "secretsmanager"])
  isolated_endpoint_policies = {
    sqs = jsonencode({
      Version = "2008-10-17"
      Statement = [{
        Effect    = "Allow"
        Principal = "*"
        Action    = "sqs:*"
        Resource = [
          # Every queue the framework reads + writes, main queues AND DLQs.
          # Missing one → the task's SQS call is denied BY THE ENDPOINT (not IAM),
          # surfacing as QueueDoesNotExist — the llm-calls gap from 2026-08-22.
          data.terraform_remote_state.persistent.outputs.queue_arns["harness-jobs"],
          data.terraform_remote_state.persistent.outputs.queue_arns["eval-jobs"],
          data.terraform_remote_state.persistent.outputs.queue_arns["results"],
          data.terraform_remote_state.persistent.outputs.queue_arns["llm-calls"],
          data.terraform_remote_state.persistent.outputs.queue_arns["model-observations"],
          data.terraform_remote_state.persistent.outputs.dlq_arns["harness-jobs"],
          data.terraform_remote_state.persistent.outputs.dlq_arns["eval-jobs"],
          data.terraform_remote_state.persistent.outputs.dlq_arns["results"],
          data.terraform_remote_state.persistent.outputs.dlq_arns["llm-calls"],
          data.terraform_remote_state.persistent.outputs.dlq_arns["model-observations"],
        ]
      }]
    })
  }
}

resource "aws_vpc_endpoint" "isolated" {
  for_each = local.isolated_endpoint_services

  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [local.private_subnet_ids[0]] # one AZ (C-2 / review §9: keep 1 AZ)
  security_group_ids  = [local.endpoint_sg_id]
  private_dns_enabled = true
  policy              = lookup(local.isolated_endpoint_policies, each.key, null)

  tags = {
    Name    = "${var.name_prefix}-vpce-${each.key}"
    purpose = var.purpose
  }
}
# Replaces the long-running harness-worker SERVICE (which could not run per-env
# images) with a small Fargate dispatcher that reads harness-jobs and launches
# one task per job against the H2 families (module harness_task_families above).
# The launched task — running the per-env `-hw` image — owns the receipt handle
# and deletes the message on success; at-least-once delivery is unchanged.
module "harness_dispatcher" {
  source = "../../../modules/ecs-service-harness-dispatcher"

  name_prefix  = var.name_prefix
  purpose      = var.purpose
  cluster_name = local.cluster_name
  image        = data.terraform_remote_state.persistent.outputs.ecr_repository_urls["orchestrator"]
  # The DISPATCHER itself keeps the normal network (it runs our code).
  private_subnet_ids = local.private_subnet_ids
  security_group_ids = [local.task_sg_id]
  # A1/ADR-0033: the launched HARNESS task goes into the ISOLATED network. This
  # is the enforcement point — there is no other subnet set a harness task can
  # be launched into. The dispatcher asserts at startup (against LIVE route/SG
  # state) that neither carries a 0.0.0.0/0 and refuses dispatch otherwise.
  harness_subnet_ids         = local.harness_isolated_subnet_ids
  harness_security_group_ids = [local.harness_sg_id]
  log_group                  = "/aws/ecs/${var.name_prefix}-harness-worker"
  harness_queue_url          = data.terraform_remote_state.persistent.outputs.queue_urls["harness-jobs"]
  harness_queue_arn          = data.terraform_remote_state.persistent.outputs.queue_arns["harness-jobs"]
  results_queue_arn          = data.terraform_remote_state.persistent.outputs.queue_arns["results"]
  # §6.6: the autoscaler's observation events go to their own queue, never Aurora directly.
  model_observations_queue_arn = data.terraform_remote_state.persistent.outputs.queue_arns["model-observations"]
  # §2.2: the observe->live flip is a tfvars change, never a code edit.
  autoscaler_mode        = var.autoscaler_mode
  autoscaler_model_alias = var.autoscaler_model_alias
  # ADR-0039: the dispatcher reads operator control state from this tier's Valkey.
  redis_endpoint = local.redis_endpoint
  # H4: admission ceiling — the STATIC safety cap (min-wins with the per-run
  # max_parallel_harness_tasks and, in live mode, the L2 planner's dynamic ceiling
  # — the planner is what actually sizes the fleet). 2026-09-03 forecast review:
  # 50 -> 145 for the 500-instance run. 145 x 1 vCPU harness tasks is the measured
  # wall under the account's Fargate On-Demand quota (200 vCPU, verified via
  # service-quotas; TPM-ceiling design §369: "~200 vCPU -> ~145 harness tasks before
  # a hard wall") with the orchestrator/gateway/dispatcher services on the same
  # quota; going higher needs a quota increase first, or RunTask returns capacity
  # errors the dispatcher backs off on (RunTaskCapacityError) but cannot fix.
  # (Scaling review 2026-09-03: was 150, five above the wall — the last launches at
  # full fleet would have churned on capacity errors.)
  max_concurrent_harness_tasks = 145
  # The family tasks run under TWO roles now (A1-8 exec/task split) — the
  # dispatcher must PassRole both for RunTask.
  family_role_arns = [
    module.harness_task_families.task_role_arn,
    module.harness_task_families.execution_role_arn,
  ]
}

# --- ADR-0030 H2: per-env harness task-definition families (task-per-instance) -
# ECS cannot override a task's image at run-task time, so the dispatcher (H3)
# picks a PRE-REGISTERED family per job's env hash. The family set is derived
# from the warm-cache manifest by scripts/gen_harness_task_families.py into
# harness-task-families.auto.tfvars.json — one family per env image, each
# pointing at its `-hw` tag. Task definitions only: no service, so nothing runs
# them until H3's dispatcher exists (they cannot double-process the queue).
module "harness_task_families" {
  source = "../../../modules/ecs-task-def-harness-family"

  name_prefix          = var.name_prefix
  purpose              = var.purpose
  families             = var.harness_task_families
  gateway_base_url     = local.gateway_base_url
  redis_endpoint       = local.redis_endpoint
  artifact_bucket_name = data.terraform_remote_state.persistent.outputs.s3_bucket_ids["artifacts"]
  artifact_bucket_arn  = "arn:aws:s3:::${data.terraform_remote_state.persistent.outputs.s3_bucket_ids["artifacts"]}"
  dataset_bucket_name  = data.terraform_remote_state.persistent.outputs.s3_bucket_ids["dataset"]
  log_group            = "/aws/ecs/${var.name_prefix}-harness-worker"
  secrets = {
    # A1-8: OPENROUTER_API_KEY no longer injected — the harness calls the gateway,
    # never OpenRouter, and no framework code reads the key.
    master = data.terraform_remote_state.persistent.outputs.secret_arns["litellm-master"]
  }
  queue_arns = {
    harness   = data.terraform_remote_state.persistent.outputs.queue_arns["harness-jobs"]
    eval      = data.terraform_remote_state.persistent.outputs.queue_arns["eval-jobs"]
    results   = data.terraform_remote_state.persistent.outputs.queue_arns["results"]
    llm_calls = data.terraform_remote_state.persistent.outputs.queue_arns["llm-calls"]
  }
  queue_urls = {
    harness = data.terraform_remote_state.persistent.outputs.queue_urls["harness-jobs"]
    eval    = data.terraform_remote_state.persistent.outputs.queue_urls["eval-jobs"]
    results = data.terraform_remote_state.persistent.outputs.queue_urls["results"]
  }
}
