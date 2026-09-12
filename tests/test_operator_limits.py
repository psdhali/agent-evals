"""Operator-adjustable limits (owner request 2026-09-04) — control_plane/operator_limits.py,
the /limits routes, and the two readers (the dispatcher's planner, the eval scaler)."""

from __future__ import annotations

import json
import time
from typing import Any, Self
from unittest import mock

import pytest

from swebench_eval.orchestrator.control_plane import operator_limits as ol
from swebench_eval.orchestrator.control_plane.run_launch import AUTOSCALER_OVERRIDES_KEY
from tests.test_autoscaler_forecast_review import BIG, FLAT, FakeRedis, _cfg, _progress, _scaler

ALIAS = "deepseek-v4-flash-0731-mini"
POOL = "deepseek-v4-flash-0731"


class Redis(FakeRedis):
    """The forecast-review fake plus the two verbs the limits writers use."""

    def __init__(self) -> None:
        super().__init__()
        self.expires: dict[str, int] = {}

    def hdel(self, key: str, *fields: str) -> int:
        h = self.hashes.get(key, {})
        n = 0
        for f in fields:
            if f in h:
                del h[f]
                n += 1
        return n

    def expire(self, key: str, ttl: int) -> None:
        self.expires[key] = ttl


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    monkeypatch.setattr(ol, "_audit", lambda *a: rows.append(a))
    return rows


# --- global scope -----------------------------------------------------------------------------


class TestGlobal:
    def test_set_read_clear_and_audit(self, audit: list[tuple[Any, ...]]) -> None:
        r = Redis()
        out = ol.set_global(r, "borrowed_curve_cap", 60, "preet", "the 500")
        assert out == {"field": "borrowed_curve_cap", "old": None, "new": "60"}
        assert ol.read_global(r) == {"borrowed_curve_cap": 60.0}
        ol.set_global(r, "growth_cap_factor", 3.0, "preet")
        assert ol.read_global(r)["growth_cap_factor"] == 3.0
        ol.set_global(r, "borrowed_curve_cap", None, "preet", "back to default")
        assert "borrowed_curve_cap" not in ol.read_global(r)
        assert [(a[0], a[2], a[3], a[4], a[5]) for a in audit] == [
            ("global", "borrowed_curve_cap", None, "60", "preet"),
            ("global", "growth_cap_factor", None, "3.0", "preet"),
            ("global", "borrowed_curve_cap", "60", None, "preet"),
        ]

    def test_validation(self, audit: list[tuple[Any, ...]]) -> None:
        r = Redis()
        with pytest.raises(ol.LimitError):
            ol.set_global(r, "nope", 1, "x")
        with pytest.raises(ol.LimitError):
            ol.set_global(r, "utilization", 1.5, "x")  # above hi
        with pytest.raises(ol.LimitError):
            ol.set_global(r, "borrowed_curve_cap", 2.5, "x")  # int field
        with pytest.raises(ol.LimitError):
            ol.set_global(r, "utilization", "abc", "x")
        assert audit == [] and r.hashes.get(ol.OPERATOR_LIMITS_KEY, {}) == {}

    def test_read_ignores_junk_and_failures(self) -> None:
        r = Redis()
        r.hashes[ol.OPERATOR_LIMITS_KEY] = {
            "utilization": "0.95",
            "junk": "1",
            "growth_cap_factor": "x",
        }
        assert ol.read_global(r) == {"utilization": 0.95}

        class Broken:
            def hgetall(self, key: str) -> None:
                raise ConnectionError("down")

        assert ol.read_global(Broken()) == {}

    def test_rehydrate_restores_the_latest_row_per_field_when_the_hash_is_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Cur:
            def __enter__(self) -> Self:
                return self

            def __exit__(self, *a: object) -> None:
                return None

            def execute(self, sql: str, params: Any = None) -> None:
                assert "DISTINCT ON (field)" in sql

            def fetchall(self) -> list[tuple[str, str | None]]:
                return [("borrowed_curve_cap", "80"), ("utilization", None), ("junk", "1")]

        class Conn:
            def cursor(self) -> Cur:
                return Cur()

            def close(self) -> None:
                return None

        monkeypatch.setattr(
            "swebench_eval.database.connection.get_connection", lambda: Conn(), raising=False
        )
        r = Redis()
        assert ol.rehydrate_global(r) == 1
        assert r.hashes[ol.OPERATOR_LIMITS_KEY] == {"borrowed_curve_cap": "80"}
        # a populated hash is left alone
        assert ol.rehydrate_global(r) == 0


