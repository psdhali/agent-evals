"""Capacity observer (CAPACITY-AND-PIPELINE-VIEW-DESIGN-2026-08-31.md §3.1) — the observation
tick that is the ONE writer of ``capacity_snapshot``.

The properties under test are the design's non-negotiables: it runs with or without an
autoscaler (records folded when present, NULL when absent — never zero); eval's backlog
includes jobs that do not exist yet; ETAs are ranges with unknown-never-healthy semantics;
a quiet idle system writes nothing (a gap is correct); and no single failed source loses
the rest of the row.
"""

from __future__ import annotations

import json
import time
from typing import Any, Self

import pytest

from swebench_eval.orchestrator.control_plane.capacity_observer import (
    CapacityObserver,
    _eta_s,
)


class FakeRedis:
    """get/scan_iter/hgetall over a plain dict; hashes are nested dicts."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data or {}

    def get(self, key: str) -> Any:
        value = self.data.get(key)
        return value if not isinstance(value, dict) else None

    def hgetall(self, key: str) -> dict[str, str]:
        value = self.data.get(key)
        return dict(value) if isinstance(value, dict) else {}

    def scan_iter(self, pattern: str, count: int = 100) -> Any:
        prefix = pattern.rstrip("*")
        return iter([k for k in self.data if k.startswith(prefix)])


class FakeCursor:
    def __init__(self, executed: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._executed = executed

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._executed.append((sql, params))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.committed = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.executed)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def _progress_key(iid: str, updated_at: float) -> tuple[str, str]:
    return f"instance_progress:run-1:{iid}:1", json.dumps({"updated_at": updated_at})


def _decision(pool: str, **extra: Any) -> tuple[str, str]:
    record = {
        "decided_at": time.time() - 12.0,
        "mode": "observe",
        "desired_ceiling": 40,
        "binding_constraint": "arrival_budget",
        **extra,
    }
    return f"autoscaler:last_decision:{pool}", json.dumps(record)


def _observer(
    redis_data: dict[str, Any] | None = None,
    depths: dict[str, tuple[int, int]] | None = None,
    eval_running: int | None = 3,
    conn: FakeConn | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> CapacityObserver:
    if monkeypatch is not None:
        monkeypatch.setenv("CLUSTER", "eval-cluster")
    depths = depths if depths is not None else {"harness-jobs": (10, 2), "eval-jobs": (4, 1)}

    class FakeEcs:
        def describe_services(self, cluster: str, services: list[str]) -> dict[str, Any]:
            if eval_running is None:
                raise RuntimeError("ecs unreachable")
            return {"services": [{"runningCount": eval_running, "desiredCount": eval_running}]}

    observer = CapacityObserver(
        redis_client=FakeRedis(redis_data),
        ecs_client=FakeEcs(),
        sqs_depth_fn=lambda q: depths[q],
        conn_factory=(lambda: conn) if conn is not None else (lambda: FakeConn()),
        tick_interval_s=0.0,
    )
    # The per-run timeline sampler rides this tick (2026-09-04) and has its own suite
    # (test_run_timeline_sampler.py); off here so these tests see only the pool rows.
    observer.run_sampler.enabled = False
    return observer


# ---------------------------------------------------------------------------
# The ETA wave model.
# ---------------------------------------------------------------------------


class TestEta:
    def test_empty_and_unstaffed_is_zero(self) -> None:
        assert _eta_s(0, 0, 600.0) == 0

    def test_backlog_with_zero_workers_is_unknown_not_infinite(self) -> None:
        # Unknown must never render as healthy — and never as a fake number either.
        assert _eta_s(25, 0, 600.0) is None

    def test_wave_arithmetic(self) -> None:
        # 10 jobs / 4 workers = 3 waves, plus half a duration for the in-flight wave.
        assert _eta_s(10, 4, 100.0) == 350

    def test_no_backlog_but_workers_running_is_half_a_duration(self) -> None:
        assert _eta_s(0, 4, 100.0) == 50


# ---------------------------------------------------------------------------
# The read pass.
# ---------------------------------------------------------------------------


class TestObserve:
    def test_two_rows_one_per_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        obs = _observer(monkeypatch=monkeypatch).observe()
        assert [o.pool for o in obs] == ["harness", "eval"]

    def test_runs_without_any_autoscaler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """THE design rule: no decision records at all -> measured fields still land,
        autoscaler fields are None (not zero)."""
        now = time.time()
        data = dict([_progress_key("astropy-1", now), _progress_key("astropy-2", now)])
        obs = _observer(redis_data=data, monkeypatch=monkeypatch).observe()
        harness, eval_ = obs
        assert harness.queue_depth == 10
        assert harness.not_visible == 2
        assert harness.current_workers == 2
        assert harness.desired is None
        assert harness.binding_constraint is None
        assert harness.decision_age_s is None
        assert eval_.current_workers == 3

    def test_folds_decision_records_with_age(self, monkeypatch: pytest.MonkeyPatch) -> None:
        data = dict([_decision("harness"), _decision("eval", desired_ceiling=2)])
        obs = _observer(redis_data=data, monkeypatch=monkeypatch).observe()
        harness, eval_ = obs
        assert harness.desired == 40
        assert harness.binding_constraint == "arrival_budget"
        assert harness.decision_age_s is not None and 10.0 <= harness.decision_age_s <= 20.0
        assert eval_.desired == 2

    def test_constants_provenance_surfaces_defaults_visibly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§2.4 (wiring review): a chart full of decisions computed from GENERIC DEFAULTS
        must say so on every tick — records carry budgets.source; the observer must
        surface it, never drop it."""
        data = dict([_decision("harness", budgets={"source": "defaults", "r_tok": 20000})])
        obs = _observer(redis_data=data, monkeypatch=monkeypatch).observe()
        assert obs[0].constants_source == "defaults"
        assert obs[1].constants_source is None  # eval records have no budget constants

    def test_constants_source_none_when_no_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        obs = _observer(monkeypatch=monkeypatch).observe()
        assert obs[0].constants_source is None

    def test_stale_progress_keys_are_not_workers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = time.time()
        data = dict([_progress_key("live", now), _progress_key("dead", now - 600)])
        obs = _observer(redis_data=data, monkeypatch=monkeypatch).observe()
        assert obs[0].current_workers == 1

    def test_eval_backlog_includes_jobs_that_do_not_exist_yet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§2: every live harness instance becomes an eval job. 4 visible + 1 in flight +
        6 live harness = 11 jobs over 3 workers = 4 waves."""
        now = time.time()
        data = dict(_progress_key(f"i{n}", now) for n in range(6))
        monkeypatch.setenv("CAP_ETA_EVAL_MEDIAN_S", "100")
        obs = _observer(redis_data=data, monkeypatch=monkeypatch).observe()
        assert obs[1].eta_low_s == 450  # 4 waves x 100s + 50s in-flight allowance

    def test_eval_eta_unknown_when_service_unreadable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        obs = _observer(eval_running=None, monkeypatch=monkeypatch).observe()
        assert obs[1].current_workers is None
        assert obs[1].eta_low_s is None and obs[1].eta_high_s is None

    def test_eta_is_a_range_not_a_point(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = time.time()
        data = dict(_progress_key(f"i{n}", now) for n in range(4))
        obs = _observer(redis_data=data, monkeypatch=monkeypatch).observe()
        harness = obs[0]
        assert harness.eta_low_s is not None and harness.eta_high_s is not None
        assert harness.eta_high_s > harness.eta_low_s

    def test_failed_queue_read_nulls_depth_but_keeps_the_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def depth_fn(queue: str) -> tuple[int, int]:
            if queue == "harness-jobs":
                raise RuntimeError("sqs down")
            return (4, 1)

        observer = _observer(monkeypatch=monkeypatch)
        observer._sqs_depth_fn = depth_fn
        obs = observer.observe()
        assert obs[0].queue_depth is None and obs[0].not_visible is None
        assert obs[1].queue_depth == 4  # the other queue's read still landed

    def test_no_cluster_env_means_eval_workers_not_measured(self) -> None:
        obs = _observer().observe()  # CLUSTER unset
        assert obs[1].current_workers is None


class TestCeilingUtilization:
    def test_max_over_fresh_aliases(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = time.time()
        data = {
            "pacer:bucket:{laguna}": {"level": "500000", "upd": str(now)},
            "pacer:cfg:{laguna}": {"c_burst": "2000000"},
            "pacer:bucket:{qwen}": {"level": "1800000", "upd": str(now)},
            "pacer:cfg:{qwen}": {"c_burst": "2000000"},
        }
        obs = _observer(redis_data=data, monkeypatch=monkeypatch).observe()
        assert obs[0].ceiling_utilization == 0.75  # laguna: 1 - 500K/2M
        assert obs[1].ceiling_utilization is None  # tokens are a harness concept

    def test_stale_bucket_is_history_not_utilisation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        data = {
            "pacer:bucket:{laguna}": {"level": "0", "upd": str(time.time() - 600)},
            "pacer:cfg:{laguna}": {"c_burst": "2000000"},
        }
        obs = _observer(redis_data=data, monkeypatch=monkeypatch).observe()
        assert obs[0].ceiling_utilization is None

    def test_alias_without_cfg_is_skipped_not_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        data = {"pacer:bucket:{laguna}": {"level": "100", "upd": str(time.time())}}
        obs = _observer(redis_data=data, monkeypatch=monkeypatch).observe()
        assert obs[0].ceiling_utilization is None


# ---------------------------------------------------------------------------
# The write gate + the write itself.
# ---------------------------------------------------------------------------


class TestWrite:
    def test_writes_both_rows_while_a_run_is_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("swebench_eval.control.state.runs_active_marked", lambda: True)
        conn = FakeConn()
        observer = _observer(conn=conn, monkeypatch=monkeypatch)
        assert observer.maybe_tick() is not None
        assert len(conn.executed) == 2
        assert conn.committed and conn.closed
        sql, params = conn.executed[0]
        assert "INSERT INTO capacity_snapshot" in sql
        assert "gateway_headroom" not in sql  # permanently NULL — never written
        assert params[0] == "harness"
        assert conn.executed[1][1][0] == "eval"

    def test_quiet_idle_writes_nothing_a_gap_is_correct(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("swebench_eval.control.state.runs_active_marked", lambda: False)
        conn = FakeConn()
        observer = _observer(
            depths={"harness-jobs": (0, 0), "eval-jobs": (0, 0)},
            eval_running=0,
            conn=conn,
            monkeypatch=monkeypatch,
        )
        observer.maybe_tick()
        assert conn.executed == []

    def test_drain_tail_still_recorded_after_marker_expiry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The activity marker expires on TTL while the eval queue can still be full —
        observed activity alone must keep the record going."""
        monkeypatch.setattr("swebench_eval.control.state.runs_active_marked", lambda: False)
        conn = FakeConn()
        observer = _observer(
            depths={"harness-jobs": (0, 0), "eval-jobs": (37, 5)},
            eval_running=0,
            conn=conn,
            monkeypatch=monkeypatch,
        )
        observer.maybe_tick()
        assert len(conn.executed) == 2

    def test_write_failure_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("swebench_eval.control.state.runs_active_marked", lambda: True)

        def broken_factory() -> Any:
            raise RuntimeError("aurora down")

        observer = _observer(monkeypatch=monkeypatch)
        observer._conn_factory = broken_factory
        assert observer.maybe_tick() is None  # logged, swallowed, next tick retries

    def test_rate_limited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("swebench_eval.control.state.runs_active_marked", lambda: True)
        conn = FakeConn()
        observer = _observer(conn=conn, monkeypatch=monkeypatch)
        observer._tick_interval_s = 3600.0
        observer.maybe_tick()
        observer.maybe_tick()
        assert len(conn.executed) == 2  # second call inside the interval did nothing


class TestBackgroundThread:
    def test_start_stop_and_single_thread(self) -> None:
        observer = _observer()
        observer._tick_interval_s = 3600.0
        observer._last_tick_at = time.monotonic()  # keep the loop from actually ticking
        thread = observer.start_background()
        assert thread.is_alive() and thread.daemon
        assert observer.start_background() is thread  # idempotent — no second thread
        observer.stop_background()
        assert not thread.is_alive()

    def test_kill_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAPACITY_OBSERVER_ENABLED", "0")
        assert CapacityObserver().enabled is False
