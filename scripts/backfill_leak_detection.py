#!/usr/bin/env python3
"""B5 (stage-c-handover-round-2 §4) — offline leak-detection backfill.

Leak detection was moved OFF the harness tier (the harness must never carry the
gold-derived leak-detectable map).  It now runs here, on the orchestrator/build
side:

1. read the stored `patch.diff` + `trajectory.jsonl` S3 keys from the
   harness-phase `instance_results` row (the worker uploads them as
   `patch_path` / `trajectory_path`),
2. apply `leak_detection.detect_leaked_node_ids` against the committed map,
3. `UPDATE leaked_node_ids` / `leak_detectable` on that same row.

It is an OFFLINE, re-runnable pass: it sees every attempt (including ones that
failed in the harness and never reached grading), and it is what makes the
probe (B6) deferrable — once the map is populated, re-run this and the honesty
columns fill in.

Usage:
  .venv/bin/python scripts/backfill_leak_detection.py \
      --run-id <run_id> [--attempt 1]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swebench_eval.database.connection import get_connection
from swebench_eval.evaluation import leak_detection
from swebench_eval.queue.client import get_artifact


def _bucket() -> str:
    import os

    return os.environ.get("ARTIFACTS_BUCKET", "eval-artifacts")


def backfill_run(
    run_id: str, attempt: int | None = None, instance_ids: list[str] | None = None
) -> int:
    """Backfill leak columns for *run_id*'s harness-phase rows; returns the
    number of rows updated.

    *instance_ids*, when given, scopes the scan to just those instances
    (offline-analysis-design.md §10.1's judge-launch instance selector) —
    filtered in SQL, not fetched-then-discarded, so a judge launch scoped to
    a handful of instances doesn't pull the whole run's rows across the wire.

    ``leak_scan_at`` is set on EVERY row this pass examines — including the
    UNKNOWN/omit case (offline-analysis-design.md §2.3 / §9 DoD #3).  Omitting
    is a completed decision ("we looked, this instance isn't in the map"), not
    an absence of one, and it must be distinguishable from a row this pass
    never reached at all.  Without that distinction, leaked_node_ids=NULL means
    two different things and the Pass A vs Pass B confusion matrix (§10.6)
    silently miscounts "unscanned" rows as "scanned, clean" negatives.

    Candidate selection is therefore ``leak_scan_at IS NULL`` (never scanned),
    not ``leaked_node_ids IS NULL`` — the old condition would forever re-select
    (harmlessly, since idempotent) rows this pass had already visited and
    correctly decided were UNKNOWN, and would never learn to leave them alone.
    """
    conn = get_connection()
    updated = 0
    try:
        leak_map = leak_detection.load_leak_detectable()
        map_version = leak_detection.leak_map_version()
        if not leak_map:
            print(
                "WARNING: leak-detectable map is empty (B6 not populated yet). "
                "leak_scan_at will still be set (honest: we looked, found no map) "
                "but every row stays UNKNOWN."
            )
        with conn.cursor() as cur:
            params: list[object] = [run_id]
            instance_filter = ""
            if instance_ids:
                instance_filter = " AND instance_id = ANY(%s)"
                params.append(list(instance_ids))
            cur.execute(
                f"""SELECT run_id, instance_id, attempt_number,
                          patch_path, trajectory_path
                     FROM instance_results
                     WHERE run_id = %s AND phase = 'harness'
                       AND leak_scan_at IS NULL{instance_filter}
                     ORDER BY instance_id, attempt_number""",
                params,
            )
            rows = cur.fetchall()
            for rid, instance_id, attempt_number, patch_path, trajectory_path in rows:
                if attempt is not None and attempt_number != attempt:
                    continue
                absent = leak_map.get(instance_id)
                # absent=None -> unknown (Trap 3): leaked_node_ids/leak_detectable
                # stay NULL, never a fabricated empty list — but the row IS marked
                # scanned, because we did look it up and this is the answer.
                if absent is None:
                    cur.execute(
                        """UPDATE instance_results
                           SET leak_scan_at = now(), leak_map_version = %s
                           WHERE run_id = %s AND instance_id = %s
                             AND attempt_number = %s AND phase = 'harness'""",
                        (map_version, rid, instance_id, attempt_number),
                    )
                    updated += cur.rowcount
                    continue
                text = ""
                try:
                    if patch_path:
                        text += get_artifact(_bucket(), patch_path).decode("utf-8", "replace")
                    if trajectory_path:
                        text += "\n" + get_artifact(_bucket(), trajectory_path).decode(
                            "utf-8", "replace"
                        )
                except Exception as exc:  # noqa: BLE001
                    print(f"  skipping {run_id}/{instance_id} (artifact fetch failed): {exc}")
                    continue
                leaked = leak_detection.detect_leaked_node_ids(text, absent)
                # leaked is stored AS-IS, including [] — "scanned, nothing found"
                # is a real, distinct fact from "never scanned" (the bug this
                # replaces: `leaked or None` collapsed an empty finding to the
                # same NULL as unscanned, silently erasing the distinction
                # leak_scan_at exists to preserve).
                cur.execute(
                    """UPDATE instance_results
                       SET leaked_node_ids = %s, leak_detectable = %s,
                           leak_scan_at = now(), leak_map_version = %s
                       WHERE run_id = %s AND instance_id = %s
                         AND attempt_number = %s AND phase = 'harness'""",
                    (leaked, bool(absent), map_version, rid, instance_id, attempt_number),
                )
                updated += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="run_id to backfill")
    parser.add_argument("--attempt", type=int, default=None, help="restrict to one attempt")
    args = parser.parse_args()

    # backfill_run itself prints the empty-map warning (it still needs to run
    # to set leak_scan_at honestly, so "nothing will be backfilled" is no
    # longer accurate here).
    n = backfill_run(args.run_id, attempt=args.attempt)
    print(f"backfilled {n} instance_results rows for run {args.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
