"""The grade timeout (2026-09-06, task #75): explicit, env-overridable, and a terminal category
that lands as a FAILED (UNRESOLVED) gradeable attempt — owner decision after django-10097."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from swebench_eval.database.state_machine import (
    _RETRYABLE,
    _TERMINAL,
    map_eval_outcome_to_error_category,
)
from swebench_eval.evaluation import swebench_runner as runner
from swebench_eval.evaluation.grading_adapter import GradingOutput
from swebench_eval.workers import eval_worker


def test_default_timeout_is_thirty_minutes_and_env_overrides(monkeypatch: Any) -> None:
    # 1800 s: the first 500-run's slowest legitimate grade was 1204 s (p99 367 s),
    # and it is upstream SWE-bench's own default.
    monkeypatch.delenv("EVAL_GRADE_TIMEOUT_S", raising=False)
    assert eval_worker._grade_timeout_s() == 1800
    monkeypatch.setenv("EVAL_GRADE_TIMEOUT_S", "5400")
    assert eval_worker._grade_timeout_s() == 5400


def test_bad_env_values_fall_back_loudly(monkeypatch: Any, caplog: Any) -> None:
    with caplog.at_level(logging.WARNING, logger=eval_worker.__name__):
        monkeypatch.setenv("EVAL_GRADE_TIMEOUT_S", "soon")
        assert eval_worker._grade_timeout_s() == 1800
        monkeypatch.setenv("EVAL_GRADE_TIMEOUT_S", "0")
        assert eval_worker._grade_timeout_s() == 1800
    assert sum("EVAL_GRADE_TIMEOUT_S" in r.message for r in caplog.records) == 2


def _g(**kw: Any) -> GradingOutput:
    base: dict[str, Any] = {
        "instance_id": "i",
        "resolved": False,
        "report_json": "{}",
        "wall_clock_seconds": 1.0,
    }
    base.update(kw)
    return GradingOutput(**base)


def test_timed_out_grade_lands_as_an_unresolved_gradeable_row() -> None:
    """A hang is the patch's doing: state UNRESOLVED, verdict 'unresolved', the
    EVAL_TIMEOUT category + SWE-bench's note kept for visibility."""
    v = _g(timed_out=True, error="the test run exceeded the grade timeout (1800 seconds)")
    cat = map_eval_outcome_to_error_category(False, timed_out=True)
    assert cat == "EVAL_TIMEOUT"
    assert eval_worker._eval_row_shape(v, cat) == ("UNRESOLVED", "unresolved", v.error)
    assert "EVAL_TIMEOUT" in _TERMINAL


def test_voided_grades_still_have_no_verdict() -> None:
    assert eval_worker._eval_row_shape(_g(oom_killed=True, error="oom"), "EVAL_OOM_KILLED") == (
        "FAILED_EVAL",
        "",
        "oom",
    )
    assert eval_worker._eval_row_shape(_g(infra_failure=True, error="env"), "EVAL_INFRA_ERROR") == (
        "FAILED_EVAL",
        "",
        "env",
    )
    assert eval_worker._eval_row_shape(_g(invalid=True, error="tp"), "EVAL_GRADE_INVALID") == (
        "FAILED_EVAL",
        "invalid",
        "tp",
    )
    # OOM outranks a timeout on the same grade: still voided, never a verdict.
    assert (
        eval_worker._eval_row_shape(
            _g(oom_killed=True, timed_out=True, error="oom"), "EVAL_OOM_KILLED"
        )[0]
        == "FAILED_EVAL"
    )


def test_plain_verdicts_are_unchanged() -> None:
    assert eval_worker._eval_row_shape(_g(resolved=True), "RESOLVED") == (
        "RESOLVED",
        "resolved",
        "",
    )
    assert eval_worker._eval_row_shape(_g(), "UNRESOLVED") == ("UNRESOLVED", "unresolved", "")
    assert eval_worker._eval_row_shape(_g(), "PATCH_APPLY_FAILED") == (
        "PATCH_APPLY_FAILED",
        "unresolved",
        "",
    )


def test_detect_timeout_reads_swebenchs_marker_from_the_tail(tmp_path: Path) -> None:
    from swebench.harness.constants import LOG_TEST_OUTPUT

    assert runner._detect_timeout(str(tmp_path)) == ""
    out = tmp_path / LOG_TEST_OUTPUT
    out.write_text("test_a ... ok\ntest_b ... ok\n")
    assert runner._detect_timeout(str(tmp_path)) == ""
    out.write_text("x" * 5000 + "\ntest_z ...\n\n\nTimeout error: 7200 seconds exceeded.")
    note = runner._detect_timeout(str(tmp_path))
    assert "exceeded the grade timeout" in note and "7200 seconds" in note


def test_timeout_is_its_own_terminal_category_below_oom_and_infra() -> None:
    assert map_eval_outcome_to_error_category(False, timed_out=True) == "EVAL_TIMEOUT"
    assert (
        map_eval_outcome_to_error_category(False, timed_out=True, oom_killed=True)
        == "EVAL_OOM_KILLED"
    )
    assert (
        map_eval_outcome_to_error_category(False, timed_out=True, infra_failure=True)
        == "EVAL_INFRA_ERROR"
    )
    assert "EVAL_TIMEOUT" not in _RETRYABLE  # the same patch would hang again


def test_grading_output_carries_the_flag_defaulting_to_false() -> None:
    g = GradingOutput(instance_id="i", resolved=False, report_json="{}", wall_clock_seconds=1.0)
    assert g.timed_out is False
    assert GradingOutput(
        instance_id="i", resolved=False, report_json="{}", wall_clock_seconds=1.0, timed_out=True
    ).timed_out
