"""Manual close + reviewed restart against a real Postgres —
BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md.

Real transactions and real ``SELECT ... FOR UPDATE`` row-locking are the
whole point of M1's fix; a mocked connection cannot exercise that, so this
requires the compose stack (``-m integration``), same rationale as
``test_queries_integration.py``.

``restart.finalise_run`` and ``restart._dispatch_restarted`` are monkeypatched
in every test here — the real versions touch AWS ECS / the LiteLLM gateway /
SQS-backed dataset lookups, none of which are up tonight (AWS is
deliberately down) and none of which are what these tests are actually
verifying (the DB-level locking/classification/denominator logic is).
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from swebench_eval.orchestrator.control_plane import restart

pytestmark = pytest.mark.integration

_PREFIX = "rc-test-"


def _db():
    from swebench_eval.database.connection import get_connection

    return get_connection()


@pytest.fixture(autouse=True)
def _clean_rows():
    def _clean():
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM instance_results WHERE run_id LIKE %s", (f"{_PREFIX}%",))
                cur.execute("DELETE FROM runs WHERE run_id LIKE %s", (f"{_PREFIX}%",))
            conn.commit()
        finally:
            conn.close()

    _clean()
    yield
    _clean()


@pytest.fixture(autouse=True)
def _stub_finalise_and_dispatch(monkeypatch: pytest.MonkeyPatch):
    """Real finalise_run/_dispatch_restarted touch AWS — replace with
    recorders so these tests exercise DB logic only, never the network."""
    calls: dict[str, list[Any]] = {"finalise": [], "dispatch": [], "regrade_dispatch": []}

    def _fake_finalise(run_id: str) -> None:
        calls["finalise"].append(run_id)
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE runs SET active_key = NULL, status = 'completed', "
                    "finalised_at = now() WHERE run_id = %s",
                    (run_id,),
                )
            conn.commit()
        finally:
            conn.close()

    def _fake_dispatch(run_id: str, restarted: list[Any]) -> None:
        calls["dispatch"].append((run_id, restarted))

    def _fake_regrade_dispatch(run_id: str, regraded: list[Any]) -> None:
        calls["regrade_dispatch"].append((run_id, regraded))

    monkeypatch.setattr(restart, "finalise_run", _fake_finalise)
    monkeypatch.setattr(restart, "_dispatch_restarted", _fake_dispatch)
    monkeypatch.setattr(restart, "_dispatch_regraded", _fake_regrade_dispatch)
    return calls


def _insert_run(conn, run_id: str, status: str = "running") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (run_id, config_snapshot, status) "
            'VALUES (%s, \'{"harness": "custom_minimal", "model_alias": "cheap-oss-model"}\'::jsonb, %s)',
            (run_id, status),
        )
    conn.commit()


def _insert_instance(
    conn,
    run_id: str,
    instance_id: str,
    attempt_number: int,
    state: str,
    error_category: str | None = None,
    phase: str = "harness",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO instance_results
                   (run_id, instance_id, attempt_number, phase, state, error_category)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (run_id, instance_id, attempt_number, phase, state, error_category),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# M1 — close/restart race
# ---------------------------------------------------------------------------


def test_close_refuses_when_instances_are_still_in_flight() -> None:
    run_id = f"{_PREFIX}inflight"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        _insert_instance(conn, run_id, "inst-1", 1, "HARNESS_RUNNING")
    finally:
        conn.close()

    with pytest.raises(restart.CloseConflictError, match="still in flight"):
        restart.close_run(run_id)

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM runs WHERE run_id = %s", (run_id,))
            (status,) = cur.fetchone()
    finally:
        conn.close()
    assert status == "running"  # never flipped — nothing to roll back


def test_close_succeeds_when_zero_non_terminal_rows(_stub_finalise_and_dispatch) -> None:
    run_id = f"{_PREFIX}clean"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        _insert_instance(conn, run_id, "inst-1", 1, "RESOLVED", phase="eval")
    finally:
        conn.close()

    report = restart.close_run(run_id)
    assert report == {"run_id": run_id, "status": "completed"}
    assert _stub_finalise_and_dispatch["finalise"] == [run_id]


def test_close_can_be_retried_after_finalise_run_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 (implementation review, 2026-08-31): a `finalise_run` failure after
    the status flip has already committed must not strand the run — the old
    guard (`status == 'running'`) refused every retry forever, with both
    keys still live and no other path back except /abort mislabelling a
    finished run as aborted.

    Reproduces the reviewer's exact scenario: first close call flips to
    'finalising' then finalise_run raises; confirm status is stuck at
    'finalising'; retry close_run and confirm it now succeeds (with the
    fix) rather than refusing with CloseConflictError (the bug).
    """
    run_id = f"{_PREFIX}retry-close"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        _insert_instance(conn, run_id, "inst-1", 1, "RESOLVED", "RESOLVED", phase="eval")
    finally:
        conn.close()

    calls = {"n": 0}

    def _flaky_finalise(run_id: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient ECS StopTask failure")
        # second call succeeds — mirrors the real finalise_run's own effect.
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE runs SET active_key = NULL, status = 'completed', "
                    "finalised_at = now() WHERE run_id = %s",
                    (run_id,),
                )
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(restart, "finalise_run", _flaky_finalise)

    with pytest.raises(RuntimeError, match="transient ECS"):
        restart.close_run(run_id)

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM runs WHERE run_id = %s", (run_id,))
            (stuck_status,) = cur.fetchone()
    finally:
        conn.close()
    assert stuck_status == "finalising"  # the failure mode the review proved

    # The fix: retrying /close must succeed, not refuse with CloseConflictError.
    report = restart.close_run(run_id)
    assert report == {"run_id": run_id, "status": "completed"}
    assert calls["n"] == 2


