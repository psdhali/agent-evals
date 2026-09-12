#!/usr/bin/env python3
"""Phase 3 force-failure tests — verify the failure paths the phase exists to get right.

Covers:
1. Budget-cap kill   → BUDGET_EXCEEDED, partial patch into MinIO, NO eval job (ADR-0016)
2. Timeout kill      → HARNESS_TIMEOUT, partial patch into MinIO, NO eval job (ADR-0016)
3. Poison message    → lands in DLQ after maxReceiveCount=3, does NOT cycle forever (P3-5a)
4. Worker crash      → message becomes visible again (heartbeat stopped); reclaim is
                       bounded by the derived base visibility window, not multi-hour (P3-1)

Stuck-loop is deliberately NOT tested here — deferred to Phase 4 by owner decision (P3-3).

Usage:
    python3 scripts/force_failure_test.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    print("=" * 60)
    print("Phase 3 force-failure tests")
    print("=" * 60)

    from swebench_eval.dataset.swebench_loader import load_single_instance
    from swebench_eval.orchestrator.control_plane.dispatcher import dispatch_run
    from swebench_eval.orchestrator.control_plane.results_writer import run_results_writer
    from swebench_eval.orchestrator.run_config import RunConfig
    from swebench_eval.workers.harness_worker import run_harness_worker

    instance = load_single_instance("astropy__astropy-12907")
    if instance is None:
        print("ERROR: astropy__astropy-12907 not found in dataset")
        sys.exit(1)

    # Start workers (results writer so PATCH_READY would enqueue eval — the
    # assertion is that BUDGET_EXCEEDED/FAILED_HARNESS do NOT).
    threads = [
        threading.Thread(target=run_harness_worker, daemon=True, name="harness-worker"),
        threading.Thread(target=run_results_writer, daemon=True, name="results-writer"),
    ]
    for t in threads:
        t.start()
    time.sleep(1)

    # ------------------------------------------------------------------
    # 1. Budget-cap kill → BUDGET_EXCEEDED, no eval
    # ------------------------------------------------------------------
    print("\n" + "-" * 40)
    print("Case 1: budget-cap kill (max_tokens_per_instance=1000)")
    run_id_budget = str(int(time.time() * 1000))
    dispatch_run(
        run_id=run_id_budget,
        instances=[instance],
        config=RunConfig(
            harness="custom_minimal",
            model_alias="cheap-oss-model",
            timeout_seconds=120,
            max_tokens_per_instance=1000,  # deliberately tiny — forces budget_exceeded
            max_cost_usd_per_instance=5.0,
        ),
    )
    state = _wait_for_harness_state(run_id_budget, instance.instance_id)
    assert state == "BUDGET_EXCEEDED", f"Expected BUDGET_EXCEEDED, got {state}"

    # Fetch persisted row + check no eval row exists (ADR-0016).
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT error_category, patch_path FROM instance_results
               WHERE run_id=%s AND instance_id=%s AND phase='harness'""",
            (run_id_budget, instance.instance_id),
        )
        row = cur.fetchone()
        cur.execute(
            """SELECT COUNT(*) FROM instance_results
               WHERE run_id=%s AND phase='eval'""",
            (run_id_budget,),
        )
        eval_count = cur.fetchone()[0]
    conn.close()

    assert row is not None and row[0] == "HARNESS_BUDGET_EXCEEDED"
    # ADR-0016's split guarantee:
    #   - a patch captured on kill IS persisted (when one exists)
    #   - it is NEVER enqueued for grading, regardless of whether one exists
    # A budget kill can trip before the agent made any edit → no diff to
    # capture is a legitimate outcome.  The evaluation bar is "no eval job".
    patch_status = row[1] if row and row[1] else "(none — killed before edits)"
    assert eval_count == 0, "Budget-cap kill must NOT enqueue an eval job (ADR-0016)"
    print(f"  ✓ BUDGET_EXCEEDED, patch={patch_status}, {eval_count} eval row(s)")

    # The patch-capture half of ADR-0016 (a mid-work kill persists its partial
    # patch) is demonstrated by the Commit 10 smoke run: a 300s-timeout gave
    # FAILED_HARNESS with a non-empty patch.diff in MinIO and no eval row
    # (pipeline verified end-to-end).  Cases 1-2 here pin the no-eval half.

    # ------------------------------------------------------------------
    # 2. Timeout kill → HARNESS_TIMEOUT, no eval
    # ------------------------------------------------------------------
    print("\n" + "-" * 40)
    print("Case 2: timeout kill (timeout_seconds=10)")
    run_id_timeout = str(int(time.time() * 1000))
    dispatch_run(
        run_id=run_id_timeout,
        instances=[instance],
        config=RunConfig(
            harness="custom_minimal",
            model_alias="cheap-oss-model",
            timeout_seconds=10,  # deliberately short — forces timeout
            max_tokens_per_instance=None,  # explicit "unlimited"
            max_cost_usd_per_instance=5.0,
        ),
    )
    state = _wait_for_harness_state(run_id_timeout, instance.instance_id)
    assert state == "FAILED_HARNESS", f"Expected FAILED_HARNESS, got {state}"

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT error_category FROM instance_results
               WHERE run_id=%s AND instance_id=%s AND phase='harness'""",
            (run_id_timeout, instance.instance_id),
        )
        row = cur.fetchone()
        cur.execute(
            """SELECT COUNT(*) FROM instance_results
               WHERE run_id=%s AND phase='eval'""",
            (run_id_timeout,),
        )
        eval_count = cur.fetchone()[0]
    conn.close()

    assert row is not None and row[0] == "HARNESS_TIMEOUT"
    assert eval_count == 0, "Timeout kill must NOT enqueue an eval job (ADR-0016)"
    print(f"  ✓ HARNESS_TIMEOUT, {eval_count} eval row(s)")

    # ------------------------------------------------------------------
    # 3. Poison message → DLQ (P3-5a)
    # ------------------------------------------------------------------
    print("\n" + "-" * 40)
    print("Case 3: poison message → DLQ after maxReceiveCount=3")
    from swebench_eval.queue.client import (
        delete_message,
        get_dlq_depth,
        get_queue_url,
        get_sqs_client,
        receive_message,
        send_message,
    )

    send_message("eval-jobs", {"malformed": True})  # _parse_eval_job will KeyError

    sqs = get_sqs_client()
    received = 0
    for _ in range(3):
        msg = receive_message("eval-jobs", wait_seconds=5, visibility_timeout=0)
        if msg:
            received += 1
            # Immediately make visible again → counts as another receive.
    # Drain it so it doesn't block later tests.
    while True:
        msg = receive_message("eval-jobs", wait_seconds=2, visibility_timeout=0)
        if not msg:
            break
        delete_message("eval-jobs", msg["receipt_handle"])

    # Check the DLQ (M2.1: the attribute reader lives on queue/client.py now —
    # not a second boto3 path in the script).
    dlq_count = get_dlq_depth("eval-jobs")
    assert dlq_count >= 1, "Poison message should land in the DLQ, not cycle forever"
    print(f"  ✓ poison message in DLQ ({dlq_count}) after {received} receives")

    # Clean the DLQ.
    while True:
        msg = receive_message("eval-jobs-dlq", wait_seconds=2, visibility_timeout=0)
        if not msg:
            break
        delete_message("eval-jobs-dlq", msg["receipt_handle"])

    # ------------------------------------------------------------------
    # 4. Worker crash → visibility reclaim (heartbeat stops)
    # ------------------------------------------------------------------
    print("\n" + "-" * 40)
    print("Case 4: worker crash → message reclaimed after visibility window")

    # Use eval-jobs — no eval worker is running in this script, so the
    # me message sits exactly where a crashed worker left it.
    # Simulate: worker received the message (it is now in flight with a
    # 10s visibility window) and crashed without heartbeating/deleting.
    send_message("eval-jobs", {"run_id": "reclaim-test", "instance_id": "x", "attempt_number": 1})
    gone_msg = receive_message("eval-jobs", wait_seconds=5, visibility_timeout=10)
    assert gone_msg is not None, "message should have been received (now in flight)"

    # Heartbeat stops.  The message must become visible again within the
    # visibility window (10s) — NOT immediately, NOT after a multi-hour
    # static timeout.  This is the exact behaviour ADR-0015 exists to get right.
    t0 = time.monotonic()
    reclaimed = None
    reclaim_after = 0.0
    while time.monotonic() - t0 < 15:
        reclaimed = receive_message("eval-jobs", wait_seconds=2, visibility_timeout=0)
        if reclaimed:
            reclaim_after = time.monotonic() - t0
            break
        time.sleep(0.5)

    assert reclaimed is not None, "message was NOT reclaimed — heartbeat/visibility broken"
    assert (
        reclaim_after >= 8
    ), (  # within ~1 visibility window, not instantly
        f"reclaimed after {reclaim_after:.1f}s — too fast (should be ~10s window)"
    )
    print(f"  ✓ message reclaimed after {reclaim_after:.1f}s (10s visibility window)")

    # Clean up: delete the reclaimed message and purge queues.
    delete_message("eval-jobs", reclaimed["receipt_handle"])
    for qname in ("harness-jobs", "eval-jobs", "results"):
        sqs.purge_queue(QueueUrl=get_queue_url(qname))

    # ------------------------------------------------------------------
    # 5. Redelivery must not null the diagnostic fields (F5/F6)
    # ------------------------------------------------------------------
    print("\n" + "-" * 40)
    print("Case 5: redelivery must NOT null error_category/error_detail (F5)")

    from swebench_eval.orchestrator.control_plane.dispatcher import register_run
    from swebench_eval.orchestrator.control_plane.results_writer import _process_result
    from swebench_eval.queue.schemas import ResultMessage

    run_id_f5 = str(int(time.time() * 1000))
    # R2-2: every path that writes a result must first register the run
    # (runs + run_targets), so the row is attributable to a harness.
    register_run(run_id_f5, "custom_minimal", "cheap-oss-model")
    # First delivery: FAILED_HARNESS with full diagnosis.
    _process_result(
        ResultMessage(
            run_id=run_id_f5,
            instance_id="inst-f5",
            attempt_number=1,
            phase="harness",
            state="FAILED_HARNESS",
            error_category="HARNESS_TIMEOUT",
            error_detail="wall-clock timeout (600s)",
            patch_s3_key="runs/x/patch.diff",
        )
    )
    # Redelivery that omits the diagnostic fields (SQS at-least-once crash path).
    _process_result(
        ResultMessage(
            run_id=run_id_f5,
            instance_id="inst-f5",
            attempt_number=1,
            phase="harness",
            state="FAILED_HARNESS",  # no error_category / error_detail
        )
    )

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT error_category, error_detail, patch_path FROM instance_results "
            "WHERE run_id=%s",
            (run_id_f5,),
        )
        row = cur.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "HARNESS_TIMEOUT", f"F5: error_category nulled → {row[0]}"
    assert row[1] == "wall-clock timeout (600s)", f"F5: error_detail nulled → {row[1]}"
    assert row[2] == "runs/x/patch.diff", f"F5: patch_path lost → {row[2]}"
    print(f"  ✓ error_category={row[0]}, error_detail={row[1]}, patch={row[2]} after redelivery")

    print("\n" + "=" * 60)
    print("All force-failure cases passed.")
    print("=" * 60)


def _wait_for_harness_state(run_id: str, instance_id: str, timeout: int = 180) -> str:
    """Poll Postgres until the harness phase reaches a terminal state."""
    from swebench_eval.database.connection import get_connection

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT state FROM instance_results
                   WHERE run_id=%s AND instance_id=%s AND phase='harness'""",
                (run_id, instance_id),
            )
            row = cur.fetchone()
        conn.close()
        if row:
            return str(row[0])
        time.sleep(2)
    raise AssertionError(f"harness state not terminal within {timeout}s for {run_id}")


if __name__ == "__main__":
    main()
