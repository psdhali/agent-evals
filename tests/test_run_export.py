"""GET /runs/{run_id}/export — the M6.2 publication schema (§5.1).

BUILDER1-EXPORT-AND-LIVE-ENDPOINTS-2026-08-31.md §1.  The export is the
contract builder 2 builds the publication site against, so these tests pin
the schema-predates rules the brief calls out:

* the null rule — every numeric field nullable, never coerced to zero (an
  uninstrumented harness must NOT chart as free / infinitely efficient);
* ``totals.attempted`` comes from the resolve-rate denominator (infra-retry
  collapse), NOT the frozen dispatch ``expected``;
* ``pass_at_k`` honours ``retry_reason`` — operator_rerun_pass_at_k and
  configured attempts_per_instance slots are legitimate k, operator_infra_retry
  is not;
* aborted instances are excluded from BOTH denominators.

The route-level tests mock the three query calls; the aggregation itself
(``build_run_export``) is pure and tested directly.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from swebench_eval.orchestrator import export
from swebench_eval.orchestrator.api.main import app

# A resolved-but-uninstrumented scenario: every token/cost field is null.
_RUN = {
    "run_id": "run-1",
    "status": "completed",
    "created_at": "2026-08-20T09:00:00Z",
    "compute_cost_estimated_usd": 0.1,
    "compute_cost_reconciled_usd": None,
    "config_snapshot": {
        "harness": "custom_minimal",
        "model_alias": "cheap-oss-model",
        "attempts_per_instance": 1,
        "max_tokens_per_instance": None,
        "max_cost_usd_per_instance": 0.75,
        "framework_sha": "abc123",
        "swebench_version": "4.1.0",
        "dataset_revision": "rev1",
        "harness_image_digest": "sha256:xxx",
        "gateway_config_hash": "sha256:yyy",
        "harness_cli_versions": {"custom_minimal": "1.0.0"},
        "resolved_models": {
            "cheap-oss-model": {"model": "deepseek/deepseek-v4-flash-0731", "temperature": 0.0}
        },
    },
}


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
        "run_id": "run-1",
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


# A small, fully-instrumented run: A resolved, B unresolved (max_turns), and a
# third instance whose attempt 1 was an operator_infra_retry crash and attempt
# 2 (a rerun) resolved.
_ROWS_FULL = [
    _row("a__a-1", 1, "harness", "PATCH_READY", inp=1000, outp=500, cost=0.01, agent=100.0),
    _row("a__a-1", 1, "eval", "RESOLVED", verdict="resolved", leaked=[], leakd=True, gold=0.2),
    _row(
        "b__b-1",
        1,
        "harness",
        "HARNESS_MAX_TURNS_EXCEEDED",
        ec="HARNESS_MAX_TURNS_EXCEEDED",
        agent=50.0,
    ),
    _row("b__b-1", 1, "eval", "UNRESOLVED", verdict="unresolved", leakd=False, touch=True),
    _row(
        "c__c-1",
        1,
        "harness",
        "HARNESS_CRASH",
        ec="HARNESS_CRASH",
        rr="operator_infra_retry",
        inp=200,
        outp=100,
        cost=0.02,
        agent=30.0,
    ),
    _row("c__c-1", 2, "harness", "PATCH_READY", inp=300, outp=150, cost=0.03, agent=60.0),
    _row("c__c-1", 2, "eval", "RESOLVED", verdict="resolved", leaked=[], leakd=True),
]


def _build(rows: list[dict[str, Any]], denominator: int) -> dict[str, Any]:
    return export.build_run_export(_RUN, rows, resolve_rate_denominator=denominator)


# ── the null rule — the fixture's whole point ───────────────────────────────


def test_uninstrumented_harness_exports_null_totals_not_zero() -> None:
    """A harness with no instrumentation must export null token/cost totals,
    never 0 — otherwise it would chart as free and infinitely efficient."""
    rows = [
        _row("a__a-1", 1, "harness", "PATCH_READY", inp=None, outp=None, cost=None, agent=100.0),
        _row("a__a-1", 1, "eval", "RESOLVED", verdict="resolved"),
        _row("b__b-1", 1, "harness", "PATCH_READY", inp=None, outp=None, cost=None, agent=50.0),
        _row("b__b-1", 1, "eval", "UNRESOLVED", verdict="unresolved"),
    ]
    out = _build(rows, denominator=2)
    assert out["totals"]["cost_usd_total"] is None
    assert out["totals"]["tokens"] == {
        "input": None,
        "output": None,
        "cached": None,
        "reasoning": None,
    }
    # A partially-measured run sums only the measured values (never 0).
    rows[0]["input_tokens"] = 500
    out2 = _build(rows, denominator=2)
    assert out2["totals"]["tokens"]["input"] == 500
    assert out2["totals"]["tokens"]["output"] is None


# ── attempted = resolve-rate denominator, not expected ──────────────────────


def test_attempted_comes_from_denominator_minus_aborted() -> None:
    """attempted is the resolve-rate denominator (infra-retry collapsed) with
    aborted pairs subtracted — never run_summary.expected (frozen at dispatch)."""
    rows = _ROWS_FULL + [_row("d__d-1", 1, "harness", "NEVER_DISPATCHED")]
    # resolve_rate_denominator counts the 4 non-infra pairs (a,b,c2,d); aborted
    # d is subtracted here.
    out = _build(rows, denominator=4)
    assert out["totals"]["attempted"] == 3
    assert out["totals"]["resolved"] == 2
    assert out["totals"]["resolve_rate_attempted"] == 0.6667


def test_aborted_excluded_from_both_denominators() -> None:
    rows = _ROWS_FULL + [_row("d__d-1", 1, "harness", "ABORTED_IN_FLIGHT")]
    out = _build(rows, denominator=4)
    # d is in neither denominator.
    assert out["totals"]["attempted"] == 3
    assert out["totals"]["gradeable"] == 3
    # d is also absent from the instances list.
    assert all(i["instance_id"] != "d__d-1" for i in out["instances"])


# ── pass_at_k honours retry_reason ──────────────────────────────────────────


def test_pass_at_k_honors_retry_reason() -> None:
    """operator_infra_retry is not a legitimate k (it collapses into the retried
    attempt); operator_rerun_pass_at_k and configured slots ARE.  c's infra
    retry at attempt 1 must not make it 'solved at 1'."""
    out = _build(_ROWS_FULL, denominator=3)
    # a solved at 1, b not, c solved at 2 (its attempt 1 was an infra retry).
    assert out["totals"]["pass_at_k"] == {"1": 0.3333, "2": 0.6667}


def test_pass_at_k_counts_operator_rerun_as_legitimate_k() -> None:
    """A rerun_pass_at_k restart is an additional legitimate attempt (it is not
    an infra retry), so it extends k and counts toward solved-at-k."""
    rows = [
        _row("x__x-1", 1, "harness", "PATCH_READY", inp=10, outp=5, cost=0.001, agent=10.0),
        _row("x__x-1", 1, "eval", "UNRESOLVED", verdict="unresolved"),
        _row(
            "x__x-1",
            2,
            "harness",
            "PATCH_READY",
            rr="operator_rerun_pass_at_k",
            inp=20,
            outp=5,
            cost=0.002,
            agent=10.0,
        ),
        _row("x__x-1", 2, "eval", "RESOLVED", verdict="resolved"),
    ]
    out = _build(rows, denominator=1)
    assert out["totals"]["pass_at_k"] == {"1": 0.0, "2": 1.0}


# ── terminated_reasons derivation ───────────────────────────────────────────


def test_terminated_reasons_derived_from_error_category() -> None:
    """b was cut off at max_turns yet still graded unresolved — its terminated
    reason is max_turns_exceeded, not completed.  resolved instances complete."""
    out = _build(_ROWS_FULL, denominator=3)
    assert out["terminated_reasons"] == {"completed": 2, "max_turns_exceeded": 1}


def test_instances_merge_harness_and_eval_phases() -> None:
    out = _build(_ROWS_FULL, denominator=3)
    by_id = {i["instance_id"]: i for i in out["instances"]}
    a = by_id["a__a-1"]
    assert a["verdict"] == "resolved"
    assert a["terminated_reason"] == "completed"
    assert a["input_tokens"] == 1000
    assert a["cost_usd"] == 0.01
    assert a["leak_detectable"] is True
    assert a["leaked"] is False
    # The infra-retry attempt c/1 is not a separate row; c's row is attempt 2.
    c = by_id["c__c-1"]
    assert c["attempt"] == 2
    assert c["verdict"] == "resolved"


# ── wilson / provenance ─────────────────────────────────────────────────────


def test_wilson_interval_known_value() -> None:
    """Wilson(95%) on 5/10 is roughly [0.2366, 0.7634] — not the normal
    approximation, and never null for n > 0."""
    ci = export._wilson_interval(5, 10)
    assert ci is not None
    assert ci[0] == pytest.approx(0.2366, abs=0.0001)
    assert ci[1] == pytest.approx(0.7634, abs=0.0001)


def test_wilson_interval_null_when_no_denominator() -> None:
    assert export._wilson_interval(0, 0) is None
    assert export._wilson_interval(3, 0) is None


def test_provenance_model_resolved_never_omitted() -> None:
    """model_resolved is always present (never omitted — publication-site §6).
    It resolves from config_snapshot.resolved_models; null only when that is
    unresolvable (the site then fails loudly)."""
    out = _build(_ROWS_FULL, denominator=3)
    assert out["provenance"]["model_resolved"] == "deepseek/deepseek-v4-flash-0731"
    # model_alias / harness flow from the snapshot; limits carry the ceilings.
    assert out["provenance"]["harness"] == "custom_minimal"
    assert out["provenance"]["limits"]["max_tokens"] is None  # no token ceiling
    assert out["provenance"]["limits"]["attempts_per_instance"] == 1
    assert out["provenance"]["network_posture"] == "isolated"


def test_provenance_missing_snapshot_resolved_models_is_null_not_missing() -> None:
    run = dict(_RUN)
    run["config_snapshot"] = {"harness": "custom_minimal", "model_alias": "cheap-oss-model"}
    out = export.build_run_export(run, _ROWS_FULL, resolve_rate_denominator=3)
    assert "model_resolved" in out["provenance"]
    assert out["provenance"]["model_resolved"] is None


# ── route level ─────────────────────────────────────────────────────────────


from contextlib import contextmanager


class _FakeConn:
    """Minimal conn the route's finally closes."""

    def close(self) -> None:
        return None


