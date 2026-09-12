"""S3 artifact fetching for the dashboard (architecture §10 / builder2 phase 1 §3).

Proxies the stored artifact objects (patch.diff, trajectory.jsonl,
harness_stdout.log, eval_report.json, and — dev/BUILDER4-EXPOSE-MISSING-
ARTIFACTS-2026-08-28.md — native_trajectory.json, test_output.txt,
run_instance.log) from MinIO locally / S3 in AWS.  The ``queue.client`` module
already answers "which S3 endpoint and credentials", so this reuses
``get_s3_client`` / ``get_artifact`` rather than inventing a second S3
connection path — the local/AWS swap stays one seam.

An artifact is only ever fetched for a (run, instance, attempt) whose
instance_results row authorises it: the requested kind resolves to that row's
stored S3 key, so a caller cannot reach arbitrary objects.  A NULL path means
the artifact was never produced (honest 404), never a fabricated one.

``llm_calls.jsonl`` is deliberately NOT a kind here — the report that added
the other three explicitly scoped it out as a data/export concern, not a
viewer tab; its own ``s3_key`` already flows through ``llm_calls`` rows.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from typing import Any

logger = logging.getLogger(__name__)

# artifact kinds → (instance_results path column, S3 filename suffix, media type)
_KINDS: dict[str, tuple[str, str, str]] = {
    "patch": ("patch_path", "patch.diff", "text/plain"),
    "trajectory": ("trajectory_path", "trajectory.jsonl", "application/x-ndjson"),
    "log": ("raw_log_path", "harness_stdout.log", "text/plain"),
    "report": ("report_path", "eval_report.json", "application/json"),
    # dev/BUILDER4-EXPOSE-MISSING-ARTIFACTS-2026-08-28.md: 3 of the 4 hidden
    # artifacts (llm_calls.jsonl deliberately excluded, see module docstring).
    "native_trajectory": (
        "native_trajectory_s3_key",
        "native_trajectory.json",
        "application/json",
    ),
    "test_output": ("test_output_s3_key", "test_output.txt", "text/plain"),
    "run_log": ("run_log_s3_key", "run_instance.log", "text/plain"),
}


def _bucket() -> str:
    return os.environ.get("ARTIFACTS_BUCKET", "eval-artifacts")


def path_column_for(kind: str) -> str:
    """The instance_results column that stores the S3 key for *kind*."""
    spec = _KINDS.get(kind)
    if spec is None:
        raise ValueError(f"unknown artifact kind: {kind}")
    return spec[0]


def artifact_key_for_row(kind: str, row: dict[str, Any]) -> str | None:
    """The row's stored S3 key for *kind*, or None when the artifact is absent."""
    return row.get(path_column_for(kind))


def media_type_for(kind: str) -> str:
    spec = _KINDS.get(kind)
    if spec is None:
        raise ValueError(f"unknown artifact kind: {kind}")
    return spec[2]


def fetch(kind: str, row: dict[str, Any]) -> bytes:
    """Download the *kind* artifact for a row whose key was already resolved."""
    from swebench_eval.queue.client import get_artifact

    key = artifact_key_for_row(kind, row)
    if not key:
        raise FileNotFoundError(f"no object key stored for artifact kind '{kind}'")
    data = get_artifact(_bucket(), key)
    logger.info("artifact fetch: %s/%s (%d bytes)", _bucket(), key, len(data))
    return data


def content_type_for_name(name: str) -> str:
    """Best-effort media type by object name; never ``None`` (FastAPI rejects it)."""
    ctype, _ = mimetypes.guess_type(name)
    return ctype or "application/octet-stream"
