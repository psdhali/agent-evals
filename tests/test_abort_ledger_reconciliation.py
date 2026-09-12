"""Integration tests — abort/ledger reconciliation (2026-08-28 design round).

Requires a live Postgres + Redis + SQS (the compose stack), like
``test_results_writer_redelivery.py``. Marked ``integration`` and deselected
by default. Run explicitly with:

    uv run pytest tests/test_abort_ledger_reconciliation.py -m integration

Covers the "abort stands" decision and the abort/ledger gaps found while
reconciling ADR-0034's abort design against run-launch's pre-seeded ledger
(dev/builder4-state-machine-and-edge-cases-2026-08-28.md §9.g/h/i, §12):

- state_rank() ranks ABORTED_IN_FLIGHT/NEVER_DISPATCHED explicitly at
  terminal, so abort stands against a late straggler result.
- _wait_for_settle counts the ledger's real non-terminal set (not the old,
  inverted HARNESS_RUNNING+ABORTED_IN_FLIGHT check) and returns promptly
  when nothing is genuinely in flight.
- The leftover-row sweep gives every still-non-terminal row a definitive
  outcome before finalise, and never sweeps a row that's still reporting
  live progress.
- The drain path emits NEVER_DISPATCHED for a message it drains, instead of
  a bare delete with no ledger write.
- _stop_in_flight survives a ListTasks failure instead of raising.
"""

from __future__ import annotations

import time
from typing import Any
from unittest import mock

import pytest

from swebench_eval.orchestrator.control_plane import abort as abort_mod
from swebench_eval.orchestrator.control_plane.dispatcher import register_run
from swebench_eval.orchestrator.control_plane.results_writer import _process_result
from swebench_eval.queue.schemas import ResultMessage

pytestmark = pytest.mark.integration


class _StubECS:
    """No real AWS in this environment — a minimal stand-in, matching
    execute_abort's own ``ecs: Any | None`` injection point."""

    def __init__(self, list_tasks_raises: bool = False) -> None:
        self._raises = list_tasks_raises
        self.stopped: list[str] = []

    def list_tasks(self, **kwargs: Any) -> dict[str, Any]:
        if self._raises:
            raise RuntimeError("simulated transient ListTasks failure")
        return {"taskArns": []}

    def stop_task(self, **kwargs: Any) -> None:
        self.stopped.append(str(kwargs.get("task")))


@pytest.fixture()
def clean_run() -> str:
    """Register a fresh, isolated run and return its run_id."""
    from swebench_eval.database.connection import get_connection

    run_id = f"abort-ledger-{int(time.time() * 1000)}"
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM instance_results WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM run_targets WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))
    conn.commit()
    conn.close()
    register_run(run_id, "custom_minimal", "cheap-oss-model")
    return run_id


