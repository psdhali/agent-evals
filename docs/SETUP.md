# Set it up yourself

One ordered checklist from an empty AWS account to a graded, judged run and back to zero. Each
step is a `make` target (`make help` lists them; `scripts/adopt.py` is what they run), with what
you need, how to verify, and what it costs in time and money. The explanation behind each step
lives in the two runbooks under `docs/runbooks/`; you should not need to open them.

Steps 0–2 need no AWS account and cost cents. Stop there if all you want is to reproduce a
published number or run one instance on your laptop.

> Status: steps 0–2 are measured on a laptop (2026-09-11). Steps 3–13 were validated on
> 2026-09-12 in a fresh AWS account, region `us-east-2`, following only this file with `make`
> targets: no hand intervention, no code change — 72 minutes from an empty account to run-ready,
> two graded runs and a judge pass, then back to an empty account. The times below are those
> measurements; the discovery pass before it and its 27 fixes are in `docs/FRESH-ACCOUNT-LOG.md`.

## 0 · Reproduce a published number without any infrastructure — 20 min, $0

- **Need:** Python 3.12, `uv`, Docker.
- Pick a run on the site's Downloads page (or in `published-runs/README.md` here). Its
  predictions file is committed in this repository as `published-runs/<run_id>/preds.jsonl`; the
  full artifacts (patches, trajectories, per-call ledgers, the official harness's eval reports and
  test logs, every judge pass) are one `bundle-<run_id>.tar.gz` on the `v1.0-data` release, with
  `SHA256SUMS` beside them.
- Run the official harness against the pinned dataset revision:
  ```bash
  uv sync --locked
  uv run python -m swebench.harness.run_evaluation \
    --dataset_name SWE-bench/SWE-bench_Verified --split test \
    --predictions_path published-runs/<run_id>/preds.jsonl --run_id verify --max_workers 4
  ```
  From a downloaded bundle instead: `sha256sum -c SHA256SUMS --ignore-missing`, `tar xzf` it, and
  point `--predictions_path` at `<run_id>/preds.jsonl`; the bundle's `README.md` states the
  expected resolve count and its `eval/<instance>/1/eval_report.json` files are what this command
  produced here, so any instance that differs can be diffed.
- **Verify:** the resolve count matches the run's results page (`published-runs/README.md` lists
  them). The images are pulled from Docker Hub by the harness, ~1 GB each; a Docker Hub login
  avoids the anonymous rate limit. A 78-instance run is ~20 min after the pulls, a 500-run longer.

## 1 · Laptop tooling — 30 min, $0

- Python 3.12 + `uv`; Node 24 with npm 11 (the lock file is written by npm 11; npm 10 rejects
  it); Docker Desktop with compose and buildx; Terraform ≥ 1.11;
  AWS CLI v2 + Session Manager plugin; `pre-commit install` (gitleaks).
- `uv sync --locked && make test` — pytest, ruff, black and mypy must pass before anything else.
- **Verify:** `make test` green; `npm ci && npm test` in `ui/` green.

## 2 · One instance, graded, on your laptop — no AWS — 5 min after the first image pull, under $0.05

- **Need:** an OpenRouter key with a few dollars of credit; ~5 GB of disk for one instance image.
- `cp .env.example .env` and set `OPENROUTER_API_KEY` (the compose file reads the repo-root `.env`).
- `make local-smoke` (`scripts/local_smoke_test.sh`). It brings up the local stack (LiteLLM
  gateway on :4000, Postgres, ElasticMQ, MinIO, Valkey), seeds the gold-stripped dataset mirror
  into MinIO, pulls SWE-bench's official image for `django__django-11099` at the digest the
  committed snapshot pins, builds a laptop twin of the deployed per-instance image (framework +
  agent user + sentinel — `infra/docker/Dockerfile.local-instance`), runs the custom minimal
  harness **inside that image** against its hardened `/testbed` through the gateway, then grades
  the gold patch, a broken patch and the agent's patch with the official harness in the same
  image, and tears the stack down.
- **Verify:** the script ends with `Gold patch: resolved=True`, `Broken patch: resolved=False`,
  the agent's verdict, `Postgres: run + instance results written` and `SMOKE OK`. Artifacts are
  under `.smoke/<timestamp>/harness/` (patch, trajectory, per-call log, `harness_result.json`).
- **Measured (Apple Silicon, x86_64 emulation):** image pull ~2 min (1.4 GB), build ~2 min, agent
  11 turns / 18 s / $0.005, three grades ~90 s. `--instance-id <id>` runs any Verified instance;
  `--model <alias>` any alias in `infra/docker/litellm_config.yaml`.
