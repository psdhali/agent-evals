"""GET /runs/{run_id}/live — Redis per-instance progress over Postgres (F11).

BUILDER1-EXPORT-AND-LIVE-ENDPOINTS-2026-08-31.md §2.  The shim writes
per-turn progress to Redis on every serviced turn; nothing reads it.  This
endpoint enumerates the run's non-terminal attempts from Postgres and reads
each one's TTL'd key.

The part that is easy to get wrong, and what these tests pin:

* a missing key has >=3 causes (not started / TTL expired / worker died) and
  must NOT all render as "0 turns" — explicit per-instance state (running /
  pending / stale);
* if Redis itself is unreachable, the WHOLE response is ``state="unknown"``,
  never an empty list that reads as "nothing is running".

Integration against the real compose Postgres + Redis is in
``test_run_live_integration.py``; these unit tests pin the state machine the
route feeds and the fail-closed shapes.
"""

from __future__ import annotations

from typing import Any, Literal, Self
from unittest import mock

from fastapi.testclient import TestClient

from swebench_eval.orchestrator.api import queries
from swebench_eval.orchestrator.api.main import app

# ── scripted Postgres boundary (same shape as tests/test_dashboard_api.py) ──


class _FakeCursor:
    def __init__(self, conn: _FakeConn, results: list[dict[str, Any]]) -> None:
        self.conn = conn
        self._results = results
        self._i = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        self._i = len(self._results)
        return self._results

    def fetchone(self) -> dict[str, Any] | None:
        if self._i < len(self._results):
            row = self._results[self._i]
            self._i += 1
            return row
        return None

    def close(self) -> None:
        return None


class _FakeConn:
    """Serves one scripted result set per cursor() open, oldest first."""

    def __init__(self, *result_sets: list[dict[str, Any]]) -> None:
        self._queue: list[list[dict[str, Any]]] = list(result_sets)
        self.executed: list[tuple[str, Any]] = []

    def cursor(self, cursor_factory: Any = None) -> _FakeCursor:
        results = self._queue.pop(0) if self._queue else []
        return _FakeCursor(self, results)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def _attempts(*rows: dict[str, Any]) -> list[dict[str, Any]]:
    return list(rows)


# ── queries.list_active_attempts — SQL shape ────────────────────────────────


def test_list_active_attempts_uses_active_states_and_running_flag() -> None:
    """The query must filter to _ACTIVE_STATES and compute `running` from
    whether the pair ever reached a RUNNING state — both pinned so the live
    endpoint's pending/stale split cannot silently drift from the SQL."""
    conn = _FakeConn(
        [
            {"instance_id": "a", "attempt_number": 1, "running": True},
            {"instance_id": "b", "attempt_number": 1, "running": False},
        ]
    )
    out = queries.list_active_attempts(conn, "run-1")
    sql, params = conn.executed[0]
    assert "state IN %s" in sql
    assert params == ("run-1", queries._ACTIVE_STATES)
    assert "bool_or(state IN ('HARNESS_RUNNING', 'EVAL_RUNNING')) AS running" in sql
    assert out[0]["running"] is True
    assert out[1]["running"] is False


# ── the route ───────────────────────────────────────────────────────────────


from contextlib import contextmanager


@contextmanager
def _patch_redis(reachable: bool, payloads: dict[tuple[str, int], dict[str, Any] | None]):
    """Patch the redis reads the route makes (reachability + per-key reads).

    The route binds ``redis_client`` to a local name and calls the module
    attributes, so patching the module's attributes intercepts it.
    """
    with (
        mock.patch(
            "swebench_eval.database.redis_client.is_redis_reachable", return_value=reachable
        ),
        mock.patch(
            "swebench_eval.database.redis_client.read_progress",
            side_effect=lambda run_id, iid, attempt: payloads.get((iid, attempt)),
        ),
    ):
        yield


def test_run_live_404_when_run_missing() -> None:
    conn = _FakeConn([])  # no status row -> None
    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn):
        resp = TestClient(app).get("/runs/nope/live")
    assert resp.status_code == 404


def test_run_live_running_instance_carries_progress_and_age() -> None:
    conn = _FakeConn(
        [{"status": "running"}],
        [{"instance_id": "scikit-learn__scikit-learn-25102", "attempt_number": 1, "running": True}],
    )
    payload = {
        "run_id": "run-1",
        "instance_id": "scikit-learn__scikit-learn-25102",
        "attempt_number": 1,
        "turn_number": 31,
        "input_tokens": 19126,
        "output_tokens": 19525,
        "cached_tokens": 1024,
        "reasoning_tokens": 1826,
        "cost_usd": 0.42,
        "updated_at": 1234567890.0,
    }
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        _patch_redis(True, {("scikit-learn__scikit-learn-25102", 1): payload}),
    ):
        resp = TestClient(app).get("/runs/run-1/live")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-1"
    assert body["status"] == "running"
    assert body["state"] == "ok"
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["state"] == "running"
    assert item["turn_number"] == 31
    assert item["input_tokens"] == 19126
    assert item["output_tokens"] == 19525
    assert item["cached_tokens"] == 1024
    assert item["reasoning_tokens"] == 1826
    assert item["cost_usd"] == 0.42
    assert item["observed_at"] == 1234567890.0
    assert isinstance(item["age_s"], float)


