/**
 * envs/dev/ui — the always-on UI tier (PA-10/PA-11 three-tier split)
 *
 * Browsing historical results must NOT require the eval fleet. This tier
 * (ALB + orchestrator-api + SPA hosting) is `$0` when nothing runs and near-free
 * to leave always-on (the ALB + one small task). It reads the durable backbone
 * from `../persistent` (Aurora, S3 `results`/`artifacts`, secrets, log groups,
 * the ECS cluster, SGs) via terraform_remote_state — the SAME pattern eval/ uses.
 *
 * It deliberately does NOT host the gateway/workers/Prometheus (all in eval/).
 * It DOES read Valkey (ElastiCache) for operator CONTROL state — ADR-0039: the
 * control plane crosses the tier boundary. The endpoint comes from eval/'s
 * remote state with a null-safe fallback so this tier stays independently
 * plannable when eval/ is down. Historical results still read Postgres, never
 * Valkey — ADR-0023's historical-read sentence is unchanged (ADR-0039 §2).
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

# --- Valkey endpoint from eval/ (ADR-0039) ------------------------------------
# The orchestrator (api + control-plane) reads/writes operator control state in
# Valkey, which lives in eval/. The endpoint is read from eval/'s remote state
# and resolved to "" when absent — eval/ is destroyed nightly, and a hard read
# against a missing output would break ui/'s independent lifecycle (the whole
# point of the three-tier split). Consumers degrade to all-paused when it is ""
# (ADR-0034 §2 fail-closed — correct when nothing is running to pause).
data "terraform_remote_state" "eval" {
  count   = var.read_eval_state ? 1 : 0
  backend = "s3"
  config  = merge(local.remote_state, { key = "envs/dev/eval/terraform.tfstate" })
}

locals {
  vpc_id             = data.terraform_remote_state.persistent.outputs.vpc_id
  private_subnet_ids = data.terraform_remote_state.persistent.outputs.private_subnet_ids
  public_subnet_ids  = data.terraform_remote_state.persistent.outputs.public_subnet_ids
  task_sg_id         = data.terraform_remote_state.persistent.outputs.task_security_group_id
  alb_sg_id          = data.terraform_remote_state.persistent.outputs.alb_security_group_id
  cluster_name       = data.terraform_remote_state.persistent.outputs.cluster_name
}

# --- orchestrator-api behind its ALB (reads Aurora + S3 results) --------------
module "ui" {
  # CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md: this module used to also
  # build orchestrator-control-plane (the results_writer singleton) alongside
  # the API + ALB. That service is retired — its replacement (run-supervisor +
  # results-writer, below) rides the SAME tier for the same reason the old
  # singleton did: it needs Aurora + SQS + Valkey only, not the gateway or
  # workers, so it stays independently plannable with eval/ down.
  source = "../../../modules/ecs-service-orchestrator"

  name_prefix        = var.name_prefix
  purpose            = var.purpose
  cluster_name       = local.cluster_name
  image              = data.terraform_remote_state.persistent.outputs.ecr_repository_urls["orchestrator"] # one image, several entrypoints (A-5)
  private_subnet_ids = local.private_subnet_ids
  public_subnet_ids  = local.public_subnet_ids
  security_group_ids = {
    alb  = [local.alb_sg_id]
    task = [local.task_sg_id]
  }
  vpc_id       = local.vpc_id
  api_replicas = 1

  log_group_api = "/aws/ecs/${var.name_prefix}-orchestrator-api"

  secrets = {
    master                     = data.terraform_remote_state.persistent.outputs.secret_arns["litellm-master"]
    database_url               = data.terraform_remote_state.persistent.outputs.database_url_secret_arn
    litellm_spend_database_url = data.terraform_remote_state.persistent.outputs.litellm_spend_database_url_secret_arn
  }
  queue_urls = {
    harness = data.terraform_remote_state.persistent.outputs.queue_urls["harness-jobs"]
    eval    = data.terraform_remote_state.persistent.outputs.queue_urls["eval-jobs"]
    results = data.terraform_remote_state.persistent.outputs.queue_urls["results"]
  }
  queue_arns = {
    harness   = data.terraform_remote_state.persistent.outputs.queue_arns["harness-jobs"]
    eval      = data.terraform_remote_state.persistent.outputs.queue_arns["eval-jobs"]
    results   = data.terraform_remote_state.persistent.outputs.queue_arns["results"]
    llm_calls = data.terraform_remote_state.persistent.outputs.queue_arns["llm-calls"]
  }
  # ADR-0037 M0 §2/operator: the queue panel reads DLQ depths; get_dlq_depth
  # resolves <queue>-dlq. Grant the DLQ ARNs alongside the main queues.
  queue_dlq_arns = {
    harness   = data.terraform_remote_state.persistent.outputs.dlq_arns["harness-jobs"]
    eval      = data.terraform_remote_state.persistent.outputs.dlq_arns["eval-jobs"]
    results   = data.terraform_remote_state.persistent.outputs.dlq_arns["results"]
    llm_calls = data.terraform_remote_state.persistent.outputs.dlq_arns["llm-calls"]
  }
  # ADR-0030 review R2: the control plane hosts the dispatcher, which reads the
  # warm-cache manifest + the pinned dataset mirror from the dataset bucket.
  dataset_bucket_name = data.terraform_remote_state.persistent.outputs.s3_bucket_ids["dataset"]
  # ADR-0037 M0 §3: results writer reads llm_calls.jsonl back from the artifact bucket.
  artifact_bucket_name = data.terraform_remote_state.persistent.outputs.s3_bucket_ids["artifacts"]
  # run-launch §3.1: runs/processed/<run_id>.json (POST /runs' ADR-0024-
  # matching copy) + §7 rule 1's DLQ evidence — the results bucket.
  results_bucket_name = data.terraform_remote_state.persistent.outputs.s3_bucket_ids["results"]
  results_bucket_arn  = "arn:aws:s3:::${data.terraform_remote_state.persistent.outputs.s3_bucket_ids["results"]}"
  # ADR-0039: the control plane reads/writes operator control state in Valkey.
  # The endpoint lives in eval/'s remote state; fall back to "" (degrade to
  # all-paused, ADR-0034 §2) when eval/ is down so ui/ stays independently
  # plannable — the three-tier property. Never a bare literal "".
  redis_endpoint = try(data.terraform_remote_state.eval[0].outputs.redis_endpoint, "")
  # run-launch §5.2: same cross-tier fallback shape as redis_endpoint above —
  # the gateway lives in eval/'s tier; ui/ stays independently plannable when
  # eval/ is down (PROVISION calls then fail loudly at runtime, not at plan
  # time — same posture as an empty redis_endpoint degrading to all-paused).
  gateway_base_url = try(data.terraform_remote_state.eval[0].outputs.gateway_base_url, "")
}

# --- run-supervisor: the control-plane SINGLETON (heartbeat + reaper 2/3) -----
# CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md. Rides the ui tier for the
# same reason the retired orchestrator-control-plane did (comment on module
# "ui" above): Aurora + SQS + Valkey only, independently plannable with eval/
# down.
module "run_supervisor" {
  source = "../../../modules/ecs-service-run-supervisor"

  name_prefix        = var.name_prefix
  purpose            = var.purpose
  cluster_name       = local.cluster_name
  image              = data.terraform_remote_state.persistent.outputs.ecr_repository_urls["orchestrator"]
  private_subnet_ids = local.private_subnet_ids
  security_group_ids = [local.task_sg_id]
  log_group          = "/aws/ecs/${var.name_prefix}-run-supervisor"

  secrets = {
    database_url = data.terraform_remote_state.persistent.outputs.database_url_secret_arn
    master       = data.terraform_remote_state.persistent.outputs.secret_arns["litellm-master"]
  }
  # 2026-09-08: open-run recovery's key re-mint needs the gateway (same guarded
  # read the orchestrator module gets — "" with eval down, recovery then logs
  # its failure and the run is left as is, as before).
  gateway_base_url = try(data.terraform_remote_state.eval[0].outputs.gateway_base_url, "")
  queue_urls = {
    harness = data.terraform_remote_state.persistent.outputs.queue_urls["harness-jobs"]
    results = data.terraform_remote_state.persistent.outputs.queue_urls["results"]
  }
  queue_arns = {
    harness = data.terraform_remote_state.persistent.outputs.queue_arns["harness-jobs"]
    results = data.terraform_remote_state.persistent.outputs.queue_arns["results"]
    eval    = data.terraform_remote_state.persistent.outputs.queue_arns["eval-jobs"]
  }
  # ADR-0039, same null-safe cross-tier fallback as module "ui" above — this
  # service is the one that PUBLISHES control:flags, so an absent eval/ means
  # it simply has nothing to publish to yet, not a plan-time failure.
  redis_endpoint = try(data.terraform_remote_state.eval[0].outputs.redis_endpoint, "")

  # §2.3 (wiring review 2026-09-01): eval autoscaler knobs — the observe->live flip is a
  # tfvars change + apply. eval_asg_name rides remote state (try(): the output lands only
  # after persistent's next apply — "" until then, and the code refuses live with it empty).
  eval_autoscaler_mode = var.eval_autoscaler_mode
  eval_max_workers     = var.eval_max_workers
  eval_tasks_per_host  = var.eval_tasks_per_host
  eval_task_scale_in_s = var.eval_task_scale_in_s
  eval_asg_name        = try(data.terraform_remote_state.persistent.outputs.eval_asg_name, "")
}

variable "eval_task_scale_in_s" {
  type        = string
  default     = "300"
  description = "F7 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): seconds of consecutive lower ticks before eval TASKS scale in — root passthrough so it is a tfvars change. The 2-tick (60 s) rule killed and re-created idle workers three times in 30 min on the qwen x mini e2e while the host stayed; hosts keep the two-tick rule."
}

variable "eval_autoscaler_mode" {
  type        = string
  default     = "live"
  description = "Eval autoscaler mode (off|observe|live) — root passthrough so the owner's flip is a tfvars change (the NAT-gateway lesson: module-only variables break the documented procedure). Flipped to live 2026-09-03 for the 500-instance run: patches arrive at 0.3–12/min depending on pool and harness (BUILDER4-EVAL-PACKING-2026-09-03), so the eval fleet must follow the harness fleet rather than be hand-set."
}

variable "eval_max_workers" {
  type        = string
  default     = "48"
  description = "Required when eval_autoscaler_mode=live; the code refuses to start live without it. MUST NOT exceed persistent's eval asg_max_size × eval_tasks_per_host (the scaler can only raise hosts within that rail). 48 = 12 hosts at 4/host, inside the 16-host rail. Sized for the fastest measured case (qwen + custom_minimal: 5–12 patches/min × ~3–4 min per grade ≈ 15–48 concurrent grades); a laguna run settles at 3–5 workers on its own."
}

variable "eval_tasks_per_host" {
  type        = string
  default     = "4"
  description = "Eval tasks the scaler packs per host — MUST equal what the eval task's cpu/memory reservation lets ECS place (modules/ecs-service-eval-worker: 2048/3584 on a c5d.2xlarge = exactly 4). The scaler turns tasks into hosts with this constant; ECS turns hosts into tasks with the reservation; a mismatch is the packing bug BUILDER4-EVAL-PACKING-2026-09-03 fixed. Change both together, with the grading cap (EVAL_GRADE_MEM_LIMIT_MB) re-derived."
}

# --- results-writer: the results consume loop, N replicas ---------------------
module "results_writer" {
  source = "../../../modules/ecs-service-results-writer"

  name_prefix        = var.name_prefix
  purpose            = var.purpose
  cluster_name       = local.cluster_name
  image              = data.terraform_remote_state.persistent.outputs.ecr_repository_urls["orchestrator"]
  private_subnet_ids = local.private_subnet_ids
  security_group_ids = [local.task_sg_id]
  log_group          = "/aws/ecs/${var.name_prefix}-results-writer"
  replicas           = 2

  secrets = {
    database_url = data.terraform_remote_state.persistent.outputs.database_url_secret_arn
  }
  queue_urls = {
    harness   = data.terraform_remote_state.persistent.outputs.queue_urls["harness-jobs"]
    eval      = data.terraform_remote_state.persistent.outputs.queue_urls["eval-jobs"]
    results   = data.terraform_remote_state.persistent.outputs.queue_urls["results"]
    llm_calls = data.terraform_remote_state.persistent.outputs.queue_urls["llm-calls"]
  }
  queue_arns = {
    eval               = data.terraform_remote_state.persistent.outputs.queue_arns["eval-jobs"]
    results            = data.terraform_remote_state.persistent.outputs.queue_arns["results"]
    llm_calls          = data.terraform_remote_state.persistent.outputs.queue_arns["llm-calls"]
    model_observations = data.terraform_remote_state.persistent.outputs.queue_arns["model-observations"]
  }
  queue_dlq_arns = {
    harness = data.terraform_remote_state.persistent.outputs.dlq_arns["harness-jobs"]
    eval    = data.terraform_remote_state.persistent.outputs.dlq_arns["eval-jobs"]
  }
  artifact_bucket_name = data.terraform_remote_state.persistent.outputs.s3_bucket_ids["artifacts"]
  # ADR-0039 / not in the work order's needs list — see the module's own
  # redis_endpoint description: control_state.mark_runs_active() is called
  # unconditionally on every result received, so this is load-bearing, not
  # best-effort.
  redis_endpoint = try(data.terraform_remote_state.eval[0].outputs.redis_endpoint, "")
}

# A1-5 / review §7: the API ALB's DNS feeds the reviewer's custom SSM tunnel
# document (eval reads it via this state to pin the api:8000 forward target).
output "api_alb_dns" {
  value = module.ui.api_alb_dns
}
