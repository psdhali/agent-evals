#!/usr/bin/env bash
# One instance on your laptop, no AWS: bring up the local stack, seed the gold-stripped
# dataset mirror into MinIO, run the custom minimal harness on one SWE-bench Verified
# instance INSIDE the official per-instance image (pinned digest) through the local LiteLLM
# gateway, grade the gold patch, a broken patch and the agent's patch with the official
# harness, tear the stack down.  Costs a few cents of inference.
#
# Needs: Docker (compose; ~5 GB free for the instance image), uv, a repo-root .env with
# OPENROUTER_API_KEY (copy .env.example).  On Apple Silicon the x86_64 image runs under
# emulation — slower, but it works (allow ~15 min the first time).
#
# Usage: bash scripts/local_smoke_test.sh [--instance-id <id>] [--keep-stack] [--model <alias>]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
INSTANCE="${SMOKE_INSTANCE_ID:-django__django-11099}"
MODEL="${SMOKE_MODEL_ALIAS:-cheap-oss-model}"
KEEP=0
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --instance-id) INSTANCE="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --keep-stack) KEEP=1; shift ;;
    --rebuild-image|--skip-grading) EXTRA+=("$1"); shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -f .env ] || { echo "FAIL: no .env at repo root (copy .env.example and set OPENROUTER_API_KEY)" >&2; exit 1; }
grep -qE '^OPENROUTER_API_KEY=.{10,}' .env || { echo "FAIL: OPENROUTER_API_KEY is not set in .env" >&2; exit 1; }
command -v docker >/dev/null || { echo "FAIL: docker not found" >&2; exit 1; }
command -v uv >/dev/null || { echo "FAIL: uv not found" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "FAIL: docker daemon not reachable (is Docker running?)" >&2; exit 1; }

COMPOSE=(docker compose -f infra/docker/docker-compose.yml)
cleanup() {
  if [ "$KEEP" = "0" ]; then
    echo "== stopping the local stack"
    "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
  else
    echo "== leaving the local stack up (--keep-stack)"
  fi
}
trap cleanup EXIT

echo "== starting the local stack (LiteLLM :4000, Postgres :5432, ElasticMQ :9324, MinIO :9000, Valkey :6379)"
"${COMPOSE[@]}" up -d
for i in $(seq 1 60); do
  if curl -fsS -m 3 -H "Authorization: Bearer ${LITELLM_MASTER_KEY:-sk-local}" http://localhost:4000/health >/dev/null 2>&1; then
    echo "== gateway healthy after $((i*3))s"; break
  fi
  [ "$i" = "60" ] && { echo "FAIL: gateway did not become healthy" >&2; "${COMPOSE[@]}" logs --tail 30 litellm >&2; exit 1; }
  sleep 3
done

OUT="${SMOKE_OUTPUT_DIR:-$ROOT/.smoke/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$OUT"

# The harness may only read the gold-stripped dataset mirror (never the Hugging Face copy,
# which carries the gold patch), so seed the mirror into the local MinIO first (F1).
export S3_ENDPOINT_URL="${S3_ENDPOINT_URL:-http://localhost:9000}"
export DATASET_BUCKET="${DATASET_BUCKET:-eval-dataset}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
echo "== seeding the dataset mirror into MinIO bucket $DATASET_BUCKET (once; 500 rows)"
uv run python scripts/seed_dataset.py --endpoint-url "$S3_ENDPOINT_URL" --bucket "$DATASET_BUCKET" --create-bucket

echo "== running $INSTANCE: agent in the pinned official image, then official grading (artifacts in $OUT)"
export LITELLM_BASE_URL="${LITELLM_BASE_URL:-http://localhost:4000/v1}"
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-local}"
set +e
uv run python scripts/smoke_test.py --instance-id "$INSTANCE" --model "$MODEL" --output-dir "$OUT" "${EXTRA[@]}" 2>&1 | tee "$OUT/smoke.log"
RC=${PIPESTATUS[0]}
set -e
if [ "$RC" != "0" ]; then
  echo "SMOKE FAILED (exit $RC) — see $OUT/smoke.log" >&2
  exit "$RC"
fi
echo "SMOKE OK — instance $INSTANCE, artifacts in $OUT (harness/patch.diff, harness/trajectory.jsonl, harness/harness_result.json)"
