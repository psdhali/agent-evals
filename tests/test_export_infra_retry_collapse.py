"""An operator_infra_retry attempt collapses INTO the attempt it retried — it carries
the retry's outcome and the chain's summed spend (2026-09-06, first 500-run: the export
dropped the retry and reported the crashed original with no verdict — gradeable 498 vs
the run summary's 500)."""

from __future__ import annotations

from typing import Any

from swebench_eval.orchestrator import export


def _row(
    iid: str,
    phase: str,
    state: str,
    *,
    att: int = 1,
    verdict: str | None = None,
    error_category: str | None = None,
    retry_reason: str | None = None,
    cost: float | None = None,
    tokens: int | None = None,
    agent_s: float | None = None,
    eval_test_s: float | None = None,
    gold_sim: float | None = None,
) -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "instance_id": iid,
        "attempt_number": att,
        "phase": phase,
        "state": state,
        "error_category": error_category,
        "retry_reason": retry_reason,
        "verdict": verdict,
        "grade_invalid": False,
        "leaked_node_ids": None,
        "leak_detectable": None,
        "touches_test_files": False,
        "gold_patch_similarity": gold_sim,
        "input_tokens": tokens,
        "output_tokens": None,
        "cached_tokens": None,
        "reasoning_tokens": None,
        "cost_usd": cost,
        "agent_s": agent_s,
        "eval_test_s": eval_test_s,
        "turns_used": None,
    }


def _rows_with_one_restart() -> list[dict[str, Any]]:
    return [
        # a: crashed harness (attempt 1), operator restarted as an infra retry
        # (attempt 2) which produced a patch that graded UNRESOLVED.
        _row(
            "a__a-1",
            "harness",
            "FAILED_HARNESS",
            error_category="HARNESS_CRASH",
            cost=0.05,
            tokens=900,
            agent_s=195.0,
        ),
        _row(
            "a__a-1",
            "harness",
            "PATCH_READY",
            att=2,
            retry_reason="operator_infra_retry",
            cost=0.18,
            tokens=3800,
            agent_s=600.0,
        ),
        _row(
            "a__a-1",
            "eval",
            "UNRESOLVED",
            att=2,
            verdict="unresolved",
            retry_reason="operator_infra_retry",
            eval_test_s=120.0,
            gold_sim=0.3,
        ),
        # b: a plain resolved attempt
        _row("b__b-1", "harness", "PATCH_READY", cost=0.10, tokens=1000, agent_s=100.0),
        _row("b__b-1", "eval", "RESOLVED", verdict="resolved", eval_test_s=60.0, gold_sim=0.9),
    ]


def test_retry_outcome_and_summed_spend_land_on_the_retried_attempt() -> None:
    out = export.build_run_export(
        {"run_id": "run-1"}, _rows_with_one_restart(), resolve_rate_denominator=2
    )
    t = out["totals"]
    assert t["attempted"] == 2
    assert t["gradeable"] == 2  # a's retry verdict counts; the crash does not linger
    assert t["resolved"] == 1
    assert t["resolve_rate_gradeable"] == 0.5
    assert t["cost_usd_total"] == round(0.05 + 0.18 + 0.10, 6)  # the crash's spend is real
    assert t["tokens"]["input"] == 900 + 3800 + 1000
    assert out["terminated_reasons"] == {"completed": 2}
    assert {str(k): v for k, v in t["pass_at_k"].items()} == {"1": 0.5}

    a = next(i for i in out["instances"] if i["instance_id"] == "a__a-1")
    assert len(out["instances"]) == 2  # one entry per legitimate attempt
    assert a["attempt"] == 1  # the trial slot, not the DB row
    assert a["verdict"] == "unresolved"
    assert a["error_category"] is None  # the crash category did not survive
    assert a["terminated_reason"] == "completed"
    assert abs(a["cost_usd"] - 0.23) < 1e-9
    assert a["agent_s"] == 795.0
    assert a["gold_patch_similarity"] == 0.3


def test_timing_medians_use_the_retry_that_produced_the_outcome() -> None:
    out = export.build_run_export(
        {"run_id": "run-1"}, _rows_with_one_restart(), resolve_rate_denominator=2
    )
    # eval_test_s: a=120 (from the retry), b=60 → median 90
    assert out["timing_p50_s"]["eval_test"] == 90.0
    # agent_s is spend-like: a=795 (summed), b=100 → median 447.5
    assert out["timing_p50_s"]["agent"] == 447.5


def test_a_second_legitimate_attempt_is_its_own_entry() -> None:
    rows = _rows_with_one_restart() + [
        _row("b__b-1", "harness", "PATCH_READY", att=2, retry_reason="operator_rerun_pass_at_k"),
        _row(
            "b__b-1",
            "eval",
            "UNRESOLVED",
            att=2,
            verdict="unresolved",
            retry_reason="operator_rerun_pass_at_k",
        ),
    ]
    out = export.build_run_export({"run_id": "run-1"}, rows, resolve_rate_denominator=3)
    assert [(i["instance_id"], i["attempt"]) for i in out["instances"]] == [
        ("a__a-1", 1),
        ("b__b-1", 1),
        ("b__b-1", 2),
    ]
    assert out["totals"]["gradeable"] == 3
