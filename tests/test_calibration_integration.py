"""offline-analysis-design.md §11 — calibration review against a real Postgres.

Supersedes §3.9's offline hand-labeling: approve/deny + reasoning on the
judge's own per-dimension verdicts. Seeds judge_results/judge_dimension_scores
directly (not via judge.run_pass) — this file is testing the review layer,
not the judge pass itself, which is already covered in
test_judge_pass_integration.py.
"""

from __future__ import annotations

import json

import pytest

from swebench_eval.analysis import calibration

pytestmark = pytest.mark.integration

_PREFIX = "cal-test-"


def _db():
    from swebench_eval.database.connection import get_connection

    return get_connection()


@pytest.fixture(autouse=True)
def _clean_rows():
    def _clean():
        conn = _db()
        try:
            with conn.cursor() as cur:
                for table in (
                    "judge_calibration_reviews",
                    "judge_dimension_scores",
                    "judge_results",
                    "runs",
                ):
                    cur.execute(f"DELETE FROM {table} WHERE run_id LIKE %s", (f"{_PREFIX}%",))
            conn.commit()
        finally:
            conn.close()

    _clean()
    yield
    _clean()


def _seed_dimension_row(
    run_id: str,
    instance_id: str = "inst-1",
    attempt_number: int = 1,
    dimension_id: str = "contamination",
    score_numeric: float = 2,
) -> str:
    """Inserts a run + one judge_results row + one judge_dimension_scores
    row directly, returns judged_at (as the string the API/DB round-trips)."""
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (run_id, config_snapshot, status) VALUES (%s, '{}'::jsonb, "
                "'completed') ON CONFLICT (run_id) DO NOTHING",
                (run_id,),
            )
            cur.execute(
                """INSERT INTO judge_results
                       (run_id, instance_id, attempt_number, rubric_version, rubric_sha256,
                        judge_prompt_version, scores, summary)
                   VALUES (%s, %s, %s, '1', 'sha256:x', 'v1', %s, 'ok')
                   RETURNING judged_at""",
                (run_id, instance_id, attempt_number, json.dumps({})),
            )
            judged_at = cur.fetchone()[0]
            cur.execute(
                """INSERT INTO judge_dimension_scores
                       (run_id, instance_id, attempt_number, judged_at, dimension_id,
                        scale_type, score_numeric, reasoning, evidence, evidence_missing)
                   VALUES (%s, %s, %s, %s, %s, 'likert', %s, 'because', '[]'::jsonb, FALSE)""",
                (run_id, instance_id, attempt_number, judged_at, dimension_id, score_numeric),
            )
        conn.commit()
    finally:
        conn.close()
    return str(judged_at.isoformat())


def test_record_review_requires_a_real_dimension_score_row() -> None:
    run_id = f"{_PREFIX}missing-target"
    conn = _db()
    try:
        with pytest.raises(calibration.ReviewTargetNotFoundError):
            calibration.record_review(
                conn,
                run_id=run_id,
                instance_id="nope",
                attempt_number=1,
                judged_at="2026-01-01T00:00:00+00:00",
                dimension_id="contamination",
                decision="approve",
                reviewer_reasoning="looks right",
            )
    finally:
        conn.close()


def test_record_review_rejects_missing_reasoning() -> None:
    run_id = f"{_PREFIX}no-reasoning"
    judged_at = _seed_dimension_row(run_id)
    conn = _db()
    try:
        with pytest.raises(ValueError, match="reviewer_reasoning"):
            calibration.record_review(
                conn,
                run_id=run_id,
                instance_id="inst-1",
                attempt_number=1,
                judged_at=judged_at,
                dimension_id="contamination",
                decision="approve",
                reviewer_reasoning="   ",
            )
    finally:
        conn.close()


def test_record_review_rejects_bad_decision() -> None:
    run_id = f"{_PREFIX}bad-decision"
    judged_at = _seed_dimension_row(run_id)
    conn = _db()
    try:
        with pytest.raises(ValueError, match="decision"):
            calibration.record_review(
                conn,
                run_id=run_id,
                instance_id="inst-1",
                attempt_number=1,
                judged_at=judged_at,
                dimension_id="contamination",
                decision="maybe",
                reviewer_reasoning="unsure",
            )
    finally:
        conn.close()


def test_record_review_writes_a_real_row_and_a_rereview_adds_not_overwrites() -> None:
    run_id = f"{_PREFIX}append-only"
    judged_at = _seed_dimension_row(run_id)
    conn = _db()
    try:
        calibration.record_review(
            conn,
            run_id=run_id,
            instance_id="inst-1",
            attempt_number=1,
            judged_at=judged_at,
            dimension_id="contamination",
            decision="deny",
            reviewer_reasoning="the quote doesn't support this",
            corrected_score_numeric=0,
        )
        calibration.record_review(
            conn,
            run_id=run_id,
            instance_id="inst-1",
            attempt_number=1,
            judged_at=judged_at,
            dimension_id="contamination",
            decision="approve",
            reviewer_reasoning="on reflection, it does",
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM judge_calibration_reviews WHERE run_id = %s", (run_id,)
            )
            count = cur.fetchone()[0]
    finally:
        conn.close()
    assert count == 2  # both kept — a re-review is a new row, never an overwrite


