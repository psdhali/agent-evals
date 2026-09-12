"""M1.12's named DoD tests (ADR-0034 / observability design) — pause gates.

The design names these three as DoD items, not optional coverage:

  1. ``test_pause_does_not_receive`` — a consumer must NOT call
     ``receive_message`` while its pool is paused.  A paused consumer that
     receives and returns a message bumps its receive count toward the DLQ
     (maxReceiveCount = 3), so a gate AFTER receive is useless.  The gate is
     BEFORE receive — gates #1 (harness dispatcher), #3 (eval worker) and
     #4 (harness worker) all short-circuit before a receive could happen.
  2. ``test_pause_does_not_increment_receive_count`` — against real ElasticMQ:
     pause, enqueue 10, let the loop spin, assert depth stays 10 and the DLQ
     stays empty; then resume and assert all 10 drain.
  3. ``test_results_writer_ignores_pause`` — gate #6 must NEVER exist: pause
     every pool and assert the results writer still receives and processes.

Before this round, tests #1 and #3 existed only as weaker substitutes or not
at all; the design names them explicitly.
"""

from __future__ import annotations

import time
from typing import Self
from unittest import mock

import pytest

from swebench_eval.control import state as control_state
from swebench_eval.orchestrator.control_plane import harness_dispatcher, results_writer
from swebench_eval.workers import eval_worker, harness_worker


class _StopLoop(Exception):
    """Raised by a mocked sleep to end a consumer loop after one gate pass."""


def _raise_after(calls: int) -> mock.Mock:
    """A side_effect that raises _StopLoop after *calls* successful calls."""

    def _side_effect(*a, **k):
        _side_effect._n = getattr(_side_effect, "_n", 0) + 1  # type: ignore[attr-defined]
        if _side_effect._n >= calls:  # type: ignore[attr-defined]
            raise _StopLoop()

    return mock.Mock(side_effect=_side_effect)


# ---------------------------------------------------------------------------
# 1. test_pause_does_not_receive (gates #1, #3, #4)
# ---------------------------------------------------------------------------


def test_pause_does_not_receive_harness_dispatcher(monkeypatch) -> None:
    """Gate #1: the harness dispatcher must NOT receive while harness is paused.

    The gate is in ``_DispatcherAdmission.may_launch()`` — BEFORE the receive
    (harness_dispatcher.py:359).  ``receive_message`` is replaced with a spy
    that fails if it is ever called.
    """
    import swebench_eval.database.connection as dbc

    monkeypatch.setenv("MAX_CONCURRENT_HARNESS_TASKS", "10")

    receive = mock.Mock(side_effect=AssertionError("receive_message called while paused"))
    monkeypatch.setattr(harness_dispatcher, "receive_message", receive)
    monkeypatch.setattr(
        "swebench_eval.orchestrator.control_plane.harness_dispatcher.control_state.is_paused",
        lambda pool: True,
    )
    monkeypatch.setattr(
        "swebench_eval.orchestrator.control_plane.harness_dispatcher.time.sleep",
        _raise_after(1),
    )
    monkeypatch.setattr(harness_dispatcher, "_ecs_client", lambda: mock.Mock())
    monkeypatch.setattr(dbc, "get_connection", lambda: mock.Mock())

    with pytest.raises(_StopLoop):
        harness_dispatcher.run_harness_dispatcher()

    receive.assert_not_called()


def test_pause_does_not_receive_harness_worker(monkeypatch) -> None:
    """Gate #4: the harness worker must NOT receive while harness is paused.

    harness_worker.py:115 — ``if _pool_paused("harness"): continue`` before
    ``receive_message``.
    """
    receive = mock.Mock(side_effect=AssertionError("receive_message called while paused"))
    monkeypatch.setattr(harness_worker, "receive_message", receive)
    monkeypatch.setattr(harness_worker, "_pool_paused", lambda pool: True)
    monkeypatch.setattr(
        "swebench_eval.workers.harness_worker.time.sleep",
        _raise_after(1),
    )
    monkeypatch.setattr(harness_worker, "_install_sigterm_handler", lambda: None)

    with pytest.raises(_StopLoop):
        harness_worker.run_harness_worker()

    receive.assert_not_called()


