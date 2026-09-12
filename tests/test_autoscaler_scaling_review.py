"""L2 planner — the scaling-review fixes (BUILDER4-SCALING-REVIEW-2026-09-03.md F2, F4, F5, F6):
growth bounded to the discovered seed, overload RECOVERY lowering budgets, the borrowed-curve
cap + discovery latency floor, the static cap as a recorded verdict, and the record fields."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from swebench_eval.orchestrator.control_plane.demand_curves import DemandCurves
from swebench_eval.orchestrator.control_plane.harness_dispatcher import (
    DECISION_KEY,
    Autoscaler,
    Budgets,
    DemandModel,
    _DispatcherAdmission,
)
from tests.test_autoscaler_forecast_review import BIG, FLAT, FakeRedis, _cfg, _progress, _scaler

ALIAS = "laguna-x"


def _paced_bucket(r: FakeRedis, alias: str, ago: int, **fields: int) -> None:
    now_b = int(time.time() // 10)
    r.hashes[f"paced:{{{alias}}}:{now_b - ago}"] = {k: str(v) for k, v in fields.items()}


def _overload(r: FakeRedis, alias: str, ago: int = 1, n: int = 1) -> None:
    now_b = int(time.time() // 10)
    r.strings[f"overload:{{{alias}}}:{now_b - ago}"] = str(n)


# -- F2: growth is bounded relative to the discovered seed -------------------------------------


class TestGrowthClamp:
    def _armed(self, r: FakeRedis, budgets: Budgets, mode: str = "observe") -> Autoscaler:
        s = _scaler(r, mode=mode, demand=FLAT)
        s._budgets = lambda *a, **k: budgets  # type: ignore[method-assign]
        for i in range(3):
            _progress(r, i, turn=60)
        s._pending_peak = (time.monotonic() - 1, 999.0)
        return s

    def test_growth_within_the_cap_proceeds(self) -> None:
        r = FakeRedis()
        b = Budgets(r_tok=140.0, k_inflight=1000.0, r_qps=1.0, source="t", r_tok_seed=100.0)
        d = self._armed(r, b).maybe_tick(in_flight_tasks=3)
        assert d is not None
        assert d.would_set["r_tok"] == pytest.approx(147.0)  # <= 1.5 x 100
        assert d.growth_clamped is False

    def test_growth_past_1_5x_the_seed_is_refused_and_recorded(self) -> None:
        """The reviewer's ratchet: r_tok drifted to 145 on sporadic 429s; +5% would be 152.25,
        above 1.5 x the 100 discovered clean. Refused, recorded, nothing written."""
        r = FakeRedis()
        b = Budgets(r_tok=145.0, k_inflight=1000.0, r_qps=1.0, source="t", r_tok_seed=100.0)
        s = self._armed(r, b, mode="live")
        d = s.maybe_tick(in_flight_tasks=3)
        assert d is not None
        assert d.would_set == {}
        assert d.growth_clamped is True
        assert d.growth_applied is False
        assert f"pacer:cfg:{{{ALIAS}}}" not in r.hashes

    def test_a_cfg_with_no_seed_adopts_its_current_values_as_the_seed_on_first_growth(self) -> None:
        r = FakeRedis()
        b = Budgets(r_tok=100.0, k_inflight=1000.0, r_qps=1.0, source="pacer_cfg")
        d = self._armed(r, b, mode="live").maybe_tick(in_flight_tasks=3)
        assert d is not None and d.growth_applied is True
        cfg = r.hashes[f"pacer:cfg:{{{ALIAS}}}"]
        assert float(cfg["r_tok"]) == pytest.approx(105.0)
        assert float(cfg["r_tok_seed"]) == 100.0  # stashed, so the next growth is bounded
        assert float(cfg["r_qps_seed"]) == 1.0


# -- F2: overload recovery lowers the budget ---------------------------------------------------


class TestOverloadRecovery:
    def _wire(self, r: FakeRedis, mode: str) -> Autoscaler:
        _cfg(
            r,
            ALIAS,
            **{
                **BIG,
                "r_tok": 1000.0,
                "r_tok_seed": 1000.0,
                "r_qps": 10.0,
                "r_qps_seed": 10.0,
            },
        )
        _progress(r, 1, turn=5)
        # The pacer's ledger for the last 60 s: 48,000 tokens / 120 calls admitted
        # -> measured 800 tok/s, 2 calls/s.
        _paced_bucket(r, ALIAS, 1, n=60, tok=24_000)
        _paced_bucket(r, ALIAS, 2, n=60, tok=24_000)
        _overload(r, ALIAS)
        return _scaler(r, mode=mode, demand=FLAT)

    def test_live_lowers_r_tok_to_85pct_of_the_measured_arrival_and_floors_r_qps(self) -> None:
        r = FakeRedis()
        s = self._wire(r, "live")
        d = s.maybe_tick(in_flight_tasks=1)
        assert d is not None and d.binding_constraint == "cooldown"
        rec = d.recovery_set[ALIAS]
        assert rec["measured"] is True and rec["basis_tok_s"] == 800.0
        assert rec["r_tok"] == [1000.0, 680.0]  # 0.85 x 800, above the 500 floor
        assert rec["r_qps"] == [10.0, 5.0]  # 0.85 x 2 = 1.7 -> floored at 0.5 x seed
        assert rec["applied"] is True
        cfg = r.hashes[f"pacer:cfg:{{{ALIAS}}}"]
        assert float(cfg["r_tok"]) == 680.0 and float(cfg["r_qps"]) == 5.0
        assert float(cfg["r_tok_seed"]) == 1000.0  # the seed itself is never touched

    def test_observe_records_the_intended_write_and_touches_nothing(self) -> None:
        r = FakeRedis()
        s = self._wire(r, "observe")
        d = s.maybe_tick(in_flight_tasks=1)
        assert d is not None
        assert d.recovery_set[ALIAS]["r_tok"] == [1000.0, 680.0]
        assert d.recovery_set[ALIAS]["applied"] is False
        assert float(r.hashes[f"pacer:cfg:{{{ALIAS}}}"]["r_tok"]) == 1000.0

    def test_recovery_fires_once_per_cooldown_episode(self) -> None:
        r = FakeRedis()
        s = self._wire(r, "live")
        first = s.maybe_tick(in_flight_tasks=1)
        s._last_tick_at = 0.0
        second = s.maybe_tick(in_flight_tasks=1)  # still in cooldown, overload still in window
        assert first is not None and first.recovery_set
        assert second is not None and second.recovery_set == {}
        assert second.binding_constraint == "cooldown"

    def test_without_a_pacer_ledger_the_projection_is_the_basis(self) -> None:
        r = FakeRedis()
        _cfg(r, ALIAS, **{**BIG, "r_tok": 5000.0, "r_tok_seed": 5000.0})
        _progress(r, 1, turn=5)  # FLAT: 10K tok / 10 s = 1000 tok/s projected
        _overload(r, ALIAS)
        d = _scaler(r, mode="live", demand=FLAT).maybe_tick(in_flight_tasks=1)
        assert d is not None
        rec = d.recovery_set[ALIAS]
        assert rec["measured"] is False
        assert rec["r_tok"] == [5000.0, 2500.0]  # 0.85 x 1000 = 850 -> floored at 0.5 x seed


# -- F4: borrowed curves ---------------------------------------------------------------------


class TestBorrowedCurve:
    def test_is_own_curve_vocabulary(self) -> None:
        assert DemandModel(source="fitted:laguna-xs-2.1|codex").is_own_curve()
        assert DemandModel(source="fitted:laguna-xs-2.1|codex+survival:pooled").is_own_curve()
        assert not DemandModel(source="fitted:harness:laguna-xs-2.1|codex").is_own_curve()
        assert not DemandModel(source="fitted:pooled").is_own_curve()
        assert not DemandModel(source="pooled_default").is_own_curve()

    def test_latency_floor_is_prefill_proportional_and_never_lowers(self) -> None:
        base = DemandModel(latency_s=1.7, source="fitted:harness:x|codex")
        m = base.with_latency_floor(6.0, 262_144)
        assert m.call_latency_s(30_000) == pytest.approx(1.7)  # 6 x 30/262 = 0.69 < 1.7
        assert m.call_latency_s(262_144) == pytest.approx(6.0)
        assert m.source.endswith("+latency:discovery_floor")

    def test_an_alias_on_a_borrowed_curve_is_capped_and_floored(self) -> None:
        r = FakeRedis()
        _cfg(r, "qwen-pool", **{**BIG, "latency_s_max_context": 6.0})
        _progress(r, 1, turn=5, alias="qwen-pool", harness="custom_minimal")
        s = _scaler(r, alias="qwen-pool", curves=DemandCurves(None))  # no fit for any pool
        d = s.maybe_tick(in_flight_tasks=1)
        assert d is not None
        a = d.aliases["qwen-pool"]
        assert a["curve_borrowed"] is True
        assert a["ceiling"] == 30 and a["binding"] == "borrowed_curve"
        assert a["curve_source"].endswith("+latency:discovery_floor")
        assert d.desired_ceiling == 30

    def test_an_alias_with_its_own_fit_is_not_capped(self) -> None:
        r = FakeRedis()
        _cfg(r, "laguna-xs-2.1", **BIG)
        _progress(r, 1, turn=5, alias="laguna-xs-2.1", harness="codex")
        s = _scaler(r, alias="laguna-xs-2.1")  # packaged curves: laguna-xs-2.1|codex is fitted
        d = s.maybe_tick(in_flight_tasks=1)
        assert d is not None
        a = d.aliases["laguna-xs-2.1"]
        assert a["curve_borrowed"] is False
        assert a["ceiling"] > 30

    def test_the_cap_never_sits_below_the_fleet(self) -> None:
        r = FakeRedis()
        _cfg(r, "qwen-pool", **BIG)
        for i in range(40):
            _progress(r, i, turn=5, alias="qwen-pool", harness="codex")
        s = _scaler(r, alias="qwen-pool", curves=DemandCurves(None))
        d = s.maybe_tick(in_flight_tasks=45)  # 40 with keys + 5 booting
        assert d is not None and d.desired_ceiling == 45


# -- F5/F6: the static cap is a recorded verdict; the record carries what a replay needs ------


class TestStaticCapVerdict:
    def test_a_cap_bound_hold_ticks_publishes_and_names_static_cap(self, monkeypatch) -> None:
        from swebench_eval.control import state as control_state

        monkeypatch.setattr(control_state, "is_paused", lambda pool: False)
        r = FakeRedis()
        _cfg(r, ALIAS, **BIG)
        for i in range(3):
            _progress(r, i, turn=5)
        s = _scaler(r, demand=FLAT)
        adm = _DispatcherAdmission(ceiling=3, autoscaler=s)
        adm._refresh = lambda: None  # type: ignore[method-assign]
        adm._gt_in_flight, adm._last_gt_at = 3, time.time()

        decision = adm.may_launch()
        assert decision.allowed is False and "at_capacity" in decision.reason
        d = s._last_decision
        assert d is not None
        assert d.static_cap == 3
        assert d.binding_constraint == "static_cap"
        assert d.desired_ceiling == 3
        assert DECISION_KEY in r.strings  # published, not silent
        assert s.gate(3) == (False, "static_cap")

    def test_record_carries_c_burst_and_the_growth_flags(self) -> None:
        r = FakeRedis()
        _cfg(r, ALIAS, **{**BIG, "c_burst": 123_456.0})
        s = _scaler(r, demand=FLAT)
        d = s.maybe_tick(in_flight_tasks=0)
        assert d is not None
        assert d.budgets.c_burst == 123_456.0
        rec: dict[str, Any] = json.loads(r.strings[DECISION_KEY])
        for key in ("static_cap", "growth_applied", "growth_clamped", "recovery_set"):
            assert key in rec
        assert rec["budgets"]["c_burst"] == 123_456.0


class TestBorrowedPeriodFloor:
    """2026-09-04 (deepseek x mini, 1M window): the borrowed laguna mini curve carries a
    MEASURED 6.0 s turn period (1.4 s of it laguna's latency). Flooring only the latency left
    the period at 6 s on a pool whose max-context call takes 47 s — 236K tokens every 6 s per
    task, 39K tok/s, 2.5x the physical maximum for a serial agent — and the 13-instance run's
    ceiling came out at 5. The period is now floored too: the borrowed pool's non-LLM time
    plus THIS pool's floored latency."""

    def test_period_is_floored_by_this_pools_latency(self) -> None:
        base = DemandModel(latency_s=1.446, turn_period_s=6.01, source="fitted:harness:l|mini")
        m = base.with_latency_floor(46.7, 1_046_576)
        # small prompt: floor 46.7 x 30K/1.05M = 1.34 s < 1.446 -> nothing changes
        assert m.call_latency_s(30_000) == pytest.approx(1.446)
        assert m.turn_period_s(30_000) == pytest.approx(6.01)
        # near the borrowed curve's cap (236K): latency 10.5 s, period = 4.56 + 10.5 = 15.1 s
        assert m.call_latency_s(235_929) == pytest.approx(46.7 * 235_929 / 1_046_576, rel=1e-6)
        assert m.turn_period_s(235_929) == pytest.approx(6.01 - 1.446 + m.call_latency_s(235_929))
        # the per-task arrival at that context is bounded by tokens / period
        assert 235_929 / m.turn_period_s(235_929) < 16_000
        assert 235_929 / 6.01 > 39_000  # what the un-floored period projected

    def test_period_never_drops_below_the_borrowed_one(self) -> None:
        base = DemandModel(latency_s=5.0, turn_period_s=20.0, source="fitted:harness:l|mini")
        m = base.with_latency_floor(2.0, 262_144)  # this pool is FASTER than the borrowed one
        assert m.turn_period_s(262_144) == pytest.approx(20.0)

    def test_pool_window_lookup(self) -> None:
        from swebench_eval.orchestrator.control_plane.harness_dispatcher import (
            _MAX_CONTEXT_TOKENS,
            _pool_window_tokens,
        )

        assert _pool_window_tokens("deepseek-v4-flash-0731-mini") == 1_048_576
        assert _pool_window_tokens("qwen3-coder-next-mini") == 262_144
        assert _pool_window_tokens("not-an-alias") == _MAX_CONTEXT_TOKENS
        assert _pool_window_tokens(None) == _MAX_CONTEXT_TOKENS
