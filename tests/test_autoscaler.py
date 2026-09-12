"""L2 fleet planner (part of harness_dispatcher) — exact-design §5 + the original spec's §8
test list where it applies (compaction cap, survival applied, veto, +5% cap, decision emission,
exception isolation)."""

from __future__ import annotations

import json
import time
from typing import Any

from swebench_eval.orchestrator.control_plane.harness_dispatcher import (
    DECISION_KEY,
    Autoscaler,
    Budgets,
    DemandModel,
    DispatchDecision,
    TaskState,
    _DispatcherAdmission,
)


class FakeRedis:
    """Minimal sync-redis stand-in: strings + hashes + scan, enough for the planner's reads."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

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


def _progress(r: FakeRedis, i: int, turn: int, age_s: float = 0.0) -> None:
    r.strings[f"instance_progress:run1:inst{i}:1"] = json.dumps(
        {"turn_number": turn, "updated_at": time.time() - age_s}
    )


def _scaler(r: FakeRedis, mode: str = "observe", alias: str = "laguna-x") -> Autoscaler:
    return Autoscaler(model_alias=alias, mode=mode, redis_client=r, tick_interval_s=0.0)


class TestDemandModel:
    def test_compaction_caps_the_curve(self) -> None:
        """Spec §8 test 5: tokens_per_call(500) must equal the threshold, not the linear
        extrapolation (12,315 + 515×500 ≈ 269K > cap)."""
        d = DemandModel()
        assert d.tokens_per_call(500) == d.cap
        assert d.tokens_per_call(0) == d.a

    def test_survival_is_applied_mature_forecasts_lower_than_fresh(self) -> None:
        """Spec §8 test 7: a fleet near its median length must forecast LOWER than the same
        fleet fresh — deeper instances are (a) closer to done and (b) still survival-discounted."""
        r = FakeRedis()
        s = _scaler(r)
        fresh = [TaskState(turn=0, context_tokens=s.demand.tokens_per_call(0))] * 20
        # Same size fleet, deep — HIGHER per-call tokens but lower survival to the horizon.
        deep = [TaskState(turn=120, context_tokens=s.demand.tokens_per_call(120))] * 20
        _, _, qps_fresh, _ = s.project(fresh)
        _, _, qps_deep, _ = s.project(deep)
        # QPS is the clean survival check (per-call size cancels out of it): the deep cohort
        # must project FEWER call starts/s than the fresh one despite equal task count.
        assert qps_deep < qps_fresh


class TestGates:
    def test_fresh_many_small_fleet_binds_on_qps_not_tokens(self) -> None:
        """E12-E16's regime encoded: 60 fresh tasks bind the request axis long before tokens."""
        r = FakeRedis()
        s = _scaler(r)
        for i in range(60):
            _progress(r, i, turn=0)
        budgets = Budgets(r_tok=10_000_000, k_inflight=100_000_000, r_qps=1.5, source="test")
        fleet = s._scan_fleet()
        _, binding = s._ceiling_from_budgets(fleet, budgets)
        assert binding == "qps_budget"

    def test_deep_fleet_binds_on_tokens_not_qps(self) -> None:
        """E1's regime: deep tasks (~200K/call) bind arrival/inflight while QPS is loose."""
        r = FakeRedis()
        s = _scaler(r)
        for i in range(30):
            _progress(r, i, turn=400)  # at the compaction cap
        budgets = Budgets(r_tok=50_000, k_inflight=2_000_000, r_qps=100.0, source="test")
        fleet = s._scan_fleet()
        _, binding = s._ceiling_from_budgets(fleet, budgets)
        assert binding in ("arrival_budget", "inflight_budget")

    def test_progress_keys_are_kept_until_they_expire(self) -> None:
        """Forecast review 2026-09-03 §3: a key that EXISTS is a live-or-recently-live task.
        The old 120s staleness cut dropped every instance in a long tool call or a long pacer
        hold out of the projection — under-counting demand exactly under pressure. The
        writer's TTL (300s) is the liveness bound; ECS ground truth bounds the count."""
        r = FakeRedis()
        s = _scaler(r)
        _progress(r, 1, turn=10)
        _progress(r, 2, turn=10, age_s=200)  # long tool call / pacer hold — still ours
        assert len(s._scan_fleet()) == 2


