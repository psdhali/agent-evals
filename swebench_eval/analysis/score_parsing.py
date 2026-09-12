"""Turn a parsed judge response into judge_dimension_scores rows
(offline-analysis-design.md §3.3/§3.6/§10.3/§10.7).

Per dimension:
- ``reasoning`` is required (§10.3) regardless of scale_type or
  require_evidence — a score with no stated reasoning is not accepted.
- ``require_evidence`` dimensions scored above baseline with no evidence
  entries are demoted to ``evidence_missing = True`` rather than kept as a
  score (§3.3 — "an assertion that cannot be checked is not a measurement").
- Fields are mapped per scale_type into the normalized
  (score_numeric, score_secondary, flag, span_start_turn, span_end_turn)
  shape §10.7's table uses — never a column per dimension.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from swebench_eval.analysis.efficiency import EFFICIENCY_CAUSES
from swebench_eval.analysis.rubric import Dimension, Rubric


@dataclass(frozen=True)
class DimensionScore:
    dimension_id: str
    scale_type: str
    score_numeric: float | None
    score_secondary: float | None
    flag: bool | None
    span_start_turn: int | None
    span_end_turn: int | None
    reasoning: str | None
    evidence: list[dict[str, Any]]
    evidence_missing: bool
    missing_reasoning: bool  # honesty flag: the judge skipped this dimension entirely
    # rubric v3 `causes` scale: [{cause, share, recommendation}], else empty
    causes: list[dict[str, Any]] = field(default_factory=list)


def _parse_causes(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The `causes` list, normalised: known cause ids only (anything else becomes
    "other" with the original name kept in `label`), share clamped to 0-1 or null,
    recommendation a string or null. Order preserved; duplicates collapsed."""
    raw = entry.get("causes")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            item = {"cause": item}
        if not isinstance(item, dict):
            continue
        cause = str(item.get("cause") or item.get("id") or "").strip()
        if not cause:
            continue
        norm = cause.lower().replace("-", "_").replace(" ", "_")
        label = None
        if norm not in EFFICIENCY_CAUSES:
            label = cause
            norm = "other"
        key = norm if norm != "other" else f"other:{label}"
        if key in seen:
            continue
        seen.add(key)
        share = item.get("share")
        share_f = min(1.0, max(0.0, float(share))) if isinstance(share, (int, float)) else None
        rec = item.get("recommendation")
        out.append(
            {
                "cause": norm,
                **({"label": label} if label else {}),
                "share": share_f,
                "recommendation": rec.strip() if isinstance(rec, str) and rec.strip() else None,
            }
        )
    return out


def _is_above_baseline(dim: Dimension, entry: dict[str, Any]) -> bool:
    if dim.scale_type == "likert":
        score = entry.get("score")
        return isinstance(score, (int, float)) and score > 0
    if dim.scale_type == "count_and_severity":
        count = entry.get("count")
        return isinstance(count, (int, float)) and count > 0
    if dim.scale_type == "ratio":
        redundant = entry.get("redundant")
        return isinstance(redundant, (int, float)) and redundant > 0
    if dim.scale_type == "boolean_with_span":
        return bool(entry.get("present"))
    if dim.scale_type == "causes":
        severity = entry.get("severity")
        share = entry.get("avoidable_share")
        return (
            (isinstance(severity, (int, float)) and severity > 0)
            or (isinstance(share, (int, float)) and share > 0)
            or bool(_parse_causes(entry))
        )
    return False


