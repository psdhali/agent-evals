# Runbook — bringing the dev environment up and down

> **2026-09-11 (adoption Phase 1c):** every procedure in this file is now a `make` target
> (`make up`, `make down`, `make roll`, `make verify`, `make nat-*`, `make guard-*`,
> `make purge`, `make images` — see `docs/SETUP.md` and `scripts/adopt.py`). Region, name
> prefix and profile are inputs; the literal `us-west-2` / `eval-dev` / `~/bin/terraform` /
> `sg-…` values below are the originating account's. This file is the explanation of WHY each
> step exists; the Makefile is HOW it is run.

**Audience: every builder.** This is the procedure. Follow it rather than improvising, and if
reality disagrees with this file, fix this file in the same commit.

Written 2026-08-22 after a teardown that deleted more than intended and a bring-up ordering trap
that cost a working session. Everything here was verified against the live account.

---

## 0 · Ground rules

| | |
|---|---|
| Profile | `export AWS_PROFILE=eval-framework AWS_REGION=us-west-2` — SSO only, `aws sso login --profile eval-framework` when it expires |
| Account | `<account-id>`, `us-west-2` |
| Terraform | **`~/bin/terraform`** — it is NOT on `PATH`, so `which terraform` lies and a bare `terraform` may find a different binary |
| Budget | **$100 per QUARTER.** Idle cost is ~$30/mo, dominated by ECR storage |

**Three things you must never do:**

1. **Never `terraform destroy` `envs/dev/persistent`.** It holds Aurora (the publishable record),
   the S3 dataset mirror, every ECR image and every secret. Aurora has `deletion_protection = true`
   so the destroy would fail partway through and leave you worse off than when you started.
2. **Never connect to Aurora to check whether it is idle.** Connecting resets the auto-pause clock,
   which is the thing you were trying to measure. Read `ServerlessDatabaseCapacity` from CloudWatch
   (§5.2).
3. **Never `--no-verify` a commit.** The gitleaks pre-commit hook is not optional.

---

## 1 · The three tiers

Three independent Terraform roots, three state files, three lifecycles.

| Root | Lifecycle | Contains |
|---|---|---|
| `infra/terraform/envs/dev/persistent` | **permanent — never destroyed** | VPC + NAT instance, Aurora, S3 (dataset/results/artifacts), ECR, Secrets Manager, SQS, the ECS cluster shell, the eval ASG, observability + the nightly scale-to-zero Lambda |
| `infra/terraform/envs/dev/ui` | up/down | `orchestrator-api` + `run-supervisor` + `results-writer` + their ALB |
| `infra/terraform/envs/dev/eval` | up/down | Valkey, `gateway`, `git-mirror`, `eval-worker`, `harness-dispatcher`, the warm job, the 40 harness task families, the isolated VPC endpoints |

**The control plane lives in `ui/`, not `eval/`.** CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md
split the old `orchestrator-control-plane` singleton into two services — `run-supervisor` (still a
singleton: the heartbeat + reaper rules 2/3) and `results-writer` (N=2: the results consume loop +
reaper rule 1) — but both still ride the `ui/` tier for the reason the old one did: Aurora + SQS +
Valkey only, independently plannable with `eval/` down. This surprises everyone once.

**A fourth, independent root exists for image builds:** `infra/terraform/envs/dev/build` — its own
cluster, ASG and task-definition family, deliberately NOT part of the three-tier split above (it
reads only `persistent`, nothing from `ui`/`eval`, and nothing in `ui`/`eval` reads it either). See
§8.

---

## 2 · The ordering rule — read this before your first apply

> ### `ui` is applied BEFORE `eval`. Always.

The two roots read each other's state, but **not symmetrically**:

| Direction | Where | Guarded? |
|---|---|---|
| `ui` reads `eval`'s `redis_endpoint` | `envs/dev/ui/main.tf:133` | **YES** — `try(data.terraform_remote_state.eval.outputs.redis_endpoint, "")`. `eval` being down degrades the control panel to fail-closed/all-paused (ADR-0034 §2), which is correct when nothing is running. |
| `eval` reads `ui`'s `api_alb_dns` | `envs/dev/eval/main.tf:287` | **NO** — a bare `data.terraform_remote_state.ui.outputs.api_alb_dns` inside `aws_ssm_document.api_tunnel` |

So with `ui` destroyed, **`eval` cannot even plan** — the output does not exist and there is no
fallback. That is the trap. The workaround people reach for is deleting the `api_tunnel` SSM
document to get unblocked, which silently removes the reviewer's scoped tunnel (A1-5): without it
the reviewer role either loses API access or gets handed an unscoped `StartSession`.

**If you are stuck here: apply `ui` first, then `eval`. Do not delete the SSM document.**

*Open item: wrapping that read in `try(..., "")` the way ADR-0039 wrapped the other direction would
make the two roots genuinely independent. Not done — it needs the null case designing (an SSM
document with an empty host is not obviously better than no document).*

