/**
 * envs/dev/build — the image-build tier (builder5-image-build-tier Stage 2).
 * See providers.tf for the why. Reads the durable backbone (VPC, subnets, the
 * ECS cluster, the build-only capacity provider, ECR, S3) from ../persistent
 * via terraform_remote_state — same pattern as envs/dev/eval.
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

data "terraform_remote_state" "persistent" {
  backend = "s3"
  config  = merge(local.remote_state, { key = "envs/dev/persistent/terraform.tfstate" })
}

locals {
  private_subnet_ids = data.terraform_remote_state.persistent.outputs.private_subnet_ids
  # The task's OWN ENI security group (awsvpc mode) — same one envs/dev/eval's
  # warm_job/eval_worker modules use, NOT eval_host_sg_id (that one belongs to
  # the EC2 INSTANCE itself, wired via the launch template in modules/ecs-cluster).
  task_sg_id = data.terraform_remote_state.persistent.outputs.task_security_group_id
}

# --- The build-tier warm-job task definition ---------------------------------
# Same module as envs/dev/eval's `module "warm_job"`, a DIFFERENT family
# (task_family_suffix = "image-build") so the two coexist — this is Stage 2's
# whole point: an independently-sized EC2 pool, without touching the original
# path builder 1 uses today (brief §4 Stage 2 "the family name must differ").
#
# Digest pin (2026-09-02): this task BUILDS the -inst images, and it runs on the
# EC2 build pool — so an unpinned warm-job:latest is exactly where a reused host
# serves stale cached code and bakes it into every -inst image (the reported
# staleness). Resolve to a DIGEST at apply time, same pattern as envs/dev/eval.
# `var.framework_sha` defaults to "latest" (non-breaking); pass the per-commit
# tag for a deterministic pin the mutable :latest cannot make stale.
data "aws_ecr_image" "warm" {
  repository_name = "${var.name_prefix}-warm-job"
  image_tag       = var.framework_sha
}

# Docker Hub credentials for `--base official` pulls (dev/leftover-fixes.md
# OPS-10): created out-of-band, REFERENCED here, never managed by terraform —
# a resource would put the PAT into tfvars/state.
data "aws_secretsmanager_secret" "dockerhub" {
  name = "ecr-pullthroughcache/${var.name_prefix}-dockerhub"
}

module "image_build" {
  source = "../../../modules/ec2-task-warm-job"

  dockerhub_secret_arn = data.aws_secretsmanager_secret.dockerhub.arn

  name_prefix           = var.name_prefix
  purpose               = var.purpose
  cluster_name          = data.terraform_remote_state.persistent.outputs.build_cluster_name
  ec2_capacity_provider = data.terraform_remote_state.persistent.outputs.build_ec2_capacity_provider
  # Same warm-job image as envs/dev/eval's module — it already carries BOTH
  # entrypoints (`warm-job` -> warm_image_cache.py, `phase0-instances` ->
  # build_phase0_instances.py, see infra/docker/entrypoint.sh); no new image
  # needed for this tier.
  image               = data.terraform_remote_state.persistent.outputs.ecr_repository_urls["warm-job"]
  image_digest        = data.aws_ecr_image.warm.id
  private_subnet_ids  = local.private_subnet_ids
  security_group_ids  = [local.task_sg_id]
  log_group           = "/aws/ecs/${var.name_prefix}-image-build"
  image_repo_name     = "${var.name_prefix}-harness-worker" # the ONE collocated repo (5b §1), unchanged
  dataset_bucket_name = data.terraform_remote_state.persistent.outputs.s3_bucket_ids["dataset"]
  gateway_config_hash = filesha256("${path.module}/../../../../../infra/docker/litellm_config.yaml")

  task_family_suffix = "image-build"

  # Reservation fix (brief §4 Stage 2 item 5): size cpu/memory to ~the whole
  # build host so ECS can place only ONE shard per host — a reservation that
  # tells the truth, not a distinctInstance placement constraint that would
  # hide it instead.
  #
  # Instance-type correction (2026-08-27, reviewer addendum): the brief's §3
  # m5d.2xlarge recommendation was a guess against an ASSUMED memory-OOM risk.
  # The build host is ALREADY c5d.2xlarge, chosen deliberately after a REAL,
  # RECORDED disk-full (commit 11a6dec: 5 env images + build cache filled a
  # c5d.xlarge's 75 GB NVMe, prune only freed 1.4->6 GB) — disk, not memory,
  # is the actual documented failure mode, and it was already fixed by this
  # instance size. Stage 1 (builder5-image-build-response.md) measured on this
  # exact type: single-build peak host memory ~2.7 GB of ~15.24 GiB registered
  # (light env, no C-compile) and disk headroom never remotely tight (170+ GB
  # free throughout). c5d.2xlarge is kept as the baseline rather than upsizing
  # to m5d.2xlarge on an unverified guess; T1 (matplotlib, W=4, the actual
  # C-compile stress case) is what proves or overturns this, not this comment.
  # If T1 shows memory pressure, `instance_types` above is a one-line change.
  #
  # task_memory is sized for c5d.2xlarge's ~16 GiB (NOT the 32 GiB m5d.2xlarge
  # figure this replaced) — Bottlerocket registers somewhat under nominal
  # capacity (observed 15.24 GiB total via /proc/meminfo in Stage 1); 14000
  # MiB leaves headroom under that AND still forces single-shard placement
  # (>50% of host memory). Verify against the live container instance's actual
  # `registeredResources` before T1/T2 (`aws ecs describe-container-instances
  # ... --query containerInstances[0].registeredResources`) and adjust here if
  # the observed numbers don't leave enough margin.
  task_cpu    = "7680"  # 8192 nominal (8 vCPU) - 512 headroom
  task_memory = "14000" # of ~15.24 GiB registered (observed, c5d.2xlarge)
}

output "image_build_task_definition_arn" {
  value = module.image_build.task_definition_arn
}
