"""SWE-bench dataset loader.

The dataset identity is one source of truth (§ V1, switch-to-swebench-verified):
``_DATASET_NAME`` defaults to ``princeton-nlp/SWE-bench_Verified`` (the only
current publishable comparison) and is overridable via ``SWEBENCH_DATASET``.
``_PINNED_REVISION`` follows it (per-dataset pin), overridable via
``SWEBENCH_REVISION``.

Mirror-first loading (Phase 5b, review §2, 2026-08-16): the pipeline reads the
pinned split from the S3 ``dataset`` bucket mirror rather than HuggingFace, so
Phase 8's thousands of unauthenticated HF fetches (rate-limited at n=1)
disappear.  ``scripts/seed_dataset.py`` writes two objects per revision:

  * ``<split>/<revision>.jsonl``        — every :class:`Instance` field, INCLUDING
    the gold ``patch``.  Read only by the grading path, which legitimately needs
    it (the eval worker's ``SwebenchRunner.grade`` builds the official TestSpec
    from it).
  * ``<split>/<revision>.public.jsonl`` — ``_PUBLIC_FIELDS``, gold ``patch``
    excluded.  Read by every harness-facing path, so the answer never enters a
    dispatcher/harness process (the exclusion in ``seed_dataset.py`` now has a
    runtime consumer).

HuggingFace remains the fallback when the mirror is unseeded — the local dev
path (MinIO has no mirror) — explicit and logged, never silent.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from swebench_eval import aws_names
from swebench_eval.dataset.base import DatasetLoader, Instance
from swebench_eval.queue.client import get_s3_client

logger = logging.getLogger(__name__)

# One source of truth for dataset identity (V1, switch-to-swebench-verified):
# none of the eight call sites hardcodes the literal — they all derive from the
# constants here, and an env override flips every site at once.
#
# _DATASET_NAME is the ACTIVE eval dataset: Verified by default (the only
# current publishable comparison — nobody publishes Lite any more), forced to
# Lite via SWEBENCH_DATASET for continuity with earlier work.
#
# ADR-0043 (2026-09-05): the datasets live under the ``SWE-bench`` HF org since
# 2026-08 (5.x harness era: rows carry ``image``, ``eval_script``, ``log_parser``,
# ``eval_type``).  The old ``princeton-nlp/*`` repos are frozen at their 2025
# revisions and are what git tag ``checkpoint-pre-v5-upgrade-2026-09-05`` used.
_LITE_DATASET_NAME = "SWE-bench/SWE-bench_Lite"
_FULL_DATASET_NAME = "SWE-bench/SWE-bench"  # the full split
_VERIFIED_DATASET_NAME = "SWE-bench/SWE-bench_Verified"
_DATASET_NAME = os.environ.get("SWEBENCH_DATASET", _VERIFIED_DATASET_NAME)

# Pinned dataset revision — prevents silent drift in test lists and patches
# across runs (same principle as uv.lock: reproducibility needs a pinned
# version).  The default follows the ACTIVE dataset so a Lite run never
# accidentally reuses the Verified pin (a silent wrong-dataset run — the
# reviewer's exact warning); SWEBENCH_REVISION overrides it.
#
# Verified: ``SWE-bench/SWE-bench_Verified`` @ 78f471bf (2026-08-16, "Upload
# dataset") — the revision ADR-0043 adopts.  Versus the previous pin
# (princeton-nlp @ c104f840): identical patch/test_patch/base_commit/FAIL_TO_PASS
# for all 500; PASS_TO_PASS changed for exactly two (astropy-7606, django-10097);
# four new columns.  The image-digest snapshot that pairs with this revision is
# ``design/image-digests/SWE-bench_Verified-78f471bf.json``.
#
# Lite and full are NOT re-pinned here (no Lite/full run exists on 5.x yet);
# these are the SWE-bench-org repos' revisions at the time of the move, resolved
# 2026-09-05, and a Lite/full run must re-verify them first.
_LITE_REVISION = os.environ.get("SWEBENCH_LITE_REVISION", "main")
_VERIFIED_REVISION = "78f471bf655a3137b2e8a75af1501690ec009ec3"
_FULL_REVISION = os.environ.get("SWEBENCH_FULL_REVISION", "main")
_PINNED_REVISION: str = os.environ.get(
    "SWEBENCH_REVISION",
    _LITE_REVISION if "Lite" in _DATASET_NAME else _VERIFIED_REVISION,
)


# The harness-facing subset: everything a HarnessJob needs and NOTHING of the
# answer.  Kept in one place so the writer (scripts/seed_dataset.py) and the
# reader here cannot drift independently.
#
# ``environment_setup_commit`` is deliberately in this subset even though the
# harness never acts on it directly: ``make_test_spec`` reads it to build the
# env image key (found live 2026-08-18 — an absent/empty value made SWE-bench
# fetch requirements.txt at commit ``""`` and the django rows were dropped
# silently at dispatch).  It is a commit SHA, not the answer.
#
# B1 (stage-c-handover-round-2 §1 / dataset-mirror-test-patch-exposure): the
# answer fields are NOT in this subset.  ``test_patch`` (the gold test SOURCE)
# and ``fail_to_pass``/``pass_to_pass`` (the hidden test NAMES) are stripped
# from the public row so an agent that gets the task-role credentials cannot
# read them — a hit on a leaked node id no longer needs to mean memorisation
# vs retrieval.  The install-script path (``make_test_spec`` via
# ``_instance_to_swebench_dict``) defaults them to ""/[] and works unchanged
# (verified).  The grading path reads them from the FULL row, which is intact.
#
# ADR-0043: ``image``, ``log_parser`` and ``eval_type`` are public (an image
# name and two parser labels — nothing of the answer).  ``eval_script`` is NOT:
# 5.x's eval script embeds the gold ``test_patch`` verbatim (a heredoc that
# applies it) plus the test command with the hidden test directives, so it is
# answer material and stays on the full row only.
_PUBLIC_FIELDS = (
    "instance_id",
    "repo",
    "base_commit",
    "environment_setup_commit",
    "problem_statement",
    "version",
    "image",
    "log_parser",
    "eval_type",
)

# The grading path's row: every Instance field.  ``_instance_from_mirror_row``
# defaults missing keys to "" so the same mapper serves both files.
_FULL_FIELDS = (
    "instance_id",
    "repo",
    "base_commit",
    "problem_statement",
    "hints",
    "patch",
    "fail_to_pass",
    "pass_to_pass",
    "test_patch",
    "environment_setup_commit",
    "version",
    "created_at",
    "image",
    "eval_script",
    "log_parser",
    "eval_type",
)

# Public aliases for scripts/seed_dataset.py (single source of truth for the
# mirror's two schemas).
PUBLIC_FIELDS = _PUBLIC_FIELDS
FULL_FIELDS = _FULL_FIELDS


def image_digest_snapshot_name(dataset_name: str | None = None, revision: str | None = None) -> str:
    """``<dataset leaf>-<rev12>.json`` — the committed image-digest snapshot for a pin (ADR-0043)."""
    dataset_name = dataset_name or _DATASET_NAME
    revision = revision or _PINNED_REVISION
    return f"{dataset_name.rsplit('/', 1)[-1]}-{revision[:12]}.json"


def load_image_digest_snapshot(
    dataset_name: str | None = None, revision: str | None = None
) -> dict[str, str] | None:
    """``instance_id -> official image digest`` for the pinned (dataset, revision).

    The snapshot is package data (``swebench_eval/dataset/image_digests/``,
    written by ``scripts/snapshot_image_digests.py``) so the dispatcher's gate
    and the build tier read the same pin.  ``None`` when no snapshot exists for
    this pin (an unpinned/local dataset) — callers treat that as "no digest
    check possible", never as "everything matches".
    """
    from importlib import resources

    name = image_digest_snapshot_name(dataset_name, revision)
    try:
        path = resources.files("swebench_eval.dataset").joinpath("image_digests").joinpath(name)
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError) as exc:
        logger.info("no image-digest snapshot %s (%s)", name, exc)
        return None
    return {str(k): str(v["digest"]) for k, v in dict(doc.get("instances", {})).items()}


def _mirror_key(split: str, revision: str, include_gold: bool) -> str:
    """The S3 object key for the pinned-revision mirror of *split*."""
    leaf = "jsonl" if include_gold else "public.jsonl"
    return f"{_DATASET_NAME}/{split}/{revision}.{leaf}"


def _dataset_bucket() -> str:
    """``DATASET_BUCKET``, else the account's ``<prefix>-dataset-<acct>-<region>`` (aws_names)."""
    return os.environ.get("DATASET_BUCKET") or aws_names.dataset_bucket_default()


