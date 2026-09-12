"""L2 planner — BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04 F4/F5/F6: growth while paced when
the pool shows no overload, survival = 1 under thin or borrowed evidence, growth cadence on the
stabilisation window, and CLAMPED logged once per episode."""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest

from swebench_eval.orchestrator.control_plane.demand_curves import DemandCurves
from swebench_eval.orchestrator.control_plane.harness_dispatcher import (
    _SURVIVAL_MIN_ATTEMPTS,
    Autoscaler,
    Budgets,
    DemandModel,
    TaskState,
)
from tests.test_autoscaler_forecast_review import BIG, FLAT, FakeRedis, _cfg, _progress, _scaler
from tests.test_autoscaler_scaling_review import _overload, _paced_bucket

ALIAS = "laguna-x"


def _armed(r: FakeRedis, budgets: Budgets, mode: str = "observe") -> Autoscaler:
    s = _scaler(r, mode=mode, demand=FLAT)
    s._budgets = lambda *a, **k: budgets  # type: ignore[method-assign]
    for i in range(3):
        _progress(r, i, turn=60)
    s._pending_peak = (time.monotonic() - 1, 999.0)
    return s


# -- F4: growth while paced, when the provider is not the one pushing back --------------------


class TestGrowthWhilePaced:
    def test_paced_with_zero_overloads_still_reconciles_growth(self) -> None:
        r = FakeRedis()
        _paced_bucket(r, ALIAS, ago=1, n=100, n_over_2s=10)  # >5% waited >2s: paced
        b = Budgets(r_tok=100.0, k_inflight=1000.0, r_qps=1.0, source="t", r_tok_seed=100.0)
        d = _armed(r, b).maybe_tick(in_flight_tasks=3)
        assert d is not None
        assert d.binding_constraint == "paced"  # the hold itself is unchanged
        assert d.desired_ceiling <= 3
        assert d.would_set["r_tok"] == pytest.approx(105.0)  # ...but the loop learns

    def test_paced_growth_is_still_clamped_to_the_seed(self) -> None:
        r = FakeRedis()
        _paced_bucket(r, ALIAS, ago=1, n=100, n_over_2s=10)
        b = Budgets(r_tok=145.0, k_inflight=1000.0, r_qps=1.0, source="t", r_tok_seed=100.0)
        d = _armed(r, b).maybe_tick(in_flight_tasks=3)
        assert d is not None and d.binding_constraint == "paced"
        assert d.would_set == {} and d.growth_clamped is True

    def test_paced_with_an_overload_in_the_window_does_not_grow(self) -> None:
        r = FakeRedis()
        _paced_bucket(r, ALIAS, ago=1, n=100, n_over_2s=10)
        _overload(r, ALIAS)
        b = Budgets(r_tok=100.0, k_inflight=1000.0, r_qps=1.0, source="t", r_tok_seed=100.0)
        d = _armed(r, b).maybe_tick(in_flight_tasks=3)
        assert d is not None
        assert d.binding_constraint == "cooldown"  # a real overload freezes everything
        assert d.would_set == {} and d.growth_applied is False

    def test_live_paced_growth_writes_the_cfg(self) -> None:
        r = FakeRedis()
        _paced_bucket(r, ALIAS, ago=1, n=100, n_over_2s=10)
        b = Budgets(r_tok=100.0, k_inflight=1000.0, r_qps=1.0, source="pacer_cfg")
        d = _armed(r, b, mode="live").maybe_tick(in_flight_tasks=3)
        assert d is not None and d.growth_applied is True
        assert float(r.hashes[f"pacer:cfg:{{{ALIAS}}}"]["r_tok"]) == pytest.approx(105.0)


# -- F5: survival = 1 under a borrowed curve or a thin table ----------------------------------


def _doc(attempts: int) -> dict[str, Any]:
    g = {
        "a": 3_600,
        "b": 850,
        "n": 500,
        "insufficient": False,
        "latency_s": 2.0,
        "turn_period_s": 6.0,
        "survival": {"0": 0.5, "40": 0.5, "80": 0.5},
        "survival_attempts": {"0": attempts, "40": attempts, "80": attempts},
    }
    return {"fitted_at": "x", "groups": {"laguna-xs-2.1|mini_swe_agent": g}, "pooled": g}


def _fleet(n: int, turn: int) -> list[TaskState]:
    return [TaskState(turn=turn, context_tokens=50_000.0) for _ in range(n)]


