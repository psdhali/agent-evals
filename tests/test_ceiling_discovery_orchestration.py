"""Ceiling discovery orchestration — pure-function coverage for cost estimation and the
multi-axis protocol's decision logic, with the Phase B RAMP of
BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03.md §2.1 (probe_batch scripted, no network, no sleeps).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from swebench_eval.gateway import admin as gateway_admin
from swebench_eval.orchestrator.control_plane.ceiling_discovery import (
    _RAMP_BATCH_N,
    _RAMP_MAX_STEPS,
    estimate_cost,
)

_TARGET = 262_144 - 2_000  # resolve_target_tokens("laguna-xs-2.1")
_TOK_REAL_EST = int(_TARGET * 0.82)
_REAL_15 = 15 * 213_000  # real tokens a clean 15-call max-context batch reports


def test_estimate_cost_matches_the_full_protocol_formula() -> None:
    """The multi-axis protocol (§6, Phase B ramped): 12+24 burst-edge + the ramp's worst case
    (15 max-context calls x _RAMP_MAX_STEPS, every step clean), plus the tiny-call request-axis
    phases (1.5x fleet + 900 ramp calls), plus Phase D (F3): one warm call + 15 calls per
    multiplier of a 200K prefix + 200-token suffix, priced at the FULL input rate (an upper
    bound — cache hits bill at a discount). laguna: $0.06/1M in, $0.12/1M out (pricing.py,
    keyed on the raw upstream slug)."""
    cost = estimate_cost(
        "laguna-xs-2.1", target_tokens=262_144, target_concurrency=150, output_tokens=300
    )
    per_max = (262_144 / 1_000_000) * 0.06 + (300 / 1_000_000) * 0.12
    per_tiny = (200 / 1_000_000) * 0.06 + (5 / 1_000_000) * 0.12
    per_cached = ((200_000 + 200) / 1_000_000) * 0.06 + (300 / 1_000_000) * 0.12
    # 2026-09-04: the ramp / cached steps are latency-sized (ramp_batch_size) — the preview
    # prices every step at the _RAMP_BATCH_MAX cap, the upper bound.
    from swebench_eval.orchestrator.control_plane.ceiling_discovery import _RAMP_BATCH_MAX

    assert cost == pytest.approx(
        per_max * (12 + 24 + _RAMP_BATCH_MAX * _RAMP_MAX_STEPS)
        + per_tiny * (225 + 900)
        + per_cached * (1 + _RAMP_BATCH_MAX * 4)
    )


def test_estimate_cost_grows_with_fleet_target_via_the_tiny_burst() -> None:
    small = estimate_cost("laguna-xs-2.1", target_tokens=100_000, target_concurrency=10)
    large = estimate_cost("laguna-xs-2.1", target_tokens=100_000, target_concurrency=300)
    assert large > small  # only the tiny request-axis burst scales with the fleet target


def test_estimate_cost_unknown_model_uses_the_conservative_default_not_zero() -> None:
    """pricing.model_price falls back to the deliberately-high $1/$5 default for an unknown
    model (never prices unknown work at zero) — the estimate must reflect that, not silently
    under-quote the operator before they confirm real spend."""
    cheap = estimate_cost("laguna-xs-2.1", target_tokens=100_000, target_concurrency=10)
    unknown = estimate_cost(
        "totally-unknown-model-xyz", target_tokens=100_000, target_concurrency=10
    )
    assert unknown > cheap


def _offered(real_tokens: int, n: int, stagger_s: float, latency_s: float) -> float:
    """The honest offered rate BatchResult.offered_rate_tok_s computes — real tokens over the
    actual offering span, never a padded window."""
    return real_tokens / ((n - 1) * stagger_s + latency_s)


class TestRunDiscoveryProtocol:
    """Batch specs: ``ok``/``n429`` counts, ``tokens`` (windowed), ``real`` (un-windowed total,
    defaults to ``tokens``), ``lat`` (median per-call latency of the successes, seconds)."""

    def _wire(self, monkeypatch, batches, *, cached_phase: bool = False):
        """Monkeypatch everything around run_discovery; returns recorders.

        ``cached_phase``: Phase D (F3) is OFF for the pre-existing protocol tests (their
        scripted batch lists end at Phase C); the Phase D tests turn it on and script its
        warm call + steps. Batch specs may carry ``cached`` (per-success reported cached
        tokens) and ``cached_none`` (successes that report no cached_tokens at all)."""
        import swebench_eval.orchestrator.control_plane.ceiling_discovery as cd
        from swebench_eval.control import state as control_state
        from swebench_eval.gateway.ceiling_discovery import BatchResult

        recorded: dict[str, Any] = {"observations": None, "seeds": None, "key_deleted": False}
        calls: dict[str, list[Any]] = {"batches": []}

        async def fake_probe_batch(*, concurrency, target_tokens, stagger_s=0.0, **kw):
            calls["batches"].append((concurrency, target_tokens, round(stagger_s, 2)))
            spec = batches.pop(0)
            ok, n429 = spec["ok"], spec.get("n429", 0)
            tokens = spec.get("tokens", 0)
            lat = spec.get("lat")
            cached = spec.get("cached")
            cached_s: tuple[int, ...] = ()
            if cached is not None and ok:
                cached_s = tuple([cached] * ok)
            elif spec.get("cached_none") and ok:
                cached_s = tuple([-1] * ok)
            return BatchResult(
                concurrency=concurrency,
                elapsed_s=10.0,
                window_s=spec.get("window", 60.0),
                tokens_in_window=tokens,
                success_count=ok,
                overload_count=n429,
                error_count=concurrency - ok - n429,
                latencies_s=tuple([lat] * ok) if lat is not None and ok else (),
                real_tokens_total=spec.get("real", tokens),
                cached_tokens_s=cached_s,
                error_statuses=tuple(spec.get("statuses", ())),
            )

        monkeypatch.setattr(control_state, "is_paused", lambda pool: False)
        monkeypatch.setattr(cd, "probe_batch", fake_probe_batch)
        # 2026-09-05: target-first is the default ramp; these protocol tests script the
        # bottom-up ramp, which stays selectable (TestTargetFirstRamp covers the new mode).
        monkeypatch.setattr(cd, "_DEFAULT_RAMP_MODE", "bottom_up")
        if not cached_phase:
            monkeypatch.setattr(cd, "_CACHED_MULTIPLIERS", ())
        # 2026-09-04: ramp / cached batches are latency-sized (ramp_batch_size). The protocol
        # tests script fixed 15-call batches, so pin the sizing to the floor here; the sizing
        # itself is covered by TestRampBatchSizing, which restores the real function.
        monkeypatch.setattr(
            cd, "ramp_batch_size", lambda rate, lat, tok, *, floor=cd._RAMP_BATCH_N: floor
        )
        monkeypatch.setattr(
            cd,
            "ensure_discovery_alias_registered",
            lambda a: cd.DiscoveryUpstreamKey(
                alias=f"{a}-ceiling-discovery", openrouter_key_hash="or-hash"
            ),
        )
        monkeypatch.setattr(
            cd,
            "disable_discovery_upstream_key",
            lambda lease: recorded.__setitem__("upstream_disabled", lease.openrouter_key_hash),
        )
        monkeypatch.setattr(gateway_admin, "generate_key", lambda *a, **k: ("sk-raw", "key-id"))
        monkeypatch.setattr(
            gateway_admin,
            "delete_key",
            lambda *a, **k: recorded.__setitem__("key_deleted", True),
        )
        monkeypatch.setattr(cd, "gateway_base_url", lambda: "http://g")
        monkeypatch.setattr(cd, "gateway_api_key", lambda: "mk")
        monkeypatch.setattr(
            cd,
            "_insert_observations",
            lambda alias, prov, rows, trig, rep: recorded.__setitem__("observations", rows),
        )
        monkeypatch.setattr(
            cd, "_seed_pacer_cfg", lambda alias, seeds: recorded.__setitem__("seeds", seeds)
        )
        monkeypatch.setattr(
            cd,
            "_persist_seeds",
            lambda alias, seeds, seeded_at, *a: recorded.__setitem__(
                "persisted", (alias, seeds, seeded_at)
            ),
        )
        return cd, recorded, calls

    # -- the pre-existing protocol properties, on the ramped Phase B ------------------------

    def test_edge_found_takes_the_smaller_of_two_overloaded_bursts(self, monkeypatch) -> None:
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_100_000, "lat": 13.0},  # burst 1: edge 2.1M
            {"ok": 9, "n429": 3, "tokens": 1_900_000, "lat": 13.0},  # burst 2: edge 1.9M
            {"ok": 15, "tokens": _REAL_15, "lat": 13.0},  # ramp step 0, clean (1 step)
            {"ok": 200},  # tiny burst (225 offered)
            {"ok": 300},  # 5/s ramp clean
            {"ok": 600},  # 10/s ramp clean
        ]
        cd, recorded, _calls = self._wire(monkeypatch, batches)
        report = asyncio.run(
            cd.run_discovery(
                "laguna-xs-2.1", fleet_target=150, inter_phase_gap_s=0, max_ramp_steps=1
            )
        )

        assert report["edge_found"] is True
        assert report["burst_edge_tokens"] == 1_900_000  # conservative: the SMALLER edge
        seeds = recorded["seeds"]
        assert seeds["c_burst"] == int(0.5 * 1_900_000)  # the one evidence-backed margin, kept
        assert seeds["k_inflight"] == int(0.95 * 1_900_000)
        assert seeds["c_req"] == int(200 * 0.8)
        assert seeds["r_qps"] == 8.0  # 0.8 x the clean 10/s ramp
        assert recorded["key_deleted"] is True

    def test_clean_first_burst_escalates_the_second_to_double(self, monkeypatch) -> None:
        batches = [
            {"ok": 12, "tokens": 2_500_000, "lat": 13.0},  # burst 1: ALL admitted
            {"ok": 20, "n429": 4, "tokens": 2_300_000, "lat": 13.0},  # burst 2 at 24: overloaded
            {"ok": 15, "tokens": _REAL_15, "lat": 13.0},
            {"ok": 150},
            {"ok": 250, "n429": 50},  # 5/s ramp NOT clean
        ]
        cd, recorded, calls = self._wire(monkeypatch, batches)
        report = asyncio.run(
            cd.run_discovery(
                "laguna-xs-2.1", fleet_target=100, inter_phase_gap_s=0, max_ramp_steps=1
            )
        )

        assert calls["batches"][1][0] == 24  # escalated to 2x after a clean first burst
        assert report["burst_edge_tokens"] == 2_300_000  # only burst 2 found the edge
        # Ramp partial: r_qps = 0.8 x (admitted/60s), never the offered rate.
        assert recorded["seeds"]["r_qps"] == round(0.8 * (250 / 60.0), 2)

    def test_never_overloaded_flags_edge_as_lower_bound(self, monkeypatch) -> None:
        batches = [
            {"ok": 12, "tokens": 2_400_000, "lat": 13.0},
            {"ok": 24, "tokens": 4_800_000, "lat": 13.0},  # even 24x clean
            {"ok": 15, "tokens": _REAL_15, "lat": 13.0},
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        cd, _recorded, _calls = self._wire(monkeypatch, batches)
        report = asyncio.run(
            cd.run_discovery(
                "laguna-xs-2.1", fleet_target=100, inter_phase_gap_s=0, max_ramp_steps=1
            )
        )

        assert report["edge_found"] is False  # a LOWER BOUND, never claimed as the edge
        assert report["burst_edge_tokens"] == 4_800_000

    def test_key_revoked_even_when_a_phase_raises(self, monkeypatch) -> None:
        batches = [
            {"ok": 0, "n429": 0, "tokens": 0},  # 100% errors -> inconclusive -> refuse
        ]
        cd, recorded, _calls = self._wire(monkeypatch, batches)
        with pytest.raises(cd.CeilingDiscoveryError, match="inconclusive"):
            asyncio.run(cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0))
        assert recorded["key_deleted"] is True  # the finally held
        # ...for BOTH keys: the LiteLLM caller key and the alias's upstream OpenRouter key
        # (owner decision 2026-09-03 — the live 401 failure is exactly this path).
        assert recorded["upstream_disabled"] == "or-hash"
        assert recorded["seeds"] is None  # and nothing was seeded from garbage

    # -- the Phase B ramp (design doc §2.1) --------------------------------------------------

    def test_ramp_starts_from_half_the_burst_edge_over_MEASURED_latency(self, monkeypatch):
        """The old start was 0.9 x b_edge/60 (a serial-queue assumption, ~4.6x low for a
        parallel pool). Now rate_0 = 0.5 x b_edge / L_A, where L_A is Phase A's measured
        median latency — for a 2.0M edge at 10s that is 100K tok/s, so the first ramp stagger
        must be tok_real_est / 100K."""
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 10.0},
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        cd, _recorded, calls = self._wire(monkeypatch, batches)
        report = asyncio.run(
            cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, max_ramp_steps=1)
        )
        assert report["latency_a_s"] == 10.0
        assert report["ramp_rate_0_tok_s"] == pytest.approx(0.5 * 2_000_000 / 10.0)
        n, _target, stagger = calls["batches"][2]
        assert n == _RAMP_BATCH_N
        assert stagger == round(_TOK_REAL_EST / 100_000.0, 2)

    def test_offered_rate_is_measured_over_the_real_span_not_a_padded_window(
        self, monkeypatch
    ) -> None:
        """§1 error 2: the old rate divided by window_s = n x stagger + 60 (~37% low). The ramp
        step's recorded offered rate must equal real tokens / ((n-1) x stagger + latency)."""
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 10.0, "window": 400.0},  # padded window
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        cd, recorded, _calls = self._wire(monkeypatch, batches)
        report = asyncio.run(
            cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, max_ramp_steps=1)
        )
        stagger = _TOK_REAL_EST / 100_000.0
        expected = _offered(_REAL_15, _RAMP_BATCH_N, stagger, 10.0)
        assert report["ramp_steps"][0]["offered_rate_tok_s"] == pytest.approx(expected, rel=1e-3)
        assert recorded["seeds"]["r_tok"] == int(expected)  # clean to the top -> 1.0x, no 0.6
        assert recorded["seeds"]["r_tok"] != int(0.6 * _REAL_15 / 400.0)  # the old figure

    def test_hard_strain_seeds_the_highest_CLEAN_rate(self, monkeypatch) -> None:
        """Steps 0 and 1 clean, step 2 hits a real 429: r_tok = step 1's offered rate (the
        x1.5 step is the margin) and the strain rate is recorded; nothing is seeded above a
        rate that ran clean."""
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 10.0},  # step 0 clean
            {"ok": 15, "tokens": _REAL_15, "lat": 10.5},  # step 1 clean (1.05x, under 1.2x)
            {"ok": 9, "n429": 6, "tokens": 9 * 213_000, "lat": 11.0},  # step 2 HARD
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        cd, recorded, calls = self._wire(monkeypatch, batches)
        report = asyncio.run(
            cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, max_ramp_steps=5)
        )
        assert report["ramp_outcome"] == "hard"
        assert len(report["ramp_steps"]) == 3  # stopped at strain, steps 3-4 never fired
        assert [s["outcome"] for s in report["ramp_steps"]] == ["clean", "clean", "hard"]
        step1 = report["ramp_steps"][1]["offered_rate_tok_s"]
        assert recorded["seeds"]["r_tok"] == int(step1)
        assert report["strain_rate_tok_s"] == report["ramp_steps"][2]["offered_rate_tok_s"]
        # The stagger halves-ish per step: x1.5 rate each step.
        s0, s1, s2 = (calls["batches"][i][2] for i in (2, 3, 4))
        assert s1 == pytest.approx(s0 / 1.5, abs=0.01)
        assert s2 == pytest.approx(s0 / 2.25, abs=0.01)
        kinds = {k: v for k, v, _ in recorded["observations"]}
        assert kinds["ramp_strain_rate_tok_per_s"] == int(report["strain_rate_tok_s"])
        assert kinds["call_latency_ms_max_context"] == 10_000
        assert kinds["paced_rate_tok_per_s"] == int(step1)

    def test_soft_strain_backs_off_15pct_and_stops_pushing(self, monkeypatch) -> None:
        """Latency creep with zero 429s (E17's early signal): step 1's median latency 1.3x
        the step-0 baseline -> outcome soft, r_tok = 0.85 x step 1's rate, and the ramp stops
        there (the pool is not pushed further)."""
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 10.0},  # step 0: baseline
            {"ok": 15, "tokens": _REAL_15, "lat": 13.0},  # step 1: 1.3x -> SOFT
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        cd, recorded, calls = self._wire(monkeypatch, batches)
        report = asyncio.run(
            cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, max_ramp_steps=5)
        )
        assert report["ramp_outcome"] == "soft"
        assert len(report["ramp_steps"]) == 2
        step0 = report["ramp_steps"][0]["offered_rate_tok_s"]
        step1 = report["ramp_steps"][1]["offered_rate_tok_s"]
        # Review F3: 0.85 x the STRAINED step, AND never above the highest rate proven clean.
        assert recorded["seeds"]["r_tok"] == int(min(0.85 * step1, step0))
        assert recorded["seeds"]["r_tok"] <= int(step0)
        assert sum(1 for b in calls["batches"] if b[0] == _RAMP_BATCH_N) == 2

    def test_soft_strain_seed_never_exceeds_the_highest_clean_rate(self, monkeypatch) -> None:
        """Review F3's reproduction: burst edge 2.1M at L_A 10 s, step 0 clean at 10 s, step 1
        (x1.5) soft at 13 s. 0.85 x step 1 was 1.53x the highest CLEAN rate; the seed must be
        the clean rate."""
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_100_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_100_000, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 13.0},
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        cd, recorded, _calls = self._wire(monkeypatch, batches)
        report = asyncio.run(
            cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, max_ramp_steps=5)
        )
        clean = report["ramp_steps"][0]["offered_rate_tok_s"]
        strained = report["ramp_steps"][1]["offered_rate_tok_s"]
        assert 0.85 * strained > clean  # the old rule WOULD have seeded above clean
        assert recorded["seeds"]["r_tok"] == int(clean)

    def test_seeds_carry_the_discovered_seed_copies_and_the_pool_latency(self, monkeypatch) -> None:
        """Review F2/F4: r_tok_seed/k_inflight_seed/r_qps_seed beside the live values (growth
        bounds itself against them) and latency_s_max_context (the planner's floor under a
        borrowed curve)."""
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 11.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 11.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 11.0},
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        cd, recorded, _calls = self._wire(monkeypatch, batches)
        asyncio.run(cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, max_ramp_steps=1))
        seeds = recorded["seeds"]
        assert seeds["r_tok_seed"] == seeds["r_tok"]
        assert seeds["k_inflight_seed"] == seeds["k_inflight"]
        assert seeds["r_qps_seed"] == seeds["r_qps"]
        assert seeds["latency_s_max_context"] == 11.0

    def test_a_burst_edge_below_one_max_context_call_floors_the_seeds_loudly(
        self, monkeypatch, caplog
    ) -> None:
        """Review F1: c_burst = 0.5 x b_edge and k_inflight = 0.95 x b_edge below ONE
        max-context call (262,144) would seed a bucket the pacer can never satisfy for that
        call. Floored, and the report says so."""
        batches = [
            {"ok": 2, "n429": 10, "tokens": 400_000, "lat": 6.0},
            {"ok": 2, "n429": 10, "tokens": 400_000, "lat": 6.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 6.0},
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        cd, recorded, _calls = self._wire(monkeypatch, batches)
        with caplog.at_level("WARNING"):
            report = asyncio.run(
                cd.run_discovery("qwen3-coder-next", inter_phase_gap_s=0, max_ramp_steps=1)
            )
        seeds = recorded["seeds"]
        assert seeds["c_burst"] == 262_144  # 0.5 x 400K = 200K -> floored
        assert seeds["k_inflight"] == 380_000  # 0.95 x 400K already clears one call
        assert report["seed_floored"] == ["c_burst 200000 -> 262144"]
        assert "seeds floored" in caplog.text

    def test_clean_to_the_top_seeds_the_top_rate_at_1x(self, monkeypatch) -> None:
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 10.0},
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        cd, recorded, _calls = self._wire(monkeypatch, batches)
        report = asyncio.run(
            cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, max_ramp_steps=3)
        )
        assert report["ramp_outcome"] == "clean"
        top = report["ramp_steps"][-1]["offered_rate_tok_s"]
        assert recorded["seeds"]["r_tok"] == int(top)  # proven by definition; L2 grows from here
        assert report["strain_rate_tok_s"] == 0.0
        assert (
            report["ramp_steps"][-1]["offered_rate_tok_s"]
            > report["ramp_steps"][0]["offered_rate_tok_s"]
        )

    def test_hard_strain_at_step_0_seeds_half_and_flags_it(self, monkeypatch, caplog) -> None:
        """The one case with no clean rate on record: conservative (0.5x) and LOUD."""
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 8, "n429": 7, "tokens": 8 * 213_000, "lat": 10.0},  # step 0 HARD
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        cd, recorded, _calls = self._wire(monkeypatch, batches)
        with caplog.at_level(logging.WARNING):
            report = asyncio.run(
                cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, max_ramp_steps=5)
            )
        assert report["ramp_outcome"] == "hard_at_start"
        step0 = report["ramp_steps"][0]["offered_rate_tok_s"]
        assert recorded["seeds"]["r_tok"] == int(0.5 * step0)
        assert any("HARD strain at ramp step 0" in r.message for r in caplog.records)

    def test_consistency_ratio_warns_when_the_arrival_bucket_would_bind_first(
        self, monkeypatch, caplog
    ) -> None:
        """§2.3: r_tok vs k_inflight/L_A. The matplotlib run had ratio ~0.08 (12K vs ~156K).
        A hard-at-start seed reproduces that shape -> WARNING naming the starvation mode; a
        clean ramp lands well above 0.5 -> INFO only."""
        starved = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 2, "n429": 13, "tokens": 2 * 213_000, "lat": 10.0},  # step 0 HARD, tiny
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        cd, _recorded, _calls = self._wire(monkeypatch, starved)
        with caplog.at_level(logging.INFO):
            report = asyncio.run(
                cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, max_ramp_steps=5)
            )
        assert report["consistency_ratio"] < 0.5
        assert report["inflight_turnover_tok_s"] == pytest.approx(0.95 * 2_000_000 / 10.0, rel=1e-3)
        assert any(
            r.levelno == logging.WARNING and "arrival bucket will bind BEFORE" in r.message
            for r in caplog.records
        )

        caplog.clear()
        clean = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 10.0},
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        cd, _recorded, _calls = self._wire(monkeypatch, clean)
        with caplog.at_level(logging.INFO):
            report = asyncio.run(
                cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, max_ramp_steps=2)
            )
        assert report["consistency_ratio"] >= 0.5
        assert not any(
            r.levelno == logging.WARNING and "arrival bucket" in r.message for r in caplog.records
        )
        assert any("binding physical constraint" in r.message for r in caplog.records)


