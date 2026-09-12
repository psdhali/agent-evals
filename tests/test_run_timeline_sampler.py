"""The per-run timeline sampler (timeline plan §4.2, 2026-09-04).

Properties: ONE progress-key scan yields both the fleet count and the per-run sums; stale
keys count as stale and contribute nothing; a field no key carried is None (never 0); one
row per ACTIVE run per tick and nothing for an idle system; every source guarded — a failed
read leaves its columns NULL and the row still lands; the tick never raises.
"""

from __future__ import annotations

import json
import time
from typing import Any, Self

import pytest

from swebench_eval.orchestrator.control_plane import run_timeline
from swebench_eval.orchestrator.control_plane.run_timeline import (
    RunProgressAggregate,
    RunTimelineSampler,
    scan_progress,
)


class FakeRedis:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def scan_iter(self, pattern: str, count: int = 100) -> Any:
        prefix = pattern.rstrip("*")
        return iter([k for k in self.data if k.startswith(prefix)])


class FakeCursor:
    def __init__(self, conn: FakeConn, results: list[Any]) -> None:
        self.conn = conn
        self._results = results
        self._i = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        pass

    def execute(self, sql: str, params: Any = None) -> None:
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError(f"scripted failure on {self.conn.fail_on}")
        self.conn.executed.append((sql, params))

    def fetchall(self) -> list[Any]:
        self._i = len(self._results)
        return self._results

    def fetchone(self) -> Any:
        if self._i < len(self._results):
            row = self._results[self._i]
            self._i += 1
            return row
        return None


class FakeConn:
    """One scripted result set per cursor open, oldest first (the queries module opens one
    cursor per _rows/_one call; the sampler's own SQL opens one each)."""

    def __init__(self, *result_sets: list[Any], fail_on: str | None = None) -> None:
        self._queue = list(result_sets)
        self.executed: list[tuple[str, Any]] = []
        self.committed = 0
        self.closed = False
        self.fail_on = fail_on

    def cursor(self, cursor_factory: Any = None) -> FakeCursor:
        results = self._queue.pop(0) if self._queue else []
        return FakeCursor(self, results)

    def commit(self) -> None:
        self.committed += 1

    def close(self) -> None:
        self.closed = True


def _key(run: str, iid: str, attempt: int = 1) -> str:
    return f"instance_progress:{run}:{iid}:{attempt}"


def _payload(age_s: float, **fields: Any) -> str:
    return json.dumps({"updated_at": time.time() - age_s, **fields})


# ── the scan ──────────────────────────────────────────────────────────────────


def test_scan_splits_the_fleet_count_per_run_and_sums_only_live_keys() -> None:
    now = time.time()
    data = {
        _key("run-a", "x"): _payload(5, input_tokens=100, output_tokens=10, cost_usd=0.01),
        _key("run-a", "y"): _payload(20, input_tokens=200, cost_usd=0.02),
        _key("run-a", "z"): _payload(500, input_tokens=999, cost_usd=9.9),  # stale
        _key("run-b", "q"): _payload(1),  # live, no metering fields
        "instance_progress:garbage": "{}",
        _key("run-c", "bad"): "not json",
    }
    fleet, per_run = scan_progress(FakeRedis(data), now=now)
    assert fleet == 3
    a = per_run["run-a"]
    assert (a.live, a.stale) == (2, 1)
    assert a.tokens["input_tokens"] == 300
    assert a.tokens["output_tokens"] == 10  # only one live key carried it
    assert a.tokens["cached_tokens"] is None  # no key carried it: not measured, never 0
    assert a.cost_usd == pytest.approx(0.03)
    b = per_run["run-b"]
    assert (b.live, b.stale) == (1, 0)
    assert b.cost_usd is None and b.tokens["input_tokens"] is None
    assert "run-c" not in per_run


# ── the row ───────────────────────────────────────────────────────────────────


class _View:
    harness_paused = True
    eval_paused = False
    gateway_paused = False
    stale = False


def _progress_conn(*, landed_cost: float | None, fail_on: str | None = None) -> FakeConn:
    # Cursor order for sample(): get_run_progress opens two (status row, phase rows);
    # _landed opens one; list_run_targets opens one (pacer).
    return FakeConn(
        [{"status": "running", "summary": json.dumps({"expected": 13, "denominator": 5})}],
        [
            {"phase": "harness", "state": "PENDING", "count": 4},
            {"phase": "harness", "state": "DISPATCHED", "count": 1},
            {"phase": "harness", "state": "HARNESS_RUNNING", "count": 3},
            {"phase": "harness", "state": "ABORTED_IN_FLIGHT", "count": 1},
            {"phase": "eval", "state": "RESOLVED", "count": 3},
            {"phase": "eval", "state": "UNRESOLVED", "count": 1},
            {"phase": "eval", "state": "EVAL_RUNNING", "count": 1},
        ],
        [(landed_cost, 500, 50, 300, 5, 1 if landed_cost is not None else 0, 1)],
        [],  # run_targets: none -> no pacer aliases
        fail_on=fail_on,
    )


