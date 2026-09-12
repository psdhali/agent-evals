#!/usr/bin/env python3
"""Regenerate the publication-site fixtures from the exporter (BUILDER1).

`dev/BUILDER1-REGENERATE-SITE-FIXTURES-2026-08-31.md`.  The two fixtures in
`dev/site-fixtures/` are builder 2's build target, but they were hand-authored
and self-inconsistent — same `error_category` mapping to different
`terminated_reason`s, and none of the schema-predates cases (aborted,
operator_infra_retry, operator_rerun_pass_at_k, grade_invalid) existed.

Do NOT hand-correct them: that reproduces the failure one row later.  Feed
constructed instance_results rows through the real `build_run_export` and emit
what IT produces — the fixture then cannot disagree with the code, because the
code produced it.

Requirements honoured here:

  * synthetic + obviously so: `01JFIXTURE…` run_id, arithmetic placeholder ids
    — nobody should mistake a fixture for a real result (it feeds a public site);
  * two distinct purposes kept: the instrumented case and the null-rule proof
    (every token/cost null — that file is why the null rule is testable);
  * the schema-predates cases now covered: aborted, operator_infra_retry
    (excluded from measured outputs), operator_rerun_pass_at_k (legitimate k),
    grade_invalid;
  * the output is `json.dumps(indent=2)` matching the committed files, so the
    generator is idempotent and the pinning test can compare byte-for-byte.

Usage:  .venv/bin/python scripts/generate_site_fixtures.py
Writes both files under dev/site-fixtures/ (path resolved relative to this
script's repo root — the fixtures live OUTSIDE the repo, in the sibling dev/).
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from swebench_eval.orchestrator import export

# The fixtures live in the sibling dev/ dir, NOT in the repo (they are builder
# 2's build target, kept out of the repo like the other dev/ docs).
# This script is at <repo>/scripts/...; parents[1] = <repo>, so the fixtures
# dir is <repo>/../dev/site-fixtures.
_FIXTURES_DIR = pathlib.Path(__file__).resolve().parents[1].parent / "site-fixtures"

# Synthetic, obviously-not-real run id (publication-site-design §5.3).
_RUN_ID = "01JFIXTUREBBBBBBBBBBBBBBBB"
_FIXED = "2026-08-24T11:20:00Z"

# The 24 base instances, arithmetic sequence 11000..11851 (step 37), mirroring
# the original fixture's shape.
_ID_SEQUENCE: list[str] = [
    "django__django-11000",
    "astropy__astropy-11037",
    "sympy__sympy-11074",
    "scikit-learn__scikit-learn-11111",
    "django__django-11148",
    "astropy__astropy-11185",
    "sympy__sympy-11222",
    "scikit-learn__scikit-learn-11259",
    "django__django-11296",
    "astropy__astropy-11333",
    "sympy__sympy-11370",
    "scikit-learn__scikit-learn-11407",
    "django__django-11444",
    "astropy__astropy-11481",
    "sympy__sympy-11518",
    "scikit-learn__scikit-learn-11555",
    "django__django-11592",
    "astropy__astropy-11629",
    "sympy__sympy-11666",
    "scikit-learn__scikit-learn-11703",
    "django__django-11740",
    "astropy__astropy-11777",
    "sympy__sympy-11814",
    "scikit-learn__scikit-learn-11851",
]


def _row(
    iid: str,
    att: int,
    phase: str,
    state: str,
    *,
    verdict: str | None = None,
    ec: str | None = None,
    rr: str | None = None,
    gi: bool = False,
    inp: int | None = None,
    outp: int | None = None,
    cost: float | None = None,
    agent: float | None = None,
    leakd: bool | None = None,
    leaked: list[str] | None = None,
    touch: bool = False,
    gold: float | None = None,
    qw: float | None = None,
    prov: float | None = None,
    ipull: float | None = None,
    rp: float | None = None,
    et: float | None = None,
) -> dict[str, Any]:
    return {
        "run_id": _RUN_ID,
        "instance_id": iid,
        "attempt_number": att,
        "phase": phase,
        "state": state,
        "error_category": ec,
        "retry_reason": rr,
        "verdict": verdict,
        "grade_invalid": gi,
        "leaked_node_ids": leaked,
        "leak_detectable": leakd,
        "touches_test_files": touch,
        "gold_patch_similarity": gold,
        "input_tokens": inp,
        "output_tokens": outp,
        "cached_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "cost_usd": cost,
        "queue_wait_s": qw,
        "provision_s": prov,
        "image_pull_s": ipull,
        "repo_prep_s": rp,
        "agent_s": agent,
        "eval_test_s": et,
        "task_observed_s": None,
        "task_billed_s": agent,
    }


# Each base instance's outcome: verdict + error_category + instrumentation +
# integrity flags.  The terminated_reason is ALWAYS derived from error_category
# by the exporter, so the fixtures are self-consistent by construction.
_BASE_OUTCOMES: list[dict[str, Any]] = [
    {
        "verdict": "resolved",
        "inp": 158478,
        "outp": 2442,
        "cost": 0.011435,
        "agent": 357.7,
        "leakd": False,
        "gold": 0.218,
    },
    {
        "verdict": "unresolved",
        "ec": "HARNESS_MAX_TURNS_EXCEEDED",
        "agent": 260.3,
        "leakd": False,
        "gold": 0.114,
    },
    {
        "verdict": "resolved",
        "inp": 95117,
        "outp": 1132,
        "cost": 0.006742,
        "agent": 347.5,
        "leakd": True,
        "gold": 0.248,
    },
    {"verdict": "unresolved", "ec": "HARNESS_STUCK", "agent": 791.2, "leakd": True, "gold": 0.257},
    {
        "verdict": "resolved",
        "inp": 120945,
        "outp": 1893,
        "cost": 0.009215,
        "agent": 97.8,
        "leakd": True,
        "gold": 0.227,
    },
    {
        "verdict": "unresolved",
        "ec": "HARNESS_MAX_TURNS_EXCEEDED",
        "agent": 128.6,
        "leakd": True,
        "gold": 0.031,
    },
    {
        "verdict": "resolved",
        "inp": 88423,
        "outp": 905,
        "cost": 0.005533,
        "agent": 215.5,
        "leakd": True,
        "gold": 0.218,
    },
    {
        "verdict": "unresolved",
        "ec": "HARNESS_TIMEOUT",
        "agent": 850.6,
        "leakd": False,
        "gold": 0.238,
    },
    {
        "verdict": "unresolved",
        "ec": "HARNESS_MAX_TOKENS_TRUNCATED",
        "agent": 618.8,
        "leakd": True,
        "gold": 0.103,
    },
    {
        "verdict": "resolved",
        "inp": 138204,
        "outp": 2108,
        "cost": 0.010927,
        "agent": 595.2,
        "leakd": False,
        "gold": 0.119,
    },
    {
        "verdict": "unresolved",
        "ec": "HARNESS_MAX_TURNS_EXCEEDED",
        "agent": 756.7,
        "leakd": True,
        "gold": 0.188,
    },
    {
        "verdict": "resolved",
        "inp": 66233,
        "outp": 815,
        "cost": 0.004212,
        "agent": 61.7,
        "leakd": True,
        "gold": 0.192,
    },
    {
        "verdict": "invalid",
        "ec": "HARNESS_CRASH",
        "gi": True,
        "agent": 141.5,
        "leakd": True,
        "gold": 0.109,
        "touch": True,
    },
    {
        "verdict": "resolved",
        "inp": 170388,
        "outp": 2611,
        "cost": 0.014313,
        "agent": 691.2,
        "leakd": True,
        "gold": 0.351,
    },
    {
        "verdict": "unresolved",
        "ec": "MODEL_API_ERROR",
        "agent": 643.9,
        "leakd": True,
        "gold": 0.042,
    },
    {
        "verdict": "unresolved",
        "ec": "HARNESS_MAX_TURNS_EXCEEDED",
        "agent": 267.8,
        "leakd": True,
        "leaked": ["n1"],
        "gold": 0.107,
    },
    {
        "verdict": "resolved",
        "inp": 104920,
        "outp": 1777,
        "cost": 0.008513,
        "agent": 827.7,
        "leakd": True,
        "gold": 0.037,
    },
    {
        "verdict": "unresolved",
        "ec": "HARNESS_BUDGET_EXCEEDED",
        "agent": 582.5,
        "leakd": True,
        "gold": 0.100,
    },
    {
        "verdict": "unresolved",
        "ec": "HARNESS_MAX_TURNS_EXCEEDED",
        "agent": 58.1,
        "leakd": True,
        "leaked": ["n2"],
        "gold": 0.235,
    },
    {
        "verdict": "unresolved",
        "ec": "HARNESS_MAX_TURNS_EXCEEDED",
        "agent": 468.2,
        "leakd": True,
        "gold": 0.173,
    },
    {
        "verdict": "unresolved",
        "ec": "HARNESS_TIMEOUT",
        "agent": 588.8,
        "leakd": True,
        "gold": 0.156,
    },
    {
        "verdict": "resolved",
        "inp": 142211,
        "outp": 2310,
        "cost": 0.011198,
        "agent": 184.6,
        "leakd": True,
        "gold": 0.348,
    },
    {
        "verdict": "unresolved",
        "ec": "HARNESS_MODEL_REFUSED",
        "agent": 203.5,
        "leakd": True,
        "gold": 0.021,
    },
    {
        "verdict": "resolved",
        "inp": 51244,
        "outp": 688,
        "cost": 0.003817,
        "agent": 74.5,
        "leakd": False,
        "gold": 0.105,
    },
]


def _pair(iid: str, o: dict[str, Any], *, instrumented: bool) -> list[dict[str, Any]]:
    """The harness + eval rows for one instance.

    A resolved/unresolved pair has NO harness error category (EMPTY_PATCH is
    never emitted for a graded patch) — the exporter then maps it to
    "completed".  A cut harness keeps its category so the exporter emits
    "max_turns_exceeded" even when the partial patch was still graded.
    """
    verdict = o["verdict"]
    ec = o.get("ec")
    inp = o.get("inp") if instrumented else None
    outp = o.get("outp") if instrumented else None
    cost = o.get("cost") if instrumented else None
    agent = o.get("agent")
    harness_state = ec if ec else "PATCH_READY"
    eval_verdict = verdict if verdict in ("resolved", "unresolved") else None
    eval_state = verdict.upper() if verdict in ("resolved", "unresolved") else "UNRESOLVED"
    return [
        _row(
            iid,
            1,
            "harness",
            harness_state,
            ec=ec,
            inp=inp,
            outp=outp,
            cost=cost,
            agent=agent,
            qw=2.0,
            prov=5.0,
            ipull=10.0,
            rp=20.0,
            et=30.0,
        ),
        _row(
            iid,
            1,
            "eval",
            eval_state,
            verdict=eval_verdict,
            gi=o.get("gi", False),
            leakd=o.get("leakd"),
            leaked=o.get("leaked"),
            touch=o.get("touch", False),
            gold=o.get("gold"),
        ),
    ]


def _instrumented_rows() -> list[dict[str, Any]]:
    """The 24 base instances + the schema-predates cases (fully instrumented)."""
    rows: list[dict[str, Any]] = []
    for iid, o in zip(_ID_SEQUENCE, _BASE_OUTCOMES):
        rows.extend(_pair(iid, o, instrumented=True))
    # 25: aborted (never dispatched) — excluded from both denominators.
    rows.append(_row("scikit-learn__scikit-learn-11888", 1, "harness", "NEVER_DISPATCHED"))
    # 26: operator_infra_retry — attempt 1 crashed on infra, attempt 2 (a
    # rerun) resolved.  The exporter collapses attempt 1 into attempt 2, so it
    # contributes to NONE of the measured outputs.
    rows.append(
        _row(
            "django__django-11925",
            1,
            "harness",
            "HARNESS_CRASH",
            ec="HARNESS_CRASH",
            rr="operator_infra_retry",
            inp=200,
            outp=100,
            cost=0.02,
            agent=30.0,
        )
    )
    rows.append(
        _row(
            "django__django-11925",
            2,
            "harness",
            "PATCH_READY",
            inp=300,
            outp=150,
            cost=0.03,
            agent=60.0,
        )
    )
    rows.append(
        _row(
            "django__django-11925",
            2,
            "eval",
            "RESOLVED",
            verdict="resolved",
            leakd=True,
            leaked=[],
            gold=0.05,
        )
    )
    # 27: operator_rerun_pass_at_k — attempt 1 terminal unresolved, attempt 2 a
    # deliberate rerun that resolved.  BOTH are legitimate k (pass@1=0, pass@2=1).
    rows.append(
        _row(
            "astropy__astropy-11962",
            1,
            "harness",
            "PATCH_READY",
            inp=500,
            outp=200,
            cost=0.04,
            agent=40.0,
        )
    )
    rows.append(
        _row(
            "astropy__astropy-11962",
            1,
            "eval",
            "UNRESOLVED",
            verdict="unresolved",
            leakd=True,
            leaked=[],
            gold=0.12,
        )
    )
    rows.append(
        _row(
            "astropy__astropy-11962",
            2,
            "harness",
            "PATCH_READY",
            rr="operator_rerun_pass_at_k",
            inp=700,
            outp=300,
            cost=0.05,
            agent=50.0,
        )
    )
    rows.append(
        _row(
            "astropy__astropy-11962",
            2,
            "eval",
            "RESOLVED",
            verdict="resolved",
            leakd=True,
            leaked=[],
            gold=0.09,
        )
    )
    return rows


def _uninstrumented_rows() -> list[dict[str, Any]]:
    """The same 24 base instances, EVERY token/cost field null — the null-rule
    proof (a harness with no instrumentation must export null totals, never 0)."""
    rows: list[dict[str, Any]] = []
    for iid, o in zip(_ID_SEQUENCE, _BASE_OUTCOMES):
        rows.extend(_pair(iid, o, instrumented=False))
    return rows


# ── provenance ──────────────────────────────────────────────────────────────
# The exporter derives provenance from config_snapshot; the fixtures carry the
# resolved_models entry that yields model_resolved, matching §5.1.
def _snapshot(harness: str, cli_version: str) -> dict[str, Any]:
    return {
        "harness": harness,
        "model_alias": "cheap-oss-model",
        "attempts_per_instance": 1,
        "max_tokens_per_instance": None,
        "max_cost_usd_per_instance": 0.75,
        "framework_sha": "43bf763aa1c04e77bd2f9e0f1b3d5a7c9e11d204",
        "swebench_version": "2.1.0",
        "dataset_revision": "a1b2c3d4",
        "harness_image_digest": "sha256:9f1c…",
        "gateway_config_hash": "sha256:4d0a…",
        "harness_cli_versions": {harness: cli_version},
        "resolved_models": {
            "cheap-oss-model": {"model": "deepseek/deepseek-v4-flash-0731", "temperature": 0.0}
        },
    }


def _run_row(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": _RUN_ID,
        "status": "completed",
        "created_at": _FIXED,
        "compute_cost_estimated_usd": 0.1,
        "compute_cost_reconciled_usd": None,
        "config_snapshot": snapshot,
    }


def _build_fixtures() -> tuple[dict[str, Any], dict[str, Any]]:
    """Build both fixtures in-memory (used by main() AND the pinning test).

    Returning them as values keeps the generator importable — the pinning test
    regenerates here and byte-compares against the committed files, so a
    schema change that forgets to regenerate goes red.
    """
    custom_rows = _instrumented_rows()
    # 24 base + 25 aborted + 26 (2 rows: infra-retry + rerun) + 27 (2 attempts
    # × 2 phases) — the denominator counts every distinct non-infra pair,
    # which includes the aborted one.
    custom_denominator = 24 + 1 + 1 + 1  # base + aborted + infra-collapsed + rerun
    custom = export.build_run_export(
        _run_row(_snapshot("custom_minimal", "n/a")),
        custom_rows,
        resolve_rate_denominator=custom_denominator,
    )

    mini_rows = _uninstrumented_rows()
    mini = export.build_run_export(
        _run_row(_snapshot("mini_swe_agent", "1.4.2")),
        mini_rows,
        resolve_rate_denominator=24,
    )
    return custom, mini


def main() -> int:
    custom, mini = _build_fixtures()
    _FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    (_FIXTURES_DIR / "run-custom_minimal.json").write_text(json.dumps(custom, indent=2))
    (_FIXTURES_DIR / "run-mini_swe_agent-uninstrumented.json").write_text(
        json.dumps(mini, indent=2)
    )
    print(f"wrote {_FIXTURES_DIR / 'run-custom_minimal.json'}")
    print(f"wrote {_FIXTURES_DIR / 'run-mini_swe_agent-uninstrumented.json'}")
    print("custom totals:", json.dumps(custom["totals"]))
    print("mini totals:", json.dumps(mini["totals"]))
    print("custom terminated:", custom["terminated_reasons"])
    print("mini terminated:", mini["terminated_reasons"])
    print("custom instances:", len(custom["instances"]))
    print("mini instances:", len(mini["instances"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
