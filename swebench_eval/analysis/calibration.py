"""In-app calibration review (offline-analysis-design.md §11).

Supersedes §3.9's offline "hand-label ~20 trajectories, compare afterward" —
the reviewer approves or denies the judge's own per-dimension verdicts,
directly in the app, with reasoning required either way. Recorded
append-only (a re-review is a new row, never an overwrite, same convention
`judge_results` itself uses) so the review history is reconstructable.

§11's honesty tradeoff, restated here so it travels with the code: this is
**reviewer-endorsement rate**, not blind inter-rater agreement — the
reviewer sees the judge's score before forming a decision, which is the
exact bias independent hand-labeling exists to avoid. `reviewer_reasoning`
being required on both approve and deny is the (partial, not complete)
mitigation the design accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from swebench_eval.analysis.rubric import load_rubric

# §3.9's original hand-label threshold, unchanged by §11 — only the
# mechanism for reaching it changed, not the bar itself.
MIN_DISTINCT_REVIEWED_INSTANCES = 20

VALID_DECISIONS = frozenset({"approve", "deny"})


class ReviewTargetNotFoundError(ValueError):
    """No judge_dimension_scores row exists for the given key. Checked
    explicitly so the caller gets a clean error instead of an opaque FK
    IntegrityError from the INSERT."""


def record_review(
    conn: Any,
    *,
    run_id: str,
    instance_id: str,
    attempt_number: int,
    judged_at: str,
    dimension_id: str,
    decision: str,
    reviewer_reasoning: str,
    reviewed_by: str = "operator",
    corrected_score_numeric: float | None = None,
    corrected_score_secondary: float | None = None,
    corrected_flag: bool | None = None,
) -> None:
    """One reviewer decision on one dimension of one judge_results row.
    `judged_at` pins the exact row reviewed — required precisely because a
    re-judge produces a new judge_results row (§4/§10.6): reviewing the
    latest without pinning it would silently re-target a future re-judge's
    row instead of the one the reviewer actually looked at."""
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(VALID_DECISIONS)}, got {decision!r}")
    if not reviewer_reasoning or not reviewer_reasoning.strip():
        raise ValueError("reviewer_reasoning is required on both approve and deny (§11)")

    with conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM judge_dimension_scores
                WHERE run_id = %s AND instance_id = %s AND attempt_number = %s
                  AND judged_at = %s AND dimension_id = %s""",
            (run_id, instance_id, attempt_number, judged_at, dimension_id),
        )
        if cur.fetchone() is None:
            raise ReviewTargetNotFoundError(
                f"no judge_dimension_scores row for run={run_id!r} instance={instance_id!r} "
                f"attempt={attempt_number} judged_at={judged_at!r} dimension={dimension_id!r}"
            )
        cur.execute(
            """INSERT INTO judge_calibration_reviews
                   (run_id, instance_id, attempt_number, judged_at, dimension_id,
                    reviewed_by, decision, reviewer_reasoning,
                    corrected_score_numeric, corrected_score_secondary, corrected_flag)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                run_id,
                instance_id,
                attempt_number,
                judged_at,
                dimension_id,
                reviewed_by,
                decision,
                reviewer_reasoning,
                corrected_score_numeric,
                corrected_score_secondary,
                corrected_flag,
            ),
        )
    conn.commit()


def fetch_reviews_for_result(
    conn: Any, run_id: str, instance_id: str, attempt_number: int, judged_at: str
) -> list[dict[str, Any]]:
    """Every review ever recorded against one judge_results row, newest
    first within each dimension. The UI's per-instance Judge panel needs
    this — without it, a reviewer has no way to see "did I already review
    this?" and would either re-review blind or have to trust their own
    memory across a page reload."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT dimension_id, decision, reviewer_reasoning, reviewed_by, reviewed_at,
                      corrected_score_numeric, corrected_score_secondary, corrected_flag
                 FROM judge_calibration_reviews
                WHERE run_id = %s AND instance_id = %s AND attempt_number = %s
                  AND judged_at = %s
                ORDER BY dimension_id, reviewed_at DESC""",
            (run_id, instance_id, attempt_number, judged_at),
        )
        return [
            {
                "dimension_id": dimension_id,
                "decision": decision,
                "reviewer_reasoning": reviewer_reasoning,
                "reviewed_by": reviewed_by,
                "reviewed_at": reviewed_at.isoformat(),
                "corrected_score_numeric": (
                    float(corrected_score_numeric) if corrected_score_numeric is not None else None
                ),
                "corrected_score_secondary": (
                    float(corrected_score_secondary)
                    if corrected_score_secondary is not None
                    else None
                ),
                "corrected_flag": corrected_flag,
            }
            for (
                dimension_id,
                decision,
                reviewer_reasoning,
                reviewed_by,
                reviewed_at,
                corrected_score_numeric,
                corrected_score_secondary,
                corrected_flag,
            ) in cur.fetchall()
        ]


@dataclass(frozen=True)
class DimensionCalibration:
    dimension_id: str
    reviewed_count: int
    distinct_instances_reviewed: int
    approve_count: int
    deny_count: int
    endorsement_rate: float | None  # None when reviewed_count == 0 — never rendered as 0%
    cleared_threshold: bool  # distinct_instances_reviewed >= MIN_DISTINCT_REVIEWED_INSTANCES


def calibration_summary(conn: Any) -> list[DimensionCalibration]:
    """Global aggregate per dimension — deliberately NOT run-scoped (§11):
    the judge-model alias being calibrated is the same one across every run,
    so the 20-case threshold accumulates across runs too. Every rubric
    dimension is returned, including ones with zero reviews yet, so the
    launch control's coverage readout can show progress for all eight, not
    only the ones someone has touched.

    Counts the LATEST review per (run_id, instance_id, attempt_number,
    judged_at, dimension_id) only — a re-review replaces its predecessor in
    the aggregate the same way fetch_latest_results shows only the newest
    judge_results row, so re-reviewing doesn't inflate the denominator."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (run_id, instance_id, attempt_number, judged_at, dimension_id)
                      run_id, instance_id, attempt_number, dimension_id, decision
                 FROM judge_calibration_reviews
                ORDER BY run_id, instance_id, attempt_number, judged_at, dimension_id,
                         reviewed_at DESC"""
        )
        latest_reviews = cur.fetchall()

    by_dim: dict[str, list[tuple[str, str, int, str]]] = {}
    for run_id, instance_id, attempt_number, dimension_id, decision in latest_reviews:
        by_dim.setdefault(dimension_id, []).append((run_id, instance_id, attempt_number, decision))

    rubric = load_rubric()
    results = []
    for dimension_id in rubric.dimension_ids():
        rows = by_dim.get(dimension_id, [])
        approve_count = sum(1 for *_key, decision in rows if decision == "approve")
        distinct_instances = len({(r, i, a) for r, i, a, _decision in rows})
        results.append(
            DimensionCalibration(
                dimension_id=dimension_id,
                reviewed_count=len(rows),
                distinct_instances_reviewed=distinct_instances,
                approve_count=approve_count,
                deny_count=len(rows) - approve_count,
                endorsement_rate=(approve_count / len(rows)) if rows else None,
                cleared_threshold=distinct_instances >= MIN_DISTINCT_REVIEWED_INSTANCES,
            )
        )
    return results