def test_pause_does_not_receive_eval_worker(monkeypatch) -> None:
    """Gate #3: the eval worker must NOT receive while eval is paused.

    eval_worker.py:51 — ``if _pool_paused("eval"): continue`` before
    ``receive_message``.
    """
    receive = mock.Mock(side_effect=AssertionError("receive_message called while paused"))
    monkeypatch.setattr(eval_worker, "receive_message", receive)
    monkeypatch.setattr(eval_worker, "_pool_paused", lambda pool: True)
    monkeypatch.setattr(
        "swebench_eval.workers.eval_worker.time.sleep",
        _raise_after(1),
    )

    with pytest.raises(_StopLoop):
        eval_worker.run_eval_worker()

    receive.assert_not_called()


# ---------------------------------------------------------------------------
# 2. test_pause_does_not_increment_receive_count (integration, ElasticMQ)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_pause_does_not_increment_receive_count() -> None:
    """M1.12 integration pair: paused ⟹ depth stays, DLQ stays empty; resume
    ⟹ drains.

    Drives the REAL local-dev consumer gate against the real ElasticMQ.  A
    paused consumer that received messages would be exactly how the backlog
    headed to the DLQ (receive-count increments, maxReceiveCount = 3).
    """
    from swebench_eval.queue import client as qclient

    queue_name = "harness-jobs"
    dlq_name = "harness-jobs-dlq"

    def _depth(q: str) -> int:
        sqs = qclient.get_sqs_client()
        url = qclient.get_queue_url(q)
        resp = sqs.get_queue_attributes(
            QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"]
        )
        return int(resp["Attributes"].get("ApproximateNumberOfMessages", "0"))

    # Clean start: drain anything left by a prior run.
    stale = qclient.receive_message(queue_name, wait_seconds=0)
    while stale is not None:
        qclient.delete_message(queue_name, stale["receipt_handle"])
        stale = qclient.receive_message(queue_name, wait_seconds=0)
    assert _depth(dlq_name) == 0

    control_state.set_pause(["harness"], True, actor="test", reason="integration gate test")
    try:
        for i in range(10):
            qclient.send_message(
                queue_name,
                {"run_id": "m1-drain", "instance_id": f"inst-{i}", "attempt_number": 1},
            )
        assert _depth(queue_name) == 10, "enqueue failed"

        # The gate must keep the loop OFF receive while paused.  Simulate the
        # worker's own loop: gate first, receive only when unpaused.
        received_while_paused = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if harness_worker._pool_paused("harness"):
                time.sleep(0.1)
                continue
            # If we reach a receive, the gate failed.
            msg = qclient.receive_message(queue_name, wait_seconds=0)
            if msg is not None:
                received_while_paused = True
                qclient.delete_message(queue_name, msg["receipt_handle"])
        assert not received_while_paused, "received a message while harness paused"
        assert _depth(queue_name) == 10, "depth fell while paused (receive-count risk)"
        assert _depth(dlq_name) == 0, "messages hit the DLQ while paused"

        # Resume: drain all 10.
        control_state.set_pause(["harness"], False, actor="test", reason="resume")
        drained = 0
        for _ in range(50):
            msg = qclient.receive_message(queue_name, wait_seconds=0)
            if msg is None:
                time.sleep(0.1)
                continue
            qclient.delete_message(queue_name, msg["receipt_handle"])
            drained += 1
            if drained >= 10:
                break
        assert drained == 10, f"drained {drained}/10 after resume"
        assert _depth(queue_name) == 0
        assert _depth(dlq_name) == 0
    finally:
        control_state.set_pause(["harness"], False, actor="test", reason="cleanup")
        for _ in range(20):
            msg = qclient.receive_message(queue_name, wait_seconds=0)
            if msg is None:
                break
            qclient.delete_message(queue_name, msg["receipt_handle"])


# ---------------------------------------------------------------------------
# 3. test_results_writer_ignores_pause — gate #6 must never exist
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal redis stand-in: control:flags hash storage + the publisher
    tick's run-activity marker."""

    def __init__(self) -> None:
        self._flags: dict[bytes, bytes] = {}
        self._active_marker = False

    def hset(self, key: str, mapping: dict[str, bytes]) -> None:
        if key == "control:flags":
            # FIELD-MERGE, matching real Redis — the heartbeat HSETs only
            # published_at and must not wipe the pause flags.
            self._flags.update({k.encode(): v for k, v in mapping.items()})

    def hget(self, key: str, field: str) -> bytes | None:
        if key == "control:flags":
            return self._flags.get(field.encode())
        return None

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        if key == "control:flags":
            return self._flags
        return {}

    def sadd(self, key: str, *members: str) -> None:
        return None

    def delete(self, key: str) -> None:
        self._flags = {}
        self._active_marker = False

    def exists(self, key: str) -> int:
        if key == "control:any-run-active":
            return 1 if self._active_marker else 0
        return 0

    def set(self, key: str, value: bytes, ex: int) -> None:
        if key == "control:any-run-active":
            self._active_marker = True


