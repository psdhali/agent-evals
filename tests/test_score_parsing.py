"""offline-analysis-design.md §3.3/§3.6/§10.3/§10.7 — judge response -> rows."""

from __future__ import annotations

from swebench_eval.analysis.rubric import Dimension, load_rubric
from swebench_eval.analysis.score_parsing import (
    count_scored_dimensions,
    parse_all_dimensions,
    parse_dimension,
)

_LIKERT = Dimension(id="contamination", question="q", scale_type="likert", require_evidence=True)
_LIKERT_NO_EV = Dimension(id="gave_up_early", question="q", scale_type="likert")
_RATIO = Dimension(id="tool_efficiency", question="q", scale_type="ratio")
_BOOL_SPAN = Dimension(
    id="environment_problem", question="q", scale_type="boolean_with_span", require_evidence=True
)
_COUNT_SEV = Dimension(
    id="hallucination", question="q", scale_type="count_and_severity", require_evidence=True
)


def test_evidence_required_dimension_scored_above_baseline_with_evidence_keeps_the_score() -> None:
    entry = {
        "score": 2,
        "reasoning": "cites turn 7",
        "evidence": [{"turn": 7, "quote": "the fix for test_x"}],
    }
    out = parse_dimension(_LIKERT, entry)
    assert out.score_numeric == 2
    assert out.evidence_missing is False
    assert out.missing_reasoning is False


def test_evidence_required_dimension_scored_above_baseline_with_no_evidence_is_demoted() -> None:
    """§3.3: an assertion that cannot be checked is not a measurement."""
    entry = {"score": 3, "reasoning": "seems suspicious", "evidence": []}
    out = parse_dimension(_LIKERT, entry)
    assert out.score_numeric is None  # demoted, not kept as a score
    assert out.evidence_missing is True


def test_baseline_score_never_requires_evidence_even_on_a_require_evidence_dimension() -> None:
    entry = {"score": 0, "reasoning": "nothing found"}
    out = parse_dimension(_LIKERT, entry)
    assert out.score_numeric == 0
    assert out.evidence_missing is False


def test_dimension_missing_entirely_from_the_response_is_recorded_honestly() -> None:
    """The judge may skip a dimension — a real, honest possibility, never
    silently backfilled as a clean score."""
    out = parse_dimension(_LIKERT, None)
    assert out.score_numeric is None
    assert out.missing_reasoning is True
    assert out.evidence_missing is True  # require_evidence dim, nothing to check it against


def test_missing_reasoning_flag_even_when_evidence_present() -> None:
    entry = {"score": 1, "evidence": [{"turn": 1, "quote": "x"}]}  # no "reasoning" key
    out = parse_dimension(_LIKERT, entry)
    assert out.missing_reasoning is True


def test_ratio_scale_maps_redundant_and_total() -> None:
    entry = {"redundant": 9, "total": 34, "reasoning": "repeated pytest calls"}
    out = parse_dimension(_RATIO, entry)
    assert out.score_numeric == 9
    assert out.score_secondary == 34


def test_boolean_with_span_maps_flag_and_turn_range() -> None:
    entry = {
        "present": True,
        "first_turn": 18,
        "last_turn": 27,
        "reasoning": "missing dependency error at turn 18",
        "evidence": [{"turn": 18, "quote": "ModuleNotFoundError"}],
    }
    out = parse_dimension(_BOOL_SPAN, entry)
    assert out.flag is True
    assert out.span_start_turn == 18
    assert out.span_end_turn == 27


def test_boolean_with_span_false_never_requires_evidence() -> None:
    entry = {"present": False, "reasoning": "no environment failures observed"}
    out = parse_dimension(_BOOL_SPAN, entry)
    assert out.flag is False
    assert out.evidence_missing is False


def test_count_and_severity_maps_count_and_severity() -> None:
    entry = {
        "count": 2,
        "severity": 3,
        "reasoning": "asserted two nonexistent functions",
        "evidence": [{"turn": 5, "quote": "calls helper_fn"}],
    }
    out = parse_dimension(_COUNT_SEV, entry)
    assert out.score_numeric == 2
    assert out.score_secondary == 3


def test_evidence_entries_missing_required_keys_are_dropped() -> None:
    entry = {
        "score": 2,
        "reasoning": "x",
        "evidence": [{"turn": 1, "quote": "ok"}, {"turn": 2}, {"not": "valid"}],
    }
    out = parse_dimension(_LIKERT, entry)
    assert out.evidence == [{"turn": 1, "quote": "ok"}]


def test_parse_all_dimensions_covers_every_rubric_dimension_even_if_response_is_empty() -> None:
    rubric = load_rubric()
    scores = parse_all_dimensions(rubric, {"dimensions": {}})
    assert {s.dimension_id for s in scores} == set(rubric.dimension_ids())
    assert all(s.missing_reasoning for s in scores)