def test_close_refuses_a_run_that_is_not_running() -> None:
    run_id = f"{_PREFIX}already-done"
    conn = _db()
    try:
        _insert_run(conn, run_id, status="completed")
    finally:
        conn.close()

    with pytest.raises(restart.CloseConflictError, match="not open for close"):
        restart.close_run(run_id)


def test_restart_refused_once_run_is_closed() -> None:
    run_id = f"{_PREFIX}closed"
    conn = _db()
    try:
        _insert_run(conn, run_id, status="completed")
        _insert_instance(conn, run_id, "inst-1", 1, "FAILED_HARNESS", "MODEL_API_ERROR")
    finally:
        conn.close()

    with pytest.raises(restart.RestartError, match="closed"):
        restart.restart_instances(run_id, ["inst-1"])


def test_restart_then_close_race_never_leaves_a_pending_row_in_a_completed_run(
    _stub_finalise_and_dispatch,
) -> None:
    """The actual M1 race, run for real with two threads and real row-locking.

    Mutation check (done by hand, restored): drop the ``FOR UPDATE`` from
    both ``close_run`` and ``restart_instances`` — this test then goes flaky-
    to-red (a PENDING row lands in a run that ``close_run`` already marked
    'completed'), matching the race the review described. With the locks in
    place, exactly one of the two operations can be the "last word": either
    restart lands first and close's re-check catches it (409, no revoke), or
    close's lock is acquired first and restart correctly refuses against the
    now-'finalising'/'completed' status.
    """
    run_id = f"{_PREFIX}race"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        _insert_instance(conn, run_id, "inst-1", 1, "FAILED_HARNESS", "MODEL_API_ERROR")
    finally:
        conn.close()

    results: dict[str, object] = {}
    # A barrier, not a sleep bias: both threads reach their FOR UPDATE
    # acquisition attempt at nearly the same instant, maximising the chance
    # of the exact interleaving M1 protects against — a sleep-biased ordering
    # would let one operation simply finish before the other starts, which
    # proves nothing about the lock.
    start = threading.Barrier(2)

    def _do_restart() -> None:
        start.wait(timeout=5)
        try:
            results["restart"] = restart.restart_instances(run_id, ["inst-1"])
        except restart.RestartError as exc:
            results["restart"] = exc

    def _do_close() -> None:
        start.wait(timeout=5)
        try:
            results["close"] = restart.close_run(run_id)
        except restart.CloseConflictError as exc:
            results["close"] = exc

    t1 = threading.Thread(target=_do_restart)
    t2 = threading.Thread(target=_do_close)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM runs WHERE run_id = %s", (run_id,))
            (final_status,) = cur.fetchone()
            cur.execute(
                "SELECT count(*) FROM instance_results "
                "WHERE run_id = %s AND state IN ('PENDING','DISPATCHED','HARNESS_RUNNING','EVAL_RUNNING')",
                (run_id,),
            )
            (non_terminal,) = cur.fetchone()
    finally:
        conn.close()

    # The invariant M1 exists to protect: a 'completed' run NEVER coexists
    # with a non-terminal (just-restarted) row.
    if final_status == "completed":
        assert non_terminal == 0, "close revoked keys while a restarted row was still pending"
    else:
        # restart won the race; close correctly refused (409) rather than
        # revoking keys out from under the just-enqueued job.
        assert isinstance(results.get("close"), restart.CloseConflictError)