def _seed_row(run_id: str, instance_id: str, attempt_number: int, phase: str, state: str) -> None:
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO instance_results
               (run_id, instance_id, attempt_number, phase, state, seeded_at)
               VALUES (%s, %s, %s, %s, %s, now())
               ON CONFLICT (run_id, instance_id, attempt_number, phase)
               DO UPDATE SET state = EXCLUDED.state""",
            (run_id, instance_id, attempt_number, phase, state),
        )
    conn.commit()
    conn.close()


def _row_state(run_id: str, instance_id: str, phase: str) -> str | None:
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM instance_results WHERE run_id=%s AND instance_id=%s AND phase=%s",
            (run_id, instance_id, phase),
        )
        row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _drain_and_process_results(max_messages: int = 20) -> int:
    """Simulate the results-writer consumer: drain `results`, call
    _process_result on each. Test-only stand-in for the real poll loop."""
    from swebench_eval.orchestrator.control_plane.results_writer import _parse_result
    from swebench_eval.queue.client import delete_message, receive_message

    processed = 0
    for _ in range(max_messages):
        msg = receive_message("results", wait_seconds=1)
        if msg is None:
            break
        _process_result(_parse_result(msg["body"]))
        delete_message("results", msg["receipt_handle"])
        processed += 1
    return processed


def test_abort_stands_against_a_late_straggler(clean_run: str) -> None:
    """§9.i / state_rank(): a genuine result after ABORTED_IN_FLIGHT does not win."""
    run_id = clean_run
    _process_result(
        ResultMessage(
            run_id=run_id,
            instance_id="i1",
            attempt_number=1,
            phase="harness",
            state="ABORTED_IN_FLIGHT",
        )
    )
    # The straggler — arrives after, would-be genuine progress.
    _process_result(
        ResultMessage(
            run_id=run_id,
            instance_id="i1",
            attempt_number=1,
            phase="harness",
            state="PATCH_READY",
            patch_s3_key="runs/x/patch.diff",
        )
    )
    assert _row_state(run_id, "i1", "harness") == "ABORTED_IN_FLIGHT"


def test_wait_for_settle_returns_promptly_when_nothing_in_flight(clean_run: str) -> None:
    """No non-terminal rows for this run -> settle returns near-instantly,
    not after burning the bound."""
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    t0 = time.monotonic()
    abort_mod._wait_for_settle(conn, clean_run, timeout=10)
    elapsed = time.monotonic() - t0
    conn.close()
    assert elapsed < 2, f"settle should return immediately, took {elapsed:.1f}s"


def test_wait_for_settle_waits_while_genuinely_non_terminal(clean_run: str) -> None:
    """A row still HARNESS_RUNNING keeps the wait outstanding until the bound.

    Regression guard for the inverted-logic bug this replaces: the OLD query
    counted ABORTED_IN_FLIGHT (a terminal outcome) as still-outstanding, so
    it could never converge; masked by HARNESS_RUNNING never being emitted
    at all. This proves the NEW query actually distinguishes "still running"
    from "done."
    """
    from swebench_eval.database.connection import get_connection

    _seed_row(clean_run, "i1", 1, "harness", "HARNESS_RUNNING")
    conn = get_connection()
    t0 = time.monotonic()
    abort_mod._wait_for_settle(conn, clean_run, timeout=2)
    elapsed = time.monotonic() - t0
    conn.close()
    assert elapsed >= 1.5, f"should have waited out the bound, returned after {elapsed:.1f}s"


def test_sweep_gives_every_leftover_row_a_definitive_outcome(clean_run: str) -> None:
    """§9.g/h: PENDING -> NEVER_DISPATCHED, DISPATCHED -> ABORTED_IN_FLIGHT,
    EVAL_RUNNING -> ABANDONED (self-correcting, not sticky — eval is never
    actually stopped by abort)."""
    from swebench_eval.database.connection import get_connection

    _seed_row(clean_run, "never-dispatched-inst", 1, "harness", "PENDING")
    _seed_row(clean_run, "was-running-inst", 1, "harness", "DISPATCHED")
    _seed_row(clean_run, "eval-running-inst", 1, "eval", "EVAL_RUNNING")

    conn = get_connection()
    swept = abort_mod._sweep_remaining_rows(conn, clean_run)
    conn.close()
    assert swept == 3

    _drain_and_process_results()

    assert _row_state(clean_run, "never-dispatched-inst", "harness") == "NEVER_DISPATCHED"
    assert _row_state(clean_run, "was-running-inst", "harness") == "ABORTED_IN_FLIGHT"
    assert _row_state(clean_run, "eval-running-inst", "eval") == "ABANDONED"


def test_sweep_does_not_touch_a_row_still_reporting_progress(clean_run: str) -> None:
    """A row with a live progress key is left alone, not swept out from under it."""
    from swebench_eval.database.connection import get_connection
    from swebench_eval.database.redis_client import write_progress
    from swebench_eval.harnesses.base import Usage

    _seed_row(clean_run, "still-alive-inst", 1, "harness", "HARNESS_RUNNING")
    write_progress(
        clean_run,
        "still-alive-inst",
        1,
        turn_number=3,
        usage=Usage(input_tokens=60, output_tokens=40, cost_usd=0.01),
    )

    conn = get_connection()
    swept = abort_mod._sweep_remaining_rows(conn, clean_run)
    conn.close()

    assert swept == 0
    assert _row_state(clean_run, "still-alive-inst", "harness") == "HARNESS_RUNNING"


def test_drain_emits_never_dispatched_instead_of_bare_delete(clean_run: str) -> None:
    """§9.f: a message drained for this run gets a ledger write, not just a
    queue delete. `_exactly_one_active` is patched True — its own refusal
    gate is already covered by test_abort.py's unit test; this isolates the
    drain-path write fix specifically."""
    from swebench_eval.database.connection import get_connection
    from swebench_eval.queue.client import send_message

    _seed_row(clean_run, "queued-inst", 1, "harness", "PENDING")
    send_message(
        "harness-jobs",
        {
            "run_id": clean_run,
            "instance_id": "queued-inst",
            "attempt_number": 1,
            "harness_name": "custom_minimal",
            "model_alias": "cheap-oss-model",
            "repo_url": "https://github.com/x/y",
            "base_commit": "abc",
            "problem_statement": "p",
        },
    )

    conn = get_connection()
    with mock.patch.object(abort_mod, "_exactly_one_active", return_value=True):
        report = abort_mod._drain_queued(
            conn,
            clean_run,
            "all",
            abort_mod.AbortReport(
                run_id=clean_run,
                scope="all",
                reason="test",
                actor="test",
                requested_at=time.time(),
            ),
        )
    conn.close()

    assert report.drained == 1
    assert not report.drain_skipped

    _drain_and_process_results()
    assert _row_state(clean_run, "queued-inst", "harness") == "NEVER_DISPATCHED"


def test_stop_in_flight_survives_list_tasks_failure(clean_run: str) -> None:
    """§4 hardening (Builder 3's own flagged item): a transient ListTasks
    error must not raise out of the abort — it degrades to "stopped 0" and
    lets the sweep catch anything still genuinely in flight."""
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    ecs = _StubECS(list_tasks_raises=True)
    report = abort_mod.AbortReport(
        run_id=clean_run,
        scope="all",
        reason="test",
        actor="test",
        requested_at=time.time(),
    )
    result = abort_mod._stop_in_flight(conn, ecs, report)  # must not raise
    conn.close()
    assert result.in_flight_stopped == 0
