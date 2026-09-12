#!/usr/bin/env python3
"""Generate data/contamination/leak_detectable.json (ADR-0038 §2).

The static, offline, per-dataset pass: for every instance determine which
FAIL_TO_PASS node ids are absent at base_commit, and commit the result.

At base_commit the gold test patch is NOT applied, so a test the patch ADDS
(its ``def test_...`` / ``class Test...`` on a ``+`` line) was not present in
the repo the agent saw.  Parsing the gold ``test_patch`` for added test
definitions therefore yields the absent FAIL_TO_PASS ids directly — NO
checkout, clone, install, or pytest collection (brief §1.2).  This collapses
the old build-host plan entirely.

The map distinguishes:
  - instance PRESENT with non-empty list  -> leak-detectable (those ids absent)
  - instance present with []              -> checked, nothing absent (False)
  - instance ABSENT from the map          -> unknown (NULL)  [P-3 omit]

A node-id format mismatch yields UNKNOWN (P-3): if the patch adds tests but
NONE of the FAIL_TO_PASS ids names any added test, that is a format mismatch,
never "all absent" — omitting keeps the claim honest.

Run where the dataset mirror exists (or HF with SWEBENCH_DATASET set) with gold
enabled.  Never per-run — the result is committed once per dataset.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swebench_eval.dataset.swebench_loader import SwebenchLiteLoader
from swebench_eval.evaluation import leak_detection


def build_map(instances: list[Any]) -> dict[str, list[str]]:
    """Produce instance_id -> absent FAIL_TO_PASS ids from gold test_patches.

    Instances whose node-id format we cannot trust are omitted (UNKNOWN) rather
    than written with a guessed empty/absent list.
    """
    out: dict[str, list[str]] = {}
    for inst in instances:
        absent = leak_detection.leak_detectable_via_test_patch(inst.test_patch, inst.fail_to_pass)
        if absent is None:
            # UNKNOWN (P-3): refuse to guess -> omit so the worker records NULL,
            # never a fabricated all-clear or all-absent.
            continue
        out[inst.instance_id] = absent
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        default="data/contamination/leak_detectable.json",
        help="output path (builder 1 owns the default artifact name)",
    )
    ap.add_argument(
        "--dataset",
        default=None,
        help="SWE-bench dataset name; defaults to the SWEBENCH_DATASET env / loader default",
    )
    args = ap.parse_args()

    if args.dataset:
        os.environ["SWEBENCH_DATASET"] = args.dataset

    # include_gold=True: parsing authoritative absence requires the gold
    # test_patch (which is exactly the delta introduced at base).  This is the
    # eval/grading load, never a harness load — the map is gold-derived and must
    # never reach the harness tier (ADR-0038 / P-1).
    loader = SwebenchLiteLoader(include_gold=True)
    instances = loader.load()
    result = build_map(instances)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"wrote {args.out} ({len(result)} instances; "
        f"{len(instances) - len(result)} omitted as UNKNOWN)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
