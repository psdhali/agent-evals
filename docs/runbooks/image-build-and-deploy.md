# Runbook — building, tagging, and deploying container images

> **2026-09-11 (adoption Phase 1c):** `make service-images` builds and pushes the six images;
> `make images` runs the per-instance builder end to end (build tier apply, ASG up, run-task,
> manifest, families, ASG down); `make roll` moves the three control-plane services. Account,
> region and prefix below are the originating account's — they are inputs now.

**Audience: every builder.** This is the procedure for getting new code into a running ECS
task/service safely. Follow it rather than improvising; if reality disagrees with this file, fix
this file in the same commit.

Written 2026-09-02 after a stale-image incident (the second of its kind — see §1) where a rebuilt
image did **not** take effect: tasks ran old content with no error, no build-log failure, and no
Terraform drift signal. Everything here was verified against the code and, where noted, the live
account.

---

## 0 · Ground rules

| | |
|---|---|
| Profile | `export AWS_PROFILE=eval-framework AWS_REGION=us-west-2` — SSO only; `aws sso login --profile eval-framework` when it expires |
| Account / region | `<account-id>`, `us-west-2` |
| Terraform | **`~/bin/terraform`** — NOT on `PATH`, so a bare `terraform` may find a different binary or none. Use the absolute path. |
| Build script | `scripts/docker_build_push.sh` — builds+pushes the six service images for `linux/amd64` and reads back the manifest arch |
| Registry | `<account-id>.dkr.ecr.us-west-2.amazonaws.com/eval-dev-<image>` |

**The one rule that prevents the whole incident class:** a `:latest` push does **not** reliably
change what a task runs. Deploy by **content** (a per-commit `:<sha>` tag / digest), and **verify
the baked `FRAMEWORK_SHA` of what's actually running** before you trust a deploy.

---

## 1 · Why `:latest` alone is not a deploy (the staleness trap)

`docker_build_push.sh` pushes every image to the mutable `:latest` tag. `:latest` is a shared
pointer, and two things make "I pushed `:latest`" ≠ "the fleet runs my code":

1. **EC2-pool host cache.** The warm-job / image-build / eval-worker tasks run on the **EC2 pool**
   (they need a real Docker host — DinD grading, image builds). A reused host keeps `:latest` in
   its local Docker cache and will **not re-pull** for a task whose image reference string is still
   `:latest`. It serves the cached (old) image.
2. **Terraform plan-time capture.** The EC2 task-defs pin the image to a **digest** resolved from
   `data.aws_ecr_image { image_tag = ... }` — but that resolution happens at **`terraform plan`
   time**. If `:latest` moved *after* the last apply, the task-def stays pinned to the **old**
   digest until someone re-applies. It does not self-heal. (This is what looked, in the 2026-09-02
   incident, like "ECS tag resolution stuck for hours" — it wasn't; the digest was captured stale
   at plan time and used faithfully.)

Additionally, with concurrent builders, whoever pushes `:latest` **last** wins it — a stale-tree
build can silently clobber a newer one.

**Fargate** services (orchestrator/api, run-supervisor, results-writer, harness-worker,
harness-dispatcher, gateway, git-mirror) don't have the host cache (fresh microVM pull per task),
but they *are* exposed to the clobber race and to "I pushed but didn't `force-new-deployment`."

### The fix that's now in place

- `docker_build_push.sh` also pushes an **immutable `:<FRAMEWORK_SHA>` tag** beside `:latest`
  (tainted `-dirty` if the tree has tracked changes vs HEAD, so a tag never lies about its commit).
- A **`framework_sha` Terraform variable** (default `"latest"`, non-breaking) drives the
  `data.aws_ecr_image` lookups in **both** `envs/dev/eval` and `envs/dev/build`. Pass the per-commit
  tag for a **deterministic** pin the mutable `:latest` can never make stale.
- The two EC2-pool warm-jobs (`module "warm_job"` in eval, `module "image_build"` in build) and
  `eval-worker` are all digest-pinned through it.

Commits: `d63f9ff` (script + eval tier + eval-worker), `f82c3ba` (build tier). Both tiers pass
`terraform validate`.

---

## 2 · The image set

| Image (repo `eval-dev-…`) | Built from | Runs on | Consumed by |
|---|---|---|---|
| `orchestrator` | `Dockerfile.orchestrator` | Fargate | api, run-supervisor, results-writer, ceiling-discovery, **llm-judge** (all one image, `command` selects the entrypoint) |
| `harness-worker` | `Dockerfile.harness-worker` | Fargate | harness tasks; also the **collocated repo the `-hw`/`-inst` images are pushed to** |
| `eval-worker` | `Dockerfile.eval-worker` | **EC2** (DinD grading) | eval grading tasks |
| `warm-job` | `Dockerfile.warm-job` | **EC2** | `warm_image_cache.py` (builds `-hw`/`-inst` env images) + `build_phase0_instances.py` |
| `gateway` | `Dockerfile.gateway` | Fargate | LiteLLM gateway (config baked in) |
| `dispatch` | `Dockerfile.dispatch` | Lambda/Fargate | S3-drop → SQS dispatcher |

