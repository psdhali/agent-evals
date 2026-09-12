"""dev/BUILDER4-EXPOSE-MISSING-ARTIFACTS-2026-08-28.md: the API's artifact
surface was hard-wired to 4 kinds while 8 objects existed in S3 for every
completed instance. This pins the 3 newly-exposed kinds (native_trajectory,
test_output, run_log) resolve to the right column and behave like the
existing 4 — and that llm_calls stays deliberately excluded (a data/export
concern, not a viewer tab, per the report's own scope note).
"""

from __future__ import annotations

import pytest

from swebench_eval.orchestrator.api import artifacts


@pytest.mark.parametrize(
    "kind,column,filename",
    [
        ("patch", "patch_path", "patch.diff"),
        ("trajectory", "trajectory_path", "trajectory.jsonl"),
        ("log", "raw_log_path", "harness_stdout.log"),
        ("report", "report_path", "eval_report.json"),
        ("native_trajectory", "native_trajectory_s3_key", "native_trajectory.json"),
        ("test_output", "test_output_s3_key", "test_output.txt"),
        ("run_log", "run_log_s3_key", "run_instance.log"),
    ],
)
def test_path_column_for_every_kind(kind: str, column: str, filename: str) -> None:
    assert artifacts.path_column_for(kind) == column
    assert artifacts._KINDS[kind][1] == filename


def test_llm_calls_is_deliberately_not_a_kind() -> None:
    """Scoped out by the report itself — a data/export concern, not a viewer
    tab; its s3_key already flows through llm_calls rows."""
    with pytest.raises(ValueError, match="unknown artifact kind"):
        artifacts.path_column_for("llm_calls")


def test_artifact_key_for_row_resolves_new_kinds() -> None:
    row = {
        "native_trajectory_s3_key": "runs/r1/harness/i1/1/native_trajectory.json",
        "test_output_s3_key": "runs/r1/eval/i1/1/test_output.txt",
        "run_log_s3_key": None,
    }
    assert (
        artifacts.artifact_key_for_row("native_trajectory", row)
        == "runs/r1/harness/i1/1/native_trajectory.json"
    )
    assert artifacts.artifact_key_for_row("test_output", row) == "runs/r1/eval/i1/1/test_output.txt"
    # NULL column (grade never produced this file) -> None, never fabricated.
    assert artifacts.artifact_key_for_row("run_log", row) is None


def test_media_type_for_new_kinds() -> None:
    assert artifacts.media_type_for("native_trajectory") == "application/json"
    assert artifacts.media_type_for("test_output") == "text/plain"
    assert artifacts.media_type_for("run_log") == "text/plain"
