#!/usr/bin/env python3
"""Seed the active SWE-bench dataset into the durable `dataset` bucket.

The dataset identity derives from ``swebench_loader._DATASET_NAME`` (V1,
switch-to-swebench-verified) — Verified by default, Lite via SWEBENCH_DATASET.

Phase 5b / review §2 (2026-08-16): the mirror is no longer write-only — the
loader reads it by default, so this script writes BOTH variants per revision:

    <dataset>/<split>/<revision>.jsonl        — FULL row,
      every Instance field INCLUDING the gold ``patch``.  Read by the grading
      path only (the eval worker builds SWE-bench's TestSpec from it).
    <dataset>/<split>/<revision>.public.jsonl — harness-facing
      subset (`_PUBLIC_FIELDS`), gold excluded, read by every dispatch/harness path.

The gold-patch exclusion in `_PUBLIC_FIELDS` now has a runtime consumer; before
5b it protected nothing because no code read the mirror and HF's copy carries
the patch anyway (the reviewer's §2 correction).

Idempotent: skips re-upload when BOTH objects already exist AND the full file
actually parses as full (has a `patch` key) — the full key was previously
public-only, so a legacy object at that key must be detected and re-seeded.

Usage:
    python scripts/seed_dataset.py [--split test] [--force]
    python scripts/seed_dataset.py --endpoint-url http://localhost:9000 --create-bucket   # MinIO

Adoption F1 (2026-09-11): ``--endpoint-url`` (or ``S3_ENDPOINT_URL``) points the
seeder at an S3-compatible endpoint such as the compose stack's MinIO, with the
stack's static credentials, so the no-AWS laptop path can seed the gold-stripped
mirror the harness is allowed to read.  Without it the client is real S3 under the
configured AWS profile, as before.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swebench_eval.aws_names import dataset_bucket_default, region


def _aws_s3(endpoint_url: str | None = None, region_name: str | None = None) -> Any:
    """An S3 client for the seed step.

    NOT swebench_eval.queue.client.get_s3_client(): that helper is
    environment-aware and on a laptop (not a deployed container) it returns the
    local MinIO endpoint with dummy creds — the seed step runs from a dev
    machine, so by default it must target real S3 with the configured AWS
    profile.  With ``endpoint_url`` (the local MinIO) it uses the compose
    stack's static credentials (``S3_ACCESS_KEY``/``S3_SECRET_KEY``, default
    ``minioadmin``) — the same pair ``get_s3_client`` uses on the read side.
    """
    import boto3

    region_name = region_name or region()
    if endpoint_url:
        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "minioadmin"),
            aws_secret_access_key=os.environ.get("S3_SECRET_KEY", "minioadmin"),
        )
    return boto3.client("s3", region_name=region_name)


def _ensure_bucket(s3: Any, bucket: str) -> None:
    """Create *bucket* on an S3-compatible endpoint if it does not exist (local only)."""
    try:
        s3.head_bucket(Bucket=bucket)
        return
    except Exception as exc:  # noqa: BLE001 - 404/403 both mean "not ours yet" here
        print(f"bucket {bucket} not found ({exc.__class__.__name__}); creating")
    s3.create_bucket(Bucket=bucket)
    print(f"created bucket {bucket}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror the pinned SWE-bench dataset to S3")
    parser.add_argument(
        "--bucket",
        default=os.environ.get("DATASET_BUCKET") or None,
        help="Dataset bucket (default: DATASET_BUCKET, else the account's "
        "<prefix>-dataset-<acct>-<region> from swebench_eval.aws_names).",
    )
    parser.add_argument("--force", action="store_true", help="Re-upload even if the seed exists.")
    parser.add_argument(
        "--endpoint-url",
        default=os.environ.get("S3_ENDPOINT_URL") or None,
        help="S3-compatible endpoint (e.g. the compose stack's MinIO, http://localhost:9000). "
        "Default: real S3. Also read from S3_ENDPOINT_URL.",
    )
    parser.add_argument("--region", default=None, help="AWS region (default: AWS_REGION / config).")
    parser.add_argument(
        "--create-bucket",
        action="store_true",
        help="Create the bucket if it is missing (local endpoints only; real S3 buckets "
        "come from Terraform).",
    )
    args = parser.parse_args()
    if args.create_bucket and not args.endpoint_url:
        parser.error(
            "--create-bucket is for local S3-compatible endpoints; real buckets are Terraform's"
        )

    from swebench_eval.dataset.swebench_loader import (
        _DATASET_NAME,
        FULL_FIELDS,
        PUBLIC_FIELDS,
        SwebenchLiteLoader,
    )

    # Stage 0.3: seed from the FULL rows (gold included) and derive the public
    # subset from them. Loading the PUBLIC path here would be a bootstrap loop —
    # that path now RAISES when the public mirror is absent (it must never serve
    # gold from HF), which is exactly the state this script exists to fix.
    loader = SwebenchLiteLoader(include_gold=True)
    revision = loader.revision or "main"
    split = loader._split
    bucket = args.bucket or dataset_bucket_default()

    def _base_key(revision: str, split: str) -> str:
        # Derived from the loader's single source of truth (V1) so the seed
        # step and the mirror key can never drift.
        return f"{_DATASET_NAME}/{split}/{revision}"

    base = _base_key(revision, split)
    full_key = f"{base}.jsonl"
    public_key = f"{base}.public.jsonl"

    s3 = _aws_s3(args.endpoint_url, args.region)
    if args.create_bucket:
        _ensure_bucket(s3, bucket)

    def _looks_full(key: str) -> bool:
        """The full file must actually carry the gold patch (schema check)."""
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            first = obj["Body"].read(1 << 16).decode("utf-8").splitlines()[0]
            return bool(json.loads(first)["patch"] != "")
        except Exception:  # noqa: BLE001
            return False

    def _exists(key: str) -> bool:
        try:
            s3.head_object(Bucket=bucket, Key=key)
            return True
        except s3.exceptions.ClientError:
            return False

    if not args.force and _exists(full_key) and _exists(public_key) and _looks_full(full_key):
        print(f"already seeded: s3://{bucket}/{{{full_key}, {public_key}}}")
        return 0

    print(f"loading {_DATASET_NAME} {split} @ {revision} ...")
    instances = loader.load()

    def _rows(fields: tuple[str, ...]) -> str:
        lines = [
            json.dumps({field: getattr(inst, field, "") for field in fields}, separators=(",", ":"))
            for inst in instances
        ]
        return "\n".join(lines) + "\n"

    full = _rows(FULL_FIELDS)
    public = _rows(PUBLIC_FIELDS)
    s3.put_object(Bucket=bucket, Key=full_key, Body=full.encode("utf-8"))
    s3.put_object(Bucket=bucket, Key=public_key, Body=public.encode("utf-8"))
    print(
        f"uploaded {len(instances)} instances -> s3://{bucket}/{full_key} "
        f"({len(full)} bytes) + public variant ({len(public)} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
