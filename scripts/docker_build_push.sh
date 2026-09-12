#!/usr/bin/env bash
# Build, push, and verify the four 5a-i service images (DoD 10).
#
# Covers the load-bearing guard for B-3/C-1: every image is built and pushed
# for linux/amd64, and the pushed manifest's architecture is READ BACK — the
# eval worker runs on the EC2 pool where no Fargate-side validation catches an
# arm64 slip, so this manifest check is the only guard for it.
#
# Usage:  [ONLY="orchestrator eval-worker"] bash scripts/docker_build_push.sh [--build-only] [--push-only]
#   (default: build + push; --build-only skips push; --push-only skips build;
#    ONLY restricts the image set — e.g. a dispatcher-only fix while another
#    builder's -inst run is consuming the harness-worker base by :latest, which
#    a full rebuild would move underneath it mid-run — 2026-09-03)
set -euo pipefail

# Adoption Phase 1a: region / account / prefix come from the environment (or STS), never literals.
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region --profile "${AWS_PROFILE:-eval-framework}" 2>/dev/null || echo us-west-2)}}"
ACCOUNT="${ACCOUNT_ID:-${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text --region "$REGION" --profile "${AWS_PROFILE:-eval-framework}")}}"
PREFIX="${EVAL_ENV_PREFIX:-eval-dev}"
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
# gateway (5a-ii): the LiteLLM image WITH our config baked — the stock image has
# no /app/config.yaml and ECS has no bind mounts. Built+verified like the rest.
# dispatch (5a-ii, ADR-0024): the VPC-attached Lambda that turns an S3-dropped
# run config into SQS jobs. Both are container images, all amd64.
# git-mirror (adoption Phase 2 finding 13): the eval tier runs it as a service, so it is one
# of the images a deployment needs; it was built by hand on the originating account.
IMAGES=(orchestrator harness-worker eval-worker warm-job gateway dispatch git-mirror)
if [[ -n "${ONLY:-}" ]]; then
  read -r -a IMAGES <<< "$ONLY"
  for img in "${IMAGES[@]}"; do
    [[ -f "infra/docker/Dockerfile.$img" ]] || { echo "FAIL: ONLY names unknown image '$img'"; exit 1; }
  done
fi
BUILD=true; PUSH=true
[[ "${1:-}" == "--build-only" ]] && PUSH=false
[[ "${1:-}" == "--push-only" ]] && BUILD=false

# PA-9 provenance: bake the framework SHA + gateway-config hash INTO the images
# (not a runtime env), so a container has config_snapshot facts without git or
# the repo's infra/ dir. The Dockerfiles ARG them in.
FRAMEWORK_SHA="${FRAMEWORK_SHA:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
GATEWAY_CONFIG_HASH="${GATEWAY_CONFIG_HASH:-$(sha256sum infra/docker/litellm_config.yaml 2>/dev/null | awk '{print $1}' || echo unknown)}"

# 2026-09-02: publish an IMMUTABLE, content-addressed tag (:<framework-sha>)
# ALONGSIDE the mutable :latest. Root cause of the -inst staleness (and the
# 2026-08-27 refresh-not-carrying-code incident): :latest is a shared mutable
# pointer — a reused EC2 host serves its cached :latest, and concurrent builders
# clobber each other's push. A per-commit tag is the reference a task-def can
# pin exactly (data.aws_ecr_image image_tag = the sha), and it can never be
# clobbered by a different commit's build. A dirty working tree (tracked changes
# vs HEAD) TAINTS the tag with -dirty so it never claims to be a clean commit.
# :latest is still pushed, so nothing that references it today breaks.
SHA_TAG=""
if [[ "$FRAMEWORK_SHA" != "unknown" ]]; then
  if git diff --quiet HEAD 2>/dev/null; then
    SHA_TAG="$FRAMEWORK_SHA"
  else
    SHA_TAG="${FRAMEWORK_SHA}-dirty"
    echo "WARNING: working tree has tracked changes vs HEAD — content tag will be :$SHA_TAG"
  fi
fi