def _load_mirror(
    split: str = "test",
    revision: str | None = _PINNED_REVISION,
    include_gold: bool = False,
) -> list[Instance] | None:
    """Read the pinned-revision mirror from S3; ``None`` when it is unseeded.

    S3 client comes from ``queue.client.get_s3_client`` so the same
    environment-aware selection applies: real S3 under the task role in AWS,
    local/MinIO elsewhere (where the mirror does not exist → fallback).
    """
    if revision is None:
        return None  # "follow main" has no pinned object — HF only
    key = _mirror_key(split, revision, include_gold)
    bucket = "<unresolved>"
    try:
        bucket = _dataset_bucket()
        s3 = get_s3_client()
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - unseeded/absent mirror must not crash
        logger.info("dataset mirror %s/%s unavailable (%s); using HF", bucket, key, exc)
        return None

    rows: list[Instance] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(_instance_from_mirror_row(json.loads(line)))
        except Exception:
            logger.exception("dropping malformed mirror row: %.120s", line)
    rows.sort(key=lambda i: i.instance_id)
    return rows


def _instance_from_mirror_row(row: dict[str, Any]) -> Instance:
    """Build an :class:`Instance` from a mirror row (dataclass field names).

    Missing keys (e.g. a public row lacking ``patch``) default to ``""``, so a
    single mapper serves both the public and full files.
    """
    return Instance(**{field: str(row.get(field, "")) for field in _FULL_FIELDS})