---

## 3 · TEAR DOWN

### 3.1 Decide which teardown you want

**Measured 2026-08-24 from Cost Explorer, with all six services already at
`desiredCount 0`. The earlier estimates in this table were roughly 5× too low.**

| | Cost while down | Time to come back | Use when |
|---|---|---|---|
| **A. Scale to zero** | **~$5.70/day (~$172/mo)** | seconds — `update-service --desired-count N` | a pause of *hours*, inside a working day |
| **B. Destroy `eval` + `ui`** | **~$1.95/day (~$58/mo)** | 15–25 min + a full apply | **overnight and every longer gap** |

**Default to B for anything overnight.** Scaling ECS to zero stops Fargate and
nothing else — every ALB, VPC endpoint and the cache keep billing by the hour.
"Bringing it down for the night" with Option A costs about **$3.75/day more than
Option B for zero benefit**, because nothing is running to benefit.

Against a **$100/quarter** budget that difference is decisive: at Option A's rate
the idle stack alone consumes a full quarter's budget in roughly three weeks.

#### What Option A leaves running — and why it is the expensive one

| still billing | rate |
|---|---|
| **14 VPC interface-endpoint ENIs** | **$3.36/day** |
| `eval-dev-api-alb` + `eval-dev-gateway-alb`, zero targets | $1.08/day |
| ~5 public IPv4 addresses | ~$0.60/day |
| 24 custom CloudWatch metrics | ~$0.24/day |
| `eval-dev-nat-instance` (t3.micro) | $0.25/day |
| `eval-dev-cache` (Valkey serverless) | ~$0.10/day |
| Secrets Manager · Route 53 · ECR storage | ~$0.10/day |

**Nine of the fourteen ENIs belong to ElastiCache Serverless** — it creates three
vpce-services × three subnets automatically, and they are billed as PrivateLink
endpoint-hours (~$65/mo before a byte is cached). Count them with
`length(SubnetIds)`, not by counting endpoints:

```bash
aws ec2 describe-vpc-endpoints \
  --query 'sum(VpcEndpoints[?VpcEndpointType==`Interface`].length(SubnetIds))'
```

**Destroying the cache nightly is safe.** `swebench_eval/control/state.py`:
Aurora is the source of truth, Valkey is only the read path, and `read()` is
**fail-closed** — unreachable, missing or stale ⇒ *paused*. Losing the cache can
only ever over-stop, which is recoverable; the orchestrator republishes on its
30 s tick at bring-up.

**Do not merge the two ALBs to save ~$16/mo.** The gateway ALB is reachable by
harness tasks running untrusted agent code; the api ALB is the operator control
surface. One ALB fronting both puts the operator API where agent containers can
reach it. Destroy them nightly instead — same saving, no trust-boundary change.

Aurora needs no action either way: `MinCapacity 0.0`, it genuinely pauses.

### 3.2 Option A — scale to zero (a pause of hours only)

```bash
export AWS_PROFILE=eval-framework AWS_REGION=us-west-2
for s in orchestrator-api run-supervisor results-writer gateway eval-worker harness-dispatcher git-mirror; do
  aws ecs update-service --cluster eval-dev-cluster --service "$s" --desired-count 0 >/dev/null \
    && echo "$s -> 0"
done
aws autoscaling set-desired-capacity --auto-scaling-group-name eval-dev-eval-asg --desired-capacity 0
```

That is the same set the nightly guard uses. **The ASG goes last** — zeroing hosts under running
tasks strands them.

### 3.3 Option B — destroy the two ephemeral tiers (the overnight default)

**Destroy in the reverse of the apply order: `eval` first, then `ui`.**

```bash
cd infra/terraform/envs/dev/eval && ~/bin/terraform destroy
cd ../ui                        && ~/bin/terraform destroy
```

**Do not pass `-auto-approve`.** Read the plan and confirm it says `0 to add, 0 to change` and that
every destroy line is prefixed `module.` from *this* root. If you see anything from `persistent`,
stop — you are in the wrong directory.

### 3.3.1 · Optional — switch NAT egress back to the t3 instance

**Only after `eval` and `ui` are destroyed above** — with nothing running, the route flip below
cannot interrupt work (§4.1 of BUILDER4-NAT-GATEWAY-IMPLEMENTATION-2026-08-29.md). This is a
`persistent`-tier change; it does not delete the t3 instance either way (that box is the SSM tunnel
host and always stays — see §4 below).