def test_run_live_missing_key_pending_vs_stale() -> None:
    """A missing key must NOT render as "0 turns".  An attempt that reached a
    RUNNING state but has no key is `stale` (TTL expired / worker died); one
    still PENDING/DISPATCHED is `pending` (not started).  All numbers None."""
    conn = _FakeConn(
        [{"status": "running"}],
        [
            {"instance_id": "a__a-1", "attempt_number": 1, "running": True},
            {"instance_id": "b__b-1", "attempt_number": 1, "running": False},
        ],
    )
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        _patch_redis(True, {}),  # reachable, no keys
    ):
        resp = TestClient(app).get("/runs/run-1/live")
    body = resp.json()
    by_id = {i["instance_id"]: i for i in body["items"]}
    stale = by_id["a__a-1"]
    pending = by_id["b__b-1"]
    assert stale["state"] == "stale"
    assert pending["state"] == "pending"
    for i in (stale, pending):
        assert i["turn_number"] is None
        assert i["input_tokens"] is None
        assert i["output_tokens"] is None
        assert i["cost_usd"] is None
        assert i["observed_at"] is None
        assert i["age_s"] is None


def test_run_live_redis_unreachable_is_whole_response_unknown() -> None:
    """If Redis itself is unreachable the WHOLE response is explicitly
    unknown, never an empty list that reads as "nothing is running"."""
    conn = _FakeConn(
        [{"status": "running"}],
        [{"instance_id": "a__a-1", "attempt_number": 1, "running": True}],
    )
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        _patch_redis(False, {}),
    ):
        resp = TestClient(app).get("/runs/run-1/live")
    body = resp.json()
    assert body["state"] == "unknown"
    assert body["reason"] == "redis_unreachable"
    assert body["items"] == []


def test_run_live_terminal_run_returns_empty_ok_list() -> None:
    """A run with no active attempts (e.g. aborted / finished) returns an
    empty list with state="ok" — the honest "nothing in flight" that is
    distinct from the redis-unreachable "unknown"."""
    conn = _FakeConn(
        [{"status": "aborted"}],
        [],  # no active attempts
    )
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        _patch_redis(True, {}),
    ):
        resp = TestClient(app).get("/runs/run-1/live")
    body = resp.json()
    assert body["state"] == "ok"
    assert body["reason"] == ""
    assert body["items"] == []


def test_run_live_eval_phase_payload_carries_grade_progress() -> None:
    """2026-09-06: the eval worker's heartbeat publishes the grade's exec-tee
    snapshot under the same key; the route passes the eval fields through and
    the harness fields read as not-measured (None), never 0."""
    conn = _FakeConn(
        [{"status": "running"}],
        [{"instance_id": "django__django-10097", "attempt_number": 1, "running": True}],
    )
    payload = {
        "run_id": "run-1",
        "instance_id": "django__django-10097",
        "attempt_number": 1,
        "phase": "eval",
        "turn_number": None,
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "reasoning_tokens": None,
        "cost_usd": None,
        "eval_elapsed_s": 1830.5,
        "eval_lines": 3412,
        "eval_last_line": "test_broken_pipe_errors ... ok",
        "updated_at": 1234567890.0,
    }
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        _patch_redis(True, {("django__django-10097", 1): payload}),
    ):
        resp = TestClient(app).get("/runs/run-1/live")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["state"] == "running"
    assert item["phase"] == "eval"
    assert item["eval_lines"] == 3412
    assert item["eval_elapsed_s"] == 1830.5
    assert item["eval_last_line"] == "test_broken_pipe_errors ... ok"
    assert item["turn_number"] is None and item["cost_usd"] is None


# ── 2026-09-08: reaped-then-regraded attempts, silence ─────────────────────────


def test_list_reaped_attempts_sql_shape() -> None:
    conn = _FakeConn([{"instance_id": "a", "attempt_number": 1}])
    out = queries.list_reaped_attempts(conn, "run-1")
    sql, params = conn.executed[0]
    assert "bool_or(state = 'ABANDONED')" in sql and "NOT bool_or(state IN %s)" in sql
    assert params == ("run-1", queries._ACTIVE_STATES)
    assert out == [{"instance_id": "a", "attempt_number": 1}]


def test_run_live_shows_a_reaped_attempt_only_while_its_key_is_live() -> None:
    """The SIGTERM-handback case: Postgres says ABANDONED, a worker is still grading under
    the same attempt and publishing progress. Live while the key is there; gone when not."""
    conn = _FakeConn(
        [{"status": "running"}],
        [],  # nothing non-terminal
        [{"instance_id": "django__django-10097", "attempt_number": 1}],  # reaped
    )
    payload = {
        "phase": "eval",
        "eval_elapsed_s": 4020.0,
        "eval_lines": 12512,
        "eval_last_line": "test_admin ... ok",
        "eval_silent_s": 0.0,
        "updated_at": 1234567890.0,
    }
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        _patch_redis(True, {("django__django-10097", 1): payload}),
    ):
        body = TestClient(app).get("/runs/run-1/live").json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["state"] == "running" and item["revived_after_reap"] is True
    assert item["phase"] == "eval" and item["eval_lines"] == 12512
    assert item["eval_silent_s"] == 0.0

    conn = _FakeConn(
        [{"status": "running"}],
        [],
        [{"instance_id": "django__django-10097", "attempt_number": 1}],
    )
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        _patch_redis(True, {}),
    ):
        body = TestClient(app).get("/runs/run-1/live").json()
    assert body["items"] == []  # terminal AND silent: not live


def test_run_live_active_attempt_is_never_flagged_revived() -> None:
    conn = _FakeConn(
        [{"status": "running"}],
        [{"instance_id": "a", "attempt_number": 1, "running": True}],
        [],
    )
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        _patch_redis(True, {("a", 1): {"updated_at": 1.0, "eval_silent_s": 95.5}}),
    ):
        body = TestClient(app).get("/runs/run-1/live").json()
    assert body["items"][0]["revived_after_reap"] is False
    assert body["items"][0]["eval_silent_s"] == 95.5
