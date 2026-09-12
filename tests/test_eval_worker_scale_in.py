"""Eval-worker scale-in behaviour (BUILDER4-EVAL-PACKING-2026-09-03 §6, question 2).

Three pieces: ECS task scale-in protection for the duration of a grade (set on receive,
refreshed by the heartbeat, cleared on completion), a SIGTERM handler that stops receiving,
and the heartbeat handing a mid-grade job straight back to the queue on SIGTERM.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from unittest import mock

import pytest

from swebench_eval.evaluation import grade_containers
from swebench_eval.queue.schemas import ResultMessage
from swebench_eval.workers import eval_worker, task_protection


class _StopLoop(Exception):
    pass


@pytest.fixture(autouse=True)
def _clean_shutdown_flag() -> Any:
    eval_worker._SHUTDOWN.clear()
    yield
    eval_worker._SHUTDOWN.clear()


# -- task_protection ----------------------------------------------------------------------------


def test_no_agent_uri_is_a_logged_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ECS_AGENT_URI", raising=False)
    calls: list[Any] = []
    monkeypatch.setattr(task_protection, "_http", lambda *a: calls.append(a))
    assert task_protection.set_protection(True, 15) is False
    assert calls == []


def test_enable_sends_put_with_expiry_and_confirms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECS_AGENT_URI", "http://169.254.170.2/api/abc/")
    seen: list[tuple[str, str, dict[str, Any] | None]] = []

    def _http(method: str, url: str, body: dict[str, Any] | None) -> tuple[int, str]:
        seen.append((method, url, body))
        assert body is not None  # a PUT always carries a body
        return 200, json.dumps(
            {"protection": {"ProtectionEnabled": body["ProtectionEnabled"], "TaskArn": "t"}}
        )

    monkeypatch.setattr(task_protection, "_http", _http)
    assert task_protection.set_protection(True, 15) is True
    assert task_protection.set_protection(False) is True
    assert seen[0] == (
        "PUT",
        "http://169.254.170.2/api/abc/task-protection/v1/state",
        {"ProtectionEnabled": True, "ExpiresInMinutes": 15},
    )
    assert seen[1][2] == {"ProtectionEnabled": False}


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (400, json.dumps({"error": {"Code": "AccessDeniedException", "Message": "no"}})),
        (200, json.dumps({"protection": {"ProtectionEnabled": False}})),  # not the state asked
        (500, "not json"),
    ],
)
def test_unconfirmed_replies_are_false_and_warned(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, status: int, text: str
) -> None:
    monkeypatch.setenv("ECS_AGENT_URI", "http://agent")
    monkeypatch.setattr(task_protection, "_http", lambda *a: (status, text))
    with caplog.at_level("WARNING"):
        assert task_protection.set_protection(True, 15) is False
    assert "NOT confirmed" in caplog.text


def test_transport_failure_is_false_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECS_AGENT_URI", "http://agent")

    def _boom(*a: Any) -> tuple[int, str]:
        raise OSError("connection refused")

    monkeypatch.setattr(task_protection, "_http", _boom)
    assert task_protection.set_protection(True, 15) is False


# -- the worker loop ----------------------------------------------------------------------------


def _wire_loop(monkeypatch: pytest.MonkeyPatch, receive: Any) -> dict[str, list[Any]]:
    log: dict[str, list[Any]] = {"protect": [], "delete": [], "send": [], "vis": [], "sweep": []}
    monkeypatch.setattr("swebench_eval.logging_bootstrap.configure_logging", lambda: None)
    monkeypatch.setattr(eval_worker, "_sweep_orphans", lambda: log["sweep"].append(1))
    monkeypatch.setattr(eval_worker, "_install_sigterm_handler", lambda: None)
    monkeypatch.setattr(eval_worker, "_pool_paused", lambda pool: False)
    monkeypatch.setattr(eval_worker, "receive_message", receive)

    def _set_protection(enabled: bool, m: int | None = None) -> bool:
        log["protect"].append((enabled, m))
        return True

    monkeypatch.setattr(eval_worker, "set_protection", _set_protection)
    monkeypatch.setattr(eval_worker, "delete_message", lambda q, r: log["delete"].append(r))
    monkeypatch.setattr(eval_worker, "send_message", lambda q, body: log["send"].append(body))
    monkeypatch.setattr(
        eval_worker, "change_message_visibility", lambda q, r, t: log["vis"].append((r, t))
    )
    monkeypatch.setattr(
        eval_worker,
        "_run_eval",
        lambda job, timing=None: ResultMessage(
            run_id=job.run_id,
            instance_id=job.instance_id,
            attempt_number=job.attempt_number,
            phase="eval",
            state="RESOLVED",
        ),
    )
    return log


_MSG = {
    "receipt_handle": "rcpt-1",
    "body": {
        "run_id": "r1",
        "instance_id": "django__django-1",
        "attempt_number": 1,
        "patch_s3_key": "runs/r1/x.diff",
    },
    "attributes": {},
}


def test_grade_is_protected_for_its_duration_then_released(monkeypatch: pytest.MonkeyPatch) -> None:
    receive = mock.Mock(side_effect=[dict(_MSG), _StopLoop()])
    log = _wire_loop(monkeypatch, receive)
    with pytest.raises(_StopLoop):
        eval_worker.run_eval_worker()
    assert log["protect"] == [(True, eval_worker._PROTECTION_MINUTES), (False, None)]
    assert log["delete"] == ["rcpt-1"]
    assert [b["state"] for b in log["send"]] == ["EVAL_RUNNING", "RESOLVED"]
    assert log["sweep"] == [1]  # eval scaling review F1: the startup sweep ran


def test_sigterm_stops_receiving_and_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    receive = mock.Mock(side_effect=AssertionError("must not receive after SIGTERM"))
    _wire_loop(monkeypatch, receive)
    eval_worker._SHUTDOWN.set()
    eval_worker.run_eval_worker()  # returns instead of polling
    receive.assert_not_called()


def test_sigterm_after_a_grade_skips_the_release_and_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The release is skipped on shutdown (the task is stopping) and the loop exits."""

    def _receive(*a: Any, **k: Any) -> dict[str, Any]:
        eval_worker._SHUTDOWN.set()  # SIGTERM lands while this grade runs
        return dict(_MSG)

    log = _wire_loop(monkeypatch, _receive)
    eval_worker.run_eval_worker()
    assert log["protect"] == [(True, eval_worker._PROTECTION_MINUTES)]