- **What this does not prove:** the laptop container has internet egress and no pacer; the
  isolation controls on the Integrity page are VPC properties of the AWS deployment.

Everything after this point spends real money.

## 3 · AWS account, region, credentials — 30 min, $0

- One account, any region (`us-east-2`, `eu-central-1`, … — `region` in `setup.yaml`). A fresh
  member account under AWS Organizations is ideal; MFA on root; a budget alarm before anything
  exists. That is the whole of the account setup: MFA on root, a budget alarm, and a member
  account if you have an Organization.
- A credential profile with AdministratorAccess and **no stored long-lived key**. Two shapes
  work; put the profile name in `setup.yaml` as `aws_profile` (or leave it empty for the ambient
  credential chain):
  - **IAM Identity Center** (an Organization): assign your user the `AdministratorAccess`
    permission set on the account, then `aws configure sso`.
  - **A role assumed from a login you already have** (a standalone account; what the
    validation used): in the new account, as root once, IAM → Roles → Create role → "AWS
    account" → the ID of the account you log in to today → `AdministratorAccess`, name it
    `EvalAdoptAdmin` (no external ID, no MFA condition — both accounts are yours). Then:
    ```ini
    [profile eval-adopt]
    role_arn       = arn:aws:iam::<NEW-ACCOUNT-ID>:role/EvalAdoptAdmin
    source_profile = <your existing profile>
    region         = us-east-2
    ```
  Either way: MFA on the root user, a budget alarm, and never root again.
- **Verify:** `aws sts get-caller-identity --profile <profile>` prints the new account id.

## 4 · Quotas — 10 min to check, minutes to 2 days to raise, $0

**A fresh account starts far below what even the control-plane services need** (measured
2026-09-11 on a new account: 6 Fargate vCPU, 5 Spot vCPU, 5 On-Demand vCPU). Request the
raises first thing — approval is usually automatic within minutes, sometimes a day — and let the
rest of the setup proceed meanwhile; nothing before step 8 needs them.

```bash
make quotas            # requests every quota below the 10-instance need (SCALE=full for the whole split)
make doctor            # shows the current values; re-run until the quota rows read OK
```

