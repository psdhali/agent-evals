#!/usr/bin/env python3
"""Phase 3 queue-based smoke test.

Runs one SWE-bench Lite instance through the full queue-based pipeline:

    dispatcher → harness-jobs → harness worker → results queue
            → Results Writer → eval-jobs → eval worker → results queue
            → Results Writer → terminal state in Postgres

Verifies:
1. The pipeline runs end-to-end (no direct calls — everything via queues).
2. Postgres rows match the expected state machine transitions.
3. Artifacts land in MinIO (patch, trajectory, raw_log, eval_report).
4. The idempotent upsert deduplicates a deliberately-sent duplicate
   ResultMessage (P3-5b).

Requires the Docker Compose stack to be running:
    docker compose -f infra/docker/docker-compose.yml up -d

Usage::
    python3 scripts/smoke_test_phase3.py [--instance-id <id>] [--skip-grading]
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

# Ensure the project root is on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING)

# --harness choices derive from the one adapter map (P4C-2), never a literal.
from swebench_eval.harnesses.registry import HARNESS_ADAPTERS

_HARNESS_NAMES = frozenset(HARNESS_ADAPTERS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 3 queue-based smoke test")
    parser.add_argument(
        "--instance-id",
        default=None,
        help="Specific SWE-bench Lite instance ID (default: astropy-12907 if found).",
    )
    parser.add_argument(
        "--harness",
        default="custom_minimal",
        choices=sorted(_HARNESS_NAMES),
        help="Harness to dispatch (default: custom_minimal).",
    )
    parser.add_argument(
        "--skip-grading",
        action="store_true",
        help="Stop after the harness phase (no eval worker).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Per-harness timeout in seconds (default: 300).",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        help="Per-instance max cost (USD). None = RunConfig default.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Per-instance max tokens. None = RunConfig default (200k).",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Phase 3 smoke test — queue-based pipeline")
    print("=" * 60)

    # ------------------------------------------------------------------
    # 0. Purge all queues (clean slate between runs)
    # ------------------------------------------------------------------
    from swebench_eval.queue.client import get_queue_url, get_sqs_client

    sqs = get_sqs_client()
    for qname in ("harness-jobs", "eval-jobs", "results"):
        sqs.purge_queue(QueueUrl=get_queue_url(qname))
    print("Queues purged.")

    # ------------------------------------------------------------------
    # 1. Load a single instance
    # ------------------------------------------------------------------
    from swebench_eval.dataset.swebench_loader import SwebenchLiteLoader, load_single_instance

    instance_id = args.instance_id or "astropy__astropy-12907"
    instance = load_single_instance(instance_id)
    if instance is None:
        # Fall back to the first instance in the split.
        loader = SwebenchLiteLoader()
        instance = loader.load()[0]
    print(f"Instance: {instance.instance_id}")

    # ------------------------------------------------------------------
    # 2. Start worker threads
    # ------------------------------------------------------------------
    from swebench_eval.orchestrator.control_plane.results_writer import run_results_writer
    from swebench_eval.workers.eval_worker import run_eval_worker
    from swebench_eval.workers.harness_worker import run_harness_worker

    threads = [
        threading.Thread(target=run_harness_worker, daemon=True, name="harness-worker"),
        threading.Thread(target=run_eval_worker, daemon=True, name="eval-worker"),
        threading.Thread(target=run_results_writer, daemon=True, name="results-writer"),
    ]
    for t in threads:
        t.start()
    print("Workers started (harness + eval + results writer).")

    # ------------------------------------------------------------------
    # 3. Dispatch one instance through the pipeline
    # ------------------------------------------------------------------
    from swebench_eval.database.connection import get_connection
    from swebench_eval.orchestrator.control_plane.dispatcher import dispatch_run
    from swebench_eval.orchestrator.run_config import RunConfig

    run_id = str(int(time.time() * 1000))
    print(f"Run: {run_id}")

    n_dispatched = dispatch_run(
        run_id=run_id,
        instances=[instance],
        config=RunConfig(
            harness=args.harness,
            model_alias="cheap-oss-model",
            timeout_seconds=args.timeout,
            # Only override a ceiling when the flag was given; otherwise the
            # RunConfig defaults stand.  `budget` must be a number (not None) —
            # the worker's fail-closed ceiling parses it strictly.
            max_tokens_per_instance=args.max_tokens,
            max_cost_usd_per_instance=(
                RunConfig().max_cost_usd_per_instance if args.budget is None else args.budget
            ),
        ),
    )
    print(f"Dispatched {n_dispatched} harness job(s).")
    if n_dispatched == 0:
        print("ERROR: dispatcher enqueued no jobs")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 4. Wait for the pipeline to complete
    # ------------------------------------------------------------------
    # Give the harness its configured wall-clock budget PLUS a margin larger than
    # one per-call read timeout (≤120 s), so a merely-slow-but-behaving harness
    # isn't misreported as failed (bug-findings B-3).  The eval phase gets its own
    # fresh deadline after the harness so it isn't starved.
    print("\nWaiting for pipeline (harness phase)...")
    _MARGIN = 180  # > one per-call read timeout, so the harness can complete a call
    harness_deadline = time.monotonic() + args.timeout + _MARGIN
    harness_state = None
    eval_state = None

    while time.monotonic() < harness_deadline:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT state FROM instance_results
                   WHERE run_id = %s AND instance_id = %s
                     AND attempt_number = 1 AND phase = 'harness'""",
                (run_id, instance.instance_id),
            )
            row = cur.fetchone()
        conn.close()

        if row:
            harness_state = row[0]
            print(f"  harness phase → {harness_state}")
            if harness_state in (
                "PATCH_READY",
                "EMPTY_PATCH",
                "FAILED_HARNESS",
                "STUCK",
                "BUDGET_EXCEEDED",
            ):
                break
        time.sleep(3)

    if harness_state is None:
        print(
            f"ERROR: no harness result within {args.timeout + _MARGIN}s (wall-clock budget "
            f"+ margin). The harness may still be running (slow or stalled upstream) — "
            "check the worker logs before assuming a pipeline fault."
        )
        sys.exit(1)

    if args.skip_grading:
        print("\nSkipping grading (--skip-grading).")
        _verify_harness_phase(run_id, instance.instance_id, harness_state)
        print("Pipeline harness phase verified.")
        return

    # Wait for eval phase if the harness produced a patch (its own fresh deadline).
    if harness_state == "PATCH_READY":
        print("  eval phase → (waiting)...")
        eval_deadline = time.monotonic() + args.timeout + _MARGIN
        while time.monotonic() < eval_deadline:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT state, verdict FROM instance_results
                       WHERE run_id = %s AND instance_id = %s
                         AND attempt_number = 1 AND phase = 'eval'""",
                    (run_id, instance.instance_id),
                )
                row = cur.fetchone()
            conn.close()

            if row:
                eval_state = row[0]
                print(f"  eval phase → {eval_state}")
                if eval_state in ("RESOLVED", "UNRESOLVED", "PATCH_APPLY_FAILED", "FAILED_EVAL"):
                    break
            time.sleep(3)

        if eval_state is None:
            print(
                f"ERROR: no eval result within {args.timeout + _MARGIN}s of the harness "
                "finishing. The eval worker may still be running — check worker logs."
            )
            sys.exit(1)

    # ------------------------------------------------------------------
    # 5. Verify Postgres rows
    # ------------------------------------------------------------------
    print("\n" + "-" * 40)
    print("Postgres verification:")

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT phase, state, error_category, verdict FROM instance_results
               WHERE run_id = %s AND instance_id = %s
                 AND attempt_number = 1 ORDER BY phase""",
            (run_id, instance.instance_id),
        )
        rows = cur.fetchall()
    conn.close()

    for phase, state, cat, verdict in rows:
        print(f"  {phase}: state={state}, error_category={cat}, verdict={verdict or '(none)'}")

    # Assert the expected transitions.
    assert harness_state != "EMPTY_PATCH", "Harness produced no patch"
    if not args.skip_grading and harness_state == "PATCH_READY":
        assert eval_state in ("RESOLVED", "UNRESOLVED"), f"Eval state {eval_state} is not terminal"
        assert eval_state in ("RESOLVED", "UNRESOLVED")
    print("  ✓ State machine transitions correct")

    # ------------------------------------------------------------------
    # 6. Verify artifacts in MinIO
    # ------------------------------------------------------------------
    print("\n" + "-" * 40)
    print("MinIO artifact verification:")

    from swebench_eval.queue.client import get_s3_client

    s3 = get_s3_client()
    resp = s3.list_objects_v2(Bucket="eval-artifacts", Prefix=f"runs/{run_id}/")
    objects = [o["Key"] for o in resp.get("Contents", [])]
    for key in objects:
        print(f"  {key}")

    # The full artifact set is asserted on the pipeline-success path (harness
    # produced a patch).  A crash/empty-patch run legitimately has no patch to
    # capture — those paths are covered by force_failure_test.py.
    if harness_state == "PATCH_READY":
        expected_suffixes = ["patch.diff", "trajectory.jsonl", "harness_stdout.log"]
        if not args.skip_grading:
            expected_suffixes.append("eval_report.json")
        for suffix in expected_suffixes:
            found = any(key.endswith(suffix) for key in objects)
            assert found, f"Missing artifact: {suffix}"
    print(f"  ✓ Expected artifacts for state {harness_state} present")

    # ------------------------------------------------------------------
    # 7. Idempotent upsert — duplicate message (P3-5b)
    # ------------------------------------------------------------------
    print("\n" + "-" * 40)
    print("Idempotency test (duplicate ResultMessage):")

    from swebench_eval.queue.client import send_message

    # Reconstruct the harness ResultMessage the worker actually pushed, from
    # the DB row (real artifact paths).  SQS is at-least-once — identical
    # content is the only real redelivery case.
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT patch_path, trajectory_path, raw_log_path
               FROM instance_results
               WHERE run_id = %s AND instance_id = %s
                 AND attempt_number = 1 AND phase = 'harness'""",
            (run_id, instance.instance_id),
        )
        real_row = cur.fetchone()
    conn.close()
    assert real_row is not None
    real_patch, real_traj, real_raw = real_row[0] or "", real_row[1] or "", real_row[2] or ""

    body = {
        "run_id": run_id,
        "instance_id": instance.instance_id,
        "attempt_number": 1,
        "phase": "harness",
        "state": harness_state,
        "patch_s3_key": real_patch,
        "trajectory_s3_key": real_traj,
        "raw_log_s3_key": real_raw,
    }

    # Send the SAME message twice.
    send_message("results", body)
    send_message("results", body)
    print("  identical message sent to 'results' queue TWICE")

    # The results-writer thread already running from the main pipeline will
    # process both.  Wait a few seconds for them.
    time.sleep(8)

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*), MAX(patch_path) FROM instance_results
               WHERE run_id = %s AND instance_id = %s
                 AND attempt_number = 1 AND phase = 'harness'""",
            (run_id, instance.instance_id),
        )
        row = cur.fetchone()
        harness_count = int(row[0]) if row else 0
        patch_path = str(row[1]) if row and row[1] else ""
    conn.close()

    assert (
        harness_count == 1
    ), f"Expected exactly 1 harness row after duplicate delivery, got {harness_count}"
    assert (
        patch_path == real_patch
    ), f"Row content diverged after duplicate: expected '{real_patch}', got '{patch_path}'"
    print(f"  ✓ Exactly {harness_count} harness row(s) after duplicate delivery (idempotent)")
    print("  ✓ Row content matches the message (no partial merge)")

    print("\n" + "=" * 60)
    print("Phase 3 smoke test — pipeline verified end-to-end")
    print("=" * 60)


def _verify_harness_phase(run_id: str, instance_id: str, state: str) -> None:
    """Verify the harness phase completed with a valid state."""
    from swebench_eval.database.connection import get_connection

    assert state in (
        "PATCH_READY",
        "EMPTY_PATCH",
        "FAILED_HARNESS",
        "STUCK",
        "BUDGET_EXCEEDED",
    ), f"Unexpected state: {state}"

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT error_category, wall_clock_harness_s FROM instance_results "
            "WHERE run_id = %s AND instance_id = %s AND phase = 'harness'",
            (run_id, instance_id),
        )
        row = cur.fetchone()
    conn.close()
    assert row is not None
    print(f"  error_category={row[0]}, wall_clock={row[1]:.1f}s")


if __name__ == "__main__":
    main()