**Cost — this is why the flip matters.** The NAT *gateway* bills ≈ **$3/day** (the $0.045/hr hourly
charge plus per-GB data processing), whereas the t3 NAT *instance* egress path is a few cents/day. So
**if nothing is running, bring the gateway down for the night** — leaving it up overnight buys nothing
but cost. During the active e2e phase the gateway stays **on** (owner decision 2026-08-29 — a $625
inference run should not depend on a single burstable EC2 instance with no auto-recovery), but the
standing guidance for any idle gap where the compute tiers are torn down is: **flip egress back to the
t3 instance** (owner decision 2026-09-03, supersedes the earlier "leave it up across a normal
overnight gap" note). The flip is still a `persistent`-tier apply against the tier holding the
publishable record, so treat it with the same care — run it only after `eval`/`ui` are destroyed,
review the plan, and confirm no `aws_instance.nat` **replace** appears (that would kill the SSM tunnel
host). When in doubt about cadence, raise it with the owner.

```bash
export AWS_PROFILE=eval-framework AWS_REGION=us-west-2
cd infra/terraform/envs/dev/persistent
~/bin/terraform plan -var nat_gateway_enabled=false -out=nat-down.tfplan
```

Expected — the exact inverse of bring-up, and note the EIP does **not** appear (it is held across
toggles on purpose, to keep the egress IP stable for the day a model provider allowlists it):

```
Plan: 1 to add, 2 to destroy, 0 to change.

  + module.network.aws_route.private_nat[0]
  - module.network.aws_nat_gateway.main[0]
  - module.network.aws_route.private_nat_gw[0]
```

**Stop and report if anything from §6 of that doc appears** — in particular `aws_instance.nat` shown
as *replace* (it carries `user_data_replace_on_change = true` and a replace kills the SSM tunnel).
Apply only with owner authorization, and never mid-run:

```bash
~/bin/terraform apply nat-down.tfplan     # the SAVED plan, never a bare apply
```

**Known transient — `RouteAlreadyExists` (seen live 2026-09-03).** The apply can fail *after*
destroying the gateway with `Error: RouteAlreadyExists: Route ... with destination (0.0.0.0/0)
already exists`. The just-deleted gateway leaves a **blackhole** default route occupying the
`0.0.0.0/0` slot, so terraform's create of the instance route races it. This is safe and
self-clearing: the gateway destroy already succeeded (state clean), and AWS removes the blackhole
route within a minute. Recover by simply **re-running plan + apply** — the second plan is a single
clean `+ aws_route.private_nat[0]` (nothing destroyed). Confirm the slot is empty first with
`describe-route-tables ... Routes[?DestinationCidrBlock=='0.0.0.0/0']` → `[]`, then apply.

```bash
# route is back on the instance ENI
aws ec2 describe-route-tables --filters Name=tag:Name,Values='*private*' \
  --query 'RouteTables[].Routes[?DestinationCidrBlock==`0.0.0.0/0`]'
#    EXPECT: NetworkInterfaceId present, NatGatewayId absent

# nothing left billing
aws ec2 describe-nat-gateways --query 'NatGateways[?State!=`deleted`]'
#    EXPECT: []
```

**A gateway in `deleting` still bills until it reaches `deleted`.** If it was still deleting when you
stopped looking, check again later and say so.

### 3.4 Verify the teardown — do not assume it worked

```bash
aws ecs describe-clusters --clusters eval-dev-cluster \
  --query 'clusters[0].{active:activeServicesCount,running:runningTasksCount}'
aws ecs list-tasks --cluster eval-dev-cluster --desired-status RUNNING --query 'length(taskArns)'
aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names eval-dev-eval-asg \
  --query 'AutoScalingGroups[0].{desired:DesiredCapacity,instances:length(Instances)}'
aws elasticache describe-serverless-caches --query 'ServerlessCaches[].{n:ServerlessCacheName,s:Status}'
aws elbv2 describe-load-balancers --query 'LoadBalancers[].LoadBalancerName'
aws ec2 describe-instances --filters Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,Tags[?Key==`Name`].Value|[0]]' --output text
```

**Expected after option B:** `activeServicesCount 0`, RUNNING tasks `0`, ASG desired `0` /
0 instances, no serverless caches, no load balancers, and **exactly one running EC2 — the
`eval-dev-nat-instance` t3.micro, which belongs to `persistent` and stays up.**

**Confirm the destroy actually finished.** Terraform can exit part-way on a slow delete
(ElastiCache serverless takes minutes) and leave the resource in state:

```bash
aws s3 cp s3://<tfstate-bucket>/envs/dev/eval/terraform.tfstate - \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
      print('resources left:', [r['type'] for r in d['resources'] if r['mode']=='managed'])"
```

An empty list means clean. Anything else means the destroy exited early — re-run it. *(This is
exactly what happened on 2026-08-22: `aws_elasticache_serverless_cache.main` was left in state
while AWS had already deleted it.)*

Then confirm Aurora actually parks — see §5.2.

### 3.5 Re-enable the nightly guard if it was paused for a run (§4.6)

```bash
cd infra/terraform/envs/dev/persistent
~/bin/terraform plan -target='module.observability.aws_cloudwatch_event_rule.nightly_scale_to_zero[0]' -out=unpause.tfplan
#   (the variable defaults to false)  EXPECT:  ~ state = "DISABLED" -> "ENABLED"
~/bin/terraform apply unpause.tfplan
aws events describe-rule --name eval-dev-nightly-scale-to-zero --query State --output text   # ENABLED
```

---

## 4 · BRING UP

### 4.0 · Before `ui`/`eval` — check which NAT egress mode `persistent` is in

The private route table's `0.0.0.0/0` points at either the NAT Gateway or the t3 NAT instance
(toggle: `nat_gateway_enabled` on `envs/dev/persistent`, default **on** during the e2e phase — owner
decision 2026-08-29, see BUILDER4-NAT-GATEWAY-IMPLEMENTATION-2026-08-29.md). **The t3 instance
always exists and always stays regardless of the toggle — it is the SSM tunnel host**
(`local._nat_instance_arn` in `envs/dev/eval/main.tf`), not a NAT swap. Only the route moves.

This only carries the LiteLLM gateway's egress to OpenRouter, plus anything else in the private
subnets without a VPC endpoint — harness tasks sit in `harness_isolated` subnets with no default
route at all and never touch NAT either way (ADR-0033).

If the mode was switched down for a pause (§3.3.1) and you are bringing the stack back up for a
scored run, switch it back on **first**, before applying `eval` — the gateway container in `eval`
will start routing model calls through whichever path is live the moment it comes up:

```bash
cd infra/terraform/envs/dev/persistent
~/bin/terraform plan -var nat_gateway_enabled=true -out=nat-up.tfplan
# review the plan — see §3.3.1 for the expected shape — then, with owner authorization:
~/bin/terraform apply nat-up.tfplan
```

Then verify egress actually works — this is the only one of the checks that proves anything, not
just configuration:

```bash
# start the SSM tunnel, open the UI, confirm GET /models returns live aliases.
# That call is API -> LiteLLM gateway -> OpenRouter, so it exercises the whole NAT path end to end.
```

If `persistent` is already in the mode you want (the common case — it defaults on and this runbook
recommends leaving it on for the duration of the e2e phase, §3.3.1), skip straight to §4.1.

### 4.1 Order

```
persistent (already up — do NOT re-apply casually)
     │
     ▼
    ui        ← FIRST. eval cannot plan without its api_alb_dns output.
     │
     ▼
   eval       ← second.
```

```bash
export AWS_PROFILE=eval-framework AWS_REGION=us-west-2
cd infra/terraform/envs/dev/ui   && ~/bin/terraform init && ~/bin/terraform apply
cd ../eval                       && ~/bin/terraform init && ~/bin/terraform apply
```

**`terraform apply` is builder 1's to run, and only after the owner authorises it.** Everyone else:
plan, show the plan, stop.

### 4.2 Before you apply `eval` — regenerate the harness task families

The 40 `eval-dev-harness-<envhash>` families are **derived from the warm-cache manifest**, not
hand-maintained. If images were rebuilt since the last apply, regenerate first or the families will
point at tags that no longer exist:

```bash
uv run python scripts/gen_harness_task_families.py     # reads the live S3 manifest
git diff --stat infra/terraform/envs/dev/eval/harness-task-families.auto.tfvars.json
```

### 4.3 Verify the bring-up

```bash
# 1. Every service reaches its desired count
aws ecs describe-services --cluster eval-dev-cluster \
  --services orchestrator-api run-supervisor results-writer gateway eval-worker harness-dispatcher git-mirror \
  --query 'services[].{name:serviceName,desired:desiredCount,running:runningCount}' --output table

# 2. The control plane started WITHOUT a schema-drift error.
#    This is the only cheap proof that Aurora's schema matches the code —
#    the drift check raises on a missing column. Read the FIRST log lines of
#    BOTH halves — run_migrations()/ensure_additional_databases() run in
#    results-writer's startup, not run-supervisor's (CONTROL-PLANE-
#    DECOMPOSITION-DESIGN-2026-08-31.md).
aws logs tail /aws/ecs/eval-dev-run-supervisor --since 10m --format short | head -40
aws logs tail /aws/ecs/eval-dev-results-writer --since 10m --format short | head -40

# 3. The git mirror cloned all twelve repos — a green deployment only proves the container started
aws logs tail /aws/ecs/eval-dev-git-mirror --since 15m --format short | grep -iE "pylint|FATAL|error"

# 4. Every harness family has a matching image (35 families vs 2 images has happened)
#    Note both quirks: exclude eval-dev-harness-DISPATCHER, which shares the prefix
#    but is not a per-env family; and guard imageTag against null or the
#    ends_with() blows up on untagged images.
aws ecs list-task-definition-families --status ACTIVE --family-prefix eval-dev-harness \
  --query "length(families[?@!='eval-dev-harness-dispatcher'])" --output text
aws ecr list-images --repository-name eval-dev-harness-worker \
  --query "length(imageIds[?imageTag!=null && ends_with(imageTag, '-hw')])" --output text
```

**Those two numbers must match.** A family whose `-hw` tag is missing fails at pull time with
`CannotPullContainerError`, and you find out by spending money on a dispatch. (Right now, with
`eval` destroyed, they read `0` and `40` — the families come back with the apply.)

### 4.4 Things that CHANGE across a destroy/apply cycle

- **The ALB DNS name is new.** Anything holding the old hostname breaks — UI config, bookmarks, the
  SSM tunnel document. Re-read it: `cd envs/dev/ui && ~/bin/terraform output api_alb_dns`.
- **The Valkey endpoint is new.** `eval` outputs `redis_endpoint`; `ui` picks it up on its *next*
  apply. **So after recreating `eval`, re-apply `ui`** or the control plane keeps the stale endpoint
  and the control panel sits fail-closed.

  **Re-applying `ui` is NOT enough on its own** (found the hard way, 2026-08-22). The orchestrator
  services carry `ignore_changes = [task_definition]`, so the apply registers a new revision with
  the correct `REDIS_URL` and then **leaves the service running the old one** — which still has
  `REDIS_URL=''`, and the control plane crashes on `redis.Redis.from_url("")`. The apply reports
  `Services updated` and looks clean. You must move the service to the new revision explicitly —
  **and `--force-new-deployment` on its own does NOT do that** (verified 2026-09-03: it restarted
  the old revision, `REDIS_URL` still empty, `Connection refused localhost:6379` in the log). Pass
  `--task-definition <family>` — with no revision suffix it resolves to the latest ACTIVE one:

  ```bash
  for s in orchestrator-api:eval-dev-orchestrator-api run-supervisor:eval-dev-run-supervisor \
           results-writer:eval-dev-results-writer; do
    aws ecs update-service --cluster eval-dev-cluster --service "${s%%:*}" \
      --task-definition "${s##*:}" --force-new-deployment --region us-west-2 \
      --query 'service.deployments[0].taskDefinition' --output text
  done
  ```

  Then confirm the running task is on the NEW revision, not just that the service is green:

  ```bash
  aws ecs describe-services --cluster eval-dev-cluster --services run-supervisor results-writer \
    --query 'services[].deployments[].{rev:taskDefinition,status:status,running:runningCount}'
  ```
- **Task definition revisions jump.** Harmless — services reference families, and images use mutable
  tags, so a redeploy picks up new images without new revisions.

### 4.5 Purge stranded queue messages before the first run

A destroy does not drain SQS — the queues live in `persistent`. A job enqueued before the teardown
is still there (14-day retention) and **gets consumed the instant the dispatcher comes back**,
against a run that may no longer exist.

```bash
for q in harness-jobs eval-jobs results llm-calls; do
  printf "%-14s " "$q"
  aws sqs get-queue-attributes --queue-url "$(aws sqs get-queue-url --queue-name eval-dev-$q --query QueueUrl --output text)" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible --query Attributes --output text
done
# If a queue is non-empty and you know the work is dead:
# aws sqs purge-queue --queue-url <url>
```

### 4.6 Before a scored run that spans 03:00 PDT — switch the nightly guard OFF

Owner decision 2026-09-03: for a multi-hour scored run (the 500-instance qwen run) the nightly
scale-to-zero guard (§5) is **disabled**, not trusted to refuse. Its "running tasks" check is a
safety net; a refusal that fails to fire costs the whole run's inference spend. The Lambda and its
IAM stay — only the EventBridge rule's state flips, one in-place attribute:

```bash
export AWS_PROFILE=eval-framework AWS_REGION=us-west-2
cd infra/terraform/envs/dev/persistent
~/bin/terraform plan -var nightly_scale_to_zero_paused=true \
  -target='module.observability.aws_cloudwatch_event_rule.nightly_scale_to_zero[0]' -out=pause.tfplan
#   EXPECT exactly:  ~ state = "ENABLED" -> "DISABLED"    Plan: 0 to add, 1 to change, 0 to destroy.
~/bin/terraform apply pause.tfplan
aws events describe-rule --name eval-dev-nightly-scale-to-zero --query State --output text   # DISABLED
```

**Why `-target`:** `persistent` is a shared root and carries out-of-band drift from other builders —
at the 2026-09-03 bring-up the untargeted plan also offered `eval-dev-build-asg desired 2 -> 0`,
which would have terminated builder 5's live image build (§8.5). Target the rule, nothing else.

**Re-enable at teardown (§3.5).** Leaving it disabled after the run means the night you forget has
no safety net at all.

---

## 5 · The nightly scale-to-zero guard

### 5.1 What it is

`eval-dev-scale-to-zero` (Lambda, in `persistent`) fires on EventBridge rule
`eval-dev-nightly-scale-to-zero` and scales the six services and the eval ASG to zero — **but only
if every idle check passes.** Any check that reports work, or that *errors*, means REFUSE and leave
everything running. That direction is deliberate: a six-hour run trivially spans the cron, and
killing it destroys sunk inference spend.

**Schedule: `cron(0 10 * * ? *)` = 03:00 PDT / 02:00 PST.** EventBridge cron is always UTC — there
is no timezone field on `aws_cloudwatch_event_rule`. Change it via
`var.nightly_scale_to_zero_cron`.

The guard **scales to zero; it does not destroy** — so it leaves the whole
§3.1 Option-A tail running: 14 VPC endpoint ENIs, both ALBs, the cache, the NAT
instance. **Measured 2026-08-24, that is ~$5.70/day, about $3.75/day more than a
§3.3 destroy**, for an environment where nothing is running.

**The guard is not a substitute for the overnight teardown.** Treat it as a
safety net for the night you forget, not as the plan. If you are done for the
day, run the §3.3 destroy yourself.

### 5.2 Reading a refusal

```bash
aws logs tail /aws/lambda/eval-dev-scale-to-zero --since 3d --format short | grep -i refus
```

Every invocation logs a structured line with each check's result. **A refusal is not an error** —
Lambda `Errors` will read 0. Check `Invocations` vs the refusal count, not `Errors`:

```bash
aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Errors \
  --dimensions Name=FunctionName,Value=eval-dev-scale-to-zero \
  --start-time "$(date -u -v-7d +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 86400 --statistics Sum
```

| Refusal reason | What it means | Fix |
|---|---|---|
| `active runs in Postgres` + `postgres_active: N` | N rows in `runs` are `pending`/`running` | If no run is really live, they are **stale rows from a crashed run** and they block the guard *forever*. Clear them — the guard cannot. |
| `postgres: empty` | the `COUNT(*)` returned no rows at all | Not stale runs — a query/Data-API problem. Different bug, same old symptom. |
| `queued/in-flight work {...}` | a message is sitting in harness-jobs/eval-jobs | §4.5 |
| `running tasks {...}` / `running cluster tasks` | something genuinely is running | Working as designed — usually means the cron is too early, not that the guard is broken |
| `... check ERRORS (fail-safe refuse)` | an API call failed — IAM, throttling | Real bug. Read the exception in the log line. |

**Aurora idle check, and why you must not shortcut it:** the guard reads
`ServerlessDatabaseCapacity` from CloudWatch and treats capacity `0` as paused-therefore-idle
*without* querying. Only an awake cluster gets a `runs` query. Use the same trick yourself:

```bash
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name ServerlessDatabaseCapacity \
  --dimensions Name=DBClusterIdentifier,Value=eval-dev-aurora-v2 \
  --start-time "$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 --statistics Average --output text | sort -k3
```

`0.0` = parked. It takes ~5 minutes of zero connections (`seconds_until_auto_pause = 300`).

### 5.3 Test it without waiting for 03:00

```bash
aws lambda invoke --function-name eval-dev-scale-to-zero --payload '{}' /tmp/out.json && cat /tmp/out.json
```

Safe to run any time: if work is in flight it refuses, which is the whole point.

---

## 6 · What idle actually costs

**Measured Aug 1–24 2026 (Cost Explorer, unblended). Read §3.1 first — the
number depends entirely on which teardown you ran.**

| | Option A (scale to zero) | Option B (`eval` + `ui` destroyed) |
|---|---|---|
| VPC interface endpoints | $3.36/day (14 ENIs) | $1.20/day (5 ENIs) |
| ALBs | $1.08/day (2) | $0 |
| Public IPv4 | ~$0.60/day | ~$0.15/day |
| CloudWatch custom metrics | ~$0.24/day | ~$0.24/day |
| NAT instance | $0.25/day | $0.25/day |
| ElastiCache Serverless | ~$0.10/day | $0 |
| Secrets · Route 53 · ECR | ~$0.10/day | ~$0.10/day |
| **Total** | **~$5.70/day (~$172/mo)** | **~$1.95/day (~$58/mo)** |

**The lever is the endpoints and the ALBs, not ECR.**

> **Correction (2026-08-24).** This section previously claimed *"ECR — 175 GB —
> ~$17.50/mo … ECR is the lever, not compute."* That is wrong by roughly 10×.
> ECR bills **unique layer blobs**, and summing `imageSizeInBytes` across tags
> double-counts every shared base layer. The actual billed line for August was
> **12.1 GB-months → $1.21** (`USW2-TimedStorage-ByteHrs`). Pruning superseded
> `-hw` tags is still good hygiene — it is not a cost lever.
>
> Always take ECR cost from the billed usage-type line, never from image sizes.

The rest of a real day's spend is work, not idle: Fargate vCPU-hours, the
`c5d.2xlarge` spot host during image builds, and Aurora ACU-hours. Those are
what you are paying for on purpose.

---

## 7 · Rebuilding harness `-hw` layers

The warm job reaps each layer the moment it is pushed (`990dbb5`), so a 40-layer rebuild holds
~3.4 GB rather than accumulating ~136 GB. Before that fix it died at layer 29 of 40 with ENOSPC.

**If a rebuild dies part-way, do NOT delete ECR tags to resume.** The `-hw` tag encodes the env
hash only — not `FRAMEWORK_SHA`, not the Dockerfile — so stale and fresh layers look identical, and
deleting the stale ones leaves the harness task families unable to pull until the rebuild lands.
Name the hashes instead:

```
FORCE_HARNESS=1                       # rebuild all 40
FORCE_HARNESS=<hash>,<hash>,...       # rebuild only these (resume a partial run)
```

An unmatched hash raises rather than silently rebuilding nothing.

Which layers are stale is a push-date question:

```bash
aws ecr describe-images --repository-name eval-dev-harness-worker \
  --query 'imageDetails[].{t:imageTags[0],p:imagePushedAt}' --output json \
  | python3 -c "import json,sys;[print(x['p'][:19],x['t']) for x in sorted((y for y in json.load(sys.stdin) if y['t'] and y['t'].endswith('-hw')),key=lambda z:z['p'])]"
```

Watch two lines in the run log: `reaped local image ...` once per layer (free disk must not trend
down), and `built+pushed 0 env and N harness images` — **`0 env` is the proof no env hash churned.**

---

## 8 · The image-build tier (`envs/dev/build`) — builder5-image-build-tier Stage 2

### 8.1 What it is and why it is a fourth, separate root

Building images used to require the ENTIRE eval tier up (cache, gateway, git-mirror, eval-worker,
dispatcher), because the warm-job task definition lived in `envs/dev/eval`, and image building
shared ONE EC2 ASG (max 1) with eval grading — so neither could be sized for its own workload.
`envs/dev/build` fixes both: its own cluster, its own ASG (independently sized for image builds),
its own task-definition family.

| | `envs/dev/eval`'s `module "warm_job"` (still the active path) | `envs/dev/build` (new, additive) |
|---|---|---|
| cluster | `eval-dev-cluster` | `eval-dev-build-cluster` |
| ASG | `eval-dev-eval-asg` (shared with eval grading, `c5d.2xlarge`, max 1) | `eval-dev-build-asg` (`c5d.2xlarge`, max 4, dedicated) |
| task-definition family | `eval-dev-warm-job` | `eval-dev-image-build` |
| task reservation | `cpu=1024, memory=512` (fiction — sized to fit a c5d.large's ECS-agent overhead, not the real build) | `cpu=7680, memory=14000` — sized to ~the whole c5d.2xlarge host, so ECS can place only ONE shard per host |

**Instance type: `c5d.2xlarge`, not `m5d.2xlarge`.** The build brief's original §3 recommended
`m5d.2xlarge` (32 GiB) against an assumed memory-OOM risk. Reviewer correction (2026-08-27): the
build host is ALREADY `c5d.2xlarge`, chosen deliberately after a REAL, recorded disk-full (commit
`11a6dec`: 5 env images + build cache filled a `c5d.xlarge`'s 75 GB NVMe, prune only freed 1.4→6
GB) — disk, not memory, is the documented failure mode, and it was already fixed at this size.
Stage 1 measured on this exact type: single-build peak host memory ~2.7 GB of ~15.24 GiB registered
(light env, no C-compile) with disk headroom never remotely tight. `c5d.2xlarge` is the baseline;
T1 (matplotlib, W=4, the real C-compile stress case) is what proves or overturns it, not a guess.

**Why a wholly separate CLUSTER, not a second capacity provider on `eval-dev-cluster`** (the
original Stage 2 shape): `eval-dev-cluster`'s `aws_ecs_cluster_capacity_providers` resource already
lists `aws_ecs_capacity_provider.ec2`, which references `aws_autoscaling_group.eval` — so ANY edit
to that shared list forces Terraform to re-plan that whole dependency chain, including the eval
ASG. Live drift was found this way while planning the original shape (2026-08-27): the eval ASG's
live `min_size`/`desired_capacity` were `1`/`1` (builder 1 holding a build host up between retry
attempts) while the Terraform config said `0`/`0` — a full apply would have "corrected" that drift
and terminated their host as a side effect of a change that had nothing to do with them. A separate
cluster (`modules/ecs-cluster-build`) has its own capacity-provider-list resource with zero
reference to anything in the eval cluster's graph, so nothing here can ever touch the eval ASG, no
matter what state it drifts into.

**Status as of 2026-08-27: infra only, ADDITIVE.** `envs/dev/eval`'s `module "warm_job"` is still
the path in active use (builder 1 builds through it today) — `envs/dev/build` exists in parallel and
is not yet the thing that actually builds images at scale. The 500-capable builder script, the
per-instance task families, and the 500-image build itself are separate, later stages of the same
effort. **Removing the old path is a deliberate final step** after the new tier is proven, with the
owner's explicit go-ahead — never do it as a side effect of something else.

### 8.2 NOT covered by the nightly scale-to-zero guard — you must scale it down yourself

The guard Lambda (`eval-dev-scale-to-zero`, §5) reads exactly ONE `ECS_CLUSTER` env var before
zeroing every ASG in `ASG_ARNS`. Adding the build ASG to that list without also teaching the Lambda
to check the BUILD cluster's own running tasks would let the guard zero a build host while a shard
is actively building on it — the Lambda genuinely cannot see tasks on a cluster it was never told
about. Extending it safely means changing `_idle_cluster_tasks` to check a cluster per-ASG rather
than one shared cluster for all — a change to shared safety-critical code, flagged for the owner
rather than made unilaterally.

**Practical consequence: `eval-dev-build-asg` must be scaled back to 0 by hand after every build
run, every time.** It is not swept by the 03:00 PDT guard the way the eval ASG is. Treat forgetting
this exactly like forgetting §3.2 for the eval ASG, except there is no safety net at all here.

### 8.3 Bring-up (run a build)

```bash
export AWS_PROFILE=eval-framework AWS_REGION=us-west-2

# 1. Apply the tier once (task-definition changes only after this; skip if already applied)
cd infra/terraform/envs/dev/build && ~/bin/terraform init && ~/bin/terraform apply

# 2. Before touching anything: confirm nothing of builder 1's is mid-run on the OTHER cluster/ASG
aws ecs list-tasks --cluster eval-dev-cluster
aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names eval-dev-eval-asg \
  --query 'AutoScalingGroups[0].{min:MinSize,desired:DesiredCapacity,instances:length(Instances)}'
# (informational only — envs/dev/build's own resources never touch these; this is just situational
# awareness before spending EC2 time)

# 3. Scale the BUILD asg up (0 -> N hosts)
aws autoscaling set-desired-capacity --auto-scaling-group-name eval-dev-build-asg --desired-capacity 1

# 4. Wait for a container instance to register
aws ecs list-container-instances --cluster eval-dev-build-cluster

# 5. Get that instance's subnet, then run-task against the BUILD cluster/capacity-provider
SUBNET=$(aws ecs describe-container-instances --cluster eval-dev-build-cluster \
  --container-instances $(aws ecs list-container-instances --cluster eval-dev-build-cluster \
    --query 'containerInstanceArns[0]' --output text) \
  --query "containerInstances[0].attributes[?name=='ecs.subnet-id'].value | [0]" --output text)

aws ecs run-task --cluster eval-dev-build-cluster --task-definition eval-dev-image-build --count 1 \
  --capacity-provider-strategy capacityProvider=eval-dev-build-capacity-provider,weight=1,base=1 \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET],securityGroups=[sg-0ec587858cebbfd71],assignPublicIp=DISABLED}" \
  --enable-execute-command \
  --overrides '{"containerOverrides":[{"name":"warm-job","command":["phase0-instances-v2","--only","<instance_id>,..."]}]}'
# phase0-instances-v2, not phase0-instances: the latter is builder 1's
# untouched six-instance mechanism (scripts/build_phase0_instances.py) — the
# 500-scale extension (ENV_SHARD, INSTANCE_MAX_WORKERS, resume) is a SEPARATE
# script (scripts/build_phase0_instances_v2.py) so their path never changes
# underneath them. Or, for a sharded run instead of --only: swap the command
# array for env overrides, e.g. {"name":"ENV_SHARD","value":"0/3"}.

# Watch logs:
aws logs tail /aws/ecs/eval-dev-image-build --since 5m --format short
```

### 8.4 Tear-down (after every run, no exceptions — §8.2)

```bash
export AWS_PROFILE=eval-framework AWS_REGION=us-west-2
aws ecs list-tasks --cluster eval-dev-build-cluster --desired-status RUNNING \
  --query 'length(taskArns)'          # must be 0 before the next line
aws autoscaling set-desired-capacity --auto-scaling-group-name eval-dev-build-asg --desired-capacity 0
```

Verify: `aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names eval-dev-build-asg
--query 'AutoScalingGroups[0].{desired:DesiredCapacity,instances:length(Instances)}'` reads `0` / no
instances (instances take a minute or two to actually terminate after `desired_capacity` drops).

### 8.5 A general lesson from building this tier — read `terraform plan` line by line, always

The drift described in §8.1 was found only because the FULL (untargeted) `terraform plan` for
`persistent` was read line-by-line before applying, not skimmed for "Plan: N to add." **Any resource
Terraform touches pulls in everything it depends on**, and a shared root like `persistent` can carry
live drift from someone else's out-of-band `aws` CLI action (here: builder 1 manually setting
`min-size`/`desired-capacity` to keep a host up between retries) that the plan will offer to
"correct" as a side effect of an unrelated change. If a plan shows an `update in-place` on something
you didn't intend to touch, stop and find out why before applying — `-target` scoped to exactly the
new resources is the safe path when the cause turns out to be pre-existing drift on something
someone else is actively using.
