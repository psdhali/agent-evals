"""Integration test — F5/F6: a redelivered result must not null diagnostic fields.

Requires a live Postgres (the compose stack).  Marked ``integration`` and
deselected by default (``addopts = "-m 'not integration'"`` in pyproject.toml)
so CI stays green without a database.  Run it explicitly with:

    uv run pytest tests/test_results_writer_redelivery.py -m integration

Resides here (not in scripts/) because it guards the results_writer upsert —
the same regression-test-in-tests/ standard applied to P2-1's cost tracking.
"""

from __future__ import annotations

import pytest

from swebench_eval.orchestrator.control_plane.dispatcher import register_run
from swebench_eval.orchestrator.control_plane.results_writer import _process_result
from swebench_eval.queue.schemas import ResultMessage

pytestmark = pytest.mark.integration


@pytest.fixture()
def clean_run() -> str:
    """Register a fresh run and return its run_id."""
    from swebench_eval.database.connection import get_connection

    run_id = "f6-integration"
    # Ensure a clean slate for this deterministic run_id.
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM instance_results WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM run_targets WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))
    conn.commit()
    conn.close()
    # R2-2: every result-writing path registers its run first.
    register_run(run_id, "custom_minimal", "cheap-oss-model")
    return run_id


def test_redelivery_preserves_diagnostic_fields(clean_run: str) -> None:
    """F6: a fields-absent redelivery must not null error_category/error_detail.

    To re-prove this guard in its new home: revert F5's COALESCE in
    results_writer.py back to bare ``EXCLUDED``, run this test, confirm it
    fails, then restore.
    """
    from swebench_eval.database.connection import get_connection

    # First delivery: FAILED_HARNESS with full diagnosis.
    _process_result(
        ResultMessage(
            run_id=clean_run,
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
            run_id=clean_run,
            instance_id="inst-f5",
            attempt_number=1,
            phase="harness",
            state="FAILED_HARNESS",  # no error_category / error_detail
        )
    )

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT error_category, error_detail, patch_path "
                "FROM instance_results WHERE run_id = %s",
                (clean_run,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == "HARNESS_TIMEOUT", f"F5: error_category nulled → {row[0]}"
    assert row[1] == "wall-clock timeout (600s)", f"F5: error_detail nulled → {row[1]}"
    assert row[2] == "runs/x/patch.diff", f"F5: patch_path lost → {row[2]}"


def test_unknown_run_id_rejected_by_fk(clean_run: str) -> None:
    """R2-2: the FK rejects a result whose run was never registered."""
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO instance_results "
                "(run_id, instance_id, attempt_number, phase, state) "
                "VALUES ('no-such-run', 'i', 1, 'harness', 'X')"
            )
            # If this succeeds we should have committed and failed the test;
            # rollback first so the transaction doesn't linger.
            conn.rollback()
            raise AssertionError("insert with unknown run_id was accepted")
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        # ForeignKeyViolation is the expected rejection.
        assert "foreign key" in str(exc).lower() or exc.__class__.__name__ in (
            "ForeignKeyViolation",
            "OperationalError",
        )
    finally:
        conn.close()
