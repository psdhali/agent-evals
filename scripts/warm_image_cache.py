#!/usr/bin/env python3
"""Publish the warm-cache manifest from what the build tier actually pushed (ADR-0043).

SWE-bench 5.x publishes one image per instance and no environment images, so
the 4.1.0-era ``--images`` mode (build the env images with the harness's own
``build_env_images``, stack the six-CLI layer, push ``<ver>-<hash>`` and
``<ver>-<hash>-hw``) is gone with the harness API that generated those
builds.  Per-instance images are built by ``scripts/build_phase0_instances_v2.py``
(``--base official --promote``) FROM the official image the committed digest
snapshot pins; each promotion writes one record to
``cache-manifest/<swebench-version>/instances/<instance_id>.json``.

What remains here:

  * ``--manifest`` — merge those records into ``instance_images``, keeping only
    the ones whose ``-inst`` tag ECR still holds at the recorded digest, and
    write ``cache-manifest/<swebench-version>.json`` — the admission list the
    dispatcher's gate reads before any dispatch (ADR-0031 + ADR-0043).
  * ``--dry-run`` — print the plan and exit 0 (CI-safe).

``--images`` now fails loudly rather than silently doing nothing.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
from typing import Any

from swebench_eval import aws_names
from swebench_eval.cache_manifest import CacheManifest, load_instance_records
from swebench_eval.cache_manifest import write_manifest as persist_manifest
from swebench_eval.logging_bootstrap import configure_logging

logger = logging.getLogger(__name__)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Adoption Phase 1a: the registry / repo / bucket defaults derive from
# swebench_eval.aws_names (region + prefix + account), never from literals.
def _region() -> str:
    return aws_names.region()


def _registry() -> str:
    return _env("ECR_REGISTRY", "") or aws_names.ecr_registry()


def _image_repo() -> str:
    return _env("HARNESS_IMAGE_REPO", "") or aws_names.named(
        "harness-worker"
    )  # ONE collocated repo (5b §1)


def _bucket() -> str:
    return _env("DATASET_BUCKET", "") or aws_names.dataset_bucket_default()


def _swebench_version() -> str:
    import importlib.metadata

    try:
        return importlib.metadata.version("swebench")
    except Exception:  # noqa: BLE001 - unknown version is a degraded-but-usable fallback
        return "unknown"


def _ecr_login(registry: str) -> None:
    """``docker login`` to ECR using boto3 (the warm container has no aws CLI)."""
    import base64

    import boto3

    auth = boto3.client("ecr", region_name=_region()).get_authorization_token()[
        "authorizationData"
    ][0]
    user, _, password = base64.b64decode(auth["authorizationToken"]).decode().partition(":")
    login = subprocess.run(  # noqa: PLW1510 - retcode checked below
        ["docker", "login", "--username", user, "--password-stdin", registry],
        input=password,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if login.returncode != 0:
        raise RuntimeError(f"docker login failed: {login.stderr[-2000:]}")


# ---------------------------------------------------------------------------
# --manifest
# ---------------------------------------------------------------------------


def _ecr_inst_digests(swebench_version: str) -> dict[str, str]:
    """``tag -> digest`` for every ``<version>-<instance_id>-inst`` tag ECR holds now."""
    import boto3

    client = boto3.client("ecr", region_name=_region())
    paginator = client.get_paginator("describe_images")
    prefix = f"{swebench_version}-"
    out: dict[str, str] = {}
    for page in paginator.paginate(repositoryName=_image_repo()):
        for detail in page.get("imageDetails", []):
            for tag in detail.get("imageTags", []) or []:
                if tag.startswith(prefix) and tag.endswith("-inst"):
                    out[tag] = str(detail["imageDigest"])
    return out


def merge_instance_images(
    records: dict[str, dict[str, str]], ecr_digests: dict[str, str]
) -> dict[str, dict[str, str]]:
    """The manifest's ``instance_images``: records whose tag ECR still serves at the recorded digest.

    A record whose tag is gone (deleted) or moved (re-promoted without a new
    record) is dropped — the manifest is the ADMISSION list, so it must
    describe what a dispatch would actually run, never what once existed.
    """
    kept: dict[str, dict[str, str]] = {}
    for instance_id, rec in sorted(records.items()):
        tag = rec.get("tag", "")
        live = ecr_digests.get(tag)
        if live is None:
            logger.warning("instance record %s: tag %s is not in ECR; dropped", instance_id, tag)
            continue
        if rec.get("digest") and rec["digest"] != live:
            logger.warning(
                "instance record %s: tag %s moved (%s -> %s); dropped until rebuilt/re-recorded",
                instance_id,
                tag,
                rec["digest"],
                live,
            )
            continue
        kept[instance_id] = {
            "tag": tag,
            "digest": live,
            "base_image_digest": rec.get("base_image_digest", ""),
        }
    return kept


def _write_manifest() -> str:
    """Merge the per-instance records with live ECR state and persist the manifest."""
    sw_ver = _swebench_version()
    bucket = _bucket()
    records = load_instance_records(sw_ver, bucket)
    ecr = _ecr_inst_digests(sw_ver)
    instance_images = merge_instance_images(records, ecr)
    manifest = CacheManifest(swebench_version=sw_ver, instance_images=instance_images)
    key = persist_manifest(manifest, bucket)
    logger.info(
        "manifest %s: %d instance images (%d records, %d -inst tags in ECR)",
        key,
        len(instance_images),
        len(records),
        len(ecr),
    )
    return key


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish the warm-cache manifest (ADR-0043)")
    parser.add_argument(
        "--images",
        action="store_true",
        help="REMOVED (ADR-0043): per-instance images are built by build_phase0_instances_v2.py",
    )
    parser.add_argument("--manifest", action="store_true", help="write the cache manifest")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and exit 0 — no docker, no S3 writes. CI-safe.",
    )
    args = parser.parse_args(argv)

    configure_logging()
    logger.info(
        "warm-job starting FRAMEWORK_SHA=%s GATEWAY_CONFIG_HASH=%s",
        _env("FRAMEWORK_SHA", "unknown"),
        _env("GATEWAY_CONFIG_HASH", "unknown"),
    )

    if args.dry_run:
        print(
            "dry-run: "
            f"registry={_registry()} "
            f"repo={_image_repo()} "
            f"swebench={_swebench_version()} "
            f"bucket={_bucket()} "
            "mode=manifest-only (env-image builds were removed by ADR-0043)"
        )
        return 0

    if args.images:
        logger.error(
            "--images was removed (ADR-0043): SWE-bench 5.x has no env images to build; "
            "run phase0-instances-v2 --base official --promote for the per-instance images"
        )
        return 2

    try:
        _write_manifest()
    except RuntimeError as exc:
        logger.error("warm job failed: %s", exc)
        return 1
    logger.info("warm job complete")
    return 0


_ = Any  # keep the typing import honest for the helpers' annotations above

if __name__ == "__main__":
    raise SystemExit(main())
