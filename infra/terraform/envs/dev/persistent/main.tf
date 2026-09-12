/**
 * envs/dev/persistent — durable backbone: network + Aurora + master secrets
 *
 * This is the state that is applied ONCE and destroyed deliberately and rarely.
 * It holds:
 *   - the network module (VPC /16 + /18 per AZ, subnets, SG; no EFS — review U1)
 *   - master credentials (Secrets Manager, recovery_window=0)
 *   - the ONE Aurora Serverless v2 cluster (ADR-0022) — the sole teardown
 *     exception (ADR-0014)
 *
 * DEVATION from plan §9, flagged (rule 7): the plan text put `network` in
 * `ephemeral/` and only Aurora here. That is mechanically impossible — Aurora
 * references the VPC subnets + security group, so a per-session `ephemeral/`
 * destroy would try to delete the VPC under a live cluster and fail (or orphan
 * it). The durable network backbone therefore lives in persistent/ with the
 * datastore; `ephemeral/` reads it via terraform_remote_state and destroys only
 * compute/cache/ECR/s3/workers/observability. This preserves the intent of
 * ADR-0014/ADR-0022 (destroy everything except the datastore and what it needs)
 * and keeps both destroys mechanical. Recorded in the Phase 5a summary.
 *
 * master passwords/secrets are passed via a gitignored `terraform.tfvars` (and
 * could come from the environment); they never appear as literals in this file.
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

variable "master_database_password" {
  type        = string
  description = "Aurora master password (from gitignored tfvars / env)."
  sensitive   = true
}

variable "notification_email" {
  type        = string
  description = "Where the budget alarm, the billing alarm and the nightly scale-to-zero guard send their SNS notifications. Confirm the subscription email AWS sends after the first apply."
}

# Adoption Phase 1b: two per-cycle values the dispatch Lambda used to carry as
# literals in this file (a stale -hw digest, a Valkey endpoint copied by hand
# after every eval bring-up).
variable "harness_image_digest" {
  type        = string
  default     = ""
  description = "Optional: the harness image digest recorded in config_snapshot. Empty = the dispatcher resolves the -inst digest live from ECR (it has ecr:DescribeImages), which is what actually happens on every run."
}

# The dispatch Lambda needs eval/'s Valkey endpoint (REDIS_URL). eval/ is applied
# AFTER persistent/ and torn down between sessions, so the read is guarded the
# same way ui/ guards it: "" until eval/ exists, then re-apply persistent/ (part
# of `make up`) to hand the Lambda the live endpoint.
data "terraform_remote_state" "eval" {
  count   = var.read_eval_state ? 1 : 0
  backend = "s3"
  config  = merge(local.remote_state, { key = "envs/dev/eval/terraform.tfstate" })
}

# BUILDER4-NAT-GATEWAY-IMPLEMENTATION-2026-08-29 / owner decision 2026-08-29:
# root-level passthrough so `terraform plan -var nat_gateway_enabled=...` at
# this root (the documented bring-up/bring-down procedure) can actually
# override the module's default, instead of erroring as an undeclared
# variable. Default matches the module's own default (ON for the e2e phase).
variable "nat_gateway_enabled" {
  type        = bool
  default     = true
  description = "When true, a managed NAT Gateway carries private-subnet egress; when false, the t3 NAT instance does. See modules/network's variable of the same name."
}

# Owner decision 2026-09-03: the nightly scale-to-zero guard is switched OFF for
# the duration of a scored run rather than trusted to refuse. Same passthrough
# shape as nat_gateway_enabled: `terraform plan -var nightly_scale_to_zero_paused=true`
# before the run, `=false` (the default) at teardown. Runbook §4.6 / §3.5.
# Adoption Phase 2 (fresh account, 2026-09-11): a new account's Spot vCPU quota is 5 and the
# raise can take a day, while its On-Demand quota is approved within minutes. Both EC2 pools
# can run On-Demand (≈ 2× the hourly price) until the Spot quota lands: set these false in
# terraform.tfvars, re-apply, flip back later.
variable "eval_hosts_spot" {
  type        = bool
  default     = true
  description = "Eval grading hosts on Spot (true) or On-Demand (false — while a fresh account's Spot quota is pending)."
}

variable "build_hosts_spot" {
  type        = bool
  default     = true
  description = "Image-build hosts on Spot (true) or On-Demand (false — while a fresh account's Spot quota is pending)."
}

# Adoption teardown (make teardown-all): the two guards that make `terraform destroy` refuse by
# design — Aurora's deletion protection and the durable buckets' force_destroy=false (PA-2/PA-3).
# Both stay on by default; the teardown flips them with -var for the final destroy only.
variable "aurora_deletion_protection" {
  type        = bool
  default     = true
  description = "Aurora deletion protection (PA-2). false ONLY for a deliberate full teardown of the account."
}

variable "durable_buckets_force_destroy" {
  type        = bool
  default     = false
  description = "force_destroy on the results/artifacts buckets (PA-3). true ONLY for a deliberate full teardown."
}

variable "nightly_scale_to_zero_paused" {
  type        = bool
  default     = false
  description = "When true the nightly scale-to-zero EventBridge rule is DISABLED (kept, not destroyed). Set for a scored run that spans 03:00 PDT; clear at teardown."
}

# --- Network: durable backbone ----------------------------------------------
module "network" {
  source = "../../../modules/network"

  region              = var.region
  azs                 = local.azs
  purpose             = var.purpose
  name_prefix         = var.name_prefix
  nat_enabled         = true
  nat_gateway_enabled = var.nat_gateway_enabled
  endpoint_azs        = [local.azs[0]]
  # Q-1 / ADR-0023: envs/dev provisions NO interface VPC endpoints — the owner
  # chose to drop them rather than move them (ADR-0004 justified them by the NAT
  # *Gateway* per-GB fee; this project uses a NAT *instance*, which has none).
  # The free S3 Gateway endpoint is created unconditionally by the network module
  # and stays. envs/scale keeps all interface endpoints across three AZs.
  # ADR-0033 (2026-08-19): the eval tier now creates its OWN five interface
  # endpoints (ecr.api, ecr.dkr, logs, sqs, secretsmanager), destroyed with
  # eval-down — see envs/dev/eval. persistent/ still provisions none, so its
  # no-standing-cost property is preserved.
  vpc_endpoint_services = []
  # A1-6 / ADR-0033 decision 3: the S3 gateway endpoint's policy is part of the
  # control. After isolation, S3 is the harness's only egress; an unsigned GET
  # against any public in-region bucket would still traverse the endpoint. Restrict
  # it to the framework's own buckets (results/artifacts/dataset) plus ECR's us-
  # west-2 layer storage bucket (Fargate image pulls fetch layer blobs from it via
  # the gateway endpoint — the probe's very first check is the task STARTING).
  endpoint_gateway_policy_bucket_arns = concat(
    [for _, b in module.s3.bucket_ids : "arn:aws:s3:::${b}"],
    ["arn:aws:s3:::prod-${var.region}-starport-layer-bucket"],
  )
}

# --- Master secrets (durable) ------------------------------------------------
variable "litellm_master_key" {
  type        = string
  description = "LiteLLM master key (tfvars/env)."
  sensitive   = true
}
variable "openrouter_api_key" {
  type        = string
  description = "OpenRouter provider API key (tfvars/env)."
  sensitive   = true
}

module "secrets" {
  source = "../../../modules/secrets"

  name_prefix  = var.name_prefix
  purpose      = var.purpose
  secret_names = ["aurora-master", "litellm-master", "openrouter-api-key"]
  secret_values = {
    "aurora-master"      = var.master_database_password
    "litellm-master"     = var.litellm_master_key
    "openrouter-api-key" = var.openrouter_api_key
  }
}

# --- Durable S3 (PA-3/PA-4): results + artifacts + dataset ---------------------
# These are inputs/outputs of a PUBLISHED result. force_destroy=false on
# results/artifacts (a destroy must fail loudly, not wipe); dataset is
# re-downloadable so force_destroy=true stays. All versioned. Move to
# persistent/ so a session destroy of ephemeral never erases them.
module "s3" {
  source = "../../../modules/s3"

  name_prefix = var.name_prefix
  purpose     = var.purpose
  region      = var.region
  account_id  = local.account_id
  bucket_force_destroy = {
    "artifacts" = var.durable_buckets_force_destroy # PA-3: false except a deliberate teardown
    "results"   = var.durable_buckets_force_destroy
    "dataset"   = true
  }
}

# --- Durable ECR (PA-5): all repos live in persistent/ ------------------------
# ECR storage is $0.10/GB-mo in either state; moving it here means a session
# destroy never deletes repos/images — at 5b this is what keeps the 61-tag
# warm-cache (an expensive 'one-time' build) intact across sessions.
module "ecr" {
  source = "../../../modules/ecr"

  name_prefix = var.name_prefix
  purpose     = var.purpose
}

# --- Observability (G-1): budget + billing alarm + nightly guarded scale-to-zero + log groups
# modules/observability holds 11 resources and was instantiated in ZERO envs —
# the budget alarm, billing alarm and nightly scale-to-zero would NOT exist after
# apply, and DoD 5/10 would test controls that don't exist. It is account-scoped
# and must outlive any session, so it lives in persistent/. The 6 durable log
# groups (PA-6, 90-day retention) are RELOCATED here from the raw resource below
# — the module owns them, so nothing is created twice.
#
# The nightly teardown is GUARDED (review item 2): it refuses unless Postgres has
# no pending/running runs, SQS harness-jobs+eval-jobs are empty (visible AND
# NotVisible), and no eval/ui task is RUNNING. Service ARNs are EMPTY until the
# eval/ui services exist (item 8); the budget/alarm/guard still function now.
module "observability" {
  source = "../../../modules/observability"
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name_prefix       = var.name_prefix
  region            = var.region
  purpose_tag_value = var.purpose
  # DoD 8 fire test RESULT (2026-08-15): threshold was briefly lowered to $1;
  # the us-east-1 AWS/Billing alarm flipped to ALARM (one evaluation after the
  # F-2 Currency-dimension fix), then restored here. TimeUnit=QUARTERLY in the
  # module. Both halves proven: made to fire, restored, confirmed not firing.
  purpose_dev_threshold_usd = 25

  # Durable log groups (PA-6): 90-day retention, owned by the module.
  # CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md: orchestrator-control-plane
  # is retired, replaced by run-supervisor (singleton) + results-writer (N=2).
  log_group_names = [
    "/aws/ecs/${var.name_prefix}-orchestrator-api",
    "/aws/ecs/${var.name_prefix}-run-supervisor",
    "/aws/ecs/${var.name_prefix}-results-writer",
    "/aws/ecs/${var.name_prefix}-harness-worker",
    "/aws/ecs/${var.name_prefix}-eval-worker",
    "/aws/ecs/${var.name_prefix}-gateway",
    "/aws/ecs/${var.name_prefix}-prometheus", # PA-8: service must log to a DECLARED group
    "/aws/ecs/${var.name_prefix}-warm-job",   # 5b: the warm job's one-shot task logs here
    "/aws/ecs/${var.name_prefix}-git-mirror", # 2.2: the git mirror service logs here
    # builder5-image-build-tier Stage 2: the build tier's own one-shot task,
    # registered under a DIFFERENT family (eval-dev-image-build) so it coexists
    # with the warm-job family above rather than fighting over it.
    "/aws/ecs/${var.name_prefix}-image-build",
  ]
  log_retention_days = 90

  # Nightly guarded scale-to-zero (dev only). Service ARNs are COMPUTED here —
  # they are deterministic (cluster + service name), so persistent/ can name the
  # eval/ui services before those states exist, with no cross-state reference.
  # The guard owns the IAM statement against these ARNs and the actual scaling.
  nightly_scale_to_zero_enabled = true
  nightly_scale_to_zero_paused  = var.nightly_scale_to_zero_paused
  # 2026-08-22 correction. This list was wrong in BOTH directions and the guard
  # could not tell you, because _idle_ecs short-circuits on the first busy
  # service and orchestrator-api is always first:
  #   - "${var.name_prefix}-custom_minimal" has never existed as a service. It would have
  #     raised ServiceNotFoundException on the first genuinely-idle night — the
  #     one night the guard could have succeeded. (The Lambda now treats an
  #     absent service as idle, so a future rename degrades instead of wedging.)
  #   - harness-dispatcher and git-mirror were MISSING, so a "successful"
  #     scale-to-zero left two services running all night.
  # Verified against the live cluster before the 05:23 UTC teardown: the six
  # services that existed were orchestrator-api, orchestrator-control-plane,
  # harness-dispatcher, eval-worker, gateway, git-mirror.
  #
  # CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md: orchestrator-control-plane
  # is retired, replaced by run-supervisor (singleton) + results-writer (N=2) —
  # both still ui-tier, same reason the retired singleton was.
  ecs_services_to_scale_to_zero = [
    for svc in [
      "orchestrator-api",   # ui tier
      "run-supervisor",     # ui tier (control-plane singleton: heartbeat + reaper 2/3)
      "results-writer",     # ui tier (results consume loop + reaper 1, N=2)
      "gateway",            # eval tier
      "eval-worker",        # eval tier
      "harness-dispatcher", # eval tier
      "git-mirror",         # eval tier
    ] : "arn:aws:ecs:${var.region}:${local.account_id}:service/${module.ecs_cluster.cluster_name}/${svc}"
  ]
  notification_email = var.notification_email

  # Guard inputs (item 2): the Lambda refuses to tear down unless all idle.
  guard_db_cluster_arn = module.database.cluster_arn
  # DoD 5 fix: the guard's AWAKE-branch queries via the RDS Data API, which
  # needs a JSON username/password secret — the bare-password aurora-master
  # made every awake night error into a permanent refusal.
  guard_db_master_secret_arn = aws_secretsmanager_secret.guard_data_api.arn
  guard_db_name              = "app_control_plane"
  guard_queue_urls           = [module.sqs.queue_urls["harness-jobs"], module.sqs.queue_urls["eval-jobs"]]
  guard_queue_arns           = [module.sqs.queue_arns["harness-jobs"], module.sqs.queue_arns["eval-jobs"]]
  guard_ecs_cluster_name     = module.ecs_cluster.cluster_name
  # A-6 (closed): the guard can now zero the eval host ASG too — the one
  # resource the 61-image build and the eval fleet run on, previously out of
  # its reach. Zeroed behind the same in-flight checks as the services.
  #
  # builder5-image-build-tier Stage 2: the build ASG (module.ecs_cluster_build,
  # a SEPARATE cluster below) is DELIBERATELY NOT added here. The guard's
  # `_idle_cluster_tasks` check (scale_to_zero_lambda/index.py) reads exactly
  # ONE cluster name (`ECS_CLUSTER`, this one, eval-dev-cluster) before zeroing
  # every ASG in `guard_asg_arns` — adding the build ASG here without teaching
  # the Lambda to check the BUILD cluster's tasks too would let the guard zero
  # a build host while a shard is actively running on it (the Lambda genuinely
  # cannot see it). Extending the guard safely needs a Lambda code change
  # (per-ASG cluster, not one shared cluster for all), which is a change to
  # shared safety-critical code outside this builder's scope to make
  # unilaterally — flagged for the owner. Until then the build ASG is
  # scaled up/down manually around each run (docs/runbooks/dev-env-bring-up-
  # and-tear-down.md §8).
  guard_asg_arns = [module.ecs_cluster.eval_asg_arn]
}

# --- Aurora: one cluster, two databases (ADR-0022) ---------------------------
module "database" {
  source = "../../../modules/database"

  name_prefix                = var.name_prefix
  purpose                    = var.purpose
  vpc_id                     = module.network.vpc_id
  subnet_ids                 = module.network.private_subnet_ids
  master_password_secret_arn = module.secrets.secret_arns["aurora-master"]
  # Autoscaler spec §1.4 (2026-09-01): raised from the module default (4 ACU) as INSURANCE
  # against a slow scale-up during a spend-log flush stall — not because steady-state
  # throughput needs it (both write paths batch; ~2 MB/s of batched inserts fits 4 ACU).
  # Serverless v2 bills by ACTUAL use, so a higher ceiling costs nothing while idle.
  max_capacity        = 16
  skip_final_snapshot = true                           # dev only (safe because PA-2 sets 7-day backups)
  deletion_protection = var.aurora_deletion_protection # PA-2: protects the publishable record
}

# --- database-url secret: computed AFTER Aurora exists (from the endpoint) -----
# This is the one secret that can only exist post-cluster. The control-plane and
# gateway read DATABASE_URL from it (ADR-0022: one endpoint, two databases, two
# roles — secrets change, app code does not).
resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.name_prefix}-database-url"
  recovery_window_in_days = 0
  tags = {
    Name    = "${var.name_prefix}-database-url"
    purpose = var.purpose
  }
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql://eval:${var.master_database_password}@${module.database.endpoint}:5432/app_control_plane"
}

# 5a-ii: LiteLLM's spend log must write to ADR-0022's `litellm_spend` database,
# NOT the control-plane's `app_control_plane`. ADR-0022 says the two connection
# strings differ ONLY by database name. The gateway's config reads it from the
# DATABASE_URL env (the sealed-door principle: never bake the compose
# litellm-db host into the image and then wonder why writes vanish).
resource "aws_secretsmanager_secret" "litellm_spend_database_url" {
  name                    = "${var.name_prefix}-litellm-spend-database-url"
  recovery_window_in_days = 0
  tags = {
    Name    = "${var.name_prefix}-litellm-spend-database-url"
    purpose = var.purpose
  }
}

resource "aws_secretsmanager_secret_version" "litellm_spend_database_url" {
  secret_id     = aws_secretsmanager_secret.litellm_spend_database_url.id
  secret_string = "postgresql://eval:${var.master_database_password}@${module.database.endpoint}:5432/litellm_spend"
}

# --- guard Data-API secret (DoD 5, 2026-08-16) --------------------------------
# The nightly scale-to-zero Lambda's awake check queries the `runs` table via
# the RDS Data API, which requires the secret to be a JSON object with
# `username`/`password` — NOT the bare-password `aurora-master` secret it was
# pointed at. With the wrong secret the awake branch always threw
# InvalidSecretException and fail-safed into permanent refusal every awake
# night. This secret gives the Data API the shape it needs; the guard is the
# only consumer. (The paused branch never touches the Data API — S-2
# short-circuits on the capacity metric, which is why DoD 5's paused-side test
# passed before this fix landed.)
resource "aws_secretsmanager_secret" "guard_data_api" {
  name                    = "${var.name_prefix}-guard-data-api"
  recovery_window_in_days = 0
  tags = {
    Name    = "${var.name_prefix}-guard-data-api"
    purpose = var.purpose
  }
}

resource "aws_secretsmanager_secret_version" "guard_data_api" {
  secret_id = aws_secretsmanager_secret.guard_data_api.id
  secret_string = jsonencode({
    username = "eval"
    password = var.master_database_password
  })
}

# --- Security groups (PA-10): ~free, shared by ui + eval, so durable ----------
module "security_groups" {
  source = "../../../modules/security-groups"

  name_prefix = var.name_prefix
  vpc_id      = module.network.vpc_id
  vpc_cidr    = module.network.vpc_cidr
  region      = var.region
  purpose     = var.purpose
}

# --- ADR-0024 dispatch Lambda (durable: the trigger + the record) -------------
# The S3-dropped run config → register_run()/dispatch_run() trigger. VPC-attached
# (private subnets + task SG) = the existing psycopg path to Aurora, reuse no
# Data API. The reserved-concurrency fan-out guard is inside the module.
module "dispatch" {
  source = "../../../modules/dispatch-lambda"

  name_prefix = var.name_prefix
  purpose     = var.purpose
  # Lambda's image_uri requires an explicit tag (a bare repo URL is rejected as
  # "Source image ... is not valid"). ECS accepts a bare URL; Lambda does not.
  lambda_image            = "${module.ecr.repository_urls["dispatch"]}:latest"
  results_bucket_name     = module.s3.bucket_names["results"]
  results_bucket_arn      = "arn:aws:s3:::${module.s3.bucket_names["results"]}"
  harness_queue_arn       = module.sqs.queue_arns["harness-jobs"]
  database_url_secret_arn = aws_secretsmanager_secret.database_url.arn
  # ADR-0030 review R2: the dispatcher reads the warm-cache manifest + the
  # pinned dataset mirror — both live in the dataset bucket.
  dataset_bucket_name    = module.s3.bucket_names["dataset"]
  private_subnet_ids     = module.network.private_subnet_ids
  task_security_group_id = module.security_groups.task_security_group_id
  # Window-lookup + reproducibility. LITELLM_BASE_URL defaults to the stable
  # gateway name (no override needed). litellm_master_key comes from the same
  # tfvars the secrets use; harness_image_digest is the current -hw digest
  # (D-3: config_snapshot records it for reproducibility; update on rebuild).
  litellm_master_key   = var.litellm_master_key
  harness_image_digest = var.harness_image_digest
  # REDIS_URL: the dispatch lambda's register_run() -> control_state.publish_from_db()
  # -> _write_flags() writes operator pause flags to Valkey, so it NEEDS Redis.
  # It cannot use cache.eval.internal (the CNAME fails rediss TLS hostname
  # validation — the cert is *.cache.amazonaws.com), so it must be the concrete
  # enpdoint, which changes per cycle. Persistent doesn't know eval's cache, so
  # this is a per-cycle value like harness_image_digest — update on bring-up.
  redis_url = var.read_eval_state ? try(data.terraform_remote_state.eval[0].outputs.redis_endpoint, "") : ""
  # D-3: ecr:DescribeImages on the harness-worker repo so the dispatcher resolves
  # the -inst/-hw digest live for config_snapshot (a permission, not a value).
  harness_image_repo_arn = "arn:aws:ecr:${var.region}:${local.account_id}:repository/${var.name_prefix}-harness-worker"
}

# --- SQS (PA-10): idle SQS costs nothing — durable with the datastore ---------
module "sqs" {
  source = "../../../modules/sqs"

  name_prefix = var.name_prefix
  purpose     = var.purpose
}

# --- ECS cluster (PA-10): an EMPTY cluster costs nothing — durable ------------
# Both tiers (ui + eval) register services into this one cluster.
module "ecs_cluster" {
  source = "../../../modules/ecs-cluster"

  name_prefix        = var.name_prefix
  purpose            = var.purpose
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  # 2026-09-01 (BUILDER4-EVAL-AUTOSCALER decisions, owner-approved): the eval autoscaler
  # (decision B — owns both levels itself; managed scaling stays DISABLED) needs a rail above 1
  # to do anything. 4 is a conservative interim matching the observe-mode default max_workers;
  # the REAL ceiling value stays blocked on the e2e's resource_usage.json data (ADR-0006: never
  # inferred). The scaler can only ever raise desired WITHIN this rail; nightly teardown and
  # asg_desired=0 semantics are unchanged.
  # 2026-09-03 (forecast review, 500-instance run): 4 -> 16. This is the RAIL only; the eval
  # autoscaler (live in ui/) raises desired within it and idle-terminates hosts, and nightly
  # teardown still scales to 0.
  # BUILDER4-EVAL-PACKING-2026-09-03 (owner decision B): the "1 grade/host, a grade drives all
  # 8 cores" reading that set 16 was wrong — 31 sampled grades show 0.2–1.2 cores average
  # (one brief 8-core spike) and ≤ 1.8 GB RSS, so the eval task reservation (2048/3584) now
  # packs FOUR grades per c5d.2xlarge and ui/ runs EVAL_TASKS_PER_HOST=4, EVAL_MAX_WORKERS=48
  # (12 hosts). 16 stays as headroom for tuning tasks_per_host; hosts exist only while the
  # eval queue has demand.
  asg_max_size = 16
  asg_desired  = 0 # eval host is scaled up by eval/ (PA-10); empty by default
  # Stage 1 (agreed-architecture-changes §1): docker's data-root is on the
  # instance-store NVMe via Bottlerocket's native ephemeral-storage API
  # (bootstrap-commands in the LT user_data), so the 'd' premium is used again
  # and the grown-EBS workaround is retired (asg_data_volume_size_gb -> 20).
  # c5d.2xlarge = 2x150 GB NVMe (RAID-0'd by `apiclient ephemeral-storage init`).
  # BUILDER4-EVAL-PACKING-2026-09-03: spot alternates of the SAME shape (8 vCPU, ≥16 GiB,
  # NVMe instance store — `ephemeral-storage init` handles one disk or two) so a thin
  # c5d.2xlarge spot pool cannot stall the fleet at `no_hosts`. Spot in us-west-2 on
  # 2026-09-03: c5d.2xlarge $0.15–0.18, c6id.2xlarge $0.15–0.16, m5d.2xlarge (32 GiB)
  # $0.14–0.21. The eval task reservation + EVAL_TASKS_PER_HOST are sized for the
  # smallest (16 GiB); m5d's extra memory is simply unused headroom.
  asg_instance_types = ["c5d.2xlarge", "c6id.2xlarge", "m5d.2xlarge"]
  asg_spot           = var.eval_hosts_spot # default true: Spot (an interruption mid-grade = one redelivered re-grade)
}

# --- Image-build tier: a SEPARATE ECS cluster (builder5-image-build-tier ------
# Stage 2, revised). A second capacity provider on aws_ecs_cluster.main was the
# brief's original shape, but that resource's provider list already contains
# aws_ecs_capacity_provider.ec2, which itself references aws_autoscaling_group.
# eval — so ANY edit to the shared list forces Terraform to re-evaluate that
# whole dependency chain, including the eval ASG. Live drift found while
# planning that approach (min_size/desired 1/1 live vs 0/0 in config — builder 1
# holding a host up for their own retry) would have been "corrected" as a side
# effect, terminating their host. A wholly separate cluster has its own
# capacity-provider-list resource with ZERO reference to anything in
# aws_ecs_cluster.main's graph, so nothing here can ever touch the eval ASG.
# ADDITIVE: envs/dev/eval's `module "warm_job"` keeps using the ORIGINAL
# cluster/ASG until this tier is proven (T1-T6) and the owner signs off on
# removing the old path.
module "ecs_cluster_build" {
  source = "../../../modules/ecs-cluster-build"

  name_prefix        = var.name_prefix
  purpose            = var.purpose
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  # Build HOSTS on public IPs (owner decision 2026-09-06, before the 500-image
  # build): the host docker daemon's Hub pulls (~1 TB of official base images)
  # and ECR pushes then bypass NAT entirely. See var.public_hosts in the module
  # for the traffic analysis; the launcher puts the task ENI in the private
  # subnet of the host's AZ.
  public_subnet_ids = module.network.public_subnet_ids
  public_hosts      = true

  # max_size=4 covers "prove at n=2, run at n=3 or n=4" (builder5 brief §3).
  # desired stays 0 — an operator (or envs/dev/build tooling) scales it up only
  # around an actual build run, back to 0 after (§8 of the runbook).
  asg_max_size = 4
  asg_desired  = 0
  # c5d.4xlarge (2026-09-04, owner decision, supersedes the c5d.2xlarge
  # default below): a live 17-instance matplotlib test at INSTANCE_MAX_WORKERS
  # raised to 8 (host RAM formula computed 7 actual workers, not the nominal
  # cap) measured 728.7s/instance avg -- essentially IDENTICAL to
  # c5d.2xlarge's 742s/instance at W=3 -- while running 7 concurrent builds
  # instead of 3. ~2.38x per-host throughput, no OOM, no disk pressure (400GB
  # NVMe vs c5d.2xlarge's 200GB -- the reviewer correction below re: disk
  # safety, 2026-08-27/commit 11a6dec, still holds: same family, larger NVMe,
  # not smaller). Drops hosts needed for the 500-image/2h target from ~8-10
  # c5d.2xlarge to ~4-5 c5d.4xlarge, fitting inside asg_max_size=4 above with
  # little to no bump needed. c5d.2xlarge is the longer-proven prior default
  # (disk-safety history, T1-validated at W=3) -- kept below, commented, as
  # the fallback if c5d.4xlarge ever needs reverting.
  #   instance_types = ["c5d.2xlarge"]
  instance_types = ["c5d.4xlarge"]
  asg_spot       = var.build_hosts_spot # default true: builds are restartable via ECR (resume-from-ECR-state)
}

output "aurora_endpoint" {
  value = module.database.endpoint
}
output "s3_bucket_ids" {
  value = module.s3.bucket_ids
}
output "ecr_repository_urls" {
  value = module.ecr.repository_urls
}
output "log_group_names" {
  value = [
    "/aws/ecs/${var.name_prefix}-orchestrator-api",
    "/aws/ecs/${var.name_prefix}-run-supervisor",
    "/aws/ecs/${var.name_prefix}-results-writer",
    "/aws/ecs/${var.name_prefix}-harness-worker",
    "/aws/ecs/${var.name_prefix}-eval-worker",
    "/aws/ecs/${var.name_prefix}-gateway",
    "/aws/ecs/${var.name_prefix}-prometheus",
    "/aws/ecs/${var.name_prefix}-warm-job",
  ]
}
output "task_security_group_id" {
  value = module.security_groups.task_security_group_id
}
output "alb_security_group_id" {
  value = module.security_groups.alb_security_group_id
}
output "cache_security_group_id" {
  value = module.security_groups.cache_security_group_id
}
output "harness_security_group_id" {
  value = module.security_groups.harness_security_group_id
}
output "harness_isolated_subnet_ids" {
  value = module.network.harness_isolated_subnet_ids
}
output "harness_isolated_subnet_cidrs" {
  value = module.network.harness_isolated_subnet_cidrs
}
output "harness_isolated_route_table_id" {
  value = module.network.harness_isolated_route_table_id
}
output "endpoint_security_group_id" {
  value = module.network.endpoint_security_group_id
}
output "nat_instance_id" {
  value = module.network.nat_instance_id
}
output "nat_gateway_id" {
  value = module.network.nat_gateway_id
}
output "cluster_name" {
  value = module.ecs_cluster.cluster_name
}
output "eval_host_sg_id" {
  value = module.ecs_cluster.eval_host_sg_id
}
# §2.3 (wiring review 2026-09-01): the eval autoscaler (run-supervisor daemon) actuates
# this ASG by name — ui/ reads it from remote state.
output "eval_asg_name" {
  value = module.ecs_cluster.eval_asg_name
}

output "ec2_capacity_provider" {
  value = module.ecs_cluster.ec2_capacity_provider
}
# builder5-image-build-tier Stage 2: the build tier's own cluster + capacity
# provider — envs/dev/build reads these via terraform_remote_state to
# instantiate the new warm-job task definition against the SEPARATE build
# cluster, not eval-dev-cluster above.
output "build_cluster_name" {
  value = module.ecs_cluster_build.cluster_name
}
output "build_ec2_capacity_provider" {
  value = module.ecs_cluster_build.ec2_capacity_provider
}
output "build_asg_arn" {
  value = module.ecs_cluster_build.build_asg_arn
}
output "build_asg_name" {
  value = module.ecs_cluster_build.build_asg_name
}
output "queue_urls" {
  value = module.sqs.queue_urls
}
output "queue_arns" {
  value = module.sqs.queue_arns
}
# #9 (aws-wiring brief): the sqs module exports DLQ ARNs but the persistent root
# never forwarded them — so ui/ (the orchestrator's SqsScoped policy) could not
# grant dead-letter reads. Forward them now; keys match the main queues.
output "dlq_arns" {
  value = module.sqs.dlq_arns
}
output "db_security_group_id" {
  value = module.database.db_security_group_id
}
output "aurora_reader_endpoint" {
  value = module.database.reader_endpoint
}
output "vpc_id" {
  value = module.network.vpc_id
}
output "private_subnet_ids" {
  value = module.network.private_subnet_ids
}
output "public_subnet_ids" {
  value = module.network.public_subnet_ids
}
output "default_security_group_id" {
  value = module.network.default_security_group_id
}
output "vpc_cidr" {
  value = module.network.vpc_cidr
}
output "git_mirror_discovery_arn" {
  value = module.network.git_mirror_discovery_arn
}
output "git_mirror_host" {
  value = module.network.git_mirror_host
}
# Stable-name indirection for internal ALBs (builder1-gateway-stable-dns): re-export
# the eval.internal private hosted zone id so the eval tier can alias its gateway ALB
# at `gateway.eval.internal` independent of the ALB's AWS-assigned numeric suffix.
output "internal_dns_zone_id" {
  value       = module.network.internal_dns_zone_id
  description = "Route53 private hosted zone id for eval.internal — for alias records to internal ALBs."
}
# And the Cloud Map NAMESPACE id (see network output): Cloud Map services (which the
# gateway alias requires) reference the namespace by .id, not the hosted zone id.
output "internal_dns_namespace_id" {
  value       = module.network.internal_dns_namespace_id
  description = "Cloud Map namespace id for eval.internal — for services that alias internal ALBs (gateway)."
}
output "secret_arns" {
  value = { for k, v in module.secrets.secret_arns : k => v } // includes aurora-master, litellm-master, openrouter-api-key
}
output "database_url_secret_arn" {
  value = aws_secretsmanager_secret.database_url.arn
}
output "litellm_spend_database_url_secret_arn" {
  value = aws_secretsmanager_secret.litellm_spend_database_url.arn
}