class SwebenchLiteLoader(DatasetLoader):
    """Load SWE-bench instances (default Verified) — S3 mirror first, HF fallback.

    The class name is kept (``SwebenchLiteLoader``) to avoid a cosmetic rename
    rippling everywhere; the scope of V1 is to make identity a value, not to
    rename (the docstrings now say dataset, not Lite).

    Parameters
    ----------
    split: Which split — ``"test"`` (the shared test split on the active
        dataset) or ``"dev"``.
    revision: Dataset revision to pin; defaults to ``_PINNED_REVISION``.  Pass
        ``None`` to follow ``main`` (HF only — no pinned mirror object).
    include_gold: If ``True`` load the full rows (incl. the gold ``patch``) —
        for the grading path only.  ``False`` (default) returns the harness
        ``_PUBLIC_FIELDS`` mirror, gold excluded, for every harness-facing path.
    """

    def __init__(
        self,
        split: str = "test",
        revision: str | None = _PINNED_REVISION,
        include_gold: bool = False,
    ) -> None:
        self._split = split
        self._revision = revision
        self._include_gold = include_gold

    @property
    def revision(self) -> str | None:
        """The pinned revision, or ``None`` if following ``main``."""
        return self._revision

    def load(self) -> list[Instance]:
        mirror = _load_mirror(self._split, self._revision, self._include_gold)
        if mirror is not None:
            logger.info("loaded %d instances from S3 mirror (%s)", len(mirror), self._split)
            return mirror
        return _load_from_huggingface(self._split, self._revision, self._include_gold)