def test_fetch_reviews_for_result_returns_full_history_newest_first() -> None:
    """The per-instance Judge panel's data source for "already reviewed"
    state — without this a reviewer either re-reviews blind or has to trust
    their own memory across a page reload."""
    run_id = f"{_PREFIX}history"
    judged_at = _seed_dimension_row(run_id, dimension_id="contamination")
    conn = _db()
    try:
        calibration.record_review(
            conn,
            run_id=run_id,
            instance_id="inst-1",
            attempt_number=1,
            judged_at=judged_at,
            dimension_id="contamination",
            decision="deny",
            reviewer_reasoning="first pass: looked wrong",
            corrected_score_numeric=0,
        )
        calibration.record_review(
            conn,
            run_id=run_id,
            instance_id="inst-1",
            attempt_number=1,
            judged_at=judged_at,
            dimension_id="contamination",
            decision="approve",
            reviewer_reasoning="re-checked, it's right",
        )
        reviews = calibration.fetch_reviews_for_result(conn, run_id, "inst-1", 1, judged_at)
    finally:
        conn.close()

    assert len(reviews) == 2
    # newest first
    assert reviews[0]["decision"] == "approve"
    assert reviews[0]["reviewer_reasoning"] == "re-checked, it's right"
    assert reviews[1]["decision"] == "deny"
    assert reviews[1]["corrected_score_numeric"] == 0


def test_fetch_reviews_for_result_is_scoped_to_the_exact_judged_at() -> None:
    """A re-judge produces a NEW judge_results row (new judged_at) — its
    reviews must never bleed into an older judged_at's history."""
    run_id = f"{_PREFIX}scoped"
    judged_at = _seed_dimension_row(run_id, dimension_id="contamination")
    conn = _db()
    try:
        calibration.record_review(
            conn,
            run_id=run_id,
            instance_id="inst-1",
            attempt_number=1,
            judged_at=judged_at,
            dimension_id="contamination",
            decision="approve",
            reviewer_reasoning="fine",
        )
        reviews = calibration.fetch_reviews_for_result(
            conn, run_id, "inst-1", 1, "2020-01-01T00:00:00+00:00"
        )
    finally:
        conn.close()
    assert reviews == []


def test_calibration_summary_counts_the_latest_review_only_and_includes_unreviewed_dimensions() -> (
    None
):
    run_id = f"{_PREFIX}summary"
    judged_at = _seed_dimension_row(run_id, dimension_id="contamination")
    conn = _db()
    try:
        # First review: deny. Then a re-review flips it to approve — the
        # summary must count the LATEST only, not both.
        calibration.record_review(
            conn,
            run_id=run_id,
            instance_id="inst-1",
            attempt_number=1,
            judged_at=judged_at,
            dimension_id="contamination",
            decision="deny",
            reviewer_reasoning="first pass: looked wrong",
        )
        calibration.record_review(
            conn,
            run_id=run_id,
            instance_id="inst-1",
            attempt_number=1,
            judged_at=judged_at,
            dimension_id="contamination",
            decision="approve",
            reviewer_reasoning="re-checked, it's right",
        )
        summary = calibration.calibration_summary(conn)
    finally:
        conn.close()

    by_dim = {d.dimension_id: d for d in summary}
    assert by_dim["contamination"].reviewed_count == 1  # not 2 — latest only
    assert by_dim["contamination"].approve_count == 1
    assert by_dim["contamination"].deny_count == 0
    assert by_dim["contamination"].endorsement_rate == 1.0
    assert by_dim["contamination"].distinct_instances_reviewed == 1
    assert by_dim["contamination"].cleared_threshold is False  # 1 < 20

    # every rubric dimension appears, even with zero reviews
    assert "hallucination" in by_dim
    assert by_dim["hallucination"].reviewed_count == 0
    assert by_dim["hallucination"].endorsement_rate is None  # never 0 for "not reviewed"


def test_calibration_summary_is_global_not_run_scoped() -> None:
    """§11: judge-model is one alias across every run, so the 20-case
    threshold — and the summary itself — accumulates across runs, not per
    run. Two different run_ids' reviews of the same dimension both count."""
    run_a = f"{_PREFIX}global-a"
    run_b = f"{_PREFIX}global-b"
    judged_at_a = _seed_dimension_row(run_a, dimension_id="test_gaming")
    judged_at_b = _seed_dimension_row(run_b, dimension_id="test_gaming")
    conn = _db()
    try:
        calibration.record_review(
            conn,
            run_id=run_a,
            instance_id="inst-1",
            attempt_number=1,
            judged_at=judged_at_a,
            dimension_id="test_gaming",
            decision="approve",
            reviewer_reasoning="fine",
        )
        calibration.record_review(
            conn,
            run_id=run_b,
            instance_id="inst-1",
            attempt_number=1,
            judged_at=judged_at_b,
            dimension_id="test_gaming",
            decision="approve",
            reviewer_reasoning="fine too",
        )
        summary = calibration.calibration_summary(conn)
    finally:
        conn.close()

    by_dim = {d.dimension_id: d for d in summary}
    assert by_dim["test_gaming"].reviewed_count == 2
    assert by_dim["test_gaming"].distinct_instances_reviewed == 2
