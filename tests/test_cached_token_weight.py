"""F3 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): the cached-token weight, end to end.

98% of the qwen x mini fleet's prompt tokens were cache hits; the pacer priced them at the
probe's UNCACHED r_tok and throttled a fleet Parasail was taking ~40x under its limit. Four
pieces, one number: discovery Phase D measures how much faster the pool takes a mostly-cached
stream and seeds ``cached_weight``; the shim charges ``uncached + cached x w`` (the prior
prompt as the expected prefix) and settles against the real split; the Lua draws the weighted
charge; the planner projects arrival with the same weight over each curve's fitted cached
share.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from swebench_eval.gateway import ceiling_discovery as gcd
from swebench_eval.gateway.ceiling_discovery import BatchResult
from swebench_eval.gateway.pacer import Admission, weighted_charge
from swebench_eval.orchestrator.control_plane.harness_dispatcher import (
    Budgets,
    DemandModel,
    TaskState,
)

# ── the probe's building blocks ────────────────────────────────────────────────


def test_cached_prefix_is_fixed_per_run_and_the_suffix_is_unique_per_call() -> None:
    prefix = gcd.cached_prefix(1_000)
    assert prefix.startswith("probe-prefix:")
    assert len(prefix) == 1_000 * gcd._CHARS_PER_TOKEN
    a = gcd.cached_probe_content(prefix, 50)
    b = gcd.cached_probe_content(prefix, 50)
    assert a != b  # the per-call nonce forces the suffix to miss
    assert a.startswith(prefix) and b.startswith(prefix)  # the shared span comes FIRST
    assert "probe-nonce:" in a[len(prefix) :]
    # A different run gets a different prefix — a stale cache entry can never look warm.
    assert gcd.cached_prefix(1_000) != prefix


def test_usage_cached_tokens_reads_the_openai_shape_and_never_fabricates() -> None:
    assert gcd._usage_cached_tokens({"usage": {"prompt_tokens_details": {"cached_tokens": 7}}}) == 7
    assert gcd._usage_cached_tokens({"usage": {"prompt_tokens": 10}}) is None
    assert gcd._usage_cached_tokens({"usage": {"prompt_tokens_details": {}}}) is None
    assert gcd._usage_cached_tokens({}) is None


def _batch(cached: tuple[int, ...]) -> BatchResult:
    return BatchResult(
        concurrency=len(cached),
        elapsed_s=1.0,
        window_s=60.0,
        tokens_in_window=0,
        success_count=len(cached),
        overload_count=0,
        error_count=0,
        latencies_s=tuple([1.0] * len(cached)),
        real_tokens_total=0,
        cached_tokens_s=cached,
    )


def test_cache_hit_share_counts_proven_hits_only() -> None:
    assert _batch((150_000, 150_000, 10, -1)).cache_hit_share(147_600) == pytest.approx(0.5)
    assert _batch(()).cache_hit_share(1) == 0.0  # nothing succeeded: never a hit
    assert _batch((-1, -1)).cache_hit_share(1) == 0.0  # nothing reported: never a hit


# ── Phase D outcomes, through run_discovery ────────────────────────────────────

_PREFIX = 200_000  # min(_CACHED_PREFIX_TOKENS, target - headroom) for a 260K target
_MIN_CACHED = int(0.9 * _PREFIX * 0.82)
_CACHED_REAL_15 = 15 * int((_PREFIX + 200) * 0.82)


def _run(monkeypatch, phase_d: list[dict[str, Any]]):
    """A protocol whose A-C phases are all clean at fixed numbers, followed by *phase_d*
    (the warm call first, then the scripted steps)."""
    from tests.test_ceiling_discovery_orchestration import _REAL_15, TestRunDiscoveryProtocol

    batches = [
        {"ok": 12, "tokens": 2_000_000, "lat": 13.0},  # burst 1 clean
        {"ok": 20, "n429": 4, "tokens": 2_100_000, "lat": 13.0},  # burst 2: the edge
        {"ok": 15, "tokens": _REAL_15, "lat": 13.0},  # ramp step 0 clean (max_ramp_steps=1)
        {"ok": 200},
        {"ok": 300},
        {"ok": 600},
        {"ok": 1, "tokens": 170_000, "lat": 5.0, "cached": 0},  # Phase D warm call
        *phase_d,
    ]
    t = TestRunDiscoveryProtocol()
    cd, recorded, calls = t._wire(monkeypatch, batches, cached_phase=True)
    report = asyncio.run(cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, max_ramp_steps=1))
    return report, recorded, calls


def _clean(lat: float = 5.0, cached: int = 180_000) -> dict[str, Any]:
    return {"ok": 15, "tokens": _CACHED_REAL_15, "lat": lat, "cached": cached}


def test_strain_at_a_later_step_seeds_r_tok_over_the_last_clean_cached_rate(monkeypatch):
    report, recorded, calls = _run(
        monkeypatch,
        [
            _clean(),
            _clean(),
            {"ok": 12, "n429": 3, "tokens": _CACHED_REAL_15, "lat": 5.0, "cached": 180_000},
        ],
    )
    assert report["cached_outcome"] == "strain"
    assert [s["outcome"] for s in report["cached_steps"]] == ["clean", "clean", "hard"]
    r_tok = recorded["seeds"]["r_tok"]
    rate = report["cached_rate_tok_s"]
    assert rate == report["cached_steps"][1]["offered_rate_tok_s"]  # the LAST clean step
    assert rate > r_tok  # the cached stream ran faster than the uncached seed
    assert recorded["seeds"]["cached_weight"] == pytest.approx(r_tok / rate, abs=1e-3)
    assert recorded["seeds"]["cached_weight_seed"] == recorded["seeds"]["cached_weight"]
    assert report["cached_weight_is_bound"] is False
    # The steps ran at 1x, 2x, 4x the Phase B basis (stagger halves each step).
    d_steps = [b for b in calls["batches"] if b[0] == 15 and b[1] == _PREFIX + 200]
    assert len(d_steps) == 3
    assert d_steps[0][2] == pytest.approx(2 * d_steps[1][2], rel=0.02)
    assert d_steps[1][2] == pytest.approx(2 * d_steps[2][2], rel=0.02)
    # The warm call happened once, alone, before any step.
    warm = [b for b in calls["batches"] if b[0] == 1]
    assert len(warm) == 1


def test_every_step_clean_reports_the_weight_as_an_upper_bound(monkeypatch):
    report, recorded, _calls = _run(monkeypatch, [_clean(), _clean(), _clean(), _clean()])
    assert report["cached_outcome"] == "upper_bound"
    assert report["cached_weight_is_bound"] is True
    assert len(report["cached_steps"]) == 4
    assert 0.05 <= recorded["seeds"]["cached_weight"] < 1.0


def test_a_step_that_does_not_prove_its_hits_is_invalid_and_leaves_full_price(monkeypatch):
    """The provider reported no cached_tokens (or too few): nothing here says anything about
    a cached stream — the weight must stay 1.0, never a number from an uncached step."""
    report, recorded, _calls = _run(monkeypatch, [_clean(cached=10_000)])
    assert report["cached_outcome"] == "invalid"
    assert report["cached_steps"][0]["outcome"] == "invalid"
    assert report["cached_steps"][0]["cache_hit_share"] == 0.0
    assert recorded["seeds"]["cached_weight"] == 1.0
    assert report["cached_rate_tok_s"] == 0.0


def test_unreported_cached_tokens_are_invalid_too(monkeypatch):
    report, recorded, _calls = _run(
        monkeypatch, [{"ok": 15, "tokens": _CACHED_REAL_15, "lat": 5.0, "cached_none": True}]
    )
    assert report["cached_outcome"] == "invalid"
    assert recorded["seeds"]["cached_weight"] == 1.0


def test_strain_at_the_first_cached_step_means_cached_is_no_cheaper(monkeypatch):
    report, recorded, _calls = _run(
        monkeypatch,
        [{"ok": 10, "n429": 5, "tokens": _CACHED_REAL_15, "lat": 5.0, "cached": 180_000}],
    )
    assert report["cached_outcome"] == "strain"
    assert report["cached_rate_tok_s"] == 0.0
    assert recorded["seeds"]["cached_weight"] == 1.0


def test_soft_strain_on_the_cached_axis_stops_the_ramp(monkeypatch):
    report, _recorded, _calls = _run(monkeypatch, [_clean(lat=5.0), _clean(lat=6.5)])
    assert [s["outcome"] for s in report["cached_steps"]] == ["clean", "soft"]
    assert report["cached_outcome"] == "strain"


def test_observations_carry_the_cached_axis(monkeypatch):
    _report, recorded, _calls = _run(monkeypatch, [_clean(), _clean(), _clean(), _clean()])
    kinds = {row[0]: row[1] for row in recorded["observations"]}
    assert kinds["cached_rate_tok_per_s"] > 0
    assert 50 <= kinds["cached_weight_x1000"] < 1000


# ── the shim's charge ──────────────────────────────────────────────────────────


def test_weighted_charge_prices_the_expected_prefix_at_the_weight() -> None:
    assert weighted_charge(1_000, 0, 0.1) == 1_000  # first call: nothing cached
    assert weighted_charge(1_000, 900, 0.1) == 100 + 90
    assert weighted_charge(1_000, 5_000, 0.1) == 100  # cached clamped to est
    assert weighted_charge(1_000, 1_000, 0.0) == 50  # weight floored at 0.05
    assert weighted_charge(1_000, 1_000, 2.0) == 1_000  # weight capped at 1.0
    assert weighted_charge(1, 1, 0.05) == 1  # never below 1


def test_shim_charges_the_first_call_in_full_and_the_prior_prompt_at_the_weight(tmp_path):
    from tests.test_local_proxy_pacing import FakePacer, _OkUpstream, _run_shim

    pacer = FakePacer()
    pacer.cached_weight = 0.1
    _responses, records = _run_shim(tmp_path, _OkUpstream, pacer, n_posts=2)
    est1, est2 = pacer.acquires
    # Call 1: no prior prompt -> full price.
    assert pacer.charges[0] == est1
    assert records[0]["pacer_charge_tok"] == est1
    # Call 2: the first call reported prompt_tokens 500 (> est2), so the whole estimate is
    # expected to hit the cache and is charged at 0.1.
    assert pacer.charges[1] == weighted_charge(est2, min(500, est2), 0.1)
    assert records[1]["pacer_charge_tok"] == pacer.charges[1]
    # Settlement inputs reach release(): the charge and the real split.
    assert pacer.settlements[0] == (est1, 500, None)


def test_shim_without_a_weight_capable_pacer_charges_full_price(tmp_path):
    """A pacer with no get_cached_weight (older fakes) is full price, not a crash."""
    from tests.test_local_proxy_pacing import FakePacer, _OkUpstream, _run_shim

    class _Old(FakePacer):
        get_cached_weight = None  # type: ignore[assignment]

    pacer = _Old()
    _responses, _records = _run_shim(tmp_path, _OkUpstream, pacer, n_posts=2)
    assert pacer.charges == pacer.acquires


# ── the planner ────────────────────────────────────────────────────────────────


def _fleet(n: int, turn: int) -> list[TaskState]:
    return [TaskState(turn=turn, context_tokens=50_000.0) for _ in range(n)]


def test_projection_discounts_the_curves_cached_share_by_the_weight() -> None:
    from tests.test_autoscaler_forecast_review import FakeRedis, _scaler

    hot = DemandModel(
        a=3_600.0, b=850.0, latency_s=2.0, turn_period_s=6.0, survival={0: 1.0}, cached_share=0.98
    )
    s = _scaler(FakeRedis(), mode="observe", demand=hot)
    full, inflight_full, qps_full, _ = s.project(_fleet(4, 60), cached_weight=1.0)
    weighted, inflight_w, qps_w, _ = s.project(_fleet(4, 60), cached_weight=0.1)
    assert weighted == pytest.approx(full * (1 - 0.98 * 0.9))
    # Only the arrival axis is weighted — in-flight and QPS are the same physical load.
    assert inflight_w == pytest.approx(inflight_full)
    assert qps_w == pytest.approx(qps_full)
    # An unknown cached share (0) is full price whatever the weight.
    cold = DemandModel(a=3_600.0, b=850.0, latency_s=2.0, turn_period_s=6.0, survival={0: 1.0})
    s2 = _scaler(FakeRedis(), mode="observe", demand=cold)
    assert s2.project(_fleet(4, 60), cached_weight=0.1)[0] == pytest.approx(
        s2.project(_fleet(4, 60), cached_weight=1.0)[0]
    )


def test_budgets_read_the_weight_from_pacer_cfg_and_default_to_full_price() -> None:
    from tests.test_autoscaler_forecast_review import FakeRedis, _cfg, _scaler

    r = FakeRedis()
    _cfg(r, "laguna-x", r_tok=100.0, k_inflight=1_000.0, r_qps=1.0, cached_weight=0.2)
    assert _scaler(r, mode="observe")._budgets("laguna-x").cached_weight == pytest.approx(0.2)
    r2 = FakeRedis()
    _cfg(r2, "laguna-x", r_tok=100.0, k_inflight=1_000.0, r_qps=1.0)
    assert _scaler(r2, mode="observe")._budgets("laguna-x").cached_weight == 1.0
    assert Budgets(r_tok=1.0, k_inflight=1.0, r_qps=1.0, source="defaults").cached_weight == 1.0


def test_the_ceiling_grows_with_a_weight_below_one() -> None:
    from tests.test_autoscaler_forecast_review import FakeRedis, _scaler

    hot = DemandModel(
        a=3_600.0, b=850.0, latency_s=2.0, turn_period_s=6.0, survival={0: 1.0}, cached_share=0.98
    )
    s = _scaler(FakeRedis(), mode="observe", demand=hot)
    tight = Budgets(r_tok=26_782.0, k_inflight=1e9, r_qps=1e9, source="pacer_cfg")
    loose = Budgets(
        r_tok=26_782.0, k_inflight=1e9, r_qps=1e9, source="pacer_cfg", cached_weight=0.1
    )
    c_full, _ = s._ceiling_from_budgets(_fleet(4, 60), tight, demand=hot)
    c_weighted, _ = s._ceiling_from_budgets(_fleet(4, 60), loose, demand=hot)
    assert c_weighted > c_full


def test_curve_loader_threads_cached_share_and_defaults_to_zero() -> None:
    from swebench_eval.orchestrator.control_plane.demand_curves import DemandCurves

    doc = {
        "groups": {
            "p|mini_swe_agent": {
                "a": 1.0,
                "b": 1.0,
                "n": 100,
                "latency_s": 1.0,
                "turn_period_s": 5.0,
                "cached_share": 0.97,
            },
            "p|codex": {"a": 1.0, "b": 1.0, "n": 100, "latency_s": 1.0, "turn_period_s": 5.0},
        },
        "pooled": {},
    }
    curves = DemandCurves(doc, origin="test")
    assert curves.model_for("p", "mini_swe_agent").cached_share == pytest.approx(0.97)
    assert curves.model_for("p", "codex").cached_share == 0.0


def test_fitter_reports_the_token_weighted_cached_share() -> None:
    from scripts.fit_growth_curves import fit_group

    def rec(i: int, inp: int, cached: int) -> dict[str, Any]:
        return {
            "run_id": "r",
            "instance_id": "i",
            "attempt_number": 1,
            "call_index": i,
            "input_tokens": inp,
            "cached_tokens": cached,
            "output_tokens": 10,
            "latency_ms": 1000,
            "started_at": f"2026-09-04T01:00:{i:02d}+00:00",
        }

    g = fit_group([rec(1, 1_000, 0), rec(2, 3_000, 2_900)])
    assert g["cached_share"] == pytest.approx(2_900 / 4_000, abs=1e-3)
    assert fit_group([rec(1, 1_000, 0)])["cached_share"] == 0.0


def test_admission_defaults_keep_older_callers_whole() -> None:
    a = Admission(call_id="c", est_tokens=10, paced_wait_ms=0, fallback=False)
    assert a.charge_tokens == 0
    assert json.dumps(a.__dict__)  # still a plain record