| quota | needed for | small (10-instance validation) | full (500 instances) |
|---|---|---|---|
| Fargate On-Demand vCPU | 1 vCPU per agent task + 6 for the seven services + 1 for a judge or discovery task | 24 (AWS auto-approved 30 on request; ten instances graded on it) | 145 (the dispatcher's static cap) |
| EC2 Spot vCPU (standard) | grading hosts (8 each) + build hosts (16 each); hosts being drained still count | 64 | 192 |
| EC2 On-Demand vCPU (standard) | the NAT/tunnel instance (2) | 8 | 32 |
| Lambda concurrency | the dispatch Lambda | default 10 | default 10 |
| Elastic IPs | NAT instance + NAT gateway | default 5 | default 5 |

## 5 · `make doctor`, `make bootstrap` — 20 min, $0

- Create two things outside AWS first: an **OpenRouter provisioning key** (the per-run keys are
  minted from it) and a **Docker Hub personal access token** (the pull-through cache and the
  builder use it; anonymous pulls are rate-limited too hard for 500 images).
- `cp setup.yaml.example setup.yaml` and fill it in: region, name prefix, profile, state bucket
  name, Aurora password, LiteLLM master key, OpenRouter inference key, notification email, the
  provisioning key and the Docker Hub credentials. The file is gitignored.
- `make doctor` — every row must read `OK` except the state bucket (bootstrap creates it).
- `make bootstrap` — creates the state bucket (versioned, AES256, public access blocked), writes
  `backend.hcl` and `terraform.tfvars` into every Terraform root, creates the two out-of-band
  secrets, runs `terraform init`. Idempotent; `DRY_RUN=1 make bootstrap` shows what it would do.
- **Verify:** `make doctor` all `OK`.

## 6 · Persistent tier + service images — 20 min measured (12 min apply + 8 min of cached image builds; a cold build adds 15–40 min), then ≈ $2/day idle

- `make up-persistent` — in three moves, each shown as a plan and confirmed: the ECR
  repositories first; then the seven service images are built (linux/amd64; 15 min on an x86 box,
  ~40 min under emulation on Apple Silicon) and pushed if they are not in ECR yet (`make
  service-images` runs the same script on its own); then the rest of the tier — the VPC, Aurora
  (auto-pauses to 0), the three buckets, queues, secrets, the NAT/tunnel instance, the dispatch
  Lambda (it is created from the pushed `dispatch` image, which is why the images come first),
  the nightly scale-to-zero guard, the budget alarm.
- Confirm the SNS subscription email it sends you (the one click nothing can do for you).
- **Verify:** `cd infra/terraform/envs/dev/persistent && terraform output nat_instance_id`;
  `aws rds describe-db-clusters` shows the cluster `available`;
  `aws ecr describe-images --repository-name <prefix>-orchestrator` lists `latest` and the sha tag.

## 7 · (folded into 6) — the dataset mirror and the digest-snapshot check run inside `make images`

## 8 · Per-instance images — 21–29 min for 10 instances measured (Spot reclaims included), one night for 500; ≈ $2 per 10, ≈ $40 for 500

- `make images SUBSET=10` (or `ONLY=id1,id2`, or nothing for the whole split with `HOSTS=4`).
  It seeds the mirror, checks the digest snapshot, applies the build tier, scales the build ASG
  up, launches the builder task(s) (official image by pinned digest → CLI layer → instance layer →
  push), waits, writes the cache manifest, regenerates the harness task families, and scales the
  ASG back to zero whatever happens. It ends by printing the elapsed time and the ECR size.
- **Spot quota still pending?** The build host is a 16-vCPU Spot instance and the grading hosts
  are 8-vCPU Spot instances. Until the Spot raise lands you can run either pool On-Demand (about
  twice the hourly price): add `build_hosts_spot = false` and/or `eval_hosts_spot = false` to
  `infra/terraform/envs/dev/persistent/terraform.tfvars`, run `make up-persistent` again (a
  launch-template change only), and flip them back later the same way.
- **Verify:** the manifest lists every image; `harness-task-families.auto.tfvars.json` has one
  family per image; `aws ecr describe-images` shows the `-inst` tags.

## 9 · UI and eval tiers — 11 min measured, ≈ $6/day while up

- `make up` — ui → eval (families regenerated first) → ui again (Valkey endpoint) → the api
  tunnel document → the dispatch Lambda's Valkey endpoint → rolls the three control-plane services
  to their new revision → verifies. Each apply shows its plan and asks (`YES=1` to skip).
- `make tunnel` in a second terminal (SSM port-forward to the API on localhost:8000) and open
  **http://localhost:8000/ui/** — the dashboard is built into the orchestrator image and served
  by the API itself behind the private load balancer (no Node on your laptop, nothing public;
  `/docs` is the API). `cd ui && npm run dev` still works for UI development. The port-forward
  drops after about 20 minutes without traffic and the dashboard then reads "control
  unreachable": run `make tunnel` again — nothing on the AWS side has changed.
- **Verify:** `make verify` (every service at its desired count, family count == image count);
  `GET /models` in the UI lists aliases (proves the gateway reaches OpenRouter).

## 10 · Gates before a paid run — gate 6 min, pin 15 s, discovery 14 min measured; under $1 without discovery, ≈ $24 with the default discovery target

1. **Gold gate**: `make gate INSTANCES=id1,id2,...` — enqueues one gold grade per image, waits
   for the eval hosts to come up and grade them (10–20 min for ten), and passes only when every
   image graded the gold patch RESOLVED. The dispatcher refuses instances whose image did not
   pass. `--run-id` (via `scripts/adopt.py gate`) re-watches a run already enqueued.
2. **Model aliases**: `make tunnel-gateway` in a third terminal (the gateway on localhost:4000),
   then `make pin` registers the git-tracked specs in `swebench_eval/gateway/rotatable_models.py`
   (provider pins included) with the gateway, authenticating with the `litellm_master_key` from
   `setup.yaml` (or `LITELLM_MASTER_KEY` if exported). To pin an alias to a provider, set `provider_pin`
   on its spec (the slug is the endpoint `tag` prefix on OpenRouter's
   `/models/<slug>/endpoints`) and re-run `make pin`; rotatable aliases are db-models only, never
   also in `litellm_config.yaml`.
3. **Ceiling discovery**: `make discover ALIAS=<alias>` once per alias you will run, then set the
   pacer rate and ceiling in the UI (Limits). Only aliases with measured constants can be
   discovered — today `deepseek-v4-flash-0731`, `gpt-5-mini`, `laguna-xs-2.1`, `minimax-m2.5`,
   `qwen3-coder-next`. **Cost:** the ramp sends near-max-context probes; at the default target
   the API estimated $24 for `minimax-m2.5`. For a small validation pass `--target 5` (via
   `scripts/adopt.py discover`) or set the ceiling by hand
   (`POST /model-ceilings/<alias>/manual`, or the UI) and skip discovery.
4. **Isolation probe** (once per deployment): run `scripts/verify_harness_isolation.py` from a
   harness task on the isolated subnets — the assertions must all pass (`docs/runbooks/`
   explains the launch). Still a manual launch.
5. `make purge YES=1` if a previous session left messages in the queues.

## 11 · First run — 15 min for 10 instances measured, ≈ $0.55, judge ≈ $0.04; 2 h and $30–40 for 500 on a mid-priced model

- Launch from the UI: harness, alias, instances, attempts 1, cost cap per instance, run budget cap.
- Watch the live panel; the Capacity page shows the planner's decisions.
- After it finishes: judge pass from the run page (rubric v3), export, and the Downloads.
- **Verify:** the run reaches `completed`; every instance has a verdict or a recorded reason.

## 12 · Tear down — 10 min measured, back to ≈ $2/day

- `make down` — exports your cached credentials first (so an SSO token expiring between the two
  destroys cannot leave the UI tier running), destroys eval then ui after checking each plan is a
  pure destroy of its own root, scales the eval host group to zero and releases every host from
  its ECS termination hook (a host parked in `Terminating:Wait` is billed for up to an hour
  otherwise), verifies nothing billable survived, and re-enables the nightly guard.
  `make down-nat` also moves private-subnet egress back to the t3 instance (≈ $3/day saved).
- **Verify:** the final table reads zero for services, tasks, ASG desired, caches, load balancers
  and eval interface endpoints.

## 13 · Leave nothing behind — `make teardown-all`, `make account-empty` — 15 min after `make down` (30 min from a running session) measured, then $0

`terraform destroy` alone leaves the state bucket, the two out-of-band secrets, the pull-through
cache's ECR repositories and stray log groups behind, and refuses Aurora (deletion protection)
and the durable buckets (`force_destroy = false`) by design.

- `make teardown-all` — asks you to type `DESTROY <prefix>`, then: eval → ui → build destroys
  (each checked to be a pure destroy), both EC2 host groups emptied (the persistent destroy
  removes the capacity provider before the group, so a host still in its termination hook would
  stall it for an hour), persistent with the two guards lifted, the secrets
  (no recovery window), every `<prefix>-*` and `docker-hub/*` ECR repository, the log groups,
  the state bucket with all its versions, and the local `backend.hcl` / `terraform.tfvars`.
  It ends by running the sweep. It assumes your role afresh for a full hour first, and it is
  safe to re-run after a failure: a tier already gone is skipped, and the guard lift on a
  half-destroyed persistent state touches only the guarded resources (it never rebuilds).
- `make account-empty` — read-only: lists every VPC, instance, EIP, ASG, launch template, NAT
  gateway, ECS cluster, ECR repository, RDS, ElastiCache, load balancer, queue, topic, Lambda,
  secret (including scheduled deletions), log group, EventBridge rule, alarm, VPC endpoint, SSM
  document, Cloud Map namespace in your region and `us-east-1` (the billing alarm's home), plus
  S3 buckets, prefixed IAM roles and policies, hosted zones and budgets globally. Exit 1 with
  the list if anything remains. Your login role, the quota increases and Cost Explorer history
  stay on purpose. The next day's bill should read zero.

## The clean pass

A pass counts as clean only when it starts from an account `make account-empty` reports empty
and reaches step 13 with **no hand intervention and no code change**. The first pass in a new
account was the discovery pass (`docs/FRESH-ACCOUNT-LOG.md` records twenty-one fixes); the clean
pass followed on 2026-09-12: four restarts each stopped at a deviation that became a fix (six
more), and the fifth ran the whole way, empty → two graded runs → empty, at the tree tagged `v1.0` in this repository (commit `3ede14f` in the private development
history).
That pass stands behind the times in this file. Any deviation during a clean pass stops it, gets
fixed, and the pass restarts from empty.

## What is still manual, in one list

Creating the AWS account, the OpenRouter and Docker Hub accounts and their keys · MFA and the
account-level budget · quota-raise requests · confirming the SNS email · the isolation-probe
launch · setting the pacer rate and ceiling in the UI · launching, judging and closing runs from
the UI. Everything else is a `make` target.