# --- run scope --------------------------------------------------------------------------------


class TestRun:
    def test_encodings_match_the_launch_and_the_operator_marker_is_kept(
        self, audit: list[tuple[Any, ...]]
    ) -> None:
        r = Redis()
        r.hashes[AUTOSCALER_OVERRIDES_KEY] = {"run_id": "run-9", "max_parallel": "150"}
        ol.set_run(r, "max_parallel", 20, "preet", "drain")
        ol.set_run(r, "ceiling_override", 13, "preet", "take all 13")
        ol.set_run(r, "ramp_step_pct", 2.5, "preet")
        ol.set_run(r, "enabled", False, "preet")
        h = r.hashes[AUTOSCALER_OVERRIDES_KEY]
        assert h["max_parallel"] == "20" and h["ceiling_override"] == "13"
        assert h["ramp_step_pct"] == "2.5" and h["enabled"] == "0"
        assert h["_op:ceiling_override"] == "preet"
        assert audit[0][:5] == ("run", "run-9", "max_parallel", "150", "20")
        ol.set_run(r, "ceiling_override", None, "preet", "planner back")
        assert "ceiling_override" not in h and "_op:ceiling_override" not in h
        with pytest.raises(ol.LimitError):
            ol.set_run(r, "ramp_step_pct", 7, "x")  # 5 is the hard max

    def test_an_edit_before_any_launch_creates_the_hash_with_a_ttl(
        self, audit: list[tuple[Any, ...]]
    ) -> None:
        r = Redis()
        out = ol.set_run(r, "ceiling_override", 5, "preet")
        assert out["run_id"] == "" and r.expires[AUTOSCALER_OVERRIDES_KEY] > 0


# --- pacer scope ------------------------------------------------------------------------------