def test_sample_assembles_counts_landed_plus_live_and_control_flags() -> None:
    conn = _progress_conn(landed_cost=0.5)
    sampler = RunTimelineSampler(
        redis_client=FakeRedis({}), conn_factory=lambda: conn, control_reader=lambda: _View()
    )
    agg = RunProgressAggregate(live=3, stale=1, cost_usd=0.25)
    agg.tokens["input_tokens"] = 1000

    row = sampler.sample(conn, "run-a", "running", agg)

    assert row["in_flight"] == 3 and row["stale"] == 1
    assert row["pending"] == 5 and row["harness_running"] == 3 and row["eval_running"] == 1
    assert row["resolved"] == 3 and row["unresolved"] == 1 and row["aborted"] == 1
    assert row["expected"] == 13 and row["denominator"] == 5
    assert row["cost_usd_landed"] == 0.5
    assert row["cost_usd_live"] == pytest.approx(0.75)
    assert row["tok_in"] == 1500  # landed 500 + live 1000
    assert row["tok_out"] == 50  # landed only
    assert row["harness_paused"] is True and row["control_stale"] is False
    assert row["counts"][0] == {"phase": "harness", "state": "PENDING", "count": 4}
    assert row["pacer"] == {}


def test_sample_leaves_null_when_a_source_fails_and_still_returns_a_row() -> None:
    conn = _progress_conn(landed_cost=None, fail_on="instance_results")  # both reads fail

    def _broken_control() -> Any:
        raise RuntimeError("valkey down")

    sampler = RunTimelineSampler(
        redis_client=FakeRedis({}), conn_factory=lambda: conn, control_reader=_broken_control
    )
    row = sampler.sample(conn, "run-a", "running", None)
    assert row["run_id"] == "run-a"
    assert row["in_flight"] == 0 and row["stale"] == 0  # the scan saw no key for it
    assert row["pending"] is None and row["resolved"] is None  # progress read failed
    assert row["cost_usd_live"] is None and row["tok_in"] is None
    assert row["harness_paused"] is None and row["control_stale"] is None


# ── the tick ──────────────────────────────────────────────────────────────────


def test_tick_writes_one_row_per_active_run_and_nothing_when_idle() -> None:
    idle = FakeConn([])  # SELECT active runs -> none
    sampler = RunTimelineSampler(redis_client=FakeRedis({}), conn_factory=lambda: idle)
    assert sampler.tick({}) == []
    assert not any("INSERT" in sql for sql, _ in idle.executed)
    assert idle.closed

    conn = FakeConn(
        [("run-a", "running"), ("run-b", "aborting")],
        # run-a: progress (2 cursors), landed, targets
        [{"status": "running", "summary": "{}"}],
        [{"phase": "harness", "state": "HARNESS_RUNNING", "count": 2}],
        [(0.1, 10, 1, 0, 0, 1, 1)],
        [],
        # run-b
        [{"status": "aborting", "summary": "{}"}],
        [],
        [(None, None, None, None, None, 0, 0)],
        [],
    )
    sampler = RunTimelineSampler(
        redis_client=FakeRedis({}), conn_factory=lambda: conn, control_reader=lambda: _View()
    )
    rows = sampler.tick({"run-a": RunProgressAggregate(live=2)})
    assert [r["run_id"] for r in rows] == ["run-a", "run-b"]
    inserts = [p for sql, p in conn.executed if "INSERT INTO run_timeline_tick" in sql]
    assert len(inserts) == 2
    assert inserts[0][0] == "run-a" and inserts[0][2] == 2  # run_id, in_flight
    assert inserts[1][0] == "run-b" and inserts[1][2] == 0
    assert conn.committed == 1 and conn.closed
    assert sampler.rows_written == 2


def test_tick_never_raises_and_honours_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> Any:
        raise RuntimeError("aurora unreachable")

    sampler = RunTimelineSampler(redis_client=FakeRedis({}), conn_factory=_boom)
    assert sampler.tick({}) == []

    broken = FakeConn([("run-a", "running")], fail_on="INSERT INTO run_timeline_tick")
    sampler = RunTimelineSampler(
        redis_client=FakeRedis({}), conn_factory=lambda: broken, control_reader=lambda: _View()
    )
    assert sampler.tick({}) == []  # the write failed; logged, not raised
    assert broken.closed

    monkeypatch.setenv("RUN_TIMELINE_ENABLED", "0")
    off = RunTimelineSampler(redis_client=FakeRedis({}), conn_factory=_boom)
    assert off.enabled is False and off.tick({}) == []


def test_observer_hands_the_scan_to_the_sampler_on_its_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observer's live-worker count and the sampler's per-run split come from ONE scan."""
    from tests.test_capacity_observer import FakeConn as ObsConn
    from tests.test_capacity_observer import _observer

    now = time.time()
    redis_data = {
        _key("run-a", "x"): _payload(1, cost_usd=0.5),
        _key("run-b", "y"): _payload(1),
        "autoscaler:last_decision:harness": json.dumps(
            {"decided_at": now, "mode": "live", "desired_ceiling": 5, "binding_constraint": "paced"}
        ),
    }
    obs = _observer(redis_data=redis_data, conn=ObsConn(), monkeypatch=monkeypatch)
    seen: list[dict[str, RunProgressAggregate]] = []

    def _tick(per_run: dict[str, RunProgressAggregate]) -> list[dict[str, Any]]:
        seen.append(per_run)
        return []

    monkeypatch.setattr(obs.run_sampler, "tick", _tick)
    observations = obs.maybe_tick()
    assert observations is not None
    assert observations[0].current_workers == 2
    assert set(seen[0]) == {"run-a", "run-b"}
    assert seen[0]["run-a"].cost_usd == 0.5
    assert isinstance(run_timeline.STALE_PROGRESS_S, float)