def parse_dimension(dim: Dimension, entry: dict[str, Any] | None) -> DimensionScore:
    """*entry* is ``parsed["dimensions"].get(dim.id)`` — may be missing
    entirely if the judge skipped a dimension (a real, honest possibility,
    not assumed away)."""
    if entry is None or not isinstance(entry, dict):
        return DimensionScore(
            dimension_id=dim.id,
            scale_type=dim.scale_type,
            score_numeric=None,
            score_secondary=None,
            flag=None,
            span_start_turn=None,
            span_end_turn=None,
            reasoning=None,
            evidence=[],
            evidence_missing=dim.require_evidence,
            missing_reasoning=True,
        )

    reasoning = entry.get("reasoning")
    missing_reasoning = not isinstance(reasoning, str) or not reasoning.strip()

    raw_evidence = list(entry.get("evidence") or [])
    if dim.scale_type == "causes":
        # 2026-09-09 (first v3 pass, d179d3a7): the judge cited turns INSIDE each cause and
        # gave no top-level evidence list, so §3.3 demoted every token_efficiency score to
        # evidence_missing. Per-cause evidence is evidence — harvest it into the dimension's
        # list before the demotion rule runs. An answer with no cited turn anywhere is still
        # demoted, exactly as before.
        raw_causes = entry.get("causes")
        for item in raw_causes if isinstance(raw_causes, list) else []:
            if isinstance(item, dict) and isinstance(item.get("evidence"), list):
                raw_evidence.extend(item["evidence"])
    evidence = [e for e in raw_evidence if isinstance(e, dict) and "turn" in e and "quote" in e]

    above_baseline = _is_above_baseline(dim, entry)
    evidence_missing = dim.require_evidence and above_baseline and not evidence

    # §3.3: a dimension scored above baseline with no evidence is DEMOTED to
    # evidence_missing rather than kept as a score — never publish an
    # unverifiable assertion as a measurement.
    demote = evidence_missing

    score_numeric: float | None = None
    score_secondary: float | None = None
    flag: bool | None = None
    span_start: int | None = None
    span_end: int | None = None
    causes: list[dict[str, Any]] = []

    if not demote:
        if dim.scale_type == "likert":
            score_numeric = entry.get("score")
        elif dim.scale_type == "count_and_severity":
            score_numeric = entry.get("count")
            score_secondary = entry.get("severity")
        elif dim.scale_type == "ratio":
            score_numeric = entry.get("redundant")
            score_secondary = entry.get("total")
        elif dim.scale_type == "boolean_with_span":
            flag = bool(entry.get("present"))
            span_start = entry.get("first_turn")
            span_end = entry.get("last_turn")
        elif dim.scale_type == "causes":
            share = entry.get("avoidable_share")
            score_numeric = (
                min(1.0, max(0.0, float(share))) if isinstance(share, (int, float)) else None
            )
            sev = entry.get("severity")
            score_secondary = float(sev) if isinstance(sev, (int, float)) else None
            causes = _parse_causes(entry)

    return DimensionScore(
        dimension_id=dim.id,
        scale_type=dim.scale_type,
        score_numeric=score_numeric,
        score_secondary=score_secondary,
        flag=flag,
        span_start_turn=span_start,
        span_end_turn=span_end,
        reasoning=reasoning if isinstance(reasoning, str) else None,
        evidence=evidence,
        evidence_missing=evidence_missing,
        missing_reasoning=missing_reasoning,
        causes=causes,
    )


def parse_all_dimensions(rubric: Rubric, parsed_response: dict[str, Any]) -> list[DimensionScore]:
    dims_block = parsed_response.get("dimensions")
    dims_block = dims_block if isinstance(dims_block, dict) else {}
    return [parse_dimension(d, dims_block.get(d.id)) for d in rubric.dimensions]


def count_scored_dimensions(dim_scores: list[DimensionScore]) -> int:
    """How many dimensions the judge actually ADDRESSED — produced reasoning
    for (a legitimate baseline-with-reasoning verdict counts; only a
    silently-absent entry does not).

    Zero is the empty-judgment failure signature (ADR-0042): a response that
    parsed as valid JSON but carried no ``dimensions`` block at all — the
    reasoning-model serialization failure where the full judgment lands in
    ``reasoning_content`` and ``content`` is a hollow ``{": ": ", "}``.
    ``parse_all_dimensions`` turns that into every dimension coming back
    ``missing_reasoning=True``, which used to be recorded as a successful
    judged=1 with all-null scores. Callers treat count==0 as a parse failure
    that must be recovered or flagged, never a silent zero-score judgment.
    """
    return sum(1 for d in dim_scores if not d.missing_reasoning)
