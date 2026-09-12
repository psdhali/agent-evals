"""EMPTY_PATCH belongs in the gradeable denominator (ADR-0038 §4; owner, 2026-09-06)."""

from __future__ import annotations

import json
from typing import Any, Self

from swebench_eval.orchestrator import export
from swebench_eval.orchestrator.control_plane import results_writer as rw


def _row(
    iid: str, phase: str, state: str, verdict: str | None = None, att: int = 1
) -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "instance_id": iid,
        "attempt_number": att,
        "phase": phase,
        "state": state,
        "error_category": state if phase == "harness" and state == "EMPTY_PATCH" else None,
        "retry_reason": None,
        "verdict": verdict,
        "grade_invalid": False,
        "leaked_node_ids": None,
        "leak_detectable": None,
        "touches_test_files": False,
        "gold_patch_similarity": None,
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "reasoning_tokens": None,
        "cost_usd": None,
        "agent_s": None,
        "eval_test_s": None,
        "turns_used": None,
    }


def test_export_counts_empty_patches_in_gradeable_but_not_crashes() -> None:
    rows = [
        _row("a__a-1", "harness", "PATCH_READY"),
        _row("a__a-1", "eval", "RESOLVED", "resolved"),
        _row("b__b-1", "harness", "PATCH_READY"),
        _row("b__b-1", "eval", "UNRESOLVED", "unresolved"),
        _row("c__c-1", "harness", "EMPTY_PATCH"),  # model failure, no eval row
        _row("d__d-1", "harness", "FAILED_HARNESS"),  # infra: excluded
    ]
    out = export.build_run_export({"run_id": "run-1"}, rows, resolve_rate_denominator=4)
    t = out["totals"]
    assert t["attempted"] == 4
    assert t["gradeable"] == 3  # a, b, c — never d
    assert t["resolved"] == 1
    assert t["resolve_rate_gradeable"] == round(1 / 3, 4)
    assert t["resolve_rate_attempted"] == 0.25


class _Cursor:
    def __init__(self, counts: tuple[int, ...]) -> None:
        self._counts = counts
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[int, ...]:
        return self._counts


class _Conn:
    def __init__(self, counts: tuple[int, ...]) -> None:
        self.cur = _Cursor(counts)

    def cursor(self) -> _Cursor:
        return self.cur

    def commit(self) -> None:
        return None


def test_run_summary_adds_empty_patches_to_gradeable() -> None:
    # expected, completed, aborted, never, resolved, verdicts, empty_patches
    conn = _Conn((500, 500, 0, 0, 379, 496, 3))
    rw._maintain_run_summary(conn, "run-1")
    select_sql = conn.cur.executed[0][0]
    assert "state = 'EMPTY_PATCH'" in select_sql
    insert_sql, params = conn.cur.executed[1]
    assert "run_summary" in insert_sql
    summary = json.loads(params[1])
    assert summary["gradeable"] == 499 and summary["denominator"] == 499
    assert summary["empty_patches"] == 3
    assert summary["resolved_per_gradeable"] == 379 / 499
    assert summary["attempted"] == 500 and summary["resolved_per_attempted"] == 379 / 500
