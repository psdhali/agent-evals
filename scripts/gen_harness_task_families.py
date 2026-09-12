"""Generate the per-instance harness task-definition families from the warm-cache manifest (ADR-0030 H2, ADR-0043).

ECS cannot override a task's image at ``run-task`` time — image selection is a
task-DEFINITION choice, never a runtime parameter. So the dispatcher (H3) picks
a pre-registered task-definition family from the job's instance id, and
Terraform must register one family per per-instance image:
``eval-dev-harness-<instance_id>``, each pointing at that instance's ``-inst``
tag.

The family set is DERIVED — generated here from the SAME manifest the build
tier's records are merged into (``cache-manifest/<swebench-version>.json``,
written by ``scripts/warm_image_cache.py --manifest``) — and emitted as
``infra/terraform/envs/dev/eval/harness-task-families.auto.tfvars.json``, which
``envs/dev/eval`` consumes via ``for_each``. A hand-maintained family list would
drift from the images; generating it is what stops the two apart.

Two-sided unit test (``tests/test_gen_harness_task_families.py``): delete one
instance image from the manifest and the corresponding family is not rendered;
restore it and it is. A hand-maintained list passes neither half.

Usage::

    gen_harness_task_families.py                          # read the live S3 manifest
    gen_harness_task_families.py --manifest-file m.json   # offline / tests / CI
    gen_harness_task_families.py --output /tmp/x.json     # emit somewhere else
    gen_harness_task_families.py --only a__a-1,b__b-2     # narrow to these ids
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Collection, Iterable
from pathlib import Path
from typing import Any

from swebench_eval import aws_names
from swebench_eval.cache_manifest import CacheManifest, load_manifest

logger = logging.getLogger(__name__)


def _default_image_repo() -> str:
    """The ONE collocated ECR repo (5b §1 gate) where the `-inst` tags live —
    ``HARNESS_IMAGE_REPO_URL`` or ``<acct>.dkr.ecr.<region>.amazonaws.com/<prefix>-harness-worker``
    (adoption Phase 1a: derived from swebench_eval.aws_names, never a literal)."""
    return os.environ.get("HARNESS_IMAGE_REPO_URL") or aws_names.ecr_repository("harness-worker")


_FAMILY_PREFIX = f"{aws_names.name_prefix()}-harness-"

# The generated file Terraform auto-loads (auto.tfvars.json).
_DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "infra"
    / "terraform"
    / "envs"
    / "dev"
    / "eval"
    / "harness-task-families.auto.tfvars.json"
)


def harness_task_families(
    manifest: CacheManifest,
    image_repo: str | None = None,
    *,
    inst_instance_ids: Iterable[str] | None = None,
) -> dict[str, dict[str, str]]:
    image_repo = image_repo or _default_image_repo()
    """family -> {"image": ...} — one task definition per PER-INSTANCE image (ADR-0043).

    Each manifest ``instance_images`` entry yields:

      * family ``eval-dev-harness-<instance_id>``  (the string
        ``harness_dispatcher._family_for_job`` probes verbatim)
      * image  ``<image_repo>:<tag>``  (the entry's ``-inst`` tag)

    The dispatcher's probe is byte-exact on the raw instance id (double
    underscores and all) — do NOT sanitise it here, or the probe silently
    misses and the job is refused with no explanation pointing back here.

    The family set is DERIVED from the manifest — which is itself merged from
    the build tier's per-instance records and live ECR — so a family exists
    exactly when a promoted, still-present image does.  The 4.1.0-era env-hash
    families are gone (no env images exist).  ``inst_instance_ids`` narrows
    the set to those ids (a hand list can only SUBTRACT, never add an image
    the manifest does not know).
    """
    wanted = set(inst_instance_ids) if inst_instance_ids is not None else None
    families: dict[str, dict[str, str]] = {}
    for instance_id in sorted(manifest.instance_images):
        if wanted is not None and instance_id not in wanted:
            continue
        entry = manifest.instance_images[instance_id]
        tag = entry.get("tag") or f"{manifest.swebench_version}-{instance_id}-inst"
        families[f"{_FAMILY_PREFIX}{instance_id}"] = {"image": f"{image_repo}:{tag}"}
    return families


def _swebench_version() -> str:
    import importlib.metadata

    try:
        return importlib.metadata.version("swebench")
    except Exception:  # noqa: BLE001 - degraded fallback matches warm_image_cache
        return "unknown"


def _load_manifest(args: argparse.Namespace) -> CacheManifest:
    """Read the manifest from a local JSON file or the live S3 object."""
    if args.manifest_file:
        with open(args.manifest_file, encoding="utf-8") as fh:
            payload: dict[str, Any] = json.load(fh)
        manifest = CacheManifest.from_payload(payload)
        if not manifest.instance_images:
            raise SystemExit(
                f"{args.manifest_file} carries no instance_images — nothing to derive "
                "(build + promote per-instance images, then publish the manifest)"
            )
        return manifest

    version = args.swebench_version or _swebench_version()
    from_s3 = load_manifest(version)
    if from_s3 is None:
        raise SystemExit(
            f"no cache manifest for swebench {version} in S3; run the warm job "
            "first, or pass --manifest-file"
        )
    return from_s3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--manifest-file",
        help="local warm-cache manifest JSON (offline/tests); default reads the live S3 manifest",
    )
    parser.add_argument("--swebench-version", help="swebench version for the S3 manifest read")
    parser.add_argument(
        "--image-repo",
        default=None,
        help="ECR repo URL (default: the account's harness-worker repo)",
    )
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--print", action="store_true", help="print the families map to stdout")
    parser.add_argument(
        "--per-instance",
        action="store_true",
        help="accepted for compatibility; families are per-instance ALWAYS (ADR-0043)",
    )
    parser.add_argument(
        "--only",
        help="comma-separated instance ids to narrow the family set to (subtract only)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    manifest = _load_manifest(args)
    inst_instance_ids: Collection[str] | None = None
    if args.only:
        inst_instance_ids = {w.strip() for w in args.only.split(",") if w.strip()}
    families = harness_task_families(manifest, args.image_repo, inst_instance_ids=inst_instance_ids)
    payload = {"harness_task_families": families}

    if args.print:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        logger.info("wrote %d harness task-definition families -> %s", len(families), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
