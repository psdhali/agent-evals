"""offline-analysis-design.md §3.2 — stratified sampling, unit-level (pure
function, no DB)."""

from __future__ import annotations

import pytest

from swebench_eval.analysis.judge import JudgeCandidate, estimate_pass_cost_usd, stratify


def _c(instance_id, attempt=1, harness="mini", outcome="resolved", always=False) -> JudgeCandidate:
    return JudgeCandidate(
        instance_id=instance_id,
        attempt_number=attempt,
        harness=harness,
        outcome=outcome,
        patch_path="p",
        trajectory_path="t",
        leaked_node_ids=None,
        always_judge=always,
    )


def test_uniform_sampling_would_starve_a_small_cell_but_the_floor_does_not() -> None:
    """Trap 3: at 5% of a 900-attempt run split five ways, a small cell gets
    ~one trajectory. min_per_stratum protects it."""
    candidates = [_c(f"i{i}") for i in range(20)]
    selected, strata = stratify(candidates, sample_rate=0.05, min_per_stratum=5, seed=1)
    assert len(selected) >= 5  # the floor, not round(20*0.05)=1
    assert strata["mini|resolved"]["sampled"] >= 5


def test_always_judge_gets_full_coverage_regardless_of_rate() -> None:
    candidates = [_c(f"i{i}", always=(i < 3)) for i in range(50)]
    selected, _ = stratify(candidates, sample_rate=0.0, min_per_stratum=0, seed=1)
    selected_ids = {c.instance_id for c in selected}
    assert {"i0", "i1", "i2"} <= selected_ids


def test_min_per_stratum_does_not_exceed_the_strata_population() -> None:
    candidates = [_c("only-one")]
    selected, strata = stratify(candidates, sample_rate=0.05, min_per_stratum=5, seed=1)
    assert len(selected) == 1  # never samples more than exists
    assert strata["mini|resolved"] == {"eligible": 1, "sampled": 1}


def test_separate_harness_outcome_cells_are_sampled_independently() -> None:
    candidates = [_c(f"a{i}", harness="mini", outcome="resolved") for i in range(10)] + [
        _c(f"b{i}", harness="codex", outcome="unresolved") for i in range(10)
    ]
    _, strata = stratify(candidates, sample_rate=0.5, min_per_stratum=1, seed=1)
    assert strata["mini|resolved"]["eligible"] == 10
    assert strata["codex|unresolved"]["eligible"] == 10
    assert strata["mini|resolved"]["sampled"] == 5
    assert strata["codex|unresolved"]["sampled"] == 5


def test_100_percent_rate_selects_everything() -> None:
    candidates = [_c(f"i{i}") for i in range(7)]
    selected, strata = stratify(candidates, sample_rate=1.0, min_per_stratum=0, seed=1)
    assert len(selected) == 7
    assert strata["mini|resolved"]["sampled"] == 7


def test_always_judge_candidate_is_not_double_counted_in_its_stratum() -> None:
    candidates = [_c(f"i{i}", always=(i == 0)) for i in range(10)]
    selected, strata = stratify(candidates, sample_rate=0.0, min_per_stratum=0, seed=1)
    assert len(selected) == 1  # only the always_judge one
    assert strata["mini|resolved"]["sampled"] == 1


def test_seed_makes_the_sample_reproducible() -> None:
    candidates = [_c(f"i{i}") for i in range(30)]
    sel1, _ = stratify(candidates, sample_rate=0.3, min_per_stratum=0, seed=42)
    sel2, _ = stratify(candidates, sample_rate=0.3, min_per_stratum=0, seed=42)
    assert [c.instance_id for c in sel1] == [c.instance_id for c in sel2]


def test_no_candidates_returns_empty() -> None:
    selected, strata = stratify([], sample_rate=1.0, min_per_stratum=5, seed=1)
    assert selected == []
    assert strata == {}


def test_estimate_pruned_is_cheaper_than_full() -> None:
    """§9.1: pruned trajectories average fewer tokens than raw."""
    pruned = estimate_pass_cost_usd(100, "pruned")
    full = estimate_pass_cost_usd(100, "full")
    assert pruned < full


def test_estimate_scales_linearly_with_candidate_count() -> None:
    one = estimate_pass_cost_usd(1, "pruned")
    hundred = estimate_pass_cost_usd(100, "pruned")
    assert hundred == pytest.approx(one * 100)


def test_estimate_matches_the_design_docs_own_full_run_math() -> None:
    """§9.3's worked example: 2,500 attempts, 100% coverage — the doc's $12.19 / $17.74 were
    at the judge backend's then-rate ($0.06/$0.12). 2026-09-04: judge-model lands on
    OpenInference ($0.05 in / $0.16 out) after the account allowlist change, so the same
    token means (79,295 pruned / 116,244 raw in, 1,000 out) price to $10.31 / $14.93."""
    pruned = estimate_pass_cost_usd(2500, "pruned")
    raw = estimate_pass_cost_usd(2500, "full")
    assert pruned == pytest.approx(2500 * (79_295 * 0.05 + 1_000 * 0.16) / 1e6, abs=0.01)
    assert raw == pytest.approx(2500 * (116_244 * 0.05 + 1_000 * 0.16) / 1e6, abs=0.01)
    assert pruned == pytest.approx(10.31, abs=0.01)
    assert raw == pytest.approx(14.93, abs=0.01)
