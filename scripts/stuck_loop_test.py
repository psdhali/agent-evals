#!/usr/bin/env python3
"""Phase 4 stuck-loop forced-failure — the ADR-0016 proof (P4-2/P4-8).

Runs a deliberately-stuck stub harness through the queue pipeline with
STUCK_ACTIVE_KILL=1, then asserts:

1. ``state=STUCK`` and ``error_category=HARNESS_STUCK`` in Postgres.
2. The partial patch is in MinIO, tagged ``terminated_reason=HARNESS_STUCK``
   (S3 object metadata — the stated tagging mechanism for Phase 5/7).
3. **No eval row was enqueued** (ADR-0016 — the absence is the assertion).

Requires the compose stack up and STUCK_ACTIVE_KILL=1 in the env.

Usage:
    STUCK_ACTIVE_KILL=1 uv run python scripts/stuck_loop_test.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    if os.environ.get("STUCK_ACTIVE_KILL") != "1":
        print("ERROR: run with STUCK_ACTIVE_KILL=1 (the env-gated active-kill test path)")
        sys.exit(1)

    print("=" * 60)
    print("Phase 4 stuck-loop forced-failure test (ADR-0016)")
    print("=" * 60)

    from swebench_eval.dataset.swebench_loader import load_single_instance
    from swebench_eval.orchestrator.control_plane.dispatcher import dispatch_run
    from swebench_eval.orchestrator.control_plane.results_writer import run_results_writer
    from swebench_eval.orchestrator.run_config import RunConfig
    from swebench_eval.workers.harness_worker import run_harness_worker

    instance = load_single_instance("astropy__astropy-12907")
    if instance is None:
        print("ERROR: astropy__astropy-12907 not found")
        sys.exit(1)

    # Start the harness worker + results writer (NOT the eval worker — the
    # ADR-0016 assertion is that no eval job is ever enqueued).
    threads = [
        threading.Thread(target=run_harness_worker, daemon=True, name="harness-worker"),
        threading.Thread(target=run_results_writer, daemon=True, name="results-writer"),
    ]
    for t in threads:
        t.start()
    time.sleep(1)

    run_id = str(int(time.time() * 1000))
    print(f"Run: {run_id}")

    n = dispatch_run(
        run_id=run_id,
        instances=[instance],
        config=RunConfig(
            harness="stuck_stub",
            model_alias="cheap-oss-model",
            timeout_seconds=300,
            max_tokens_per_instance=None,
            max_cost_usd_per_instance=5.0,
        ),
    )
    assert n == 1, "dispatcher enqueued wrong job count"

    # ------------------------------------------------------------------
    # 1. Wait for the harness-phase STUCK row
    # ------------------------------------------------------------------
    from swebench_eval.database.connection import get_connection

    state = None
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT state, error_category, patch_path FROM instance_results
                   WHERE run_id=%s AND instance_id=%s AND phase='harness'""",
                (run_id, instance.instance_id),
            )
            row = cur.fetchone()
        conn.close()
        if row:
            state = row[0]
            print(f"  harness phase → {state}")
            if state in ("STUCK", "FAILED_HARNESS"):
                break
        time.sleep(2)

    if state is None:
        print("ERROR: stuck stub did not reach a terminal harness state")
        sys.exit(1)

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT state, error_category, error_detail, patch_path FROM instance_results
               WHERE run_id=%s AND instance_id=%s AND phase='harness'""",
            (run_id, instance.instance_id),
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) FROM instance_results WHERE run_id=%s AND phase='eval'",
            (run_id,),
        )
        eval_count = cur.fetchone()[0]
    conn.close()

    print(f"  state={row[0]}, error_category={row[1]}")
    print(f"  error_detail={row[2][:120] if row[2] else '(none)'}")

    # Assertions
    assert row[0] == "STUCK", f"Expected state=STUCK, got {row[0]}"
    assert row[1] == "HARNESS_STUCK", f"Expected HARNESS_STUCK, got {row[1]}"
    assert row[3], "Partial patch should be present in MinIO"

    # 2. Confirm the patch is tagged (S3 object metadata).
    from swebench_eval.queue.client import get_s3_client

    s3 = get_s3_client()
    head = s3.head_object(Bucket="eval-artifacts", Key=row[3])
    tags = head.get("Metadata", {})
    print(f"  S3 metadata: {tags}")
    # ADR-0016: the captured patch must be tagged with its terminated_reason.
    assert (
        tags.get("terminated_reason") == "stuck"
    ), f"patch not tagged with terminated_reason, got {tags!r}"
    print("  ✓ patch tagged terminated_reason=stuck (S3 metadata)")

    # 3. No eval row (ADR-0016 — absence is the assertion).
    assert eval_count == 0, f"Stuck kill must NOT enqueue an eval job, found {eval_count}"
    print(f"  eval rows: {eval_count} (ADR-0016 satisfied — not graded)")

    print("\n" + "=" * 60)
    print("Stuck-loop forced-failure test passed: STUCK row, tagged patch, no eval.")
    print("=" * 60)


if __name__ == "__main__":
    main()