# ---------------------------------------------------------------------------
# M3/M4 — classification, disambiguation from configured pass@k
# ---------------------------------------------------------------------------


def test_restart_tags_infra_failure_as_infra_retry() -> None:
    run_id = f"{_PREFIX}infra"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        _insert_instance(conn, run_id, "inst-1", 1, "FAILED_HARNESS", "MODEL_API_ERROR")
    finally:
        conn.close()

    report = restart.restart_instances(run_id, ["inst-1"])
    assert report["restarted"] == [
        {"instance_id": "inst-1", "attempt_number": 2, "retry_reason": "operator_infra_retry"}
    ]
    assert report["skipped"] == []


def test_restart_tags_a_rerun_of_a_resolved_instance_as_pass_at_k() -> None:
    run_id = f"{_PREFIX}rerun"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        # Real data: map_eval_outcome_to_error_category(resolved=True) sets
        # error_category = "RESOLVED", matching state — not NULL.
        _insert_instance(conn, run_id, "inst-1", 1, "RESOLVED", "RESOLVED", phase="eval")
    finally:
        conn.close()

    report = restart.restart_instances(run_id, ["inst-1"])
    assert report["restarted"] == [
        {"instance_id": "inst-1", "attempt_number": 2, "retry_reason": "operator_rerun_pass_at_k"}
    ]


def test_restart_skips_in_flight_and_unknown_instances() -> None:
    run_id = f"{_PREFIX}skips"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        _insert_instance(conn, run_id, "running-1", 1, "HARNESS_RUNNING")
    finally:
        conn.close()

    report = restart.restart_instances(run_id, ["running-1", "no-such-instance"])
    assert report["restarted"] == []
    reasons = {s["instance_id"]: s["reason"] for s in report["skipped"]}
    assert "in flight" in reasons["running-1"].lower()
    assert "unknown" in reasons["no-such-instance"].lower()


def test_restart_accepts_paused_by_operator_and_tags_it_infra_retry() -> None:
    """2026-08-31 correction: PAUSED_BY_OPERATOR must NOT be excluded from
    restart. /control/resume only reopens the dispatch/gateway gate — it has
    no mechanism to re-process a harness-jobs message that's already been
    consumed and deleted, which is exactly what happened by the time this
    instance's row landed. Excluding it here left no path back at all —
    surfaced by walking through "pause harness, then pause gateway, then how
    do these come back."
    """
    run_id = f"{_PREFIX}paused-restart"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        _insert_instance(conn, run_id, "paused-1", 1, "FAILED_HARNESS", "PAUSED_BY_OPERATOR")
    finally:
        conn.close()

    report = restart.restart_instances(run_id, ["paused-1"])
    assert report["skipped"] == []
    assert report["restarted"] == [
        {"instance_id": "paused-1", "attempt_number": 2, "retry_reason": "operator_infra_retry"}
    ]


