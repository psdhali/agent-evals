"""M1 abort-executor + API + summary tests (ADR-0034 M1.6-M1.8).

Covers the design's named tests:

  - the ordered abort steps (record intent, stop in-flight, drain, settle,
    finalise) with an injected fake ECS client;
  - drain REFUSES when a second run is active (M1.7);
  - the API exposes /control + /runs/{id}/abort (M1.10).
"""

from __future__ import annotations

from unittest import mock

import pytest

from swebench_eval.orchestrator.control_plane import abort as abort_mod


class _FakeConn:
    """A minimal Aurora stand-in: records SQL and serves scripted rows, so the
    abort's DB writes + reads are asserted without Postgres."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.active_count = 1  # exactly one run active (our own)

    def cursor(self) -> _FakeCur:
        return _FakeCur(self)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeCur:
    def __init__(self, db: _FakeConn) -> None:
        self.db = db
        self.row: tuple[object, ...] | None = ("running",)

    def execute(self, sql, params=None) -> None:
        self.db.executed.append(sql)
        if "count(*)" in sql and "instance_results" in sql:
            self.row = (0,)  # settle poll: nothing outstanding
        elif "count(*)" in sql and "runs" in sql:
            self.row = (self.db.active_count,)
        elif "UPDATE runs SET status = 'aborting'" in sql:
            self.row = None
        self.sql = sql

    def fetchone(self):
        return self.row

    def fetchall(self):
        # 2026-08-28: the leftover-row sweep's SELECT (abort.py
        # _sweep_remaining_rows) — these tests seed no non-terminal rows, so
        # "nothing to sweep" is the correct fake response. The sweep's own
        # behavior is covered against real Postgres in
        # tests/test_abort_ledger_reconciliation.py.
        return []

    def close(self) -> None:
        return None


def _fake_ecs() -> mock.Mock:
    ecs = mock.Mock()
    ecs.list_tasks.return_value = {"taskArns": ["arn:t1", "arn:t2"], "nextToken": None}
    return ecs


class _FakeReceive:
    """Callable fake for ``receive_message``: one own-run message, then one
    foreign-run message, then ``None`` (queue empty).

    A counter carried on a function object trips mypy (``Callable`` has no
    ``_count``); a small class with a typed ``_count`` is the honest form.
    """

    def __init__(self, own: dict[str, str], foreign: dict[str, str]) -> None:
        self._own = own
        self._foreign = foreign
        self._count = 0

    def __call__(self, q, wait_seconds=1, visibility_timeout=60) -> dict[str, object] | None:
        if self._count == 0:
            self._count += 1
            return {"receipt_handle": "h-own", "body": self._own}
        if self._count == 1:
            self._count += 1
            return {"receipt_handle": "h-foreign", "body": self._foreign}
        return None  # queue now empty


def _run_abort(monkeypatch, *, scope="harness", second_run=False):
    db = _FakeConn()
    if second_run:
        db.active_count = 2
    ecs = _fake_ecs()
    monkeypatch.setenv("CLUSTER", "eval-dev-cluster")
    # M1.6 step 1 publishes the abort to Redis (control:aborted). CI has no
    # redis — stub the client directly (the passed monkeypatch is a dummy
    # mock.Mock() from the callers, so patch by object, not via monkeypatch).
    # The redis behaviour itself is covered by test_control_state, not here.
    import swebench_eval.control.state as control_state

    # M1.7's drain (abort.py _drain_queued) imports receive_message /
    # delete_message from queue.client at CALL time and reaches ElasticMQ via
    # boto3 — CI has no SQS.  Stub by string at the defining module (the passed
    # monkeypatch is a dummy mock.Mock(), so monkeypatch.setattr would be a
    # silent no-op — patch the object/name directly, as builder 1 did for
    # redis).  receive_message -> None makes the drain see an empty queue and
    # break immediately; the drain's business logic (foreign-message avoidance,
    # refuse-on-second-run) is covered by test_drain_refuses_when_second_run_active.
    with (
        mock.patch.object(control_state, "_redis", lambda: mock.Mock()),
        mock.patch("swebench_eval.queue.client.receive_message", return_value=None),
        mock.patch("swebench_eval.queue.client.delete_message"),
    ):
        report = abort_mod.execute_abort(
            connection=db, run_id="run-1", scope=scope, reason="suspicion", actor="alice", ecs=ecs
        )
    return db, ecs, report


def _abort_sqls(db: _FakeConn) -> list[str]:
    return db.executed


def test_abort_marks_aborting_then_aborted() -> None:
    db, _ecs, report = _run_abort(monkeypatch=mock.Mock())
    sqls = _abort_sqls(db)
    assert any("'aborting'" in s for s in sqls), "intent not recorded"
    assert any("'aborted'" in s for s in sqls), "finalise missing"
    assert report.settled is True


def test_abort_stops_in_flight_via_startedBy() -> None:
    """M1.6 step 3: ListTasks(startedBy=run_id) -> StopTask each (M1.5)."""
    _db, ecs, _report = _run_abort(monkeypatch=mock.Mock())
    list_kw = ecs.list_tasks.call_args_list[0].kwargs
    assert list_kw.get("startedBy") == "run-1"
    assert ecs.stop_task.call_count == 2


def test_drain_refuses_when_second_run_active() -> None:
    """M1.7: a shared-queue drain refuses when a second run is active."""
    _db, _ecs, report = _run_abort(mock.Mock(), second_run=True)
    assert report.drain_skipped is True
    assert "another run" in report.drain_skip_reason


def test_abort_scope_eval_skips_harness_drain() -> None:
    _db, _ecs, report = _run_abort(mock.Mock(), scope="eval")
    assert report.drain_skipped


def test_api_exposes_control_and_abort_endpoints() -> None:
    """M1.10: the API has /control + /runs/{id}/abort + /control/pause."""
    from swebench_eval.orchestrator.api.main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/control" in paths
    assert "/control/pause" in paths
    assert "/control/resume" in paths
    assert "/runs/{run_id}/abort" in paths
    assert "/runs/{run_id}" in paths


def test_abort_report_note_reaches_the_response() -> None:
    """Reviewer §2.5 (2026-09-01): main.py passed note= but AbortReport had no such field, so
    pydantic (extra=ignore) silently dropped it — the "abort is bounded by stopTimeout..."
    caveat never reached any API response. The field must exist AND survive serialisation."""
    from swebench_eval.orchestrator.api.schemas import AbortReport

    report = AbortReport(run_id="run-1", note="abort is bounded by stopTimeout")
    assert report.model_dump()["note"] == "abort is bounded by stopTimeout"


def test_api_abort_validates_scope() -> None:
    from fastapi import HTTPException

    from swebench_eval.orchestrator.api.main import abort_run

    with pytest.raises(HTTPException, match="scope"):
        abort_run("run-1", {"scope": "datacenter"})


def test_drain_returns_foreign_message_never_deletes_it() -> None:
    """M1 review-find §3: a foreign-run message must be RETURNED, not deleted.

    The old code's comment said "Deleting it would lose another run's work" —
    three times — and then called ``delete_message`` anyway.  This test serves
    one own-run message then one foreign message, and asserts delete is called
    ONLY for the own-run receipt, the foreign one is returned (visibility
    reset to 0), and the loop breaks.
    """
    from swebench_eval.orchestrator.control_plane import abort as abort_mod

    db = _FakeConn()
    deleted: list[str] = []
    returned: list[tuple[str, int]] = []

    own = {"run_id": "run-1", "instance_id": "inst-1"}
    foreign = {"run_id": "run-999", "instance_id": "inst-999"}

    import swebench_eval.control.state as control_state
    from swebench_eval.queue import client as qclient

    with (
        mock.patch.object(control_state, "_redis", lambda: mock.Mock()),
        mock.patch.object(qclient, "receive_message", side_effect=_FakeReceive(own, foreign)),
        mock.patch.object(qclient, "delete_message", side_effect=lambda q, h: deleted.append(h)),
        mock.patch.object(
            qclient,
            "change_message_visibility",
            side_effect=lambda q, h, t: returned.append((h, t)),
        ),
        mock.patch.object(
            qclient, "upload_artifact", return_value="runs/run-1/aborted/drained-messages.jsonl"
        ),
    ):
        report = abort_mod.execute_abort(
            connection=db,
            run_id="run-1",
            scope="harness",
            reason="suspicion",
            actor="alice",
            ecs=_fake_ecs(),
        )

    assert deleted == ["h-own"], f"delete called for {deleted}, expected only own-run receipt"
    assert returned == [
        ("h-foreign", 0)
    ], f"foreign message returned as {returned}, expected visibility reset to 0"
    assert report.drained == 1


def test_drain_manifest_snapshots_dlq_tagged(monkeypatch) -> None:
    """M1.7 review-find §6: the manifest also records the DLQ contents.

    ``_write_drain_manifest`` must snapshot what is dead-lettered — the
    evidence someone would read months later to answer "why did X die?" — and
    tag each record so drained-vs-DLQ is never conflated.
    """
    import json as _json

    from swebench_eval.orchestrator.control_plane import abort as abort_mod
    from swebench_eval.queue import client as qclient

    uploaded: dict[str, str] = {}

    dlq_msgs = iter(
        [
            {"receipt_handle": "dlq-1", "body": {"run_id": "run-1", "instance_id": "inst-dead"}},
            None,
        ]
    )

    def _fake_dlq_recv(q, wait_seconds=0, visibility_timeout=0):
        return next(dlq_msgs)

    def _fake_upload(bucket, key, data):
        uploaded[key] = str(data)

    monkeypatch.setattr(qclient, "receive_message", _fake_dlq_recv)
    monkeypatch.setattr(qclient, "change_message_visibility", lambda *a, **k: None)
    monkeypatch.setattr(qclient, "upload_artifact", _fake_upload)
    monkeypatch.setenv("ARTIFACTS_BUCKET", "eval-artifacts")

    abort_mod._write_drain_manifest("run-1", [{"run_id": "run-1", "instance_id": "inst-1"}])

    key = "runs/run-1/aborted/drained-messages.jsonl"
    assert key in uploaded, f"manifest not uploaded: {list(uploaded)}"
    lines = [ln for ln in uploaded[key].splitlines() if ln.strip()]
    # First line = drained run's message, tagged drained.
    drained = _json.loads(lines[0])
    assert drained["source"] == "drained", drained
    assert drained["instance_id"] == "inst-1"
    # Second line = the DLQ snapshot, tagged dlq and NOT deleted.
    dlq = _json.loads(lines[1])
    assert dlq["source"] == "dlq", dlq
    assert dlq["instance_id"] == "inst-dead"
    assert dlq["receipt_handle"] == "dlq-1"


# ---------------------------------------------------------------------------
# 2026-09-04: the API answers inside the ALB's 60 s idle timeout; settle + sweep +
# finalise continue on a thread with their own connection.


def test_background_settle_returns_unsettled_then_finalises_on_a_thread() -> None:
    import threading
    import time

    import swebench_eval.control.state as control_state

    db = _FakeConn()
    bg_db = _FakeConn()
    ecs = _fake_ecs()
    finalised = threading.Event()

    class _Watching(_FakeConn):
        def close(self) -> None:
            finalised.set()

    bg_db = _Watching()
    with (
        mock.patch.object(control_state, "_redis", lambda: mock.Mock()),
        mock.patch("swebench_eval.queue.client.receive_message", return_value=None),
        mock.patch("swebench_eval.queue.client.delete_message"),
        mock.patch("swebench_eval.database.connection.get_connection", return_value=bg_db),
        mock.patch.dict("os.environ", {"CLUSTER": "eval-dev-cluster"}),
    ):
        t0 = time.monotonic()
        report = abort_mod.execute_abort(
            connection=db,
            run_id="run-1",
            scope="harness",
            reason="r",
            actor="alice",
            ecs=ecs,
            background_settle=True,
        )
        assert time.monotonic() - t0 < 5
        assert report.settled is False
        assert report.in_flight_stopped == 2  # StopTask already happened
        # the request connection recorded the intent but NOT the finalise...
        assert any("'aborting'" in s for s in db.executed)
        assert not any("'aborted'" in s for s in db.executed)
        # ...the thread does that, on its own connection
        assert finalised.wait(timeout=10), "background settle did not finish"
    assert any("'aborted'" in s for s in bg_db.executed)


def test_progress_key_older_than_the_abort_request_is_a_ghost() -> None:
    """The sweep skipped both HARNESS_RUNNING rows because their last (pre-abort)
    progress key was still inside its 300 s TTL when the 300 s settle ended."""
    live = abort_mod._progress_is_live
    assert live({"observed_at": 1000.0}, requested_at=1100.0) is False
    assert live({"observed_at": 1200.0}, requested_at=1100.0) is True
    assert live({"observed_at": 1000.0}, requested_at=None) is True  # old rule
    assert live({}, requested_at=1100.0) is True  # unreadable stamp -> treat as live