def load_single_instance(
    instance_id: str,
    split: str = "test",
    revision: str | None = _PINNED_REVISION,
    include_gold: bool = False,
) -> Instance | None:
    """Load a single instance by ID.

    Reads from the S3 mirror (the whole file) when available, otherwise streams
    the HF split.  Returns ``None`` when ``instance_id`` is not found.
    """
    instance = next(
        (
            inst
            for inst in _load_from_mirror_or_hf(split, revision, include_gold)
            if inst.instance_id == instance_id
        ),
        None,
    )
    return instance


def _load_from_mirror_or_hf(split: str, revision: str | None, include_gold: bool) -> list[Instance]:
    """Mirror rows when seeded, else HF rows (falls back when mirror unseeded)."""
    mirror = _load_mirror(split, revision, include_gold)
    if mirror is not None:
        return mirror
    return _load_from_huggingface(split, revision, include_gold)


def _load_from_huggingface(split: str, revision: str | None, include_gold: bool) -> list[Instance]:
    """Fetch the split from HuggingFace — GRADING path only.

    HuggingFace's copy carries the gold ``patch`` and there is no per-flag
    schema, so a harness-facing load (``include_gold=False``) must NEVER fall
    back to it: the mirror's ``.public.jsonl`` is what strips the answer, and HF
    would serve the gold row it was told to omit (agreed-architecture-changes
    §0.3). Raise instead of quietly leaking the answer — a contract honoured on
    one path and dropped on the other is worse than no contract.
    """
    if not include_gold:
        raise RuntimeError(
            "refusing expected gold-excluded load from HuggingFace: the HF copy "
            "carries the gold patch, which must never reach a harness. Seed the "
            "dataset mirror for this split/revision (scripts/seed_dataset.py) or "
            "limit HF to the grading path (include_gold=True)."
        )
    try:
        from datasets import load_dataset
    except ImportError:
        raise ImportError(
            "The `datasets` package is required to load SWE-bench instances. "
            "Install it with: uv sync"
        )

    ds = load_dataset(
        _DATASET_NAME,
        split=split,
        revision=revision,
    )
    instances = [_row_to_instance(row) for row in ds]
    instances.sort(key=lambda i: i.instance_id)
    return instances


def _row_to_instance(row: dict[str, object]) -> Instance:
    """Convert a single HuggingFace dataset row into an :class:`Instance`."""

    def _pick_str(*keys: str) -> str:
        for k in keys:
            v = row.get(k)
            # 5.x datasets type FAIL_TO_PASS/PASS_TO_PASS as list<string>, not
            # a JSON string.  Serialise as JSON (not ``str(list)`` — Python
            # repr with single quotes is not JSON and make_test_spec would
            # json.loads it) so the mirror row and the Instance stay strings.
            if isinstance(v, (list, dict)):
                return json.dumps(v) if v else ""
            if v and str(v).strip():
                return str(v)
        return ""

    instance_id = _pick_str("instance_id")
    return Instance(
        instance_id=instance_id,
        repo=_pick_str("repo"),
        base_commit=_pick_str("base_commit"),
        problem_statement=_pick_str("problem_statement"),
        hints=_pick_str("hints_text", "hints"),
        patch=_pick_str("patch"),
        fail_to_pass=_pick_str("FAIL_TO_PASS", "fail_to_pass"),
        pass_to_pass=_pick_str("PASS_TO_PASS", "pass_to_pass"),
        test_patch=_pick_str("test_patch"),
        environment_setup_commit=_pick_str("environment_setup_commit"),
        version=_pick_str("version"),
        created_at=_pick_str("created_at"),
        image=_pick_str("image"),
        eval_script=_pick_str("eval_script"),
        log_parser=_pick_str("log_parser"),
        eval_type=_pick_str("eval_type"),
    )