def test_eval_phase_row_inherits_retry_reason_from_harness_row() -> None:
    """M3/M4: the eval-phase row seeded by _enqueue_eval_job must carry the
    same retry_reason as its harness-phase sibling, or the eval row would
    look like a second, distinct, untagged attempt in the denominator."""
    from swebench_eval.orchestrator.control_plane.results_writer import _enqueue_eval_job
    from swebench_eval.queue.schemas import ResultMessage

    run_id = f"{_PREFIX}eval-inherit"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO instance_results
                       (run_id, instance_id, attempt_number, phase, state, retry_reason)
                   VALUES (%s, 'inst-1', 2, 'harness', 'PATCH_READY', 'operator_infra_retry')""",
                (run_id,),
            )
        conn.commit()

        result = ResultMessage(
            run_id=run_id,
            instance_id="inst-1",
            attempt_number=2,
            phase="harness",
            state="PATCH_READY",
            patch_s3_key="s3://bucket/patch.diff",
        )
        _enqueue_eval_job(conn, result)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT retry_reason FROM instance_results "
                "WHERE run_id = %s AND instance_id = 'inst-1' AND attempt_number = 2 AND phase = 'eval'",
                (run_id,),
            )
            (eval_retry_reason,) = cur.fetchone()
    finally:
        conn.close()

    assert eval_retry_reason == "operator_infra_retry"


# ---------------------------------------------------------------------------
# M2 — the honest denominator, derived not stored
# ---------------------------------------------------------------------------


def test_resolve_rate_denominator_collapses_infra_retries_but_not_pass_at_k() -> None:
    from swebench_eval.orchestrator.api import queries

    run_id = f"{_PREFIX}denom"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        # instance A: attempt 1 failed infra, attempt 2 (operator_infra_retry) resolved.
        # -> ONE in the denominator (the retry collapses into its predecessor).
        _insert_instance(conn, run_id, "inst-a", 1, "FAILED_HARNESS", "MODEL_API_ERROR")
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO instance_results
                       (run_id, instance_id, attempt_number, phase, state, retry_reason)
                   VALUES (%s, 'inst-a', 2, 'eval', 'RESOLVED', 'operator_infra_retry')""",
                (run_id,),
            )
        # instance B: configured pass@k, attempt 1 resolved, attempt 2 (operator
        # deliberately reran it) unresolved -> TWO in the denominator.
        _insert_instance(conn, run_id, "inst-b", 1, "RESOLVED", phase="eval")
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO instance_results
                       (run_id, instance_id, attempt_number, phase, state, retry_reason)
                   VALUES (%s, 'inst-b', 2, 'eval', 'UNRESOLVED', 'operator_rerun_pass_at_k')""",
                (run_id,),
            )
        conn.commit()

        denominator = queries.resolve_rate_denominator(conn, run_id)
    finally:
        conn.close()

    assert denominator == 3  # inst-a: 1, inst-b: 2


# ---------------------------------------------------------------------------
# Regrade (EVAL-GRADE-RESOURCE-LIMITS follow-up, 2026-09-01) — eval-only
# attempt N+1 against the EXISTING patch; no new model spend.
# ---------------------------------------------------------------------------


def _insert_harness_with_patch(
    conn, run_id: str, instance_id: str, attempt_number: int, patch_path: str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO instance_results
                   (run_id, instance_id, attempt_number, phase, state,
                    error_category, patch_path)
               VALUES (%s, %s, %s, 'harness', 'PATCH_READY', NULL, %s)""",
            (run_id, instance_id, attempt_number, patch_path),
        )
    conn.commit()