The **`-inst` images** are built by the **build-tier** warm-job (`envs/dev/build`,
`module "image_build"`, family `eval-dev-image-build`) — a separate EC2 pool from the eval tier's
`module "warm_job"`. **This is the path that stale-built the `-inst` images in the 2026-09-02
incident**: it was on floating `:latest` with no digest pin until `f82c3ba`.

---

## 3 · Deploy procedure

### 3a · Build + push (produces `:latest` AND `:<sha>`)

```bash
export AWS_PROFILE=eval-framework AWS_REGION=us-west-2
cd <repo root>
git status                      # tree MUST be clean at the intended commit — a dirty
                                # tree tags :<sha>-dirty (a deliberate, visible warning)
bash scripts/docker_build_push.sh
SHA=$(git rev-parse HEAD)       # the tag you just published, e.g. eval-dev-orchestrator:<SHA>
```

`--build-only` / `--push-only` are available. The script fails loudly if a pushed manifest isn't
`amd64`.

### 3b · Fargate services (orchestrator, gateway, harness-worker, …)

These don't need a digest pin to avoid host-cache (there isn't one), but you **must** force a new
deployment, and you should **verify** you got your build (guards against the `:latest` clobber):

```bash
# roll the service(s) that use the rebuilt image
aws ecs update-service --cluster eval-dev --service eval-dev-orchestrator-api \
  --task-definition eval-dev-orchestrator-api --force-new-deployment
# (repeat for run-supervisor / results-writer if the change touches control-plane boot,
#  e.g. an init.sql migration — those two run run_migrations() on boot)
```

> **Note on `ignore_changes`.** Every service sets `lifecycle { ignore_changes =
> [task_definition, desired_count] }`. A `terraform apply` that changes an image therefore
> registers a **new, idle** task-def revision but does **not** cycle the running service — you move
> it with the explicit `update-service --task-definition` roll above. This is why an apply is
> blast-radius-safe for running tasks (it can still surface *unrelated* drift — always `plan`
> first).

For run-task-launched one-shots (llm-judge, ceiling-discovery), the next launch picks the latest
ACTIVE revision automatically — no roll needed, but the task-def must reference the new image.

### 3c · EC2-pool tasks (warm-job, image-build, eval-worker) — digest-pin

This is where you **must** pin, or a reused host serves stale cache:

```bash
# eval tier (warm_job refresh + eval-worker grading)
cd infra/terraform/envs/dev/eval
~/bin/terraform plan  -var framework_sha=$SHA      # READ the plan — expect only image refs to change
~/bin/terraform apply -var framework_sha=$SHA

# build tier (the -inst image builder)
cd ../build
~/bin/terraform plan  -var framework_sha=$SHA
~/bin/terraform apply -var framework_sha=$SHA
```

Then launch the task (e.g. the `-inst` build via `image_build`) — the digest-pinned reference
forces a fresh pull, so the EC2 host cache can't serve stale content.

Omitting `-var framework_sha=$SHA` falls back to `:latest` (the old behavior) — only do that
knowingly.

---

## 4 · Verify (do not skip)

Every image bakes `FRAMEWORK_SHA` as a build-arg. After any deploy, confirm the **running** task is
the commit you intended — this is the drift signal the incident lacked:

```bash
# pull the task's own CloudWatch log and confirm the baked SHA
aws logs tail /aws/ecs/eval-dev-<service> --since 10m | grep -i FRAMEWORK_SHA
# or describe the task's task-def and confirm the image is <repo>@sha256:… (pinned), not :latest
aws ecs describe-task-definition --task-definition <family> \
  --query 'taskDefinition.containerDefinitions[0].image'
```

For `-inst` images: verify each rebuilt image's baked SHA equals the intended commit — "the build
ran green" is **not** the same as "the build carried the right code."

---

## 5 · Quick reference — which path for which change

| Change | Rebuild | Deploy |
|---|---|---|
| Judge / API / control-plane code | `orchestrator` | roll api + run-supervisor (migrations) via `update-service`; verify `FRAMEWORK_SHA` |
| Harness code | `harness-worker` + `-hw`/`-inst` rebuild via build tier | build tier apply `-var framework_sha=$SHA`, run `image_build`, verify each `-inst` |
| Eval/grading code | `eval-worker` | eval tier apply `-var framework_sha=$SHA`, roll eval-worker |
| Gateway config/pin | `gateway` | roll gateway service |

---

## 6 · Related

- `docs/runbooks/dev-env-bring-up-and-tear-down.md` — full tier bring-up/tear-down.
- `infra/terraform/modules/ec2-task-warm-job/main.tf` (`image_digest` var) and
  `envs/dev/eval|build/main.tf` (`data.aws_ecr_image`, `framework_sha`) — the pin implementation.