class TestSurvivalEvidence:
    def test_a_borrowed_curve_projects_the_undiscounted_horizon_peak(self) -> None:
        """qwen has no mini curve of its own: it borrows laguna's (survival 0.5 per window).
        The projection must NOT discount by that table."""
        curves = DemandCurves(_doc(attempts=100))
        s = Autoscaler(model_alias="qwen3-coder-next-mini", mode="observe", curves=curves)
        borrowed = s.demand_for("qwen3-coder-next-mini", "mini_swe_agent")
        assert not borrowed.is_own_curve()
        assert s.survival_applies(borrowed) is False
        arrival, *_ = s.project(_fleet(4, 60), demand=borrowed)
        # The same numbers with the table honoured, for comparison: strictly lower.
        honoured = DemandModel(
            a=3_600, b=850, latency_s=2.0, turn_period_s=6.0, survival={0: 0.5, 40: 0.5, 80: 0.5}
        )
        s_explicit = _scaler(FakeRedis(), mode="observe", demand=honoured)
        discounted, *_ = s_explicit.project(_fleet(4, 60))
        assert discounted < arrival
        # ...and equals the undiscounted projection exactly.
        undiscounted = DemandModel(
            a=3_600, b=850, latency_s=2.0, turn_period_s=6.0, survival={0: 1.0}
        )
        s_flat = _scaler(FakeRedis(), mode="observe", demand=undiscounted)
        assert s_flat.project(_fleet(4, 60))[0] == pytest.approx(arrival)

    def test_an_own_curve_on_too_few_attempts_is_not_discounted(self) -> None:
        thin = DemandCurves(_doc(attempts=_SURVIVAL_MIN_ATTEMPTS - 1))
        s = Autoscaler(model_alias="laguna-xs-2.1-mini", mode="observe", curves=thin)
        m = s.demand_for("laguna-xs-2.1-mini", "mini_swe_agent")
        assert m.is_own_curve() and m.survival_attempts == _SURVIVAL_MIN_ATTEMPTS - 1
        assert s.survival_applies(m) is False

    def test_an_own_curve_with_enough_attempts_is_discounted(self) -> None:
        fat = DemandCurves(_doc(attempts=_SURVIVAL_MIN_ATTEMPTS))
        s = Autoscaler(model_alias="laguna-xs-2.1-mini", mode="observe", curves=fat)
        m = s.demand_for("laguna-xs-2.1-mini", "mini_swe_agent")
        assert s.survival_applies(m) is True
        a_disc, *_ = s.project(_fleet(4, 60), demand=m)
        s_flat = _scaler(
            FakeRedis(),
            mode="observe",
            demand=DemandModel(a=3_600, b=850, latency_s=2.0, turn_period_s=6.0, survival={0: 1.0}),
        )
        assert a_disc < s_flat.project(_fleet(4, 60))[0]

    def test_an_explicit_model_is_taken_as_given(self) -> None:
        s = _scaler(FakeRedis(), mode="observe", demand=FLAT)
        assert s.survival_applies(FLAT) is True

    def test_the_decision_records_the_verdict(self) -> None:
        r = FakeRedis()
        _cfg(r, "qwen3-coder-next-mini", **BIG)
        _progress(r, 1, turn=60, alias="qwen3-coder-next-mini", harness="mini_swe_agent")
        s = Autoscaler(
            model_alias="qwen3-coder-next-mini",
            mode="observe",
            redis_client=r,
            tick_interval_s=0.0,
            curves=DemandCurves(_doc(attempts=100)),
        )
        d = s.maybe_tick(in_flight_tasks=1)
        assert d is not None
        assert d.survival_discount is False
        assert d.aliases["qwen3-coder-next-mini"]["survival_discount"] is False
        # The borrowed table's own evidence count is recorded for the reader; the verdict is
        # what matters and it is False regardless of that count.
        assert d.aliases["qwen3-coder-next-mini"]["survival_attempts"] == 100


# -- F6: cadence on the stabilisation window; CLAMPED once per episode -------------------------


class TestGrowthCadence:
    def test_the_re_arm_is_at_least_one_stabilisation_window_out(self) -> None:
        r = FakeRedis()
        b = Budgets(r_tok=100.0, k_inflight=1000.0, r_qps=1.0, source="t", r_tok_seed=100.0)
        s = _armed(r, b)
        first = s.maybe_tick(in_flight_tasks=3)
        assert first is not None and first.would_set  # growth stepped
        assert first.peak_at_s == 0.0  # an aged fleet's peak is now...
        assert s._pending_peak is not None
        assert s._pending_peak[0] - time.monotonic() >= s._stabilization_window_s - 1
        # ...so a tick 20 s later (well inside the window) must NOT step again.
        s._last_tick_at = 0.0
        second = s.maybe_tick(in_flight_tasks=3)
        assert second is not None and second.would_set == {}

    def test_clamped_is_logged_once_per_episode(self, caplog) -> None:
        r = FakeRedis()
        b = Budgets(r_tok=145.0, k_inflight=1000.0, r_qps=1.0, source="t", r_tok_seed=100.0)
        s = _armed(r, b)
        with caplog.at_level(logging.INFO, logger="swebench_eval.orchestrator.control_plane"):
            for _ in range(5):
                s._pending_peak = (time.monotonic() - 1, 999.0)
                s._last_tick_at = 0.0
                d = s.maybe_tick(in_flight_tasks=3)
                assert d is not None and d.growth_clamped is True  # the RECORD says so each tick
        assert sum("CLAMPED" in rec.getMessage() for rec in caplog.records) == 1

    def test_a_new_seed_starts_a_new_clamp_episode(self, caplog) -> None:
        r = FakeRedis()
        s = _armed(
            r, Budgets(r_tok=145.0, k_inflight=1000.0, r_qps=1.0, source="t", r_tok_seed=100.0)
        )
        with caplog.at_level(logging.INFO, logger="swebench_eval.orchestrator.control_plane"):
            s.maybe_tick(in_flight_tasks=3)
            # Re-probed: a new seed of 120 — 145 x 1.05 = 152.25 <= 180, growth resumes...
            s._budgets = lambda *a, **k: Budgets(  # type: ignore[method-assign]
                r_tok=145.0, k_inflight=1000.0, r_qps=1.0, source="t", r_tok_seed=120.0
            )
            s._pending_peak = (time.monotonic() - 1, 999.0)
            s._last_tick_at = 0.0
            grown = s.maybe_tick(in_flight_tasks=3)
            assert grown is not None and grown.would_set
            # ...and a later clamp against the new seed is logged again.
            s._budgets = lambda *a, **k: Budgets(  # type: ignore[method-assign]
                r_tok=175.0, k_inflight=1000.0, r_qps=1.0, source="t", r_tok_seed=120.0
            )
            s._pending_peak = (time.monotonic() - 1, 999.0)
            s._last_tick_at = 0.0
            s.maybe_tick(in_flight_tasks=3)
        assert sum("CLAMPED" in rec.getMessage() for rec in caplog.records) == 2