class TestPacer:
    def test_seed_rebase_restamp_and_pool_persist(
        self, audit: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = Redis()
        _cfg(r, ALIAS, r_tok=244_608.0, r_tok_seed=244_608.0, k_inflight=1e6, cached_weight=1.0)
        _cfg(r, POOL, r_tok=244_608.0, r_tok_seed=244_608.0)
        persisted: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            "swebench_eval.orchestrator.control_plane.pacer_seeds.persist_seeds",
            lambda *a, **k: persisted.append((a, k)),
        )
        before = time.time()
        out = ol.set_pacer(
            r, ALIAS, "r_tok", 600_000, "preet", "probe was a lower bound", also_pool=True
        )
        assert out["old"] == "244608.0" and out["new"] == "600000.0" and out["pool"] == POOL
        a = r.hashes[f"pacer:cfg:{{{ALIAS}}}"]
        assert a["r_tok"] == "600000.0" and a["r_tok_seed"] == "600000.0"
        assert float(a["seeded_at"]) >= before  # fresh evidence: full planner margins
        assert a["k_inflight"] == "1000000.0"  # untouched
        p = r.hashes[f"pacer:cfg:{{{POOL}}}"]
        assert p["r_tok"] == "600000.0" and p["r_tok_seed"] == "600000.0"
        ((args, kw),) = persisted
        # the row holds NUMBERS, not the hash's strings — a persisted "600000.0" rehydrates as
        # repr(str) = "'600000.0'" and nothing can read it (bit live 2026-09-07)
        assert args[0] == POOL and args[1]["r_tok"] == 600_000.0
        assert all(isinstance(v, (int, float)) for v in args[1].values())
        assert "seeded_at" not in args[1] or isinstance(args[1]["seeded_at"], float)
        assert kw["triggered_by"] == "operator:preet"
        assert [x[1] for x in audit] == [ALIAS, POOL]

    def test_cached_weight_has_no_seed_and_alias_only_by_default(
        self, audit: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = Redis()
        _cfg(r, ALIAS, cached_weight=1.0)
        called: list[Any] = []
        monkeypatch.setattr(
            "swebench_eval.orchestrator.control_plane.pacer_seeds.persist_seeds",
            lambda *a, **k: called.append(1),
        )
        out = ol.set_pacer(r, ALIAS, "cached_weight", 0.35, "preet")
        assert out["pool"] is None and called == []
        a = r.hashes[f"pacer:cfg:{{{ALIAS}}}"]
        assert a["cached_weight"] == "0.35" and "cached_weight_seed" not in a
        assert f"pacer:cfg:{{{POOL}}}" not in r.hashes
        with pytest.raises(ol.LimitError):
            ol.set_pacer(r, ALIAS, "cached_weight", 1.5, "x")
        with pytest.raises(ol.LimitError):
            ol.set_pacer(r, ALIAS, "r_tok", None, "x")  # never cleared


# --- the effective view -----------------------------------------------------------------------


class TestView:
    def test_sources_static_rails_and_pacer_rows(self, audit: list[tuple[Any, ...]]) -> None:
        r = Redis()
        r.hashes[AUTOSCALER_OVERRIDES_KEY] = {
            "run_id": "run-9",
            "max_parallel": "150",
            "set_at": "1000.0",
            "enabled": "1",
        }
        ol.set_run(r, "ceiling_override", 13, "preet")
        ol.set_global(r, "utilization", 0.95, "preet")
        _cfg(r, ALIAS, r_tok=1.0, k_inflight=2.0, r_qps=3.0, cached_weight=0.5)
        _cfg(r, POOL, r_tok=9.0)
        from swebench_eval.orchestrator.control_plane.decision_record import decision_key

        r.strings[decision_key("harness")] = json.dumps({"static_cap": 145})
        r.strings[decision_key("eval")] = json.dumps({"asg_max": 16, "max_workers": 64})

        v = ol.effective_view(r, [("mini_swe_agent", ALIAS)])
        run = v["run"]
        assert run["run_id"] == "run-9" and run["set_at"] == 1000.0
        assert run["fields"]["max_parallel"] == {
            "value": 150.0,
            "source": "run_launch",
            "set_by": None,
        }
        assert run["fields"]["ceiling_override"]["source"] == "operator"
        assert run["fields"]["ceiling_override"]["set_by"] == "preet"
        assert run["fields"]["cooldown_s"] == {"value": None, "source": "default", "set_by": None}
        assert run["fields"]["enabled"]["value"] == 1.0
        g = v["global"]["fields"]
        assert g["utilization"] == {"value": 0.95, "source": "operator", "set_by": None}
        assert g["borrowed_curve_cap"]["source"] == "default"
        assert v["static"]["max_concurrent_harness_tasks"] == 145
        assert v["static"]["eval_asg_max"] == 16 and v["static"]["eval_max_workers_env"] == 64
        (row,) = v["pacer"]
        assert row["alias"] == ALIAS and row["pool"] == POOL and row["harness"] == "mini_swe_agent"
        assert row["cfg"]["r_tok"] == 1.0 and row["cfg"]["cached_weight"] == 0.5
        assert row["pool_cfg"]["r_tok"] == 9.0
        assert {s["field"] for s in v["specs"] if s["scope"] == "pacer"} >= {
            "r_tok",
            "cached_weight",
        }


# --- the readers ------------------------------------------------------------------------------


class TestDispatcherReads:
    def test_ceiling_override_replaces_the_projection_but_not_the_caps(self) -> None:
        """5 projected with 13 queued (the deepseek morning): the operator sets 13 and the
        next tick's ceiling is 13; the static cap still min-wins; cooldown still holds."""
        r = Redis()
        _cfg(r, "laguna-x", **BIG)
        _progress(r, 1, turn=5)
        s = _scaler(r, demand=FLAT)
        base = s.maybe_tick(in_flight_tasks=1)
        assert base is not None and base.ceiling_override is None
        r.hashes[AUTOSCALER_OVERRIDES_KEY] = {"ceiling_override": "13", "model_alias": "laguna-x"}
        s._last_tick_at = 0.0
        d = s.maybe_tick(in_flight_tasks=1)
        assert d is not None and d.desired_ceiling == 13 and d.ceiling_override == 13
        # the hard cap still wins
        s.hard_cap_fn = lambda: 4
        s._last_tick_at = 0.0
        d = s.maybe_tick(in_flight_tasks=4)
        assert d is not None and d.desired_ceiling == 4 and d.binding_constraint == "static_cap"
        # an override at/below in-flight is a recorded hold of its own
        s.hard_cap_fn = None
        r.hashes[AUTOSCALER_OVERRIDES_KEY]["ceiling_override"] = "1"
        s._last_tick_at = 0.0
        d = s.maybe_tick(in_flight_tasks=1)
        assert d is not None and d.binding_constraint == "ceiling_override"
        assert s.gate(1) == (False, "ceiling_override")

    def test_global_knobs_land_on_the_planner_each_tick(self) -> None:
        r = Redis()
        _cfg(r, "laguna-x", **BIG)
        s = _scaler(r, demand=FLAT)
        s.maybe_tick(in_flight_tasks=0)
        assert (s._borrowed_curve_cap, s._growth_cap_factor, s._utilization) == (30, 1.5, 0.9)
        r.hashes[ol.OPERATOR_LIMITS_KEY] = {
            "borrowed_curve_cap": "80",
            "growth_cap_factor": "3.0",
            "utilization": "0.95",
        }
        s._last_tick_at = 0.0
        d = s.maybe_tick(in_flight_tasks=0)
        assert d is not None
        assert (s._borrowed_curve_cap, s._growth_cap_factor, s._utilization) == (80, 3.0, 0.95)
        assert d.operator_limits == {
            "borrowed_curve_cap": 80.0,
            "growth_cap_factor": 3.0,
            "utilization": 0.95,
        }
        # cleared -> defaults again
        r.hashes[ol.OPERATOR_LIMITS_KEY] = {}
        s._last_tick_at = 0.0
        s.maybe_tick(in_flight_tasks=0)
        assert (s._borrowed_curve_cap, s._growth_cap_factor, s._utilization) == (30, 1.5, 0.9)

    def test_growth_clamp_follows_the_operator_factor(self) -> None:
        from swebench_eval.orchestrator.control_plane.harness_dispatcher import Budgets

        r = Redis()
        r.hashes[ol.OPERATOR_LIMITS_KEY] = {"growth_cap_factor": "3.0"}
        b = Budgets(r_tok=145.0, k_inflight=1000.0, r_qps=1.0, source="t", r_tok_seed=100.0)
        s = _scaler(r, mode="observe", demand=FLAT)
        s._budgets = lambda *a, **k: b  # type: ignore[method-assign]
        for i in range(3):
            _progress(r, i, turn=60)
        s._pending_peak = (time.monotonic() - 1, 999.0)
        d = s.maybe_tick(in_flight_tasks=3)
        assert d is not None
        # 152.25 > 1.5 x 100 was refused before (test_autoscaler_scaling_review); under 3x it grows
        assert d.growth_clamped is False and d.would_set["r_tok"] == pytest.approx(152.25)


class TestEvalScalerReads:
    def test_max_workers_and_scale_in_from_the_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from swebench_eval.orchestrator.control_plane.eval_autoscaler import EvalAutoscaler

        monkeypatch.setenv("EVAL_MAX_WORKERS", "8")
        monkeypatch.setenv("EVAL_TASK_SCALE_IN_S", "300")
        r = Redis()
        s = EvalAutoscaler(mode="observe", redis_client=r, tick_interval_s=30.0)
        assert s._operator_limits() == (8, 10, {})
        r.hashes[ol.OPERATOR_LIMITS_KEY] = {"eval_max_workers": "32", "eval_task_scale_in_s": "60"}
        mw, ticks, lim = s._operator_limits()
        assert (mw, ticks) == (32, 2) and lim["eval_max_workers"] == 32.0
        r.hashes[ol.OPERATOR_LIMITS_KEY] = {"eval_max_workers": "0"}  # never below min_workers
        assert s._operator_limits()[0] == s.min_workers


# --- the routes -------------------------------------------------------------------------------


def _client():
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    return TestClient(app)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, audit: list[tuple[Any, ...]]) -> Redis:
    from swebench_eval.database import redis_client as redis_reads

    r = Redis()
    monkeypatch.setattr(redis_reads, "is_redis_reachable", lambda: True)
    monkeypatch.setattr(redis_reads, "_get_client", lambda: r)
    return r