class TestRampBatchSizing:
    """2026-09-04 (deepseek probe): a 15-call step cannot offer more than ~15 x T / L tok/s —
    the seed was the probe's own ceiling on a 1M-token, 45 s pool. The batch is sized to keep
    _RAMP_OVERLAP_LATENCIES x (R x L / T) calls overlapping, floored at 15, capped at 60."""

    def test_short_calls_keep_the_floor(self) -> None:
        from swebench_eval.orchestrator.control_plane.ceiling_discovery import ramp_batch_size

        # laguna-like: 213K real tokens, 13 s, 100K tok/s -> 6.1 in flight x3 = 19
        assert ramp_batch_size(100_000, 13.0, 213_000) == 19
        # ...and a low rate stays at the floor
        assert ramp_batch_size(20_000, 13.0, 213_000) == 15
        # degenerate inputs never divide by zero and fall to the floor
        assert ramp_batch_size(0, 13.0, 213_000) == 15
        assert ramp_batch_size(50_000, 0.0, 213_000) == 15
        assert ramp_batch_size(50_000, 13.0, 0) == 15

    def test_long_calls_grow_the_batch_and_cap(self) -> None:
        from swebench_eval.orchestrator.control_plane.ceiling_discovery import (
            _RAMP_BATCH_MAX,
            ramp_batch_size,
        )

        # deepseek at 1M: 858K real tokens, 46.7 s. Step 0 at 211K -> 11.5 in flight x3 = 35;
        # step 4 at 1.07M -> 58 in flight x3 = 175 -> capped.
        assert ramp_batch_size(211_000, 46.7, 858_000) == 35
        assert ramp_batch_size(1_070_000, 46.7, 858_000) == _RAMP_BATCH_MAX == 60
        # the floor argument is honoured (Phase D uses _CACHED_BATCH_N)
        assert ramp_batch_size(1_000, 1.0, 100_000, floor=15) == 15

    def test_the_ramp_offers_its_target_on_a_slow_wide_pool(self, monkeypatch) -> None:
        """The deepseek shape: no edge (both bursts admitted), 1M calls at ~47 s. With the
        latency-sized batch the ramp's steps carry 35..60 calls, so the offered rate tracks
        the target instead of flattening at ~15 x T / L."""
        import swebench_eval.orchestrator.control_plane.ceiling_discovery as cd

        real_sizing = cd.ramp_batch_size  # _wire pins it to the floor; this test wants it live
        t = TestRunDiscoveryProtocol()
        real_1m = int((1_048_576 - 2_000) * 0.82)
        lat = 46.7
        batches = [
            {"ok": 12, "tokens": 12 * real_1m, "lat": lat},  # burst 1 clean
            {"ok": 24, "tokens": 24 * real_1m, "lat": lat},  # burst 2 clean -> no edge
        ]
        # five clean ramp steps; each spec's ok/tokens must match the batch size the code picks
        n_expected = []
        rate = 0.5 * 24 * real_1m / lat
        for _ in range(5):
            n = real_sizing(rate, lat, real_1m)
            n_expected.append(n)
            batches.append({"ok": n, "tokens": n * real_1m, "lat": lat})
            rate *= 1.5
        batches += [{"ok": 225}, {"ok": 300}, {"ok": 600}]  # request axis, all clean
        cd, recorded, calls = t._wire(monkeypatch, batches)
        monkeypatch.setattr(cd, "ramp_batch_size", real_sizing)
        report = asyncio.run(
            cd.run_discovery(
                "deepseek-v4-flash-0731", fleet_target=150, inter_phase_gap_s=0, max_ramp_steps=5
            )
        )
        ramp_ns = [b[0] for b in calls["batches"][2:7]]
        assert ramp_ns == n_expected
        assert ramp_ns[0] >= 35 and ramp_ns[-1] == 60
        assert [s["n"] for s in report["ramp_steps"]] == n_expected
        # the top step's offered rate is a real fraction of its target, not ~15 x T / L
        top = report["ramp_steps"][-1]
        assert top["offered_rate_tok_s"] > 0.45 * top["target_rate_tok_s"]
        assert top["offered_rate_tok_s"] > 15 * real_1m / lat * 1.5
        assert report["ramp_outcome"] == "clean"
        assert recorded["seeds"]["r_tok"] == int(top["offered_rate_tok_s"])


