"""Integration tests — the eval-handoff blocker (BUILDER4-EVAL-HANDOFF-BLOCKER-2026-08-28.md).

Requires a live Postgres + SQS (the compose stack). Marked ``integration`` and
deselected by default. Run explicitly with:

    uv run pytest tests/test_eval_handoff_blocker.py -m integration

The bug: run_launch._seed_instance_rows pre-inserts a `phase='harness',
state='PENDING'` row at launch, so the real PATCH_READY write always takes
the ON CONFLICT DO UPDATE branch (xmax != 0) — a genuine, successful state
advance, but the old `inserted` check read that as False, so the
eval-enqueue gate at results_writer.py never fired for any run-launch-
originated run. Fixed by gating on `advanced = row is not None` instead —
this file proves the fix against a real seeded row, not a mock, and is
written to fail loudly if the fix is reverted (see
test_seeded_patch_ready_enqueues_eval_job's mutation-check note).
"""

from __future__ import annotations

import time

import pytest

from swebench_eval.orchestrator.control_plane.dispatcher import register_run
from swebench_eval.orchestrator.control_plane.results_writer import _process_result
from swebench_eval.orchestrator.control_plane.run_launch import _seed_instance_rows
from swebench_eval.queue.schemas import ResultMessage

pytestmark = pytest.mark.integration


@pytest.fixture()
def clean_run() -> str:
    """A fresh, isolated run_id — registered, not seeded (each test seeds its
    own rows so the scenario is explicit)."""
    from swebench_eval.database.connection import get_connection

    run_id = f"eval-handoff-{int(time.time() * 1000)}"
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM instance_results WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM run_targets WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))
    conn.commit()
    conn.close()
    register_run(run_id, "custom_minimal", "cheap-oss-model")
    return run_id


def _seed_pending(run_id: str, instance_id: str) -> None:
    """Reuse the real seeding path (run_launch._seed_instance_rows), not a
    hand-rolled INSERT — the bug is specifically about what THIS function
    leaves behind."""
    from swebench_eval.dataset.base import Instance

    inst = Instance(
        instance_id=instance_id,
        repo="x/y",
        base_commit="abc",
        problem_statement="p",
        patch="",
        test_patch="",
        fail_to_pass="[]",
        pass_to_pass="[]",
    )
    _seed_instance_rows(run_id, [inst], attempts_per_instance=1)


def _drain_eval_jobs(max_messages: int = 20) -> list[dict[str, object]]:
    """Drain + delete every message currently on eval-jobs, return their
    bodies. Test-only: keeps each test's queue state isolated from the next."""
    from swebench_eval.queue.client import delete_message, receive_message

    bodies: list[dict[str, object]] = []
    for _ in range(max_messages):
        msg = receive_message("eval-jobs", wait_seconds=1)
        if msg is None:
            break
        bodies.append(msg["body"])
        delete_message("eval-jobs", msg["receipt_handle"])
    return bodies


def _eval_row_state(run_id: str, instance_id: str) -> str | None:
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM instance_results WHERE run_id=%s AND instance_id=%s AND phase='eval'",
            (run_id, instance_id),
        )
        row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def test_seeded_patch_ready_enqueues_eval_job(clean_run: str) -> None:
    """The core blocker: a PATCH_READY over a SEEDED (pre-run-launch-style)
    PENDING row must still enqueue an eval job.

    Mutation-check performed manually 2026-08-28: reverting the gate in
    results_writer.py back to `if inserted and ...` (inserted = bool(row and
    row[0])) makes this test fail — 0 messages on eval-jobs, no eval row —
    confirming it actually exercises the bug. Restored immediately after.
    """
    _drain_eval_jobs()  # clear any cross-test residue before asserting
    _seed_pending(clean_run, "inst-1")

    _process_result(
        ResultMessage(
            run_id=clean_run,
            instance_id="inst-1",
            attempt_number=1,
            phase="harness",
            state="PATCH_READY",
            patch_s3_key="runs/x/patch.diff",
        )
    )

    bodies = _drain_eval_jobs()
    assert len(bodies) == 1, f"expected exactly one eval job, got {bodies}"
    assert bodies[0]["run_id"] == clean_run
    assert bodies[0]["instance_id"] == "inst-1"
    assert _eval_row_state(clean_run, "inst-1") == "PENDING"


def test_redelivered_patch_ready_does_not_double_enqueue(clean_run: str) -> None:
    """Idempotency survives the fix: a redelivered PATCH_READY over an
    already-PATCH_READY row is equal-rank, blocked by the guard, and must
    not spawn a second eval job."""
    _drain_eval_jobs()
    _seed_pending(clean_run, "inst-2")

    msg = ResultMessage(
        run_id=clean_run,
        instance_id="inst-2",
        attempt_number=1,
        phase="harness",
        state="PATCH_READY",
        patch_s3_key="runs/x/patch.diff",
    )
    _process_result(msg)
    _process_result(msg)  # simulated SQS redelivery of the identical message

    bodies = _drain_eval_jobs()
    assert len(bodies) == 1, f"expected exactly one eval job despite redelivery, got {bodies}"


def test_empty_patch_does_not_enqueue(clean_run: str) -> None:
    """A no-eval terminal state (EMPTY_PATCH) over a seeded row must not
    enqueue anything, regardless of the `advanced`/`inserted` distinction."""
    _drain_eval_jobs()
    _seed_pending(clean_run, "inst-3")

    _process_result(
        ResultMessage(
            run_id=clean_run,
            instance_id="inst-3",
            attempt_number=1,
            phase="harness",
            state="EMPTY_PATCH",
        )
    )

    bodies = _drain_eval_jobs()
    assert bodies == [], f"EMPTY_PATCH must never enqueue an eval job, got {bodies}"
    assert _eval_row_state(clean_run, "inst-3") is None
