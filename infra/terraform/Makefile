# Terraform orchestration — three-tier ordering (PA-10/PA-11)
#
# The envs are deliberately separate root modules with separate states (ADR-0022
# two-state split, extended to three tiers by PA-10). The ordering between them
# is exactly the kind of thing that goes wrong at 6pm on a Friday, so it lives
# here, not in someone's memory.
#
#   persistent/  always-on record: VPC, Aurora, S3×3, ECR, EFS, secrets, log
#                groups, SGs, SQS, empty ECS cluster. Applied once.
#   ui/          always-on history browsing: ALB + orchestrator-api. $0 when idle.
#   eval/        per-session eval fleet: Valkey, gateway, workers, Prometheus.
#                This is `make down` / destroy, recurring.
#   scale/       PLAN ONLY — read-only IAM profile (OPS-6); apply fails closed.
#
# Ordering rule: persistent must exist before ui/eval (they read its remote
# state). ui and eval are independent of each other.

TF        = terraform
PROFILE   = eval-framework

# Bring up the record + UI (persistent once, ui always-on). Eval fleet left down.
.PHONY: up ui-up eval-up down ui-down eval-down plan-all validate-all gen-harness-families

up: persistent-up ui-up

ui-up:
	cd envs/dev/persistent && terraform apply -auto-approve
	cd envs/dev/ui && terraform apply -auto-approve

eval-up:
	cd envs/dev/eval && terraform apply -auto-approve

persistent-up:
	cd envs/dev/persistent && terraform apply -auto-approve

# Tear down — eval fleet FIRST (it is the cost), then ui. persistent stays.
down: eval-down ui-down

eval-down:
	cd envs/dev/eval && terraform destroy -auto-approve
	# A1 / ADR-0033 DoD-9: interface endpoints are the one resource that keeps
	# billing (~$0.05/hr) if they survive eval-down. The teardown check must FAIL
	# LOUDLY if any `eval-dev-vpce-*` INTERFACE endpoint remains — a silent leak is
	# $36.50/month of the $100/quarter budget. (The persistent S3 GATEWAY endpoint
	# also matches the name wildcard but is `Gateway` type and SHOULD survive; the
	# filter is on INTERFACE endpoints only, so it is excluded.)
	@echo "== A1 DoD-9: asserting no eval-tier interface endpoints survived =="
	@n=$$(AWS_PROFILE=eval-framework aws ec2 describe-vpc-endpoints --region us-west-2 \
	  --filters Name=vpc-endpoint-type,Values=Interface Name=tag:Name,Values='eval-dev-vpce-*' \
	  --query 'length(VpcEndpoints)' --output text 2>/dev/null || echo ERR); \
	if [ "$$n" = "ERR" ] || [ "$$n" != "0" ]; then \
	  echo "FAIL: $$n eval-dev-vpce-* INTERFACE endpoint(s) survived eval-down — they bill ~\$$0.05/hr even idle. Investigate before leaving the night."; exit 1; \
	fi; \
	echo "OK: 0 eval interface endpoints remain."

ui-down:
	cd envs/dev/ui && terraform destroy -auto-approve

# Full record teardown — DELIBERATE, rare, requires the data protection off.
persistent-down:
	@echo "WARNING: destroying persistent/ removes the record (Aurora, S3, ECR, logs)."
	@echo "Aurora deletion_protection=true (PA-2) must be temporarily disabled by hand."
	@read -p "Type DESTROY-RECORD to confirm: " c; test "$$c" = "DESTROY-RECORD"
	cd envs/dev/persistent && terraform destroy -auto-approve

plan-all:
	cd envs/dev/persistent && terraform plan
	cd envs/dev/ui && terraform plan
	cd envs/dev/eval && terraform plan

# ADR-0030 H2: regenerate the per-env harness task-definition families from the
# warm-cache manifest (S3). Run after any warm-job run that changes the image
# set. The families are DERIVED — never hand-edit the generated tfvars.
gen-harness-families:
	cd ../.. && .venv/bin/python scripts/gen_harness_task_families.py

# scale/ is plan-only: under the read-only profile (OPS-6) plan renders, apply
# fails closed with AccessDenied — the DoD-3 artifact.
plan-scale:
	cd envs/scale && AWS_PROFILE=eval-framework-ro terraform plan -input=false

validate-all:
	@for e in persistent ui eval; do \
		echo "== envs/dev/$$e =="; \
		(cd envs/dev/$$e && terraform init -backend=false >/dev/null && terraform validate) || exit 1; \
	done
	@echo "== envs/scale (plan-only, read-only profile) =="
	(cd envs/scale && terraform init -backend=false >/dev/null && terraform validate)