@contextmanager
def _patch_queries(run, rows, denominator):
    """Mock the route's whole boundary: the DB connection (the route calls
    _db() first and closes it in a finally) and the three query functions."""
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=_FakeConn()),
        mock.patch("swebench_eval.orchestrator.api.queries.fetch_export_run", return_value=run),
        mock.patch(
            "swebench_eval.orchestrator.api.queries.fetch_export_instances", return_value=rows
        ),
        mock.patch(
            "swebench_eval.orchestrator.api.queries.resolve_rate_denominator",
            return_value=denominator,
        ),
    ):
        yield


def test_run_export_404_when_run_missing() -> None:
    with _patch_queries(None, [], 0):
        resp = TestClient(app).get("/runs/nope/export")
    assert resp.status_code == 404


def test_run_export_route_round_trip() -> None:
    with _patch_queries(_RUN, _ROWS_FULL, 3):
        resp = TestClient(app).get("/runs/run-1/export")
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == 1
    assert body["totals"]["attempted"] == 3
    assert body["totals"]["resolve_rate_attempted"] == 0.6667
    assert body["provenance"]["model_resolved"] == "deepseek/deepseek-v4-flash-0731"
    # 2026-09-06: response_model filtering dropped these on the first 500 gate
    # export — the schema must declare them or the route silently omits them.
    for key in ("dataset_name", "image_digest_snapshot", "pin"):
        assert key in body["provenance"], key
    assert len(body["instances"]) == 3
