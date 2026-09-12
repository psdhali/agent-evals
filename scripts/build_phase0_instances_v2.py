#!/usr/bin/env python3
"""Build the per-instance `-inst` harness images at 500-instance scale (ADR-0043).

Runs INSIDE the warm-job/image-build container (internet present, host docker
socket via /var/run/docker.sock, ECR push role, Docker Hub token).  Per instance:

  * pull SWE-bench's PUBLISHED instance image by the DIGEST the committed
    snapshot pins (``swebench_eval/dataset/image_digests/<dataset>-<rev>.json``
    — never a live ``:latest`` lookup: the tag moves, the pin does not);
  * stack the six-CLI layer (Dockerfile.harness-worker-env), the instance layer
    (sentinel + wheelhouse; records base_kind/base_image/base_image_digest) and
    Dockerfile.harness-worker-refresh (framework, LAST);
  * push as ``<version>-<instance_id>-inst-official``; with ``--promote``
    (INST_PROMOTE=1) back the current ``-inst`` up as ``-inst-prev`` and point
    ``-inst`` at the new image, then write the per-instance manifest record
    (``cache-manifest/<version>/instances/<id>.json``) the warm job merges.

The 4.1.0-era base (our env image + the harness-generated install_repo_script)
is gone with the harness API that generated it; ``--base official`` is the only
base and the default.  ``<version>`` follows the harness (5.0.2-…), so the
checkpoint's 4.1.0 tags are never overwritten.

Targets are DERIVED from the pinned S3 dataset mirror — not a hardcoded list.
Three ways to pick a target set, in priority order, all going through the SAME
resume+build path:

  --only <id>,<id>,...     explicit instance ids (any id in the mirror)
  ENV_SHARD=<i>/<n>        this shard's slice of the (optionally ENV_FILTER'd
                            and ENV_LIMIT'd) target set, LPT-bin-packed by
                            estimated build time across REPOS (one host pulls
                            one repo's images back to back)
  (neither)                every instance in the (optionally filtered) set

ENV_FILTER=<repo-prefix>,...   keep only rows whose repo starts with a prefix
ENV_LIMIT=<n>                   cap to the n repos with the most instances
FORCE_INSTANCES=<id>,<id>,...   rebuild these even if already pushed to ECR
                                 (never delete a tag to force a rebuild)
INSTANCE_MAX_WORKERS / INSTANCE_BUILD_MEM_MB / INSTANCE_WORKERS   parallelism
                                 (see _instance_parallelism)

Resume: a crashed run re-invoked with the same target set skips whatever is
already in ECR (`_ecr_inst_tags`) — never deletes tags to force a redo.

Logs per-instance wall-clock + final image size; prints one JSON array.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Collection
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from swebench_eval import aws_names
from swebench_eval.dataset.extra_wheels import extra_wheels_for

logger = logging.getLogger("phase0-v2")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_REGION = aws_names.region()
# Account + registry host; used for docker login (never derived from the repo
# name, which is not a resolvable host).  Matches warm_image_cache's ECR_REGISTRY.
# Adoption Phase 1a: ECR_REGISTRY from the task env, else derived (account via STS) —
# resolved in _resolve_defaults() at run time so importing this module needs no AWS.
_REGISTRY = os.environ.get("ECR_REGISTRY", "")
# HARNESS_IMAGE_REPO is the bare repo NAME (the warm-job task env sets it to
# `eval-dev-harness-worker`, same contract warm_image_cache uses).  It was once
# mishandled as a full DOCKER URL here, which made `_ecr_login`'s
# `_REPO_URL.split("/",1)[0]` yield the bare repo name as the registry host and
# docker login fail DNS-resolution on the `-inst` build.  Accept either: a bare
# repo name, or a legacy full URL (kept for any caller that still passes one).
_HARNESS_REPO = os.environ.get("HARNESS_IMAGE_REPO") or aws_names.named("harness-worker")
if "/" in _HARNESS_REPO:  # legacy: a full docker URL was passed
    _REPO_URL = _HARNESS_REPO
else:
    _REPO_URL = f"{_REGISTRY}/{_HARNESS_REPO}"
_VERSION = os.environ.get("SWEBENCH_VERSION", "5.0.2")
_FRAMEWORK_SHA = os.environ.get("FRAMEWORK_SHA", "unknown")
_GATEWAY_HASH = os.environ.get("GATEWAY_CONFIG_HASH", "unknown")
_DATASET_BUCKET = os.environ.get("DATASET_BUCKET", "")
# The public mirror of the pinned dataset revision (gold excluded); derived
# from the loader's single source of truth unless MIRROR_KEY overrides it.
_MIRROR_KEY = os.environ.get("MIRROR_KEY", "")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def _build_supports_progress_plain() -> bool:
    """Does this docker daemon accept `--progress=plain` on `docker build`?

    The phase0 EC2 host runs the LEGACY builder (no buildx / BuildKit), which
    rejects the flag (`unknown flag: --progress`).  PART2 §2.2 wanted it to keep
    the carriage-return progress bars from becoming hundreds of CloudWatch
    events, but only when the daemon supports it.  Probe once at import and
    gate both docker-build call sites on the result.
    """
    r = subprocess.run(
        ["docker", "build", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "--progress" in (r.stdout + r.stderr)


_SUPPORTS_PROGRESS_PLAIN = _build_supports_progress_plain()


def _redact(cmd: list[str]) -> list[str]:
    """Mask secret values passed as ``--password`` / ``-p`` so the logged argv
    never carries a credential (reviewer PART1 §2: _ecr_login passed the ECR
    token as an argv; sh() logged the full argv to CloudWatch, cleartext, in 4
    of 4 sampled streams, 90-day retention).  gitleaks cannot see it — nothing
    is committed."""
    out = list(cmd)
    for i, a in enumerate(out[:-1]):
        if a in ("--password", "-p"):
            out[i + 1] = "***"
    return out


def sh(cmd: list[str], *, check: bool = True) -> str:
    """Run ``cmd``, STREAMING stdout+stderr to the log as it happens (PART2 §2.2).

    ``capture_output`` hid the two most useful things in the build — docker
    push's per-layer ``Pushed`` / ``Layer already exists`` and the SWE-bench
    instance prep — until the process finished (or failed).  Stream merged
    output line-by-line so CloudWatch shows the build live and the push metric
    (§2.3) falls out of the returned text.

    Returns the full merged stdout so callers can count Pushed-vs-cached lines.
    """
    logger.info("$ %s", " ".join(_redact(cmd)))
    t0 = time.monotonic()
    out: list[str] = []
    p = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    assert p.stdout is not None
    for line in p.stdout:
        line = line.rstrip()
        out.append(line)
        logger.info("  | %s", line)
    rc = p.wait()
    logger.info("  = rc=%d in %.1fs", rc, time.monotonic() - t0)
    if check and rc:
        raise RuntimeError(f"cmd failed rc={rc}: {' '.join(_redact(cmd))}")
    return "\n".join(out)


def sh_quiet(cmd: list[str], *, check: bool = True) -> str:
    """Run ``cmd`` capturing output WITHOUT streaming (PART2 §2.2).

    `docker image inspect --format {{.Size}}` is parsed as an int downstream; a
    stray warning on stderr (which sh() merges in) would corrupt the parse.
    This is the quiet path for exactly that call."""
    logger.info("$ %s", " ".join(_redact(cmd)))
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and r.returncode != 0:
        for line in r.stderr.splitlines()[-20:]:
            logger.error("  err: %s", line)
        raise RuntimeError(f"cmd failed rc={r.returncode}: {' '.join(_redact(cmd))}")
    return r.stdout


def _ecr_login() -> None:
    import base64

    import boto3

    registry = _REGISTRY  # the ECR host — never the repo name (not resolvable)
    auth = boto3.client("ecr", region_name=_REGION).get_authorization_token()
    data = auth["authorizationData"][0]
    user, _, password = base64.b64decode(data["authorizationToken"]).decode().partition(":")
    # reviewer PART1 §2: --password-stdin, not --password <arg> — the token
    # never appears in the argv (and sh() redacts it even if it did).
    r = subprocess.run(
        ["docker", "login", "--username", user, "--password-stdin", registry],
        input=password,
        text=True,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        for line in r.stderr.splitlines()[-20:]:
            logger.error("  err: %s", line)
        raise RuntimeError(f"docker login failed rc={r.returncode}")
    logger.info("$ docker login --username %s --password-stdin %s", user, registry)


def _dockerhub_login() -> bool:
    """``docker login`` to Docker Hub with the credentials in Secrets Manager
    (``DOCKERHUB_SECRET_ID``, shape ``{"username", "accessToken"}`` — the
    ``ecr-pullthroughcache/eval-dev-dockerhub`` secret, OPS-10).

    Anonymous Hub pulls are rate-limited per source IP and every build host
    shares one NAT IP; two official pulls were fine, five hundred are not.
    Returns False (and logs) when no secret id is configured or the read
    fails — the build then proceeds anonymously, which is what it did before.
    The token never reaches argv or the log (``--password-stdin``).
    """
    secret_id = os.environ.get("DOCKERHUB_SECRET_ID", "").strip()
    if not secret_id:
        logger.info("DOCKERHUB_SECRET_ID unset; Docker Hub pulls are anonymous (rate-limited)")
        return False
    try:
        import boto3

        raw = boto3.client("secretsmanager", region_name=_REGION).get_secret_value(
            SecretId=secret_id
        )["SecretString"]
        creds = json.loads(raw)
        user, token = str(creds["username"]), str(creds["accessToken"])
    except Exception as exc:  # noqa: BLE001 - fall back to anonymous, loudly
        logger.warning(
            "Docker Hub credential %s unreadable (%s); pulling anonymously", secret_id, exc
        )
        return False
    r = subprocess.run(
        ["docker", "login", "--username", user, "--password-stdin"],
        input=token,
        text=True,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        logger.warning("docker login to Docker Hub failed rc=%d; pulling anonymously", r.returncode)
        return False
    logger.info("$ docker login --username %s --password-stdin  (Docker Hub)", user)
    return True


def _load_mirror() -> dict[str, dict[str, Any]]:
    """Download + index the pinned Verified mirror; instance_id -> row."""
    import boto3

    from swebench_eval.dataset.swebench_loader import _PINNED_REVISION, _mirror_key

    key = _MIRROR_KEY or _mirror_key("test", _PINNED_REVISION, include_gold=False)
    client = boto3.client("s3", region_name=_REGION)
    obj = client.get_object(Bucket=_DATASET_BUCKET, Key=key)
    rows: dict[str, dict[str, Any]] = {}
    for line in obj["Body"].read().decode("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows[row["instance_id"]] = row
    logger.info("mirror loaded: %d rows", len(rows))
    return rows


def _compute_repo_map(mirror: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """repo -> sorted instance_ids, over EVERY row in *mirror*.

    ADR-0043: there are no env images or env hashes any more, so the build
    groups by REPO (the unit that shares a base image lineage and a build-time
    profile).  Deterministic: keys and lists sorted, so two ENV_SHARD workers
    on different hosts compute byte-identical maps and therefore a
    byte-identical partition (§ _lpt_shard).
    """
    out: dict[str, list[str]] = {}
    for instance_id in sorted(mirror):
        repo = str(mirror[instance_id].get("repo", "")) or instance_id.split("__", 1)[0]
        out.setdefault(repo, []).append(instance_id)
    return out


def _apply_env_filter(
    env_map: dict[str, list[str]], mirror: dict[str, dict[str, Any]]
) -> dict[str, list[str]]:
    """Narrow to envs whose instances' repo starts with any ``ENV_FILTER`` prefix.

    Same semantics as warm_image_cache._apply_env_filter (repo-prefix
    allowlist, comma-separated, lowercased both sides) — applied per-env here
    since a whole env is either in or out (every instance behind one env
    image shares that env's repo... except an env can rarely span more than
    one repo-version; a prefix match keeps an env if ANY of its instances
    matches, which is the conservative/inclusive direction — never silently
    drops a wanted instance).
    """
    raw = os.environ.get("ENV_FILTER", "").strip()
    if not raw:
        return env_map
    prefixes = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    if not prefixes:
        return env_map
    kept: dict[str, list[str]] = {}
    for env_hash, instances in env_map.items():
        if any(str(mirror[i].get("repo", "")).lower().startswith(prefixes) for i in instances):
            kept[env_hash] = instances
    return kept


def _apply_env_limit(env_map: dict[str, list[str]]) -> dict[str, list[str]]:
    """Cap to the ``ENV_LIMIT`` envs with the MOST instances (deterministic).

    Unlike warm_image_cache's per-repo ENV_LIMIT (bounded by build-class
    priority, for a first small env BUILD), this script shards by env image
    (§3/§5.4), so the natural "make this run small" lever is env count, not
    repo count — and biasing toward the largest envs makes a small ENV_LIMIT
    run exercise real concurrency (T1 wants matplotlib's 5, not a 1-instance
    env) rather than an arbitrary alphabetical slice.
    """
    raw = os.environ.get("ENV_LIMIT", "0").strip()
    if not raw.isdigit() or int(raw) <= 0:
        return env_map
    limit = int(raw)
    ordered = sorted(env_map.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return dict(ordered[:limit])


# Real per-repo -inst build-time weights (seconds), measured 2026-09-04
# rebuilding 24 real instances (matplotlib/scikit-learn/astropy/django/
# seaborn/sphinx) at HEAD c4710ce/e62ec15 on c5d.2xlarge, W=3. Used by
# _lpt_shard's weight_fn to balance ENV_SHARD by estimated WALL TIME instead
# of raw instance count — see _lpt_shard's docstring for why raw count is
# the wrong balance metric once repos have very different build costs.
# Keyed by the instance_id's ORG prefix (the part before "__" — matches
# SWE-bench's own instance_id format exactly, no mirror/repo lookup needed).
# Repos with no real sample yet (sympy, xarray, pytest, pylint, requests,
# flask — all pure Python, no C extensions in their own build) fall back to
# _DEFAULT_REPO_WEIGHT_S, the average of the real LIGHT-env samples below.
# These are MEASUREMENTS, not guesses — update this table as more repos get
# real timing data from actual builds, the same discipline brief §5.2 asked
# for ("do not reuse the env numbers") applied to repo-level weights too.
_REPO_WEIGHT_S: dict[str, float] = {
    "matplotlib": 742.0,  # 12 real samples, C-compile heavy
    "scikit-learn": 466.0,  # 2 real samples only — smaller sample, revisit
    "django": 448.0,  # 1 real sample
    "astropy": 390.0,  # 1 real sample
    "sphinx-doc": 264.0,  # 1 real sample
    "mwaskom": 214.0,  # seaborn, avg of 2 real samples
}
_DEFAULT_REPO_WEIGHT_S = 306.0  # avg of astropy/django/seaborn/sphinx real samples


def _repo_weight_seconds(instance_id: str) -> float:
    """instance_id's org prefix (before "__") -> its real/estimated
    per-instance -inst build time in seconds. See _REPO_WEIGHT_S."""
    org = instance_id.split("__", 1)[0]
    return _REPO_WEIGHT_S.get(org, _DEFAULT_REPO_WEIGHT_S)


def _lpt_shard(
    env_map: dict[str, list[str]],
    shard_index: int,
    num_shards: int,
    weight_fn: Callable[[str], float] | None = None,
) -> list[str]:
    """Longest-Processing-Time bin-packing (brief §3/§5.4): sort envs by
    TOTAL WEIGHT descending (default: 1.0 per instance, i.e. raw instance
    count — the ORIGINAL behavior, unchanged when weight_fn is omitted),
    assign each to the currently least-loaded shard. Deterministic given a
    deterministic *env_map* (see _compute_env_map) — every ENV_SHARD worker
    computes the SAME partition independently, no coordination needed.
    Returns shard *shard_index*'s instance ids (disjoint from every other
    shard's, union == every instance in *env_map*).

    *weight_fn*, when given, maps an instance_id to its estimated build-time
    cost instead of counting it as 1. Found 2026-09-04 (T1 + 500-build
    planning): raw instance-count balancing ignores that a matplotlib/
    scikit-learn instance takes ~1.5-2.4x a light env's real wall time, so a
    shard that draws more heavy envs finishes later than one with the SAME
    instance count but lighter repos — hosts sit idle at the tail waiting on
    the unlucky shard. Weighting by real per-repo timing (_repo_weight_seconds)
    balances actual wall-clock completion across shards, not just item count.
    """
    if num_shards <= 0:
        raise ValueError(f"ENV_SHARD denominator must be positive, got {num_shards}")
    if not 0 <= shard_index < num_shards:
        raise ValueError(f"ENV_SHARD numerator {shard_index} out of range [0,{num_shards})")

    def _env_weight(instances: list[str]) -> float:
        if weight_fn is None:
            return float(len(instances))
        return sum(weight_fn(i) for i in instances)

    # Ties broken by env_hash (alphabetical) — same reason as _apply_env_limit.
    envs_sorted = sorted(env_map.items(), key=lambda kv: (-_env_weight(kv[1]), kv[0]))
    bin_loads = [0.0] * num_shards
    bin_envs: list[list[str]] = [[] for _ in range(num_shards)]
    for env_hash, instances in envs_sorted:
        idx = min(range(num_shards), key=lambda i: (bin_loads[i], i))
        bin_envs[idx].append(env_hash)
        bin_loads[idx] += _env_weight(instances)
    logger.info(
        "ENV_SHARD bin loads (%s per shard): %s",
        "estimated seconds" if weight_fn is not None else "instance count",
        {i: round(bin_loads[i], 1) for i in range(num_shards)},
    )
    mine = set(bin_envs[shard_index])
    return [iid for env_hash in sorted(mine) for iid in env_map[env_hash]]


def _host_total_mb() -> int:
    """Total host RAM in MiB (the daemon's builds run on the host, same
    reasoning as warm_image_cache._host_total_mb)."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def _instance_parallelism() -> int:
    """Build workers from per-build memory pressure + host RAM + buffer.

    Same shape as warm_image_cache._parallelism, DIFFERENT knobs
    (INSTANCE_* not ENV_*) and a DIFFERENT per-build estimate — brief §5.2:
    "Do not reuse the env numbers." Default INSTANCE_BUILD_MEM_MB=3072 is
    Stage 1's measured single-build peak (~2.7 GB, a LIGHT env with no
    C-compile) rounded up with headroom; it has NOT been confirmed against
    the C-compile path (scikit-learn/matplotlib) — T1 is what confirms or
    overturns both this number and INSTANCE_MAX_WORKERS's default of 4.
    INSTANCE_WORKERS (positive) hard-overrides for exactly that testing.
    """
    raw = _env("INSTANCE_WORKERS", "0").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)

    total_mb = _host_total_mb()
    if total_mb <= 0:
        return 1

    reserved = _positive_int_env("INSTANCE_RESERVED_MB", 1024)
    per_build = _positive_int_env("INSTANCE_BUILD_MEM_MB", 3072)
    buffer = float(_env("INSTANCE_BUFFER", "1.25") or 1.25)
    max_workers = _positive_int_env("INSTANCE_MAX_WORKERS", 4)

    net = total_mb - reserved
    if net <= 0:
        return 1
    workers = int(net // (per_build * buffer)) or 1
    return max(1, min(workers, max_workers))


def _free_disk_mb(path: str = "/") -> int:
    st = os.statvfs(path)
    return int((st.f_bavail * st.f_frsize) // (1024 * 1024))


def _prune_docker() -> None:
    logger.info("pruning docker build cache (free disk: %d MiB)", _free_disk_mb())
    sh(["docker", "system", "prune", "-f"], check=False)
    logger.info("prune done (free disk: %d MiB)", _free_disk_mb())


# Minimum free MiB before starting ONE build, same order of magnitude as
# warm_image_cache._MIN_FREE_PER_BUILD_MB (one env pull + build cache).
_MIN_FREE_PER_BUILD_MB = 15_000

# Disk-guard race fix (brief §5.3, builder1-per-instance-images-build-plan.md
# §5.3): "_ensure_disk_for_build checks a fixed floor, so N workers can all
# see 'enough free' and collectively exceed it." This script had no
# parallelism before Stage 3, so it never shipped that bug — but the fix has
# to land WITH the pool, not bolted on after. Two parts, both from the
# brief's own menu ("a semaphore around the check+build, or a floor of
# floor x workers" — this does both, cheaply):
#   1. the floor SCALES with the worst-case concurrent worker count, so even
#      if every worker's check lands at the same instant, the free space
#      observed at that instant already accounts for N builds' worth of
#      headroom, not one;
#   2. the read-prune-decide sequence is serialized under a lock, so no two
#      workers can even interleave the read — belt and braces, and it costs
#      nothing (the lock is held for a statvfs() call and, rarely, one prune).
_disk_lock = threading.Lock()


def _ensure_disk_for_build(what: str, workers: int) -> None:
    floor = _MIN_FREE_PER_BUILD_MB * max(1, workers)
    with _disk_lock:
        free = _free_disk_mb()
        if free >= floor:
            return
        logger.warning(
            "free disk %d MiB below %d MiB (floor = %d MiB x %d workers) before %s — pruning",
            free,
            floor,
            _MIN_FREE_PER_BUILD_MB,
            workers,
            what,
        )
        _prune_docker()
        free = _free_disk_mb()
        if free < floor:
            raise RuntimeError(
                f"insufficient free disk before {what}: {free} MiB < {floor} MiB "
                f"({_MIN_FREE_PER_BUILD_MB} MiB x {workers} workers) even after a prune. "
                "Tags already pushed to ECR are safe — re-run and it resumes from what is "
                "missing (never delete a tag to force a rebuild). If this repeats, the host "
                "needs a larger instance store or fewer INSTANCE_MAX_WORKERS."
            )


def _ecr_inst_tags(valid_ids: Collection[str] | None = None) -> set[str]:
    """Instance ids whose ``<version>-<instance_id>-inst`` tag is ALREADY in
    ECR — the resume-from-ECR-state check (brief §4 Stage 3 "resume from ECR
    state — a crashed run resumes by asking ECR what exists, never by
    deleting tags"). Fetched ONCE per run (a static snapshot): ENV_SHARD
    workers build disjoint envs, so no other actor should be pushing INTO
    this shard's target set mid-run, and one paginated describe-images call
    is far cheaper than one call per instance.

    ``valid_ids``, when given, cross-checks each tag-derived id against the
    real dataset mirror before including it. Without this, ANY ``-inst`` tag
    ever pushed — including throwaway test builds (T1/T2/T3's matplotlib/
    seaborn/pytest/xarray test envs, none of which are real target
    instances) — gets promoted verbatim. That's harmless for a resume check
    within one run (its own target set already scopes what it looks for),
    but this same function backs ``gen_harness_task_families.py
    --per-instance``, where an untrusted id becomes a PERMANENT,
    Terraform-managed, dispatcher-visible family. Found 2026-09-03 when
    builder 4's regen picked up 7 stray families (5 matplotlib, 2 seaborn)
    from this repo's own test builds.
    """
    import boto3

    client = boto3.client("ecr", region_name=_REGION)
    paginator = client.get_paginator("describe_images")
    prefix = f"{_VERSION}-"
    suffix = "-inst"
    out: set[str] = set()
    dropped: set[str] = set()
    for page in paginator.paginate(repositoryName=_HARNESS_REPO):
        for detail in page.get("imageDetails", []):
            for tag in detail.get("imageTags", []) or []:
                # endswith("-inst") alone is already exclusive of "-hw" tags (a
                # tag cannot end in both) — an extra "-hw" substring check
                # would only risk FALSE-excluding a legitimate -inst tag for
                # an instance_id that happens to contain "hw" anywhere, for
                # zero added safety. Suffix + prefix is the precise, correct
                # shape match on its own.
                if tag.startswith(prefix) and tag.endswith(suffix):
                    instance_id = tag[len(prefix) : -len(suffix)]
                    if valid_ids is not None and instance_id not in valid_ids:
                        dropped.add(instance_id)
                        continue
                    out.add(instance_id)
    if dropped:
        logger.warning(
            "_ecr_inst_tags: dropped %d ECR -inst tag(s) not in the dataset mirror "
            "(stray/test builds, not real targets): %s",
            len(dropped),
            sorted(dropped),
        )
    return out


# ---------------------------------------------------------------------------
# Official base (dev/IMAGE-PARITY-ROOT-CAUSE-AND-FIX-2026-09-05.md, ADR-0043)
#
# The 4.1.0-era path re-ran the harness-generated install_repo_script on OUR env
# image, re-solving every unpinned dependency at build time: the leaderboard
# graded matplotlib 3.5-3.7 against pandas 2.2.3 / numpy 1.25.2, our 2026-09
# rebuild got pandas 3.x and 13 Verified instances failed P2P regardless of the
# patch.  SWE-bench PUBLISHES the instance images it graded with; this builds
# the `-inst` FROM that image at the digest the committed snapshot pins and
# stacks our three layers (six-CLI layer, instance layer, framework refresh).
# ---------------------------------------------------------------------------
_OFFICIAL_NAMESPACE = os.environ.get("OFFICIAL_IMAGE_NAMESPACE", "swebench")
# ADR-0043: the dataset names the image by the MUTABLE `latest` tag.  The build
# never resolves it live — the digest comes from the committed snapshot
# (_pinned_official_digest); the tag is only recorded as provenance.
_OFFICIAL_TAG = os.environ.get("OFFICIAL_IMAGE_TAG", "latest")
_HUB_API = "https://hub.docker.com/v2/repositories"


def official_image_name(instance_id: str) -> str:
    """Docker Hub repository of SWE-bench's published instance image.

    ``owner__repo-N`` -> ``swebench/sweb.eval.x86_64.owner_1776_repo-N`` (the
    ``__`` becomes ``_1776_``; SWE-bench's own ``test_spec.instance_image_key``
    does the same rewrite for its namespace'd images).
    """
    return f"{_OFFICIAL_NAMESPACE}/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}"


def resolve_official_digest(instance_id: str, tag: str = _OFFICIAL_TAG) -> str:
    """The amd64 manifest digest behind ``<official image>:<tag>`` on Docker Hub.

    Resolved ONCE per build and recorded into the sentinel: the build then
    pulls by digest, so what was inspected is what was built from, and the
    provenance survives any later retag on the Hub.  Fails loudly if the tag
    is missing or carries no amd64 image — the harness runs on X86_64 Fargate.
    """
    import urllib.request

    name = official_image_name(instance_id)
    url = f"{_HUB_API}/{name}/tags/{tag}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    digest = str(data.get("digest") or "")
    archs = {str(img.get("architecture")) for img in data.get("images", []) or []}
    if not digest.startswith("sha256:"):
        raise RuntimeError(f"{name}:{tag}: Docker Hub returned no manifest digest ({data!r})")
    if "amd64" not in archs:
        raise RuntimeError(f"{name}:{tag}: no amd64 image behind it (architectures={archs})")
    return digest


def _pinned_official_digest(instance_id: str) -> str:
    """The digest the committed snapshot pins for *instance_id* (ADR-0043).

    Read from the package-data snapshot for the loader's pinned (dataset,
    revision) — never from the Hub — so what is built is what the ADR pinned,
    on every host, on every day.  Fails loudly when the instance is not in the
    snapshot (re-run scripts/snapshot_image_digests.py for a new pin).
    """
    from swebench_eval.dataset.swebench_loader import (
        image_digest_snapshot_name,
        load_image_digest_snapshot,
    )

    snapshot = load_image_digest_snapshot()
    if snapshot is None:
        raise RuntimeError(
            f"no image-digest snapshot {image_digest_snapshot_name()} in the package — "
            "run scripts/snapshot_image_digests.py and commit it (ADR-0043)"
        )
    digest = snapshot.get(instance_id, "")
    if not digest.startswith("sha256:"):
        raise RuntimeError(
            f"{instance_id} is not in the image-digest snapshot {image_digest_snapshot_name()}"
        )
    return digest


def _write_instance_record(instance_id: str, inst_digest: str, provenance: dict[str, str]) -> str:
    """``cache-manifest/<version>/instances/<id>.json`` for the just-promoted -inst."""
    import datetime as _dt

    from swebench_eval.cache_manifest import write_instance_record

    record = {
        "tag": f"{_VERSION}-{instance_id}-inst",
        "digest": inst_digest,
        "base_image_digest": provenance.get("BASE_IMAGE_DIGEST", ""),
        "base_image": provenance.get("BASE_IMAGE_REF", ""),
        "framework_sha": _FRAMEWORK_SHA,
        "built_at": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if provenance.get("REFRESHED_FROM_DIGEST"):
        # --base refresh: the -inst this framework layer was stacked on (the
        # previous promoted digest) — the base_image* fields above are carried
        # forward from that image's own record, not re-derived.
        record["refreshed_from_digest"] = provenance["REFRESHED_FROM_DIGEST"]
    return write_instance_record(_VERSION, instance_id, record, _DATASET_BUCKET)


def _official_build_context(instance_id: str) -> Path:
    """Scratch context for the six-CLI layer (Dockerfile.harness-worker-env).

    Same file list warm_image_cache._harness_build_context assembles: building
    straight from /app would stream the framework venv into the daemon.
    """
    root = Path(_env("HARNESS_BUILD_ROOT", "/app"))
    ctx = Path(f"/tmp/clictx-{instance_id}")
    if ctx.exists():
        shutil.rmtree(ctx)
    (ctx / "infra" / "docker").mkdir(parents=True)
    (ctx / "scripts").mkdir(parents=True)
    shutil.copy(root / "pyproject.toml", ctx / "pyproject.toml")
    shutil.copy(root / "uv.lock", ctx / "uv.lock")
    shutil.copytree(root / "swebench_eval", ctx / "swebench_eval")
    for rel in (
        "infra/docker/Dockerfile.harness-worker-env",
        "infra/docker/entrypoint.sh",
        "infra/docker/write_harness_versions.sh",
        "scripts/verify_harness_isolation.py",
        "scripts/mini_pruning_agent.py",
    ):
        shutil.copy(root / rel, ctx / rel)
    return ctx


def _ecr_manifest_for_tag(ecr: Any, tag: str) -> tuple[str, str, str] | None:
    """(manifest, mediaType, digest) behind an ECR tag, or None if absent."""
    resp = ecr.batch_get_image(repositoryName=_HARNESS_REPO, imageIds=[{"imageTag": tag}])
    images = resp.get("images") or []
    if not images:
        return None
    img = images[0]
    return (
        str(img["imageManifest"]),
        str(img.get("imageManifestMediaType", "")),
        str(img["imageId"]["imageDigest"]),
    )


def _ecr_put_tag(ecr: Any, tag: str, manifest: str, media_type: str) -> None:
    kwargs: dict[str, Any] = {
        "repositoryName": _HARNESS_REPO,
        "imageManifest": manifest,
        "imageTag": tag,
    }
    if media_type:
        kwargs["imageManifestMediaType"] = media_type
    try:
        ecr.put_image(**kwargs)
    except ecr.exceptions.ImageAlreadyExistsException:
        logger.info("ECR tag %s already points at this manifest", tag)


def promote_inst_tag(
    instance_id: str, *, now: Callable[[], int] = lambda: int(time.time())
) -> dict[str, str]:
    """Point ``<ver>-<id>-inst`` at the ``-inst-official`` manifest, keeping the
    old one.  Backup-before-overwrite: the digest ``-inst`` currently names is
    re-tagged ``-inst-prev`` first (or ``-inst-prev-<unix-ts>`` if a ``-prev``
    already exists — never clobber a backup either).  Pure ECR manifest
    copies, no layer traffic.  Returns the digests involved so the caller can
    log them; nothing here can delete an image.
    """
    import boto3

    ecr = boto3.client("ecr", region_name=_REGION)
    inst_tag = f"{_VERSION}-{instance_id}-inst"
    official_tag = f"{inst_tag}-official"
    new = _ecr_manifest_for_tag(ecr, official_tag)
    if new is None:
        raise RuntimeError(f"{official_tag} is not in ECR; nothing to promote")
    new_manifest, new_media, new_digest = new
    out = {"instance": instance_id, "new_digest": new_digest, "previous_digest": ""}

    old = _ecr_manifest_for_tag(ecr, inst_tag)
    if old is not None:
        old_manifest, old_media, old_digest = old
        out["previous_digest"] = old_digest
        if old_digest == new_digest:
            logger.info("%s already points at %s; nothing to promote", inst_tag, new_digest)
            return out
        backup = f"{inst_tag}-prev"
        if _ecr_manifest_for_tag(ecr, backup) is not None:
            backup = f"{backup}-{now()}"
        _ecr_put_tag(ecr, backup, old_manifest, old_media)
        out["backup_tag"] = backup
        logger.info("backed up %s (%s) as %s", inst_tag, old_digest, backup)

    _ecr_put_tag(ecr, inst_tag, new_manifest, new_media)
    logger.info("promoted %s -> %s (%s)", official_tag, inst_tag, new_digest)
    return out


def _prepare_base_official(
    instance_id: str, row: dict[str, Any], phase: Callable[[str], None]
) -> tuple[str, dict[str, str]]:
    """``--base official``: pull SWE-bench's published instance image by
    digest and stack the six-CLI layer on it.  Returns the local tag the
    instance layer builds FROM, plus the provenance the sentinel records.

    No install_repo_script, no env re-solve: /testbed is exactly what the
    leaderboard graded in.  The CLI Dockerfile is the SAME one every -hw was
    built with (FROM ${ENV_IMAGE}); only the base differs.
    """
    # The row's own image reference (5.x column) names the repo; the digest is
    # the committed pin, never a live tag lookup.
    image_ref = str(row.get("image") or f"{official_image_name(instance_id)}:{_OFFICIAL_TAG}")
    name = image_ref.rsplit(":", 1)[0] if ":" in image_ref.rsplit("/", 1)[-1] else image_ref
    digest = _pinned_official_digest(instance_id)
    ref = f"{name}@{digest}"
    sh(["docker", "pull", ref])
    phase("pull-official")

    clis = f"{_REPO_URL}:{_VERSION}-{instance_id}-inst-clis"
    ctx = _official_build_context(instance_id)
    sh(
        [
            "docker",
            "build",
            *(("--progress=plain",) if _SUPPORTS_PROGRESS_PLAIN else ()),
            "--build-arg",
            f"ENV_IMAGE={ref}",
            "--build-arg",
            f"FRAMEWORK_SHA_BUILD_ARG={_FRAMEWORK_SHA}",
            "--build-arg",
            f"GATEWAY_CONFIG_HASH={_GATEWAY_HASH}",
            "-t",
            clis,
            "-f",
            str(ctx / "infra" / "docker" / "Dockerfile.harness-worker-env"),
            str(ctx),
        ]
    )
    shutil.rmtree(ctx, ignore_errors=True)
    phase("cli-layer")
    provenance = {
        "BASE_KIND": "official",
        "BASE_IMAGE_REF": image_ref,
        "BASE_IMAGE_DIGEST": digest,
    }
    return clis, provenance


def _prepare_base_refresh(
    instance_id: str, phase: Callable[[str], None]
) -> tuple[str, dict[str, str]]:
    """``--base refresh``: the framework layer ONLY, stacked on the ``-inst``
    already promoted in ECR.  No Docker Hub, no CLI layer, no instance layer,
    no ``pip download`` — the environment the leaderboard graded in is exactly
    what is already there; only ``swebench_eval/`` (what
    Dockerfile.harness-worker-refresh COPYs) changes.

    Returns the pulled ``-inst`` tag the refresh builds FROM, plus provenance
    carried forward VERBATIM from the instance's S3 record: the refresh never
    sees the official base, so it must not re-derive (or invent) its digest.
    A missing record is a hard error — build that instance ``--base official``.

    Stacking a refresh on a refresh is safe: ``COPY swebench_eval`` overwrites,
    ``uv pip install -e . --no-deps`` re-registers, and the ``uv.lock`` ``cmp``
    guard still fails the build loudly if the lockfile moved (then it is a
    ``-hw`` rebuild, not a refresh).
    """
    from swebench_eval.cache_manifest import load_instance_record

    inst_tag = f"{_REPO_URL}:{_VERSION}-{instance_id}-inst"
    record = load_instance_record(_VERSION, instance_id, _DATASET_BUCKET)
    if not record or not record.get("base_image_digest"):
        raise RuntimeError(
            f"{instance_id}: no instance record with base_image_digest in "
            f"s3://{_DATASET_BUCKET} — a refresh cannot invent provenance; "
            "build it with --base official"
        )
    sh(["docker", "pull", inst_tag])
    phase("pull-inst")
    refreshed_from = str(record.get("digest", ""))
    try:
        repo_digest = sh_quiet(
            ["docker", "image", "inspect", inst_tag, "--format", "{{index .RepoDigests 0}}"]
        ).strip()
        if "@" in repo_digest:
            refreshed_from = repo_digest.rsplit("@", 1)[1]
    except RuntimeError as exc:
        logger.warning("%s: could not read the pulled -inst digest: %s", instance_id, exc)
    provenance = {
        "BASE_KIND": str(record.get("base_kind") or "official"),
        "BASE_IMAGE_REF": str(record.get("base_image", "")),
        "BASE_IMAGE_DIGEST": str(record["base_image_digest"]),
        "REFRESHED_FROM_DIGEST": refreshed_from,
    }
    logger.info(
        "%s: refresh FROM %s (%s); base provenance carried forward: %s",
        instance_id,
        inst_tag,
        refreshed_from or "digest unknown",
        provenance["BASE_IMAGE_DIGEST"],
    )
    return inst_tag, provenance


def _build_one(
    instance_id: str,
    mirror: dict[str, dict[str, Any]],
    workers: int,
    *,
    promote: bool = False,
    base: str = "official",
    build_only: bool = False,
) -> dict[str, Any]:
    """Build + push one `-inst` image FROM the pinned official image
    (``base="official"``), or refresh ONLY its framework layer FROM the
    ``-inst`` already in ECR (``base="refresh"``, see _prepare_base_refresh).

    Thread-safe: every piece of mutable state (phase timing, temp build
    contexts) is LOCAL to this call — no module-level globals — so N of these
    run concurrently in a ThreadPoolExecutor without colliding.

    Pushed as ``-inst-official``; with ``promote``, also made the ``-inst``
    the families and the eval side resolve (after backing the old one up),
    and the per-instance manifest record is written.  ``build_only`` stops
    after the build: no push, no promote, no record, and the built image is
    left in the local daemon for inspection (local testing of the pipeline).
    """
    if base not in ("official", "refresh"):
        raise ValueError(f"unknown base {base!r}")
    _push_pushed = 0
    _push_cached = 0
    build_t0 = time.monotonic()
    phases: dict[str, float] = {}

    def _phase(name: str) -> None:
        elapsed = time.monotonic() - build_t0
        logger.info("PHASE %s %s %.1f", instance_id, name, elapsed)
        phases[name] = round(elapsed, 1)

    start = time.monotonic()
    row = mirror[instance_id]
    root = _env("HARNESS_BUILD_ROOT", "/app")

    _ensure_disk_for_build(instance_id, workers)

    if base == "refresh":
        # The framework layer's base IS the promoted -inst: pull it, nothing
        # else to prepare (no CLI layer, no instance layer, no Hub).
        base_inst, provenance = _prepare_base_refresh(instance_id, _phase)
        inst_base = base_inst
    else:
        # 1-2. The base the instance layer builds FROM: SWE-bench's published
        #      image (digest-pinned) + our CLI layer — a local tag with /testbed
        #      prepared exactly as the leaderboard graded it.
        base_inst, provenance = _prepare_base_official(instance_id, row, _phase)

        _ensure_disk_for_build(f"{instance_id} (instance layer)", workers)

        # 3. Instance layer (sentinel + wheelhouse) -> INTERMEDIATE, no framework
        #    yet.  Base is the LOCAL sweb.eval.<id>:latest just built by swebench
        #    (legacy) or the CLI layer on the official image.  The wheelhouse
        #    `pip download` needs internet AT BUILD TIME (warm container); ENV
        #    PIP_NO_INDEX applies to subsequent RUNs at runtime.
        inst_base = f"{_REPO_URL}:{_VERSION}-{instance_id}-inst-base"
        ctx = Path(f"/tmp/instctx-{instance_id}")
        if ctx.exists():
            shutil.rmtree(ctx)
        ctx.mkdir(parents=True)
        shutil.copy(f"{root}/scripts/capture_build_reqs.py", ctx / "capture_build_reqs.py")
        # Per-instance extra wheels (swebench_eval/dataset/extra_wheels.py): the
        # requirement lines a gold patch ADDS at runtime (pylint-4661: appdirs),
        # downloaded into this instance's wheelhouse next to the build-system
        # requires.  ALWAYS written (empty for almost every instance) so the
        # Dockerfile's COPY has a file to copy.
        extra = extra_wheels_for(instance_id)
        (ctx / "extra-wheels.txt").write_text("".join(f"{r}\n" for r in extra))
        if extra:
            logger.info("%s: extra wheels for the offline grade: %s", instance_id, ", ".join(extra))
        provenance_args: list[str] = []
        for key, value in provenance.items():
            provenance_args += ["--build-arg", f"{key}={value}"]
        sh(
            [
                "docker",
                "build",
                *(("--progress=plain",) if _SUPPORTS_PROGRESS_PLAIN else ()),
                "--build-arg",
                f"BASE_IMAGE={base_inst}",
                "--build-arg",
                f"INSTANCE_ID={instance_id}",
                "--build-arg",
                f"BASE_COMMIT={row['base_commit']}",
                "--build-arg",
                f"SWEBENCH_VERSION={_VERSION}",
                # ARG name is FRAMEWORK_SHA_BUILD_ARG, not FRAMEWORK_SHA — see
                # Dockerfile.instance-layer's 2026-09-04 ARG/ENV name-collision fix.
                "--build-arg",
                f"FRAMEWORK_SHA_BUILD_ARG={_FRAMEWORK_SHA}",
                *provenance_args,
                "-t",
                inst_base,
                "-f",
                f"{root}/infra/docker/Dockerfile.instance-layer",
                str(ctx),
            ]
        )
        _phase("instance-layer")

    # 4. FRAMEWORK LAYER LAST (PART2 §3.3): apply the refresh FROM the
    #    instance-layer result so a framework-only patch re-runs only this thin
    #    layer — pull + COPY + push against ECR, never install_repo_script,
    #    never egress.  The uv.lock guard + re-register make it safe.  This is
    #    the ONLY build step `--base refresh` runs.  --platform: the -inst is
    #    amd64 (that is what the harness hosts run); explicit so a local
    #    arm64 daemon (a laptop --build-only test) builds the same thing.
    inst_final = f"{_REPO_URL}:{_VERSION}-{instance_id}-inst-official"
    sh(
        [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            *(("--progress=plain",) if _SUPPORTS_PROGRESS_PLAIN else ()),
            "--build-arg",
            f"HW_IMAGE={inst_base}",
            # ARG name is FRAMEWORK_SHA_BUILD_ARG, not FRAMEWORK_SHA — see
            # Dockerfile.harness-worker-refresh's 2026-09-04 ARG/ENV
            # name-collision fix (the REAL fix; the 2026-09-03 one was broken).
            "--build-arg",
            f"FRAMEWORK_SHA_BUILD_ARG={_FRAMEWORK_SHA}",
            "-t",
            inst_final,
            "-f",
            f"{root}/infra/docker/Dockerfile.harness-worker-refresh",
            root,
        ]
    )
    _phase("framework")

    promotion: dict[str, str] = {}
    if build_only:
        logger.info(
            "%s: --build-only — not pushed, not promoted, no record; image left as %s",
            instance_id,
            inst_final,
        )
    else:
        push_out = sh(["docker", "push", inst_final])
        _push_pushed = push_out.count("Pushed") if "Pushed" in push_out else 0
        _push_cached = (
            push_out.count("Layer already exists") if "Layer already exists" in push_out else 0
        )
        _phase("push")
        logger.info(
            "push layers: %d pushed, %d cached (layer-already-exists)", _push_pushed, _push_cached
        )

        if promote:
            promotion = promote_inst_tag(instance_id)
            _phase("promote")
            # ADR-0043: the record the warm job merges into the manifest's
            # admission list — written only once the -inst tag actually points at
            # this image, so the record never describes an unpromoted build.
            _write_instance_record(instance_id, promotion["new_digest"], provenance)
            _phase("record")

    # Read the size BEFORE reaping (the number §3 asks you to report — once the
    # image is rm'd the inspect fails).
    size = 0
    try:
        size = int(
            sh_quiet(["docker", "image", "inspect", inst_final, "--format", "{{.Size}}"]) or 0
        )
    except RuntimeError as exc:
        logger.warning("could not read final image size: %s", exc)

    # Reap intermediates (working set bounded).  inst_final MUST be in the
    # list — a pushed tagged image that `docker system prune` cannot touch is
    # exactly the 2026-08-22 ENOSPC cause (blocker-answer §4).  --build-only
    # keeps it (that is the point); the pulled/intermediate bases still go.
    reap = [inst_base, base_inst] if build_only else [inst_final, inst_base, base_inst]
    for img in dict.fromkeys(reap):
        sh(["docker", "rmi", "-f", img], check=False)
    if base != "refresh" and provenance.get("BASE_IMAGE_DIGEST"):
        sh(
            [
                "docker",
                "rmi",
                "-f",
                f"{official_image_name(instance_id)}@{provenance['BASE_IMAGE_DIGEST']}",
            ],
            check=False,
        )
    shutil.rmtree(Path(f"/tmp/instctx-{instance_id}"), ignore_errors=True)

    wall = time.monotonic() - start
    result: dict[str, Any] = {
        "instance": instance_id,
        "base": base,
        "wall_s": round(wall, 1),
        "size_bytes": size,
        "phases": phases,
        "push": {"pushed": _push_pushed, "cached": _push_cached},
    }
    if provenance:
        result["base_image"] = provenance["BASE_IMAGE_REF"]
        result["base_image_digest"] = provenance["BASE_IMAGE_DIGEST"]
        if provenance.get("REFRESHED_FROM_DIGEST"):
            result["refreshed_from_digest"] = provenance["REFRESHED_FROM_DIGEST"]
    if build_only:
        result["build_only"] = True
        result["image"] = inst_final
    if promotion:
        result["promotion"] = promotion
    return result


def _apply_resume(
    targets: list[tuple[str, str]],
    already: set[str],
    forced: set[str],
    *,
    only: bool = False,
) -> list[tuple[str, str]]:
    """Resume-from-ECR-state filter (brief §4 Stage 3): keep a target only if
    it is NOT already in ECR, or it was explicitly named in FORCE_INSTANCES.
    Never mutates ECR — the caller decides what to (re)build; this function
    only decides what to skip. Raises if FORCE_INSTANCES names something
    outside *targets* (a typo'd id would otherwise silently force nothing).

    ``only=True`` (i.e. this run came from ``--only``) makes EVERY target
    implicitly forced. ``--only`` predates Stage 3 and is the exact mechanism
    builder 1 already depends on (e.g. `phase0-instances --only
    scikit-learn__scikit-learn-25102` to rebuild one instance after a
    framework fix) — it always meant "rebuild whatever I named, right now,"
    unconditionally. Silently skipping a --only target because its (possibly
    STALE) -inst tag already exists would be exactly backwards for that use
    case — the existing tag being stale is usually the whole reason it was
    named. Resume-skip is for the NEW large-batch path (ENV_SHARD / full
    run), where "what's still missing after a crash" is the real question.
    """
    if only:
        forced = forced | {i for i, _ in targets}
    unmatched = forced - {i for i, _ in targets}
    if unmatched:
        raise SystemExit(
            f"FORCE_INSTANCES names instances not in this run's targets: {sorted(unmatched)}"
        )
    return [(i, h) for i, h in targets if i not in already or i in forced]


def _resolve_targets(
    args: argparse.Namespace, mirror: dict[str, dict[str, Any]]
) -> list[tuple[str, str]]:
    """(instance_id, repo) pairs to build, before the resume filter.

    Exactly one of --only / ENV_SHARD / "everything" decides the set — "few"
    and "full" then share the SAME resume+pool path below (brief §4 Stage 3).
    """
    env_map = _apply_env_limit(_apply_env_filter(_compute_repo_map(mirror), mirror))

    if args.only is not None:
        # An EMPTY --only must never mean "everything": found 2026-09-05 when a
        # shell bug passed --only "" and the task would have built (and
        # promoted) all 500 instances.  Refuse loudly instead.
        wanted = {w.strip() for w in args.only.split(",") if w.strip()}
        if not wanted:
            raise SystemExit("--only was given but names no instance ids (empty string?)")
        missing = sorted(wanted - set(mirror))
        if missing:
            raise SystemExit(f"--only names instance ids not in the mirror: {missing}")
        ids = sorted(wanted)
    else:
        shard = os.environ.get("ENV_SHARD", "").strip()
        if shard:
            try:
                i_str, n_str = shard.split("/", 1)
                shard_index, num_shards = int(i_str), int(n_str)
            except ValueError as exc:
                raise SystemExit(f"ENV_SHARD must be '<i>/<n>', got {shard!r}") from exc
            ids = _lpt_shard(env_map, shard_index, num_shards, weight_fn=_repo_weight_seconds)
            logger.info(
                "ENV_SHARD=%d/%d: %d instances across %d repos",
                shard_index,
                num_shards,
                len(ids),
                sum(1 for k in env_map if any(i in env_map[k] for i in ids)),
            )
        else:
            ids = sorted(i for instances in env_map.values() for i in instances)

    return [(i, str(mirror[i].get("repo", ""))) for i in ids]


def _resolve_defaults() -> None:
    """Adoption Phase 1a: fill the registry / repo URL / bucket from aws_names when the
    task env did not set them — at RUN time, so importing this module needs no AWS."""
    global _REGISTRY, _REPO_URL, _DATASET_BUCKET
    if not _REGISTRY:
        _REGISTRY = aws_names.ecr_registry()
    if "/" not in _HARNESS_REPO:
        _REPO_URL = f"{_REGISTRY}/{_HARNESS_REPO}"
    if not _DATASET_BUCKET:
        _DATASET_BUCKET = aws_names.dataset_bucket_default()


def main() -> int:
    _resolve_defaults()
    parser = argparse.ArgumentParser(description="Build per-instance -inst images.")
    parser.add_argument(
        "--only",
        help="comma-separated instance ids to build (default: derived from ENV_SHARD / "
        "ENV_FILTER / ENV_LIMIT, or every instance in the mirror)",
    )
    parser.add_argument(
        "--base",
        choices=("official", "refresh"),
        default="official",
        help="what the -inst is built FROM. 'official' (ADR-0043): SWE-bench's "
        "published, digest-pinned instance image — Hub pull, CLI layer, instance "
        "layer, framework layer. 'refresh': ONLY the framework layer, FROM the -inst "
        "already promoted in ECR — no Docker Hub, no pip; for a swebench_eval/-only "
        "change (the uv.lock guard still fails a lockfile change loudly). Either way "
        "the result is pushed as -inst-official; see --promote.",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        default=os.environ.get("INST_PROMOTE", "") == "1",
        help="after the push, back the current -inst tag up as -inst-prev and point "
        "-inst at the new image (then write the manifest record), so the task "
        "families and the eval side pick it up with no other change. Env: INST_PROMOTE=1.",
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="build and stop: no push, no promote, no record; the -inst-official image "
        "is left in the local daemon for inspection (local test of the pipeline).",
    )
    args = parser.parse_args()

    _ecr_login()
    if args.base == "refresh":
        logger.info("--base refresh: no Docker Hub login — nothing is pulled from the Hub")
    else:
        _dockerhub_login()
    mirror = _load_mirror()

    targets = _resolve_targets(args, mirror)

    # Resume from ECR state (brief §4 Stage 3): never delete a tag to force a
    # rebuild — FORCE_INSTANCES=<list> exists for that, same discipline as
    # warm_image_cache's FORCE_HARNESS=<list>. --only always forces (see
    # _apply_resume's docstring — it's the mechanism builder 1 already uses).
    already = _ecr_inst_tags()
    failures: list[str] = []
    if args.base == "refresh":
        # A refresh stacks on the -inst that IS in ECR: every named target is
        # rebuilt (the existing tag being stale is the whole point), and a
        # target with no -inst yet cannot be refreshed — that is a failure to
        # report, never a silent skip and never a fallback to a full build.
        todo = [(i, r) for i, r in targets if i in already]
        missing = sorted(i for i, _ in targets if i not in already)
        if missing:
            logger.error(
                "--base refresh: %d/%d target(s) have no -inst in ECR to refresh "
                "(build them --base official): %s",
                len(missing),
                len(targets),
                missing,
            )
            failures.extend(missing)
    else:
        forced = {
            w.strip() for w in os.environ.get("FORCE_INSTANCES", "").strip().split(",") if w.strip()
        }
        todo = _apply_resume(targets, already, forced, only=bool(args.only))
        skipped = len(targets) - len(todo)
        if skipped:
            logger.info(
                "resume: %d/%d instances already in ECR, skipping (never deleting tags)",
                skipped,
                len(targets),
            )
    if not todo:
        logger.info("nothing to build (%d targets, all already in ECR)", len(targets))
        print(json.dumps([]))
        return 1 if failures else 0

    workers = _instance_parallelism()
    logger.info(
        "building %d instances with INSTANCE_MAX_WORKERS-derived pool size=%d "
        "(INSTANCE_WORKERS=%s, INSTANCE_BUILD_MEM_MB=%s)",
        len(todo),
        workers,
        _env("INSTANCE_WORKERS", "auto"),
        _env("INSTANCE_BUILD_MEM_MB", "3072 (default)"),
    )

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _build_one,
                instance_id,
                mirror,
                workers,
                promote=args.promote,
                base=args.base,
                build_only=args.build_only,
            ): instance_id
            for instance_id, _repo in todo
        }
        for fut in as_completed(futures):
            instance_id = futures[fut]
            try:
                result = fut.result()
            except Exception:
                logger.exception("=== %s FAILED ===", instance_id)
                failures.append(instance_id)
                continue
            results.append(result)
            logger.info("=== %s done ===", instance_id)

    print(json.dumps(results, indent=2))
    if failures:
        logger.error("%d/%d instances failed: %s", len(failures), len(todo), sorted(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
