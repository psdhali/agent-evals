"""L2 planner — the forecast-review fixes (BUILDER4-DISPATCHER-FORECAST-REVIEW-2026-09-03.md
§3): booting tasks modelled, per-alias budgets + curves, hold-cap timeouts and the live wait
queue as back-pressure, growth arming when the peak is now, the fitted-curve loader's fallback
chain, and the launch stagger."""

from __future__ import annotations

import json
import time
from typing import Any

from swebench_eval.orchestrator.control_plane.demand_curves import DemandCurves
from swebench_eval.orchestrator.control_plane.harness_dispatcher import (
    Autoscaler,
    DemandModel,
)


class FakeRedis:
    """strings + hashes + zsets (member -> score), enough for every planner read."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    def scan_iter(self, pattern: str, count: int = 100):
        prefix = pattern.rstrip("*")
        return iter([k for k in self.strings if k.startswith(prefix)])

    def get(self, key: str):
        v = self.strings.get(key)
        return v.encode() if v is not None else None

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.strings[key] = value

    def hgetall(self, key: str):
        return {k.encode(): v.encode() for k, v in self.hashes.get(key, {}).items()}

    def hset(self, key: str, mapping: dict[str, Any]) -> None:
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})

    def mget(self, keys: list[str]):
        return [self.strings.get(k) for k in keys]

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def zrange(self, key: str, start: int, end: int, withscores: bool = False):
        ordered = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        sl = ordered[start : (None if end == -1 else end + 1)]
        return [(m.encode(), s) for m, s in sl] if withscores else [m.encode() for m, _ in sl]


def _progress(
    r: FakeRedis, i: int, turn: int, *, alias: str | None = None, harness: str | None = None
) -> None:
    r.strings[f"instance_progress:run1:inst{i}:1"] = json.dumps(
        {
            "turn_number": turn,
            "updated_at": time.time(),
            "model_alias": alias,
            "harness": harness,
        }
    )


def _cfg(r: FakeRedis, alias: str, **cfg: float) -> None:
    r.hset(f"pacer:cfg:{{{alias}}}", mapping={**cfg, "seeded_at": time.time()})


BIG = {"c_burst": 1e9, "r_tok": 1e9, "k_inflight": 1e12, "c_req": 1e6, "r_qps": 1e6}
FLAT = DemandModel(a=10_000, b=0, latency_s=1.0, turn_period_s=10.0, survival={0: 1.0})


def _scaler(r: FakeRedis, alias: str = "laguna-x", mode: str = "live", **kw: Any) -> Autoscaler:
    return Autoscaler(model_alias=alias, mode=mode, redis_client=r, tick_interval_s=0.0, **kw)


class TestBootingTasks:
    def test_ecs_in_flight_above_the_fleet_is_modelled_and_the_ceiling_is_based_on_it(
        self,
    ) -> None:
        """6 in flight per ECS, 2 with progress keys: 4 are booting. The ceiling must be
        >= 6 (never "at_capacity" against a fleet the projection cannot see), and the booting
        tasks' coming demand must be in the forecast (a fresh task is 10K tok / 10s here)."""
        r = FakeRedis()
        _cfg(r, "laguna-x", **BIG)
        _progress(r, 1, turn=5)
        _progress(r, 2, turn=5)
        s = _scaler(r, demand=FLAT)
        d = s.maybe_tick(in_flight_tasks=6)
        assert d is not None
        assert d.booting_tasks == 4
        assert d.observed_tasks == 2
        assert d.desired_ceiling >= 6
        # 6 tasks x 10K/10s = 6K tok/s projected (the 4 booting ones included)
        assert d.projected_arrival_tok_s >= 5_900
        assert s.gate(6)[0] is True  # not artificially held

    def test_a_tight_budget_holds_when_the_booting_tasks_alone_fill_it(self) -> None:
        """No flood: with r_tok = 3 tasks' worth, 6 in flight (4 booting) is already over — the
        planner must NOT permit more just because only 2 have written progress."""
        r = FakeRedis()
        _cfg(r, "laguna-x", **{**BIG, "r_tok": 3 * 1000.0})  # 3 x (10K/10s)
        _progress(r, 1, turn=5)
        _progress(r, 2, turn=5)
        s = _scaler(r, demand=FLAT)
        d = s.maybe_tick(in_flight_tasks=6)
        assert d is not None and d.desired_ceiling == 6  # base = ECS count, extra = 0
        assert d.binding_constraint == "arrival_budget"
        assert s.gate(6) == (False, "arrival_budget")


