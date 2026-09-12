"""Offline curve refit (scripts/fit_growth_curves.py) — synthetic-data correctness, including
the three modelling fixes of 2026-09-03 (prompt field, latency on output tokens, period measured
directly, pool-alias grouping)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fit_growth_curves import fit_group, group_key, load_calls, main, prompt_tokens


def _call(
    i: int,
    *,
    inp: int,
    out: int = 100,
    cached: int = 0,
    run="r1",
    inst="i1",
    att=1,
    lat_ms=2000,
    model="laguna-x",
    gap_s: int = 1,
):
    t = i * gap_s
    return {
        "run_id": run,
        "instance_id": inst,
        "attempt_number": att,
        "call_index": i,
        "input_tokens": inp,
        "cached_tokens": cached,
        "output_tokens": out,
        "latency_ms": lat_ms,
        "started_at": f"2026-09-01T{t // 3600:02d}:{(t % 3600) // 60:02d}:{t % 60:02d}",
        "model_requested": model,
        "harness": "custom_minimal",
    }


def test_fit_recovers_a_known_linear_prompt_curve() -> None:
    # prompt = 10_000 + 500*i exactly; the fit must recover both coefficients — and NOT add
    # output tokens (they are not part of the prompt the pacer meters).
    calls = [_call(i, inp=10_000 + 500 * i, out=100) for i in range(1, 60)]
    g = fit_group(calls)
    assert g["a"] == pytest.approx(10_000, abs=1)
    assert g["b"] == pytest.approx(500, abs=0.1)
    assert g["insufficient"] is False


def test_prompt_adds_cache_reads_only_when_reported_outside_input() -> None:
    """Anthropic shape: input 388 / cached 57,088 -> prompt 57,476. OpenAI shape: input
    48,629 already includes the 47,904 cached -> prompt 48,629."""
    assert prompt_tokens({"input_tokens": 388, "cached_tokens": 57_088}) == 57_476
    assert prompt_tokens({"input_tokens": 48_629, "cached_tokens": 47_904}) == 48_629
    assert prompt_tokens({"input_tokens": 1_000}) == 1_000


def test_latency_is_fitted_on_output_tokens_and_reported_at_the_median_output() -> None:
    # latency = 0.5s + 6ms/output-token, output alternating 100 / 300 (30 each) -> median
    # 200 -> 1.7s
    calls = [
        _call(i, inp=10_000, out=(100 if i % 2 else 300), lat_ms=500 + 6 * (100 if i % 2 else 300))
        for i in range(1, 61)
    ]
    g = fit_group(calls)
    assert g["latency_intercept_s"] == pytest.approx(0.5, abs=0.01)
    assert g["latency_per_output_token_s"] == pytest.approx(0.006, abs=1e-4)
    assert g["median_output_tokens"] == 200
    assert g["latency_s"] == pytest.approx(1.7, abs=0.02)


def test_turn_period_is_the_median_inter_call_gap_measured_directly() -> None:
    calls = [_call(i, inp=10_000, gap_s=9) for i in range(1, 40)]
    g = fit_group(calls)
    assert g["turn_period_s"] == 9.0
    assert g["n_gaps"] == 38
    # tool time is derived, diagnostic only: period - latency(2.0s) = 7.0
    assert g["median_tool_time_s"] == pytest.approx(7.0, abs=0.01)


def test_period_makes_no_claim_on_too_few_gaps() -> None:
    g = fit_group([_call(i, inp=10_000) for i in range(1, 6)])
    assert g["turn_period_s"] is None


def test_small_groups_are_flagged_insufficient_never_silently_authoritative() -> None:
    g = fit_group([_call(i, inp=10_000) for i in range(1, 10)])
    assert g["insufficient"] is True  # §3.2: the fallback chain decides, not this script


def test_survival_counts_attempts_not_calls_and_reports_its_sample_size() -> None:
    calls = []
    # 10 attempts reaching turn 25 (survive the 0-window), 10 dying at turn 10 (don't).
    for k in range(10):
        calls += [_call(i, inp=10_000, inst=f"long{k}") for i in range(1, 26)]
    for k in range(10):
        calls += [_call(i, inp=10_000, inst=f"short{k}") for i in range(1, 11)]
    g = fit_group(calls)
    assert g["survival"]["0"] == pytest.approx(0.5)
    assert g["survival_attempts"]["0"] == 20
    # Only 10 attempts ever reach turn 25; none reach 40+20 — but with <5 reaching deep
    # starts the script must claim NOTHING there, never a fabricated number.
    assert "80" not in g["survival"]


def test_groups_pool_the_per_harness_aliases_onto_the_provider_pool() -> None:
    assert group_key({"model_requested": "laguna-xs-2.1-codex", "harness": "codex"}) == (
        "laguna-xs-2.1|codex"
    )
    assert group_key({"model_requested": "laguna-xs-2.1", "harness": "codex"}) == (
        "laguna-xs-2.1|codex"
    )
    # An alias the registry does not know keeps its own name (never silently merged).
    assert group_key({"model_requested": "cheap-oss-model", "harness": "codex"}) == (
        "cheap-oss-model|codex"
    )


def test_refusals_and_429s_drop_out_of_the_fit(tmp_path: Path) -> None:
    f = tmp_path / "llm_calls.jsonl"
    rows = [
        _call(1, inp=10_000),
        {"call_index": 2, "http_status": 429, "run_id": "r1"},  # no usage (Trap 3) -> excluded
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows))
    assert len(load_calls([tmp_path])) == 1


def test_main_writes_the_artifact(tmp_path: Path) -> None:
    f = tmp_path / "llm_calls.jsonl"
    f.write_text("\n".join(json.dumps(_call(i, inp=10_000 + 500 * i)) for i in range(1, 40)))
    out = tmp_path / "curves.json"
    assert main([str(tmp_path), "-o", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert doc["n_calls"] == 39
    assert "laguna-x|custom_minimal" in doc["groups"]
    assert doc["pooled"]["b"] == pytest.approx(500, abs=1)
    assert "prompt_field" in doc