class TestErrorDominatedStep:
    """2026-09-05 (the first gpt-5-mini probe, $7 in the OpenRouter account): three ramp steps
    with 2/44, 31/60 and 1/60 successes were classified "clean" because the strain rule only
    looked at 429s and latency; the 380 x 402 credit rejections were invisible until a step had
    ZERO successes. A step the provider mostly rejected must stop the probe there — one more
    step is one more batch billed for nothing."""

    def test_a_mostly_rejected_step_stops_the_probe_and_names_the_status(self, monkeypatch):
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 10.0},  # step 0 clean
            {"ok": 2, "tokens": 2 * 213_000, "lat": 10.1, "statuses": [402] * 13},  # rejected
            {"ok": 15, "tokens": _REAL_15, "lat": 10.0},  # must never fire
        ]
        wired = TestRunDiscoveryProtocol()._wire(monkeypatch, batches)
        cd, recorded, calls = wired
        with pytest.raises(cd.CeilingDiscoveryError, match=r"13 of 15 calls errored \(13x 402\)"):
            asyncio.run(cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, max_ramp_steps=5))
        assert len(calls["batches"]) == 4  # stopped at the rejected step, nothing after it
        assert recorded["seeds"] is None  # nothing seeded from two calls' worth of evidence
        assert recorded["key_deleted"] is True  # the finally still revokes the key

    def test_a_few_errors_in_a_healthy_step_still_classify(self, monkeypatch):
        """Sporadic non-429 errors (OpenInference's 400s) do not trip the guard: successes win."""
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 13, "tokens": 13 * 213_000, "lat": 10.0, "statuses": [400, 400]},
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        cd, recorded, _ = TestRunDiscoveryProtocol()._wire(monkeypatch, batches)
        report = asyncio.run(
            cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, max_ramp_steps=1)
        )
        assert report["ramp_steps"][0]["outcome"] == "clean"
        assert report["ramp_steps"][0]["errors"] == 2
        assert recorded["seeds"] is not None