class TestPressureSignals:
    def test_a_hold_cap_timeout_in_the_window_is_pressure_even_with_a_clean_share(self) -> None:
        r = FakeRedis()
        _cfg(r, "laguna-x", **BIG)
        now_b = int(time.time() // 10)
        r.hashes[f"paced:{{laguna-x}}:{now_b - 1}"] = {
            "n": "100",
            "n_over_2s": "0",
            "n_timeout": "1",
        }
        s = _scaler(r, demand=FLAT)
        d = s.maybe_tick(in_flight_tasks=2)
        assert d is not None and d.binding_constraint == "paced" and d.paced_timeouts == 1
        assert s.gate(2) == (False, "paced")

    def test_a_wait_queue_head_denied_long_enough_is_pressure(self) -> None:
        r = FakeRedis()
        _cfg(r, "laguna-x", **BIG)
        r.zsets["pacer:waitq:{laguna-x}"] = {"c1": 1.0, "c2": 2.0}
        r.hashes["pacer:waitest:{laguna-x}"] = {
            "c1": f"130000:{time.time() - 12}",  # head, denied 12s ago
            "c2": f"1000:{time.time() - 1}",
        }
        s = _scaler(r, demand=FLAT)
        d = s.maybe_tick(in_flight_tasks=2)
        assert d is not None
        assert d.queue_len == 2 and d.queue_head_wait_s >= 11
        assert d.binding_constraint == "paced"

    def test_a_brief_queue_is_not_pressure(self) -> None:
        r = FakeRedis()
        _cfg(r, "laguna-x", **BIG)
        r.zsets["pacer:waitq:{laguna-x}"] = {"c1": 1.0}
        r.hashes["pacer:waitest:{laguna-x}"] = {"c1": f"1000:{time.time() - 1}"}
        s = _scaler(r, demand=FLAT)
        d = s.maybe_tick(in_flight_tasks=2)
        assert d is not None and d.queue_len == 1
        assert d.binding_constraint != "paced"


class TestMultiAlias:
    def test_each_alias_gets_its_own_budget_and_the_ceiling_is_the_sum(self) -> None:
        """Two aliases: A budgets 3 tasks, B budgets 100. Fleet: 1 on A, 1 on B. The per-alias
        record shows A binding on arrival_budget; the dispatcher ceiling is A's + B's."""
        r = FakeRedis()
        # 3 tasks x 1K tok/s = 3000 <= 0.9 x 3500 = 3150; 4 would be 4000 -> ceiling 3.
        # (Exercises the refined +1 search: the old step-of-5 search returned 1 here.)
        _cfg(r, "pool-A", **{**BIG, "r_tok": 3500.0})
        _cfg(r, "pool-B", **BIG)
        _progress(r, 1, turn=5, alias="pool-A", harness="codex")
        _progress(r, 2, turn=5, alias="pool-B", harness="codex")
        s = _scaler(r, alias="pool-A", demand=FLAT)
        d = s.maybe_tick(in_flight_tasks=2)
        assert d is not None
        assert set(d.aliases) == {"pool-A", "pool-B"}
        assert d.aliases["pool-A"]["ceiling"] == 3
        assert d.aliases["pool-B"]["ceiling"] > 50
        assert d.desired_ceiling == d.aliases["pool-A"]["ceiling"] + d.aliases["pool-B"]["ceiling"]
        assert d.binding_constraint == "none"  # A is not AT its ceiling yet (1 of 3)
        assert d.aliases["pool-A"]["budgets_source"] == "pacer_cfg"

    def test_an_overload_on_any_alias_freezes_the_whole_fleet(self) -> None:
        r = FakeRedis()
        _cfg(r, "pool-A", **BIG)
        _cfg(r, "pool-B", **BIG)
        _progress(r, 1, turn=5, alias="pool-A")
        _progress(r, 2, turn=5, alias="pool-B")
        now_b = int(time.time() // 10)
        r.strings[f"overload:{{pool-B}}:{now_b - 2}"] = "1"  # the OTHER alias
        s = _scaler(r, alias="pool-A", demand=FLAT)
        d = s.maybe_tick(in_flight_tasks=2)
        assert d is not None and d.binding_constraint == "cooldown"
        assert d.aliases["pool-B"]["overloads"] == 1 and d.aliases["pool-A"]["overloads"] == 0


class TestGrowthArming:
    def test_peak_now_still_arms_a_reconciliation(self) -> None:
        """A mature fleet's projected peak is NOW (peak_at == 0). The old `peak_at > 0` guard
        meant growth could never fire at steady state; it must arm for the next tick."""
        r = FakeRedis()
        _cfg(r, "laguna-x", **BIG)
        _progress(r, 1, turn=100)
        s = _scaler(r, mode="observe", demand=FLAT)
        d = s.maybe_tick(in_flight_tasks=1)
        assert d is not None and d.peak_at_s == 0.0
        assert s._pending_peak is not None


class TestCurveLoader:
    def _doc(self) -> dict[str, Any]:
        g = {
            "a": 30_000,
            "b": 300,
            "n": 500,
            "insufficient": False,
            "latency_s": 1.5,
            "turn_period_s": 6.0,
            "survival": {"0": 0.9, "40": 0.8},
            "survival_attempts": {"0": 40, "40": 20},
        }
        thin = {**g, "n": 10, "insufficient": True, "survival_attempts": {"0": 3}}
        return {
            "fitted_at": "x",
            "groups": {
                "laguna-xs-2.1|codex": g,
                "laguna-xs-2.1|mini_swe_agent": {**g, "a": 4_000, "b": 850},
                "other-pool|codex": {**g, "a": 99_999, "turn_period_s": 99.0},
                "laguna-xs-2.1|opencode": thin,
            },
            "pooled": {**g, "a": 17_000, "b": 458, "survival": {"0": 0.8}},
        }

    def test_exact_group_wins_and_carries_its_constants(self) -> None:
        m = DemandCurves(self._doc()).model_for("laguna-xs-2.1", "codex")
        assert m.source == "fitted:laguna-xs-2.1|codex"
        assert m.tokens_per_call(10) == 30_000 + 300 * 10
        assert m.call_latency_s(123_456) == 1.5 and m.turn_period_s(123_456) == 6.0
        assert m.survival_table == {0: 0.9, 40: 0.8}

    def test_unknown_pool_falls_to_the_same_harness_elsewhere_for_growth_only(self) -> None:
        """qwen has no runs yet: token growth per turn is harness behaviour (take codex's
        curve from laguna), but latency/period are provider behaviour — with no qwen group
        at all they fall to pooled and the source says so."""
        m = DemandCurves(self._doc()).model_for("qwen3-coder-next", "codex")
        assert m.source.startswith("fitted:harness:laguna-xs-2.1|codex")
        assert "latency/period" in m.source
        assert m.a == 30_000

    def test_thin_group_falls_through_and_survival_needs_enough_attempts(self) -> None:
        m = DemandCurves(self._doc()).model_for("laguna-xs-2.1", "opencode")
        # opencode's own group is insufficient -> another laguna harness's growth is NOT used
        # for a different harness... the chain goes: exact (no) -> same harness elsewhere (no
        # other opencode) -> pooled.
        assert m.source == "fitted:pooled"
        assert m.a == 17_000

    def test_no_document_means_pooled_default_and_says_so(self) -> None:
        m = DemandCurves(None).model_for("laguna-xs-2.1", "codex")
        assert m.source == "pooled_default"

    def test_packaged_document_loads_and_names_its_origin(self) -> None:
        c = DemandCurves.load()
        assert c.origin == "packaged"
        assert c.fitted_at
        m = c.model_for("laguna-xs-2.1", "claude_code")
        # The 2026-09-03 refit: claude_code's PROMPT (input + cache reads), not its uncached
        # slice — tens of thousands of tokens at turn 0, never ~2K. A per-field fallback
        # (e.g. survival on too few attempts -> pooled) is allowed and must be NAMED.
        assert m.source.startswith("fitted:laguna-xs-2.1|claude_code")
        assert m.tokens_per_call(0) > 20_000


class TestLaunchStagger:
    def test_stagger_is_jittered_around_the_env_base(self, monkeypatch) -> None:
        monkeypatch.setenv("LAUNCH_STAGGER_S", "2.0")
        s = _scaler(FakeRedis(), demand=FLAT)
        xs = [s.launch_stagger_s() for _ in range(200)]
        assert all(1.0 <= x <= 3.0 for x in xs)
        assert max(xs) - min(xs) > 0.5  # actually jittered
        monkeypatch.setenv("LAUNCH_STAGGER_S", "0")
        assert s.launch_stagger_s() == 0.0
