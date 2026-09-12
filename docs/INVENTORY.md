# Inventory

Everything a deployment consists of, and who creates it: **terraform** (a root applies it),
**make** (a target in the Makefile / `scripts/adopt.py`), **script** (a command in `scripts/`),
or **manual** (you do it by hand). The manual column is the adoption backlog; it shrank from
fourteen rows to six on 2026-09-11.

Names below use `<prefix>` for the `name_prefix` in `setup.yaml` (`eval-dev` on the originating
account). Region, prefix, profile and account are inputs everywhere (`swebench_eval/aws_names.py`,
each Terraform root's `settings.tf`).

## Accounts and credentials

| item | created by | notes |
|---|---|---|
| AWS account, region, budget alarm, MFA | manual | any region; MFA on root and a budget alarm before anything exists (SETUP step 3) |
| admin credential profile | manual | `aws_profile` in `setup.yaml`, or the ambient chain |
| reviewer IAM role | optional manual | `reviewer_role_name` in `setup.yaml`; nothing is attached when empty |
| Terraform state bucket | make bootstrap | versioned, AES256, public access blocked; holds secrets in plaintext |
| `backend.hcl` + `terraform.tfvars` per root | make bootstrap | from `setup.yaml` (gitignored) |
| OpenRouter inference key | manual → setup.yaml | → secret `<prefix>-openrouter-api-key` |
| OpenRouter provisioning key | manual → setup.yaml | make bootstrap creates secret `<prefix>-openrouter-management` |
| Docker Hub PAT | manual → setup.yaml | make bootstrap creates secret `ecr-pullthroughcache/<prefix>-dockerhub` |
| Aurora master password, LiteLLM master key, notification email | setup.yaml | composed into the database-URL secrets by Terraform |

## Persistent tier (`envs/dev/persistent`, `make up-persistent`, once)

| resource | created by |
|---|---|
| VPC `10.0.0.0/16`, 3 AZs (the region's first three), private / public / harness-isolated subnets | terraform |
| t3.micro NAT + SSM tunnel instance (always), NAT Gateway (toggle) | terraform; toggle `make nat-gateway` / `make nat-instance` |
| Aurora Serverless v2 (min 0, auto-pause), databases `app_control_plane`, `litellm_spend` | terraform; migrations run by results-writer at boot |
| S3: artifacts, dataset mirror, results | terraform |
| ECR: nine repositories incl. `<prefix>-harness-worker` and the Docker Hub pull-through cache | terraform |
| SQS: harness-jobs, eval-jobs, results, llm-calls, model-observations + DLQs | terraform |
| Secrets (six) from tfvars | terraform |
| CloudWatch log groups, custom metrics, budget alert, billing alarm, SNS topic | terraform; email confirmation **manual** |
| nightly scale-to-zero Lambda + EventBridge rule | terraform; `make guard-pause` / `make guard-resume`; does not cover the build ASG (`make images` scales it down itself) |
| dispatch Lambda (reads eval's Valkey endpoint from remote state) | terraform; refreshed by `make up` |
| empty ECS cluster `<prefix>-cluster`, task security group | terraform |

## UI tier (`envs/dev/ui`, `make up` / `make down`)

| resource | created by |
|---|---|
| internal ALB for the API | terraform |
| `orchestrator-api`, `run-supervisor`, `results-writer` services | terraform; `make roll` moves them to the new revision (part of `make up`) |
| `llm-judge`, `ceiling-discovery` one-shot task definitions | terraform |

## Eval tier (`envs/dev/eval`, per session)

| resource | created by |
|---|---|
| Valkey (ElastiCache Serverless) | terraform |
| LiteLLM `gateway` service + internal ALB, stable name `gateway.eval.internal` | terraform; config `infra/docker/litellm_config.yaml` |
| `git-mirror` service (vestigial; blocked from the harness by the isolation probe) | terraform |
| `eval-worker` on the EC2 Spot ASG (`<prefix>-eval-asg`, max 16) | terraform |
| `harness-dispatcher` service | terraform |
| harness task families from `harness-task-families.auto.tfvars.json` | `make images` (generator) then terraform |
| warm-job task definition | terraform |
| five VPC interface endpoints | terraform |
| SSM tunnel documents (`<prefix>-tunnel-api-8000` once ui is applied, `-gateway-4000`) | terraform |

## Build tier (`envs/dev/build`, `make images`)

| resource | created by |
|---|---|
| `<prefix>-build-cluster`, build ASG (c5d.4xlarge Spot, max 4), `<prefix>-image-build` task definition | terraform (applied by `make images`) |
| scaling the ASG up and back to zero | make images (always scaled back, even on failure) |
| the builder run (official image by digest → CLI layer → instance layer → push), `--only` or sharded | make images |

## Data and images

| item | created by |
|---|---|
| dataset mirror: full rows + gold-stripped public subset | make images (`seed_dataset.py`); `--endpoint-url` for the laptop MinIO |
| official image digest snapshot `swebench_eval/dataset/image_digests/<dataset>-<rev>.json` | committed; checked by make images (`snapshot_image_digests.py --check`) |
| seven service images, `:latest` and `:<sha>` | make service-images |
| per-instance `-inst` harness images | make images |
| cache manifest in the dataset bucket | make images (`warm_image_cache.py --manifest`) |
| laptop per-instance image `swebench-eval-local:<ver>-<id>` | make local-smoke (`local_instance_image.py`) |
| leak-detectable map `data/contamination/leak_detectable.json` | committed; `generate_leak_detectable.py` |
| judge rubric `config/judge_rubric.yaml` | committed |

## Gates and one-offs before a paid run

| step | how |
|---|---|
| gold gate | `make gate INSTANCES=...` (`POST /images/validate`) |
| model aliases + provider pins | `make pin` (specs in `swebench_eval/gateway/rotatable_models.py`) |
| ceiling discovery | `make discover ALIAS=...`; pacer rate + ceiling then set in the UI — **manual** |
| isolation probe | `scripts/verify_harness_isolation.py` from a task on the isolated subnets — **manual** launch |
| queue purge after a teardown | `make purge YES=1` |

## Operator surfaces

| surface | how |
|---|---|
| dashboard | built into the orchestrator image, served by the API at `http://localhost:8000/ui/` through `make tunnel` (private ALB; never public); `ui/` with `npm run dev` for UI development |
| API | `openapi.json` committed; `npm run gen:api` regenerates the client types |
| exports | `GET /runs/{id}/export`, `scripts/export_run_timeline.py`, `scripts/export_run_ui_snapshot.py` |
| offline analysis | leak backfill (`backfill_leak_detection.py`), judge cleanup, timeline fixtures |