for img in "${IMAGES[@]}"; do
  repo="$PREFIX-$img"
  uri="$REGISTRY/$repo"
  build=$BUILD
  if ! $BUILD; then
    # Clean-pass finding 26: --push-only pushed whatever :latest sat in the laptop's
    # Docker cache and tagged it with the CURRENT HEAD sha — a fresh account ran five
    # images built 28 commits earlier, labelled as HEAD. The baked FRAMEWORK_SHA is the
    # truth: push-only is honoured only when it equals the sha we are about to tag with;
    # otherwise the image is rebuilt here.
    baked=$(docker image inspect "$uri:latest" --format '{{json .Config.Env}}' 2>/dev/null \
      | grep -oE 'FRAMEWORK_SHA=[0-9a-f]+' | cut -d= -f2 || true)
    if [[ -z "$baked" ]]; then
      echo "=== $repo: no local :latest with a baked FRAMEWORK_SHA — building"
      build=true
    elif [[ "$baked" != "$FRAMEWORK_SHA" ]]; then
      echo "=== $repo: local :latest is built from ${baked:0:7}, HEAD is ${FRAMEWORK_SHA:0:7} — rebuilding"
      build=true
    fi
  fi
  if $build; then
    echo "=== build $repo (linux/amd64) ==="
    # E2 (DoD 10 review): --provenance=false --sbom=false drops the buildx
    # attestation, so one untagged manifest per push is NOT created (at 5b, two
    # per push would blow past the repo's 20-untagged lifecycle rule fast, and
    # the referenced-manifest question is safer settled at 5b's 24h test).
    docker build --platform linux/amd64 --provenance=false --sbom=false \
      --build-arg FRAMEWORK_SHA="$FRAMEWORK_SHA" \
      --build-arg GATEWAY_CONFIG_HASH="$GATEWAY_CONFIG_HASH" \
      -f "infra/docker/Dockerfile.$img" -t "$uri:latest" .
  fi
  if $PUSH; then
    echo "=== push $repo ==="
    aws ecr get-login-password --region "$REGION" --profile "${AWS_PROFILE:-eval-framework}" \
      | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
    docker push "$uri:latest"
    if [[ -n "$SHA_TAG" ]]; then
      # Tag from the just-pushed :latest (present locally in build+push AND in
      # --push-only, which requires the :latest image to already exist) so the
      # immutable :<sha> tag is the exact same manifest.
      docker tag "$uri:latest" "$uri:$SHA_TAG"
      docker push "$uri:$SHA_TAG"
      echo "    pushed content tag: $repo:$SHA_TAG"
    fi
  fi
  # READ BACK the pushed architecture (B-3/C-1 — the guard). `docker manifest
  # inspect` is NOT reliable here: a --platform=linux/amd64 build with
  # --provenance=false pushes a SINGLE-arch OCI manifest (no `manifests` list,
  # so the old read-back saw nothing and FAILED). buildx imagetools inspect digs
  # the platform out of the image config for both shapes.
  if ! $PUSH; then
    # --build-only (adoption Phase 2 finding 4): nothing was pushed, so read the LOCAL
    # image's architecture — the registry may not even exist yet (a fresh account
    # before its persistent tier).
    local_arch=$(docker image inspect "$uri:latest" --format '{{.Architecture}}')
    echo "=== $repo built architecture: $local_arch"
    [[ "$local_arch" == "amd64" ]] || { echo "FAIL: $repo built as $local_arch, not amd64"; exit 1; }
    continue
  fi
  echo -n "=== $repo pushed architecture: "
  archs=$(docker buildx imagetools inspect "$uri:latest" --format '{{json .Image}}' 2>/dev/null \
    | python3 -c "
import json, sys
d = json.load(sys.stdin)
def arches(x):
    if isinstance(x, dict):
        if x.get('architecture'):
            yield x['architecture']
        for v in x.values():
            yield from arches(v)
    elif isinstance(x, list):
        for v in x:
            yield from arches(v)
print(' '.join(sorted(set(arches(d)))))
")
  echo "$archs"
  [[ "$archs" == *amd64* ]] \
    || { echo "FAIL: $repo has no amd64 manifest entry"; exit 1; }
done
echo "ALL IMAGES BUILT, PUSHED, AND VERIFIED amd64"