class TestRoutes:
    def test_get_limits_without_a_run(self, wired: Redis) -> None:
        resp = _client().get("/limits")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "ok" and body["pacer"] == []
        assert body["run"]["fields"]["ceiling_override"]["source"] == "default"
        assert any(s["field"] == "eval_max_workers" for s in body["specs"])

    def test_get_limits_is_unknown_when_redis_is_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from swebench_eval.database import redis_client as redis_reads

        monkeypatch.setattr(redis_reads, "is_redis_reachable", lambda: False)
        body = _client().get("/limits").json()
        assert body["state"] == "unknown" and body["run"] is None

    def test_get_limits_with_a_run_lists_its_pacer_aliases(
        self, wired: Redis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from swebench_eval.orchestrator.api import main as api_main
        from swebench_eval.orchestrator.api import queries

        monkeypatch.setattr(api_main, "_db", lambda: mock.Mock())
        monkeypatch.setattr(queries, "get_run_status", lambda conn, run_id: "running")
        monkeypatch.setattr(
            queries, "list_run_targets", lambda conn, run_id: [("mini_swe_agent", ALIAS)]
        )
        _cfg(wired, ALIAS, r_tok=5.0)
        body = _client().get("/limits", params={"run_id": "run-9"}).json()
        (row,) = body["pacer"]
        assert row["alias"] == ALIAS and row["pool"] == POOL and row["cfg"]["r_tok"] == 5.0

    def test_post_run_global_pacer_and_validation(
        self, wired: Redis, monkeypatch: pytest.MonkeyPatch, audit: list[tuple[Any, ...]]
    ) -> None:
        c = _client()
        resp = c.post(
            "/limits/run",
            json={
                "field": "ceiling_override",
                "value": 13,
                "actor": "preet",
                "reason": "13 queued",
            },
        )
        assert resp.status_code == 200 and resp.json()["new"] == "13"
        assert wired.hashes[AUTOSCALER_OVERRIDES_KEY]["ceiling_override"] == "13"
        resp = c.post("/limits/global", json={"field": "borrowed_curve_cap", "value": 60})
        assert resp.status_code == 200 and wired.hashes[ol.OPERATOR_LIMITS_KEY] == {
            "borrowed_curve_cap": "60"
        }
        resp = c.post("/limits/global", json={"field": "borrowed_curve_cap", "value": None})
        assert resp.status_code == 200 and resp.json()["new"] is None
        monkeypatch.setattr(
            "swebench_eval.orchestrator.control_plane.pacer_seeds.persist_seeds",
            lambda *a, **k: None,
        )
        resp = c.post(
            f"/limits/pacer/{ALIAS}",
            json={"field": "cached_weight", "value": 0.35, "actor": "preet", "also_pool": True},
        )
        assert resp.status_code == 200 and resp.json()["pool"] == POOL
        assert wired.hashes[f"pacer:cfg:{{{POOL}}}"]["cached_weight"] == "0.35"
        resp = c.post("/limits/run", json={"field": "ramp_step_pct", "value": 9})
        assert resp.status_code == 400 and "maximum" in resp.json()["detail"]
        resp = c.post("/limits/global", json={"field": "nope", "value": 1})
        assert resp.status_code == 400
        assert len(audit) == 5  # run, global, global-clear, pacer alias, pacer pool