def test_parse_all_dimensions_ignores_non_dict_dimensions_block() -> None:
    rubric = load_rubric()
    scores = parse_all_dimensions(rubric, {"dimensions": "not a dict"})
    assert len(scores) == len(rubric.dimensions)
    assert all(s.score_numeric is None for s in scores)


def test_count_scored_dimensions_is_zero_for_the_hollow_response() -> None:
    """ADR-0042: the reasoning-model failure — valid JSON, no dimensions block
    — must count as ZERO addressed dimensions, the signal the cascade escalates
    on. The old behaviour recorded it as a judged-1 with all-null scores."""
    rubric = load_rubric()
    hollow = parse_all_dimensions(rubric, {": ": ", "})  # the literal shape observed live
    assert count_scored_dimensions(hollow) == 0

    empty_block = parse_all_dimensions(rubric, {"dimensions": {}})
    assert count_scored_dimensions(empty_block) == 0


def test_count_scored_dimensions_counts_dimensions_with_reasoning() -> None:
    rubric = load_rubric()
    one_dim_id = rubric.dimensions[0].id
    scores = parse_all_dimensions(
        rubric, {"dimensions": {one_dim_id: {"score": 0, "reasoning": "baseline"}}}
    )
    assert count_scored_dimensions(scores) == 1  # a baseline-with-reasoning verdict counts


# --- rubric v3: the `causes` scale (token_efficiency) --------------------------------------

_CAUSES = Dimension(id="token_efficiency", question="q", scale_type="causes", require_evidence=True)


def test_causes_scale_maps_share_severity_and_normalised_causes() -> None:
    entry = {
        "avoidable_share": 0.6,
        "severity": 2,
        "reasoning": "whole files read repeatedly",
        "evidence": [{"turn": 3, "quote": "read /testbed/card.py"}],
        "causes": [
            {
                "cause": "unbounded_file_reads",
                "share": 0.7,
                "recommendation": "cap reads at 200 lines",
            },
            {"cause": "Unbounded File Reads", "share": 0.1},  # duplicate after normalisation
            {
                "cause": "reading tests",
                "share": 2.0,
                "recommendation": " grep first ",
            },  # unknown → other
            "looping",  # bare string form
            {"nothing": 1},
        ],
    }
    out = parse_dimension(_CAUSES, entry)
    assert out.score_numeric == 0.6 and out.score_secondary == 2
    assert out.evidence_missing is False
    assert [c["cause"] for c in out.causes] == ["unbounded_file_reads", "other", "looping"]
    assert out.causes[0]["recommendation"] == "cap reads at 200 lines"
    assert out.causes[1]["label"] == "reading tests" and out.causes[1]["share"] == 1.0
    assert out.causes[1]["recommendation"] == "grep first"
    assert out.causes[2]["share"] is None and out.causes[2]["recommendation"] is None


def test_causes_scale_above_baseline_without_evidence_is_demoted() -> None:
    entry = {
        "avoidable_share": 0.5,
        "severity": 3,
        "reasoning": "r",
        "causes": [{"cause": "looping"}],
    }
    out = parse_dimension(_CAUSES, entry)
    assert out.evidence_missing is True
    assert out.score_numeric is None and out.score_secondary is None and out.causes == []


def test_causes_scale_accepts_evidence_cited_inside_the_causes() -> None:
    """2026-09-09: the first v3 pass cited turns per cause and gave no top-level evidence —
    that is evidence, not a missing citation. The shape the judge actually returned."""
    entry = {
        "avoidable_share": 0.7,
        "severity": 3,
        "causes": [
            {
                "cause": "repeated_reads",
                "share": 0.2,
                "recommendation": "do not re-read unchanged files",
                "evidence": [{"turn": 166, "quote": "cat django/db/models/fields.py"}],
            },
            {"cause": "looping", "share": 0.4, "evidence": "not a list"},
        ],
    }
    out = parse_dimension(_CAUSES, entry)
    assert out.evidence_missing is False
    assert out.score_numeric == 0.7 and out.score_secondary == 3
    assert [c["cause"] for c in out.causes] == ["repeated_reads", "looping"]
    assert out.evidence == [{"turn": 166, "quote": "cat django/db/models/fields.py"}]


def test_causes_scale_at_baseline_needs_no_evidence() -> None:
    entry = {"avoidable_share": 0, "severity": 0, "reasoning": "lean attempt", "causes": []}
    out = parse_dimension(_CAUSES, entry)
    assert out.evidence_missing is False
    assert out.score_numeric == 0 and out.score_secondary == 0 and out.causes == []


def test_other_scales_carry_an_empty_causes_list() -> None:
    out = parse_dimension(_LIKERT_NO_EV, {"score": 1, "reasoning": "r"})
    assert out.causes == []