class TestTick:
    def test_decision_is_published_every_tick_including_no_change(self) -> None:
        """Capacity-emission doc: the no-change tick is the most informative one."""
        r = FakeRedis()
        s = _scaler(r)
        s.maybe_tick(in_flight_tasks=0)
        raw = r.strings.get(DECISION_KEY)
        assert raw is not None
        d = json.loads(raw)
        assert d["mode"] == "observe"
        # The fallback level is always visible (§3.2). With the packaged fitted document the
        # unknown alias "laguna-x" (no harness) resolves to the document's pooled fit; with no
        # document at all it is the hard-coded pooled default — both say so.
        assert d["curve_source"].startswith("fitted:")
        assert "binding_constraint" in d
        assert "aliases" in d and "booting_tasks" in d  # forecast review: the "why" fields

        from swebench_eval.orchestrator.control_plane.demand_curves import DemandCurves

        bare = Autoscaler(
            model_alias="laguna-x",
            mode="observe",
            redis_client=FakeRedis(),
            tick_interval_s=0.0,
            curves=DemandCurves(None),
        )
        d2 = bare.maybe_tick(in_flight_tasks=0)
        assert d2 is not None and d2.curve_source == "pooled_default"

    def test_overload_in_window_forces_cooldown_and_zero_new_admission(self) -> None:
        """§5.2 veto + owner's §6.4: any overload in the complete window freezes admission —
        ceiling clamps to the CURRENT fleet (never below: we never kill, §5.5)."""
        r = FakeRedis()
        now_b = int(time.time() // 10)
        r.strings[f"overload:{{laguna-x}}:{now_b - 2}"] = "3"
        s = _scaler(r, mode="live")
        for i in range(4):
            _progress(r, i, turn=5)
        decision = s.maybe_tick(in_flight_tasks=4)
        assert decision is not None
        assert decision.binding_constraint == "cooldown"
        assert decision.desired_ceiling == 4  # frozen at current size, not zeroed
        allowed, reason = s.gate(4)
        assert allowed is False and reason == "cooldown"

    def test_observe_mode_never_blocks_even_in_cooldown(self) -> None:
        r = FakeRedis()
        now_b = int(time.time() // 10)
        r.strings[f"overload:{{laguna-x}}:{now_b - 2}"] = "3"
        s = _scaler(r, mode="observe")
        allowed, _ = s.gate(4)
        assert allowed is True  # observes and publishes, actuates nothing

    def test_paced_share_over_limit_blocks_with_hysteresis(self) -> None:
        """The pacer-empty invariant: >5% of admissions waiting >2s => stop launching; resume
        only after two consecutive clean windows."""
        r = FakeRedis()
        now_b = int(time.time() // 10)
        r.hashes[f"paced:{{laguna-x}}:{now_b - 1}"] = {"n": "100", "n_over_2s": "10", "sum_ms": "1"}
        s = _scaler(r, mode="live")
        # Generous budgets so the ONLY reason to hold is the paced signal (the fitted curves
        # are ~2x hungrier than the old pooled default, and the generic default r_tok is tiny).
        s._budgets = lambda *a, **k: Budgets(  # type: ignore[method-assign]
            r_tok=1e9, k_inflight=1e12, r_qps=1e6, source="test"
        )
        assert s.gate(2) == (False, "paced")
        # Pressure clears — but ONE clean tick must not resume (hysteresis).
        r.hashes.clear()
        s._last_tick_at = 0.0
        assert s.gate(2) == (False, "paced")
        s._last_tick_at = 0.0
        allowed, _ = s.gate(2)  # second consecutive clean window resumes
        assert allowed is True

    def test_growth_is_exactly_five_percent_never_more(self) -> None:
        """Owner-fixed: +5%, applied only at a predicted peak that arrived clean with high
        utilization — and in observe mode it lands in would_set, never in pacer:cfg."""
        r = FakeRedis()
        s = _scaler(r, mode="observe")
        budgets = Budgets(r_tok=100.0, k_inflight=1000.0, r_qps=1.0, source="test")
        s._budgets = lambda: budgets  # type: ignore[method-assign, assignment, misc]
        for i in range(3):
            _progress(r, i, turn=60)
        # Arm a peak that has already arrived; make utilization read high via a tiny budget.
        s._pending_peak = (time.monotonic() - 1, 999.0)
        decision = s.maybe_tick(in_flight_tasks=3)
        assert decision is not None
        assert decision.would_set  # intended, not applied
        assert decision.would_set["r_tok"] == 100.0 * 1.05
        assert decision.would_set["r_qps"] == 1.05
        assert "pacer:cfg:{laguna-x}" not in r.hashes  # observe mode wrote NOTHING

    def test_tick_failure_holds_last_decision_never_raises(self) -> None:
        """§8 exception-isolation contract: a planner bug degrades to the last good answer."""
        r = FakeRedis()
        s = _scaler(r)
        first = s.maybe_tick(0)
        assert first is not None
        s._scan_fleet = lambda: 1 / 0  # type: ignore[method-assign, assignment, return-value]
        s._last_tick_at = 0.0
        assert s.maybe_tick(0) is first  # held, not raised


class TestAdmissionWiring:
    def test_static_ceiling_still_binds_before_the_autoscaler(self, monkeypatch) -> None:
        """Min-wins (§6.7.1): the operator's static cap blocks regardless of what the planner
        would allow — and the autoscaler is consulted only below it."""
        from swebench_eval.control import state as control_state

        monkeypatch.setattr(control_state, "is_paused", lambda pool: False)

        class _NeverAsk:
            def gate(self, n: int):
                raise AssertionError("must not be consulted at static capacity")

        adm = _DispatcherAdmission(ceiling=3, autoscaler=_NeverAsk())  # type: ignore[arg-type]
        adm._gt_in_flight = 3
        adm._last_gt_at = time.time() + 3600  # suppress refresh
        decision = adm.may_launch()
        assert isinstance(decision, DispatchDecision)
        assert decision.allowed is False
        assert "at_capacity" in decision.reason

    def test_live_block_reason_flows_into_the_dispatch_decision(self, monkeypatch) -> None:
        from swebench_eval.control import state as control_state

        monkeypatch.setattr(control_state, "is_paused", lambda pool: False)

        class _Blocking:
            def gate(self, n: int):
                return False, "qps_budget"

        adm = _DispatcherAdmission(ceiling=100, autoscaler=_Blocking())  # type: ignore[arg-type]
        adm._gt_in_flight = 5
        adm._last_gt_at = time.time() + 3600
        decision = adm.may_launch()
        assert decision.allowed is False
        assert decision.reason == "qps_budget"


class TestBudgetStaleness:
    """F3 (exact-design review): E5 admitted 2.42M and E6 admitted 1.05M one minute later — the
    pool moves, so an old pacer:cfg must not get full-margin trust."""

    def _budgets_from(self, cfg: dict[str, str]) -> Budgets:
        r = FakeRedis()
        r.hashes["pacer:cfg:{laguna-x}"] = cfg
        return _scaler(r)._budgets()

    def test_fresh_cfg_gets_full_margins(self) -> None:
        b = self._budgets_from(
            {
                "r_tok": "50000",
                "k_inflight": "2000000",
                "r_qps": "1.5",
                "seeded_at": str(time.time()),
            }
        )
        assert b.source == "pacer_cfg"
        assert b.utilization_factor == 1.0

    def test_stale_cfg_halves_the_margins_and_says_so(self) -> None:
        old = time.time() - 7 * 3600  # past the 6h default
        b = self._budgets_from(
            {"r_tok": "50000", "k_inflight": "2000000", "r_qps": "1.5", "seeded_at": str(old)}
        )
        assert b.source == "pacer_cfg_stale"
        assert b.utilization_factor == 0.5
        assert b.age_s is not None and b.age_s > 6 * 3600

    def test_unknown_age_reads_as_stale_never_as_fresh(self) -> None:
        """A cfg with no seeded_at (pre-F3 writes, manual hset) must not be TRUSTED as fresh —
        unknown must never render as healthy, applied to time."""
        b = self._budgets_from({"r_tok": "50000", "k_inflight": "2000000", "r_qps": "1.5"})
        assert b.source == "pacer_cfg_stale"

    def test_stale_margins_bind_earlier(self) -> None:
        """The behavioral consequence: the same fleet that fits fresh margins trips the gate
        under stale ones."""
        r = FakeRedis()
        s = _scaler(r)
        for i in range(5):
            _progress(r, i, turn=60)
        fleet = s._scan_fleet()
        # F5 (2026-09-04): the default table is borrowed evidence, so the projection no longer
        # discounts by survival — five aged tasks project ~55K tok/s at the horizon. A 120K
        # budget fits them fresh (0.9x) with room to grow; the stale half-margin (0.45x) does not.
        fresh = Budgets(r_tok=120_000, k_inflight=5_000_000, r_qps=100.0, source="pacer_cfg")
        stale = Budgets(r_tok=120_000, k_inflight=5_000_000, r_qps=100.0, source="pacer_cfg_stale")
        ceiling_fresh, _ = s._ceiling_from_budgets(fleet, fresh)
        ceiling_stale, _ = s._ceiling_from_budgets(fleet, stale)
        assert ceiling_stale < ceiling_fresh
