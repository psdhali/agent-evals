"""offline-analysis-design.md §3.3/§10.3/Trap 4 — rubric load + hash."""

from __future__ import annotations

from pathlib import Path

import pytest

from swebench_eval.analysis import rubric as rb


def test_committed_rubric_loads_and_has_all_nine_dimensions() -> None:
    r = rb.load_rubric()
    # v2 (2026-09-08): contamination question clarified — git history at/before the checkout
    # is readable code, not contamination. v3 (2026-09-09): token_efficiency (`causes`
    # scale) — why the attempt spent its tokens and what would have prevented it.
    # Bump this when the rubric's meaning changes.
    assert r.version == 3
    assert set(r.dimension_ids()) == {
        "contamination",
        "hallucination",
        "tool_efficiency",
        "loop",
        "environment_problem",
        "gave_up_early",
        "test_gaming",
        "problem_misread",
        "token_efficiency",
    }
    te = next(d for d in r.dimensions if d.id == "token_efficiency")
    assert te.scale_type == "causes" and te.require_evidence
    # the judge is told the exact cause vocabulary the parser accepts
    from swebench_eval.analysis.efficiency import EFFICIENCY_CAUSES

    for cause in EFFICIENCY_CAUSES:
        assert cause in te.question, cause


def test_committed_rubric_evidence_requirements_match_the_design() -> None:
    r = rb.load_rubric()
    by_id = {d.id: d for d in r.dimensions}
    assert by_id["contamination"].require_evidence is True
    assert by_id["hallucination"].require_evidence is True
    assert by_id["test_gaming"].require_evidence is True
    assert by_id["environment_problem"].require_evidence is True
    assert by_id["tool_efficiency"].require_evidence is False
    assert by_id["loop"].require_evidence is False


def test_sha256_is_deterministic_and_content_addressed(tmp_path: Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text("version: 1\ndimensions:\n  - id: x\n    question: q\n    scale_type: likert\n")
    r1 = rb.load_rubric(p)
    r2 = rb.load_rubric(p)
    assert r1.sha256 == r2.sha256
    p.write_text("version: 1\ndimensions:\n  - id: x\n    question: q2\n    scale_type: likert\n")
    r3 = rb.load_rubric(p)
    assert r3.sha256 != r1.sha256  # Trap 4: a changed rubric must hash differently


def test_missing_file_raises_rubric_error(tmp_path: Path) -> None:
    with pytest.raises(rb.RubricError):
        rb.load_rubric(tmp_path / "does-not-exist.yaml")


def test_missing_version_raises(tmp_path: Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text("dimensions:\n  - id: x\n    question: q\n    scale_type: likert\n")
    with pytest.raises(rb.RubricError, match="version"):
        rb.load_rubric(p)


def test_unknown_scale_type_raises(tmp_path: Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text("version: 1\ndimensions:\n  - id: x\n    question: q\n    scale_type: bogus\n")
    with pytest.raises(rb.RubricError, match="scale_type"):
        rb.load_rubric(p)


def test_duplicate_dimension_id_raises(tmp_path: Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text(
        "version: 1\ndimensions:\n"
        "  - id: x\n    question: q1\n    scale_type: likert\n"
        "  - id: x\n    question: q2\n    scale_type: likert\n"
    )
    with pytest.raises(rb.RubricError, match="duplicate"):
        rb.load_rubric(p)


def test_missing_required_field_raises(tmp_path: Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text("version: 1\ndimensions:\n  - id: x\n    scale_type: likert\n")
    with pytest.raises(rb.RubricError, match="question"):
        rb.load_rubric(p)


def test_contamination_question_names_git_history_as_readable_code() -> None:
    """2026-09-08: the first claude_code pass flagged four `git show <pre-checkout commit>`
    attempts as severity-3 contamination. The rubric must tell the judge that repository
    history at or before the checkout is code the agent may read."""
    r = rb.load_rubric()
    q = next(d.question for d in r.dimensions if d.id == "contamination")
    assert "git history at or before the checkout" in q
    assert "NOT contamination" in q
    assert "commits after the checkout" in q
