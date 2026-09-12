"""GET /runs/{run_id}/timeline + the pause/resume event record (timeline plan §4.3/§4.4)."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from tests.test_dashboard_api import _FakeConn


def _client() -> Any:
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    return TestClient(app)


def test_timeline_route_is_registered() -> None:
    from swebench_eval.orchestrator.api.main import app

    assert "/runs/{run_id}/timeline" in {getattr(r, "path", None) for r in app.routes}


def test_timeline_404s_for_an_unknown_run() -> None:
    conn = _FakeConn([])  # get_run_stamps -> None
    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn):
        resp = _client().get("/runs/nope/timeline")
    assert resp.status_code == 404


def test_timeline_returns_every_source_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    from swebench_eval.orchestrator.api import queries

    stamps = {
        "run_id": "run-1",
        "status": "running",
        "created_at": "2026-09-06T17:00:00+00:00",
        "dispatched_at": "2026-09-06T17:00:30+00:00",
        "stop_requested_at": None,
        "stop_scope": None,
        "stop_reason": None,
        "stopped_at": None,
        "finalised_at": None,
    }
    calls: dict[str, Any] = {}
    monkeypatch.setattr(queries, "get_run_stamps", lambda conn, run_id: stamps)
    monkeypatch.setattr(
        queries, "list_run_targets", lambda conn, run_id: [("mini", "deepseek-v4-flash-0731-mini")]
    )
    monkeypatch.setattr(
        queries,
        "list_timeline_ticks",
        lambda conn, run_id, since=None: calls.setdefault("ticks", [{"ts": "t1", "in_flight": 3}]),
    )
    monkeypatch.setattr(
        queries,
        "list_capacity_between",
        lambda conn, start, end, since=None: calls.setdefault(
            "cap", [{"ts": "t1", "pool": "harness", "start": start, "end": end}]
        ),
    )
    monkeypatch.setattr(
        queries, "list_run_events_between", lambda conn, run_id, s, e: [{"kind": "pause"}]
    )
    monkeypatch.setattr(
        queries, "list_limit_edits_between", lambda conn, run_id, s, e: [{"field": "r_tok"}]
    )
    monkeypatch.setattr(
        queries,
        "list_discovery_between",
        lambda conn, pools, s, e: calls.setdefault("pools", pools) and [{"event_type": "step"}],
    )
    monkeypatch.setattr(
        queries, "list_lane_rows", lambda conn, run_id: [{"instance_id": "i", "attempt_number": 1}]
    )
    monkeypatch.setattr(queries, "list_run_calls", lambda conn, run_id: [{"call_index": 0}])

    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=_FakeConn([])):
        resp = _client().get("/runs/run-1/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_start"] == stamps["dispatched_at"]  # dispatch, not creation
    assert body["window_end"] is None  # still open
    assert body["ticks"] == [{"ts": "t1", "in_flight": 3}]
    assert body["capacity"][0]["end"] is None
    assert body["events"] == [{"kind": "pause"}]
    assert body["limit_edits"] == [{"field": "r_tok"}]
    assert body["discovery"] == [{"event_type": "step"}]
    assert calls["pools"] == ["deepseek-v4-flash-0731"]  # per-harness alias -> POOL
    assert body["lane_rows"] and body["calls"] == [{"call_index": 0}]
    assert body["targets"] == [{"harness": "mini", "model_alias": "deepseek-v4-flash-0731-mini"}]

    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=_FakeConn([])):
        light = _client().get("/runs/run-1/timeline?include_calls=false").json()
    assert light["calls"] == []


def test_pause_and_resume_record_a_run_event_with_the_pools() -> None:
    conn = _FakeConn([], [{"updated_at": None}], [])
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        mock.patch("swebench_eval.orchestrator.api.main.control_state.set_pause") as set_pause,
    ):
        resp = _client().post("/control/pause?reason=checking&actor=preet", json=["harness"])
    assert resp.status_code == 200
    inserts = [(sql, p) for sql, p in conn.executed if "INSERT INTO run_events" in sql]
    assert len(inserts) == 1
    assert inserts[0][1][:4] == (None, "pause", "preet", "checking")
    assert '"pools": ["harness"]' in inserts[0][1][4]
    set_pause.assert_called_once()

    conn = _FakeConn([], [{"updated_at": None}], [])
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        mock.patch("swebench_eval.orchestrator.api.main.control_state.set_pause"),
    ):
        resp = _client().post("/control/resume", json=["harness", "eval"])
    assert resp.status_code == 200
    inserts = [p for sql, p in conn.executed if "INSERT INTO run_events" in sql]
    assert inserts[0][1] == "resume"


def test_run_events_record_never_raises() -> None:
    from swebench_eval.orchestrator.control_plane import run_events

    class _Broken:
        def cursor(self) -> Any:
            raise RuntimeError("no table")

    assert run_events.record(_Broken(), "pause") is False
