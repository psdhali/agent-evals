"""Integration tests — preserve evidence on a blocked write
(BUILDER4-ROUND2-LEDGER-FIXES-2026-08-28.md item 4).

Requires a live Postgres (the compose stack). Marked ``integration`` and
deselected by default. Run explicitly with:

    uv run pytest tests/test_blocked_write_evidence.py -m integration

The guarded upsert in results_writer._process_result skips the ENTIRE row
when state_rank() doesn't advance — so a real result losing the state-rank
race to e.g. an abort record also loses the pointers to artifacts already
sitting in S3 (patch_path, trajectory_path, ...), plus every other
COALESCE'd field. _fill_blocked_write_evidence fills only what's still NULL
on the row that won, without ever overwriting what it already set.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from swebench_eval.orchestrator.control_plane.dispatcher import register_run
from swebench_eval.orchestrator.control_plane.results_writer import _process_result
from swebench_eval.queue.schemas import ResultMessage

pytestmark = pytest.mark.integration


@pytest.fixture()
def clean_run() -> str:
    from swebench_eval.database.connection import get_connection

    run_id = f"blocked-evidence-{int(time.time() * 1000)}"
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM instance_results WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM run_targets WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))
    conn.commit()
    conn.close()
    register_run(run_id, "custom_minimal", "cheap-oss-model")
    return run_id


def _row(run_id: str, instance_id: str) -> dict[str, object] | None:
    from psycopg2.extras import RealDictCursor

    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM instance_results WHERE run_id=%s AND instance_id=%s AND phase='harness'",
            (run_id, instance_id),
        )
        row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def test_blocked_write_fills_null_evidence_columns(clean_run: str) -> None:
    """The brief's exact scenario: row at ABORTED_IN_FLIGHT with NULL
    patch_path; a blocked PATCH_READY carrying a patch_path arrives; state
    is unchanged, patch_path is now populated.

    Mutation-check performed manually 2026-08-28: commenting out the
    `_fill_blocked_write_evidence` call in results_writer.py makes this
    test fail (patch_path stays None) — confirmed, restored.
    """
    _process_result(
        ResultMessage(
            run_id=clean_run,
            instance_id="inst-1",
            attempt_number=1,
            phase="harness",
            state="ABORTED_IN_FLIGHT",
        )
    )
    row = _row(clean_run, "inst-1")
    assert row is not None
    assert row["patch_path"] is None

    # A late straggler — same (run, instance, attempt, phase), carries real
    # evidence, but is state-rank-blocked (ABORTED_IN_FLIGHT already stands).
    _process_result(
        ResultMessage(
            run_id=clean_run,
            instance_id="inst-1",
            attempt_number=1,
            phase="harness",
            state="PATCH_READY",
            patch_s3_key="runs/x/inst-1/patch.diff",
            trajectory_s3_key="runs/x/inst-1/trajectory.jsonl",
        )
    )

    row = _row(clean_run, "inst-1")
    assert row is not None
    assert row["state"] == "ABORTED_IN_FLIGHT", "the blocked write must not change state"
    assert row["patch_path"] == "runs/x/inst-1/patch.diff"
    assert row["trajectory_path"] == "runs/x/inst-1/trajectory.jsonl"


def test_blocked_write_never_overwrites_an_existing_value(clean_run: str) -> None:
    """A column the winning row already populated must survive a blocked
    write that disagrees with it — fills gaps only, never overwrites."""
    _process_result(
        ResultMessage(
            run_id=clean_run,
            instance_id="inst-2",
            attempt_number=1,
            phase="harness",
            state="ABORTED_IN_FLIGHT",
            patch_s3_key="runs/x/inst-2/original-patch.diff",
        )
    )
    row = _row(clean_run, "inst-2")
    assert row is not None
    assert row["patch_path"] == "runs/x/inst-2/original-patch.diff"

    # Blocked write disagrees — must not win.
    _process_result(
        ResultMessage(
            run_id=clean_run,
            instance_id="inst-2",
            attempt_number=1,
            phase="harness",
            state="PATCH_READY",
            patch_s3_key="runs/x/inst-2/different-patch.diff",
        )
    )
    row = _row(clean_run, "inst-2")
    assert row is not None
    assert row["state"] == "ABORTED_IN_FLIGHT"
    assert (
        row["patch_path"] == "runs/x/inst-2/original-patch.diff"
    ), "an already-set value must never be overwritten by a blocked write"


def test_blocked_write_fills_extra_columns_too(clean_run: str) -> None:
    """Not just the brief's five named columns — the same full evidence set
    the main upsert already COALESCEs (error/verdict/timing/token-cost/
    contamination/turns_used), name-driven off _EVIDENCE_COLUMNS."""
    _process_result(
        ResultMessage(
            run_id=clean_run,
            instance_id="inst-3",
            attempt_number=1,
            phase="harness",
            state="ABORTED_IN_FLIGHT",
        )
    )
    _process_result(
        ResultMessage(
            run_id=clean_run,
            instance_id="inst-3",
            attempt_number=1,
            phase="harness",
            state="PATCH_READY",
            input_tokens=1234,
            output_tokens=567,
            cost_usd=0.42,
        )
    )
    row = _row(clean_run, "inst-3")
    assert row is not None
    assert row["state"] == "ABORTED_IN_FLIGHT"
    assert row["input_tokens"] == 1234
    assert row["output_tokens"] == 567
    cost_usd = row["cost_usd"]
    assert isinstance(cost_usd, (int, float, Decimal))
    assert float(cost_usd) == 0.42


def test_normal_advancing_write_is_unaffected(clean_run: str) -> None:
    """The common, non-blocked path (state genuinely advances) must not be
    touched by this — patch_path lands via the normal guarded upsert, the
    follow-up fill never runs (nothing left to fill)."""
    _process_result(
        ResultMessage(
            run_id=clean_run,
            instance_id="inst-4",
            attempt_number=1,
            phase="harness",
            state="PATCH_READY",
            patch_s3_key="runs/x/inst-4/patch.diff",
        )
    )
    row = _row(clean_run, "inst-4")
    assert row is not None
    assert row["state"] == "PATCH_READY"
    assert row["patch_path"] == "runs/x/inst-4/patch.diff"