# -- the heartbeat ------------------------------------------------------------------------------


def test_heartbeat_hands_the_job_back_on_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    vis: list[tuple[str, int]] = []
    reaped: list[int] = []
    monkeypatch.setattr(
        eval_worker, "change_message_visibility", lambda q, r, t: vis.append((r, t))
    )
    # Eval scaling review F1: the grading container is taken down with the job hand-back.
    monkeypatch.setattr(grade_containers, "remove_current", lambda client=None: reaped.append(1))
    monkeypatch.setattr(eval_worker, "_HEARTBEAT_POLL_SECONDS", 0.01)
    stop, released = threading.Event(), threading.Event()
    t = threading.Thread(
        target=eval_worker._heartbeat,
        args=("eval-jobs", "rcpt", stop),
        kwargs={"protected": True, "released": released},
    )
    t.start()
    eval_worker._SHUTDOWN.set()
    t.join(timeout=2)
    assert not t.is_alive()
    assert released.is_set()
    assert vis == [("rcpt", 0)]
    assert reaped == [1]


def test_heartbeat_refreshes_visibility_and_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    vis: list[int] = []
    prot: list[tuple[bool, int | None]] = []
    monkeypatch.setattr(eval_worker, "change_message_visibility", lambda q, r, t: vis.append(t))

    def _set_protection(e: bool, m: int | None = None) -> bool:
        prot.append((e, m))
        return True

    monkeypatch.setattr(eval_worker, "set_protection", _set_protection)
    monkeypatch.setattr(eval_worker, "_HEARTBEAT_POLL_SECONDS", 0.005)
    monkeypatch.setattr(eval_worker, "_HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(eval_worker, "_PROTECTION_REFRESH_SECONDS", 0.05)
    stop = threading.Event()
    t = threading.Thread(
        target=eval_worker._heartbeat, args=("eval-jobs", "rcpt", stop), kwargs={"protected": True}
    )
    t.start()
    time.sleep(0.3)
    stop.set()
    t.join(timeout=2)
    assert vis and set(vis) == {eval_worker._BASE_VISIBILITY_SECONDS}
    assert len(vis) >= 3
    assert prot and set(prot) == {(True, eval_worker._PROTECTION_MINUTES)}
    assert len(prot) >= 2


def test_heartbeat_without_protection_never_calls_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prot: list[Any] = []
    monkeypatch.setattr(eval_worker, "change_message_visibility", lambda q, r, t: None)
    monkeypatch.setattr(eval_worker, "set_protection", lambda *a: prot.append(a))
    monkeypatch.setattr(eval_worker, "_HEARTBEAT_POLL_SECONDS", 0.005)
    monkeypatch.setattr(eval_worker, "_PROTECTION_REFRESH_SECONDS", 0.01)
    stop = threading.Event()
    t = threading.Thread(target=eval_worker._heartbeat, args=("eval-jobs", "rcpt", stop))
    t.start()
    time.sleep(0.1)
    stop.set()
    t.join(timeout=2)
    assert prot == []
