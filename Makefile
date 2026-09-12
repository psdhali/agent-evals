# The operator's entry points — docs/SETUP.md in commands (adoption Phase 1c).
# Every target wraps scripts/adopt.py; `make help` lists them. Pass YES=1 to skip the
# per-plan confirmation (CI / an unattended bring-up).
#
# Identity comes from the environment: AWS_PROFILE (default eval-framework), AWS_REGION
# (default us-west-2), EVAL_ENV_PREFIX (default eval-dev) — the same defaults Terraform's
# settings.tf carries. `make bootstrap` writes them from setup.yaml.

UV       ?= uv
PY        = $(UV) run python
ADOPT     = $(PY) scripts/adopt.py
YESFLAG   = $(if $(YES),--yes,)

.PHONY: help doctor quotas bootstrap up up-persistent down down-nat roll verify service-images images gate \
        tunnel tunnel-gateway discover pin purge nat-gateway nat-instance guard-pause guard-resume \
        teardown-all account-empty local-smoke test lint

help:
	@echo "Laptop (no AWS):"
	@echo "  make test            pytest + ruff + black + mypy"
	@echo "  make local-smoke     one instance end to end on your laptop (scripts/local_smoke_test.sh)"
	@echo "Account setup:"
	@echo "  make doctor [SCALE=small|full]   tools, credentials, quotas, secrets, state bucket, HF, OpenRouter"
	@echo "  make quotas [SCALE=small|full]   request the vCPU quota increases a fresh account needs"
	@echo "  make bootstrap       state bucket + backend.hcl + tfvars + out-of-band secrets + terraform init (from setup.yaml)"
	@echo "  make up-persistent   apply the persistent tier (once)"
	@echo "  make service-images  build + push the seven service images (scripts/docker_build_push.sh)"
	@echo "  make images SUBSET=10 | ONLY=id1,id2 | (all)   the per-instance image pipeline, prints time + ECR size"
	@echo "Sessions:"
	@echo "  make up              ui -> eval -> ui -> dispatch Lambda -> roll -> verify"
	@echo "  make tunnel          SSM port-forward to the API on localhost:8000 (keep it running)"
	@echo "  make tunnel-gateway  SSM port-forward to the gateway on localhost:4000 (for make pin)"
	@echo "  make gate INSTANCES=id1,id2     gold gate (POST /images/validate)"
	@echo "  make discover ALIAS=<alias>     ceiling discovery for a model alias"
	@echo "  make pin             register the git-tracked model specs (provider pins) with the gateway"
	@echo "  make purge YES=1     drain stranded queue messages"
	@echo "  make down            eval -> ui destroy, verify, nightly guard back on"
	@echo "  make down-nat        same, then NAT egress back to the t3 instance"
	@echo "  make guard-pause | guard-resume     the nightly scale-to-zero rule"
	@echo "  make teardown-all    EVERYTHING out of the account (Aurora, buckets, state, secrets), then the sweep"
	@echo "  make account-empty   read-only sweep: exit 1 with a list if anything of ours remains"
	@echo "  make nat-gateway | nat-instance     private-subnet egress path"

test:
	$(UV) run pytest -q
	$(UV) run ruff check .
	$(UV) run black --check .
	$(UV) run mypy swebench_eval/ scripts/ tests/

lint:
	$(UV) run ruff check .
	$(UV) run black --check .

local-smoke:
	bash scripts/local_smoke_test.sh

doctor:
	$(ADOPT) doctor $(if $(SCALE),--scale $(SCALE),)

quotas:
	$(ADOPT) quotas $(if $(SCALE),--scale $(SCALE),)

bootstrap:
	$(ADOPT) bootstrap $(if $(DRY_RUN),--dry-run,)

up-persistent:
	$(ADOPT) up --persistent $(YESFLAG)

up:
	$(ADOPT) up $(YESFLAG)

down:
	$(ADOPT) down $(YESFLAG)

down-nat:
	$(ADOPT) down --nat-instance $(YESFLAG)

roll:
	$(ADOPT) roll

verify:
	$(ADOPT) verify

service-images:
	bash scripts/docker_build_push.sh

images:
	$(ADOPT) images $(if $(SUBSET),--subset $(SUBSET),) $(if $(ONLY),--only $(ONLY),) $(if $(HOSTS),--hosts $(HOSTS),) $(YESFLAG)

gate:
	$(ADOPT) gate --instances "$(INSTANCES)"

tunnel:
	$(ADOPT) tunnel

tunnel-gateway:
	$(ADOPT) tunnel --gateway

discover:
	$(ADOPT) discover $(ALIAS)

pin:
	$(ADOPT) pin

purge:
	$(ADOPT) purge $(YESFLAG)

nat-gateway:
	$(ADOPT) nat gateway $(YESFLAG)

nat-instance:
	$(ADOPT) nat instance $(YESFLAG)

guard-pause:
	$(ADOPT) guard pause $(YESFLAG)

guard-resume:
	$(ADOPT) guard resume $(YESFLAG)

teardown-all:
	$(ADOPT) teardown $(YESFLAG)

account-empty:
	$(ADOPT) sweep