def test_regrade_creates_eval_only_attempt_with_existing_patch(
    _stub_finalise_and_dispatch,
) -> None:
    run_id = f"{_PREFIX}regrade"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        _insert_harness_with_patch(conn, run_id, "inst-1", 1, "runs/r/harness/inst-1/1/patch.diff")
        # the eval half of attempt 1 OOMed — the regrade's motivating shape
        _insert_instance(conn, run_id, "inst-1", 1, "FAILED_EVAL", "EVAL_OOM_KILLED", phase="eval")
    finally:
        conn.close()

    report = restart.regrade_instances(run_id, ["inst-1"])
    assert report["regraded"] == [
        {
            "instance_id": "inst-1",
            "attempt_number": 2,
            "patch_s3_key": "runs/r/harness/inst-1/1/patch.diff",
        }
    ]
    assert report["skipped"] == []
    # dispatched as an EvalJob (stubbed), NOT a HarnessJob
    assert _stub_finalise_and_dispatch["regrade_dispatch"] == [(run_id, report["regraded"])]
    assert _stub_finalise_and_dispatch["dispatch"] == []

    # the new row: eval phase only, PENDING, tagged operator_regrade
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT phase, state, retry_reason FROM instance_results
                   WHERE run_id = %s AND instance_id = 'inst-1' AND attempt_number = 2""",
                (run_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    assert rows == [("eval", "PENDING", "operator_regrade")]


def test_regrade_skips_instances_without_a_captured_patch(_stub_finalise_and_dispatch) -> None:
    run_id = f"{_PREFIX}regrade-nopatch"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        # harness failed before any patch was captured — nothing to regrade
        _insert_instance(conn, run_id, "inst-1", 1, "FAILED_HARNESS", "HARNESS_CRASH")
    finally:
        conn.close()

    report = restart.regrade_instances(run_id, ["inst-1"])
    assert report["regraded"] == []
    assert len(report["skipped"]) == 1
    assert "no captured patch" in report["skipped"][0]["reason"]
    assert "/restart" in report["skipped"][0]["reason"]
    assert _stub_finalise_and_dispatch["regrade_dispatch"] == []


def test_regrade_skips_in_flight_and_refuses_closed_runs(_stub_finalise_and_dispatch) -> None:
    run_id = f"{_PREFIX}regrade-guards"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        _insert_harness_with_patch(conn, run_id, "busy-1", 1, "runs/r/p.diff")
        _insert_instance(conn, run_id, "busy-1", 1, "EVAL_RUNNING", phase="eval")
    finally:
        conn.close()

    report = restart.regrade_instances(run_id, ["busy-1", "no-such"])
    reasons = {s["instance_id"]: s["reason"] for s in report["skipped"]}
    assert "in flight" in reasons["busy-1"].lower()
    assert "unknown" in reasons["no-such"].lower()
    assert report["regraded"] == []

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE runs SET status = 'completed' WHERE run_id = %s", (run_id,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(restart.RestartError, match="closed"):
        restart.regrade_instances(run_id, ["busy-1"])


def test_regrade_of_a_regrade_reaches_back_to_the_harness_patch(
    _stub_finalise_and_dispatch,
) -> None:
    """Attempt 2 was itself a regrade (eval-only, no patch of its own); a
    second regrade must still find attempt 1's harness patch, at attempt 3."""
    run_id = f"{_PREFIX}regrade-again"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        _insert_harness_with_patch(conn, run_id, "inst-1", 1, "runs/r/harness/inst-1/1/patch.diff")
        _insert_instance(conn, run_id, "inst-1", 1, "FAILED_EVAL", "EVAL_OOM_KILLED", phase="eval")
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO instance_results
                       (run_id, instance_id, attempt_number, phase, state, retry_reason)
                   VALUES (%s, 'inst-1', 2, 'eval', 'FAILED_EVAL', 'operator_regrade')""",
                (run_id,),
            )
        conn.commit()
    finally:
        conn.close()

    report = restart.regrade_instances(run_id, ["inst-1"])
    assert report["regraded"] == [
        {
            "instance_id": "inst-1",
            "attempt_number": 3,
            "patch_s3_key": "runs/r/harness/inst-1/1/patch.diff",
        }
    ]


def test_resolve_rate_denominator_collapses_regrades() -> None:
    from swebench_eval.orchestrator.api import queries

    run_id = f"{_PREFIX}regrade-denom"
    conn = _db()
    try:
        _insert_run(conn, run_id)
        # attempt 1: harness ok + eval OOMed; attempt 2: the regrade resolved.
        # -> ONE logical attempt in the denominator, not two.
        _insert_harness_with_patch(conn, run_id, "inst-1", 1, "runs/r/p.diff")
        _insert_instance(conn, run_id, "inst-1", 1, "FAILED_EVAL", "EVAL_OOM_KILLED", phase="eval")
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO instance_results
                       (run_id, instance_id, attempt_number, phase, state, retry_reason)
                   VALUES (%s, 'inst-1', 2, 'eval', 'RESOLVED', 'operator_regrade')""",
                (run_id,),
            )
        conn.commit()
        denominator = queries.resolve_rate_denominator(conn, run_id)
    finally:
        conn.close()

    assert denominator == 1
