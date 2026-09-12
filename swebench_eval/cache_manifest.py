"""Warm-cache manifest — what the warm job found vs expects (5b).

The precondition gate refuses to dispatch when the cache is incomplete, so the
warm job must publish a machine-readable record of what is actually present.
Schema is small and versioned by the pinned ``swebench`` version (a version bump
invalidates the whole cache, image-environment-pipeline §6.3).

S3 object: ``cache-manifest/<swebench-version>.json`` in the durable dataset
bucket (force_destroy=true — it is exactly the re-derivable cache class).  The
control-plane dispatcher reads it before every run.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from swebench_eval import aws_names
from swebench_eval.queue.client import get_s3_client

logger = logging.getLogger(__name__)

_MANIFEST_ROOT = "cache-manifest"


@dataclass
class CacheManifest:
    """What exists (present) vs what must exist (expected), per half.

    Expected halves are LISTS so the missing list can name exactly what is
    missing — env/harness were bare int counts (review find #14), which
    structurally cannot satisfy the DoD's "and the missing list". Every field is
    now an enumerable set on both sides of the comparison.

    ``is_complete()``/``missing_summary()`` are REPORTING helpers only
    (ADR-0031): they no longer gate dispatch — admission is per-instance in
    ``dispatch_run`` against ``env_images``. Someone must not re-wire the gate
    to them.
    """

    swebench_version: str
    env_images: list[str] = field(default_factory=list)  # present env tags ("4.1.0-<h>")
    env_images_expected: list[str] = field(default_factory=list)
    harness_images: list[str] = field(default_factory=list)  # present "-hw" tags
    harness_images_expected: list[str] = field(default_factory=list)
    # The git-mirror half is INFORMATIONAL only (ADR-0025/0029 review S1): the
    # repos are baked into the git-mirror IMAGE (Dockerfile.git-mirror seeds all
    # 12), so mirror readiness is a property of the image being deployed — not
    # something a manifest can assert. is_complete() deliberately does NOT look
    # at these; the fields stay for reading old manifests. Stage 5 deletes EFS
    # and these fields entirely.
    mirrors_ok: list[str] = field(default_factory=list)  # repo names ("org/repo")
    mirrors_expected: list[str] = field(default_factory=list)
    # ADR-0043: the per-instance ``-inst`` images that are PRESENT, keyed by
    # instance id: ``{"tag": "<ver>-<id>-inst", "digest": "sha256:…" (the ECR
    # manifest digest), "base_image_digest": "sha256:…" (the official image the
    # build started FROM — what the dispatch gate compares with the committed
    # digest snapshot)}``.  Written by the build tier per instance and merged by
    # the warm job's ``--manifest``; the admission list for dispatch.  The env/
    # ``-hw`` halves above are 4.1.0-era and stay only so old manifests parse.
    instance_images: dict[str, dict[str, str]] = field(default_factory=dict)

    def is_complete(self) -> bool:
        """True when every EXPECTED env/`-hw` tag is present — REPORTING ONLY.

        ADR-0031: this no longer gates dispatch (admission is per-instance
        against ``env_images``). The warm job logs it, and it feeds the DoD's
        "N present of M expected" wording; it must not be re-wired into the
        precondition gate.
        """
        return set(self.env_images) >= set(self.env_images_expected) and set(
            self.harness_images
        ) >= set(self.harness_images_expected)

    def missing_summary(self) -> dict[str, str | list[str]]:
        """The concrete missing pieces — REPORTING ONLY, does not gate (ADR-0031).

        Feeds the 5b DoD's "and the missing list" wording; the precondition gate
        no longer refuses on it.
        """
        return {
            "env_images_missing": sorted(set(self.env_images_expected) - set(self.env_images)),
            "harness_images_missing": sorted(
                set(self.harness_images_expected) - set(self.harness_images)
            ),
        }

    @staticmethod
    def from_payload(payload: dict[str, Any]) -> CacheManifest:
        """Build from a JSON payload, tolerating the pre-5b-review int schema.

        The first published manifest used ``env_images_expected`` / ``harness_images_expected``
        as int counts. A count cannot recover the expected tag list, so an int is
        treated as "unknown" (empty list): the expected halves cannot then be
        asserted, and the whole-run gate is gone anyway (ADR-0031 — admission is
        per-instance against ``env_images``, which the payload DOES carry). The
        next warm run overwrites it with the list schema. The git-mirror half is
        informational (review S1) and is not asserted by anything.
        """
        if isinstance(payload.get("env_images_expected"), int):
            payload["env_images_expected"] = []
        if isinstance(payload.get("harness_images_expected"), int):
            payload["harness_images_expected"] = []
        # Pre-ADR-0043 manifests carry no instance_images; a missing key is
        # "none built", never a parse failure.
        known = {f.name for f in fields(CacheManifest)}
        return CacheManifest(**{k: v for k, v in payload.items() if k in known})


def key_for(swebench_version: str) -> str:
    return f"{_MANIFEST_ROOT}/{swebench_version}.json"


# ADR-0043: the build tier writes ONE small record per promoted -inst image
# (``cache-manifest/<version>/instances/<instance_id>.json``) so two build hosts
# never race a read-modify-write of the manifest; the warm job's ``--manifest``
# merges the records into ``instance_images`` after checking ECR still holds
# each tag.  A record is written only after promotion, so it describes the
# ``-inst`` tag the families and the eval side actually resolve.
def instance_record_prefix(swebench_version: str) -> str:
    return f"{_MANIFEST_ROOT}/{swebench_version}/instances/"


def instance_record_key(swebench_version: str, instance_id: str) -> str:
    return f"{instance_record_prefix(swebench_version)}{instance_id}.json"


def write_instance_record(
    swebench_version: str,
    instance_id: str,
    record: dict[str, str],
    bucket: str | None = None,
) -> str:
    """Persist one instance's ``{tag, digest, base_image_digest, …}``; returns the key.

    ``record`` must carry ``tag``, ``digest`` and ``base_image_digest`` (the
    three fields the dispatch gate reads); anything else is provenance.
    """
    missing = [k for k in ("tag", "digest", "base_image_digest") if not record.get(k)]
    if missing:
        raise ValueError(f"instance record for {instance_id} lacks {missing}")
    bucket = bucket or os.environ.get("DATASET_BUCKET") or aws_names.dataset_bucket_default()
    key = instance_record_key(swebench_version, instance_id)
    payload = json.dumps({"instance_id": instance_id, **record}, indent=2).encode("utf-8")
    get_s3_client().put_object(Bucket=bucket, Key=key, Body=payload)
    logger.info("instance record written: %s/%s", bucket, key)
    return key


def load_instance_record(
    swebench_version: str, instance_id: str, bucket: str | None = None
) -> dict[str, str] | None:
    """One instance's record (``tag``, ``digest``, ``base_image_digest``, …) or None if absent.

    The refresh-only ``-inst`` rebuild reads this to carry the official base's provenance
    forward verbatim — the refresh never re-pulls the base, so it cannot re-derive it.
    """
    bucket = bucket or os.environ.get("DATASET_BUCKET") or aws_names.dataset_bucket_default()
    key = instance_record_key(swebench_version, instance_id)
    s3 = get_s3_client()
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 — NoSuchKey or an unreachable bucket both mean "none"
        logger.info("no instance record at %s/%s (%s)", bucket, key, exc)
        return None
    doc = json.loads(body)
    if not isinstance(doc, dict):
        return None
    return {k: str(v) for k, v in doc.items() if k != "instance_id"}


def load_instance_records(
    swebench_version: str, bucket: str | None = None
) -> dict[str, dict[str, str]]:
    """``instance_id -> record`` for every record under the version's prefix (``{}`` if none)."""
    bucket = bucket or os.environ.get("DATASET_BUCKET") or aws_names.dataset_bucket_default()
    prefix = instance_record_prefix(swebench_version)
    s3 = get_s3_client()
    out: dict[str, dict[str, str]] = {}
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []) or []:
            key = str(obj["Key"])
            if not key.endswith(".json"):
                continue
            try:
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
                doc = json.loads(body)
            except Exception:
                logger.exception("skipping unreadable instance record %s", key)
                continue
            instance_id = str(doc.get("instance_id") or key[len(prefix) : -len(".json")])
            out[instance_id] = {k: str(v) for k, v in doc.items() if k != "instance_id"}
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    return out


def load_manifest(swebench_version: str, bucket: str | None = None) -> CacheManifest | None:
    """Read the manifest from S3; ``None`` when the warm job never wrote one."""
    bucket = bucket or os.environ.get("DATASET_BUCKET") or aws_names.dataset_bucket_default()
    key = key_for(swebench_version)
    try:
        s3 = get_s3_client()
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        return CacheManifest.from_payload(json.loads(body))
    except Exception as exc:  # noqa: BLE001 - unseeded = the "not warmed" state
        logger.info("cache manifest %s/%s unavailable (%s)", bucket, key, exc)
        return None


def write_manifest(manifest: CacheManifest, bucket: str | None = None) -> str:
    """Persist the manifest to S3; returns the object key."""
    bucket = bucket or os.environ.get("DATASET_BUCKET") or aws_names.dataset_bucket_default()
    key = key_for(manifest.swebench_version)
    payload = json.dumps(asdict(manifest), indent=2).encode("utf-8")
    s3 = get_s3_client()
    s3.put_object(Bucket=bucket, Key=key, Body=payload)
    logger.info("cache manifest written: %s/%s (%d bytes)", bucket, key, len(payload))
    return key