class _AuroraTimestamp:
    """A psycopg2-``timestamp``-alike: the CAS reconcile reads
    ``control_state.updated_at`` via ``row[0].timestamp()``."""

    def __init__(self, epoch: float = 0.0) -> None:
        self._epoch = epoch

    def timestamp(self) -> float:
        return self._epoch


class _FakeConn:
    """Serves the queries publish_from_db/reconcile_from_db need."""

    def cursor(self) -> _FakeCur:
        return _FakeCur()

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeCur:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        self._sql = sql

    def fetchone(self):
        # The CAS reconcile first reads control_state.updated_at (a timestamp),
        # then the control_state row (0 = not paused).
        if "updated_at" in self._sql:
            return (_AuroraTimestamp(0.0),)
        return (False, False, False)

    def fetchall(self):
        return []

    def close(self) -> None:
        return None


def test_results_writer_ignores_pause(monkeypatch) -> None:
    """Gate #6 (which MUST NOT exist): pause every pool and assert the results
    writer still receives and processes.

    Pause and abort both deliberately let in-flight work run to completion; a
    result emitted by that work must have somewhere to land.  "Completing" the
    pause by gating this consumer strands in-flight results — a defect.  The
    test drives the REAL loop one iteration; the writer must not even look at
    the pause flags.
    """
    import swebench_eval.database.connection as dbc

    # The writer's own startup publish reads a control_state row + aborted
    # runs; the receive/process fixtures below cover the loop.
    monkeypatch.setattr(control_state, "_redis", lambda: _FakeRedis())
    monkeypatch.setattr(dbc, "get_connection", lambda: _FakeConn())
    monkeypatch.setattr(dbc, "run_migrations", lambda: None)
    monkeypatch.setattr(dbc, "ensure_additional_databases", lambda: None)
    monkeypatch.setattr(results_writer, "run_llm_calls_writer", lambda *a, **k: None)
    # run-launch §7/D8: two more daemon threads now start at loop entry and
    # would ALSO call the mocked (stateful, single-shot) receive_message
    # below from a background thread — racing the main loop for the one
    # fake message and making this test flaky/wrong for a reason that has
    # nothing to do with what it verifies.  Neutralise them, same as
    # run_llm_calls_writer above.  (The reaper tick itself moved to
    # run_supervisor.py — results-writer's loop no longer calls it.)
    monkeypatch.setattr(results_writer, "_run_dlq_reaper", lambda *a, **k: None)
    # §6.6: the model-observations daemon is the same shape — neutralise it too.
    monkeypatch.setattr(results_writer, "run_model_observations_writer", lambda *a, **k: None)

    processed: list[str] = []

    def _fake_recv(*a, **k):
        # Deliver one harness result then end the loop.
        if not _fake_recv.done:  # type: ignore[attr-defined]
            _fake_recv.done = True  # type: ignore[attr-defined]
            return {
                "receipt_handle": "h1",
                "body": {
                    "run_id": "r1",
                    "instance_id": "i1",
                    "attempt_number": 1,
                    "phase": "harness",
                    "state": "COMPLETED",
                    "patch_s3_key": "runs/r1/patch.diff",
                },
            }
        raise _StopLoop()

    _fake_recv.done = False  # type: ignore[attr-defined]
    monkeypatch.setattr(results_writer, "receive_message", _fake_recv)
    # The loop must still PROCESS what it received — that is the "gate #6 must
    # never exist" claim.  Record the handoff rather than exercising the real
    # DB writes (out of scope for the gate test).
    monkeypatch.setattr(
        results_writer, "_process_result", lambda result: processed.append(result.instance_id)
    )
    monkeypatch.setattr(results_writer, "delete_message", lambda q, h: None)

    # Pause EVERY pool; the writer must still receive+process.
    control_state.set_pause(["harness", "eval", "gateway"], True, actor="test", reason="M1.12")
    try:
        with pytest.raises(_StopLoop):
            results_writer.run_results_writer()
    finally:
        control_state.set_pause(
            ["harness", "eval", "gateway"], False, actor="test", reason="cleanup"
        )

    assert processed, "results writer did not process while every pool was paused (gate #6?)"