def test_batch_result_error_dominance_and_histogram() -> None:
    from swebench_eval.gateway.ceiling_discovery import BatchResult

    def _b(ok: int, n429: int, statuses: list[int]) -> BatchResult:
        return BatchResult(
            concurrency=ok + n429 + len(statuses),
            elapsed_s=1.0,
            window_s=60.0,
            tokens_in_window=0,
            success_count=ok,
            overload_count=n429,
            error_count=len(statuses),
            error_statuses=tuple(statuses),
        )

    assert _b(2, 0, [402] * 42).error_dominated is True
    assert _b(31, 0, [402] * 29).error_dominated is False  # successes still win
    assert _b(0, 6, [0, 0]).error_dominated is False  # overloads count as signal
    assert _b(15, 0, []).error_dominated is False
    assert _b(1, 0, [402, 402, 0]).error_status_histogram() == "2x 402, 1x network"


class TestTargetFirstRamp:
    """2026-09-05 (owner): prove the rate the fleet needs first; step DOWN only on strain.
    Fake batches at the 15-call floor (ramp_batch_size pinned by _wire); L_A = 10 s from the
    bursts, so the 60-task target is 60 x 213,158 / (10 + 4) tok/s."""

    _TARGET_RATE = 60 * _TOK_REAL_EST / 14.0

    def _run(self, monkeypatch, batches, **kw):
        cd, recorded, calls = TestRunDiscoveryProtocol()._wire(monkeypatch, batches)
        report = asyncio.run(
            cd.run_discovery("laguna-xs-2.1", inter_phase_gap_s=0, ramp_mode="target_first", **kw)
        )
        return cd, recorded, calls, report

    def test_clean_target_step_ends_the_ramp_after_one_step(self, monkeypatch) -> None:
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 10.5},  # the target step, clean
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        _cd, recorded, calls, report = self._run(monkeypatch, batches)
        assert report["ramp_mode"] == "target_first" and report["target_tasks"] == 60
        assert report["target_rate_tok_s"] == pytest.approx(self._TARGET_RATE, rel=1e-3)
        assert report["ramp_outcome"] == "clean_at_target"
        assert len(report["ramp_steps"]) == 1
        step = report["ramp_steps"][0]
        assert step["target_rate_tok_s"] == pytest.approx(self._TARGET_RATE, rel=1e-3)
        assert recorded["seeds"]["r_tok"] == int(step["offered_rate_tok_s"])  # proven, 1.0x
        # Exactly one max-context ramp batch was billed (15 calls, at the target's stagger).
        assert sum(1 for b in calls["batches"] if b[0] == _RAMP_BATCH_N) == 1
        assert calls["batches"][2][2] == pytest.approx(_TOK_REAL_EST / self._TARGET_RATE, abs=0.01)

    def test_strain_steps_down_25pct_until_a_clean_step(self, monkeypatch) -> None:
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 9, "n429": 6, "tokens": 9 * 213_000, "lat": 11.0},  # target: HARD
            {"ok": 15, "tokens": _REAL_15, "lat": 13.0},  # x0.75: SOFT (1.3x the burst L_A)
            {"ok": 15, "tokens": _REAL_15, "lat": 10.5},  # x0.5625: clean
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        _cd, recorded, _calls, report = self._run(monkeypatch, batches)
        assert report["ramp_outcome"] == "clean_after_step_down"
        assert [s["outcome"] for s in report["ramp_steps"]] == ["hard", "soft", "clean"]
        rates = [s["target_rate_tok_s"] for s in report["ramp_steps"]]
        assert rates[1] == pytest.approx(rates[0] * 0.75, rel=1e-6)
        assert rates[2] == pytest.approx(rates[0] * 0.75**2, rel=1e-6)
        clean = report["ramp_steps"][2]["offered_rate_tok_s"]
        assert recorded["seeds"]["r_tok"] == int(clean)
        # The strain rate on record is the LAST strained step's offered rate.
        assert report["strain_rate_tok_s"] == report["ramp_steps"][1]["offered_rate_tok_s"]

    def test_never_clean_seeds_half_the_last_offered_rate_and_flags_it(
        self, monkeypatch, caplog
    ) -> None:
        strained = {"ok": 9, "n429": 6, "tokens": 9 * 213_000, "lat": 11.0}
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            dict(strained),
            dict(strained),
            dict(strained),
            dict(strained),  # 4 step-downs, all hard
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        with caplog.at_level(logging.WARNING):
            _cd, recorded, _calls, report = self._run(monkeypatch, batches)
        assert report["ramp_outcome"] == "strained_to_floor"
        assert len(report["ramp_steps"]) == 4
        last = report["ramp_steps"][-1]["offered_rate_tok_s"]
        assert recorded["seeds"]["r_tok"] == int(0.5 * last)
        assert "no clean step within 4 step-downs from the 60-task target" in caplog.text

    def test_target_tasks_scales_the_first_step(self, monkeypatch) -> None:
        batches = [
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 10, "n429": 2, "tokens": 2_000_000, "lat": 10.0},
            {"ok": 15, "tokens": _REAL_15, "lat": 10.0},
            {"ok": 150},
            {"ok": 300},
            {"ok": 600},
        ]
        _cd, _rec, _calls, report = self._run(monkeypatch, batches, target_tasks=30)
        assert report["target_tasks"] == 30
        assert report["target_rate_tok_s"] == pytest.approx(self._TARGET_RATE / 2, rel=1e-3)

    def test_estimate_prices_two_capped_steps_in_target_first_mode(self) -> None:
        from swebench_eval.orchestrator.control_plane.ceiling_discovery import (
            _RAMP_BATCH_MAX,
            _TARGET_PREVIEW_STEPS,
        )

        bottom = estimate_cost("laguna-xs-2.1", 262_144, 150, ramp_mode="bottom_up")
        first = estimate_cost("laguna-xs-2.1", 262_144, 150, ramp_mode="target_first")
        per_max = (262_144 / 1_000_000) * 0.06 + (300 / 1_000_000) * 0.12
        assert bottom - first == pytest.approx(
            per_max * _RAMP_BATCH_MAX * (_RAMP_MAX_STEPS - _TARGET_PREVIEW_STEPS)
        )
