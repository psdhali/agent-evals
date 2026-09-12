"""Load and hash the Pass B rubric (offline-analysis-design.md §3.3/§10.3).

``config/judge_rubric.yaml`` is configurable and versioned — ``rubric_sha256``
goes on every ``judge_results``/``judge_dimension_scores`` row so a published
score is always traceable to the exact rubric that produced it (Trap 4: "the
rubric changes and old rows are silently incomparable").
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "judge_rubric.yaml"

# The scale shapes the rubric's dimensions use (§3.3/§10.7). Anything else is a
# rubric authoring error, caught at load time rather than at write time with a
# half-populated judge_dimension_scores row.
#
# `causes` (rubric v3, 2026-09-09): a classified-waste dimension — the judge returns an
# avoidable share (0-1), a severity (0-3) and a list of {cause, share, recommendation}
# entries drawn from analysis/efficiency.EFFICIENCY_CAUSES. score_numeric carries the
# share, score_secondary the severity, and the causes land in their own JSONB column.
VALID_SCALE_TYPES = frozenset(
    {"likert", "count_and_severity", "ratio", "boolean_with_span", "causes"}
)


class RubricError(ValueError):
    """The rubric file is missing, unreadable, or malformed."""


@dataclass(frozen=True)
class Dimension:
    id: str
    question: str
    scale_type: str
    require_evidence: bool = False
    scale_min: int | None = None
    scale_max: int | None = None


@dataclass(frozen=True)
class Rubric:
    version: int
    sha256: str
    dimensions: tuple[Dimension, ...]
    path: Path

    def dimension_ids(self) -> tuple[str, ...]:
        return tuple(d.id for d in self.dimensions)


def load_rubric(path: str | Path | None = None) -> Rubric:
    """Load and validate the rubric. Raises :class:`RubricError` rather than
    guessing — a malformed rubric must fail loudly at pass start, not produce
    judge_results rows an unknown fraction of which silently lack a field."""
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        raise RubricError(f"rubric file not found: {p}")
    raw_bytes = p.read_bytes()
    try:
        data: dict[str, Any] = yaml.safe_load(raw_bytes) or {}
    except yaml.YAMLError as exc:
        raise RubricError(f"rubric file is not valid YAML: {p}: {exc}") from exc

    if "version" not in data:
        raise RubricError(f"rubric {p} has no top-level 'version'")
    if not isinstance(data.get("dimensions"), list) or not data["dimensions"]:
        raise RubricError(f"rubric {p} has no 'dimensions' list")

    dims: list[Dimension] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(data["dimensions"]):
        for field_name in ("id", "question", "scale_type"):
            if field_name not in raw:
                raise RubricError(
                    f"rubric {p}: dimension #{i} missing required field {field_name!r}"
                )
        dim_id = raw["id"]
        if dim_id in seen_ids:
            raise RubricError(f"rubric {p}: duplicate dimension id {dim_id!r}")
        seen_ids.add(dim_id)
        scale_type = raw["scale_type"]
        if scale_type not in VALID_SCALE_TYPES:
            raise RubricError(
                f"rubric {p}: dimension {dim_id!r} has unknown scale_type {scale_type!r} "
                f"(must be one of {sorted(VALID_SCALE_TYPES)})"
            )
        scale = raw.get("scale") or {}
        dims.append(
            Dimension(
                id=dim_id,
                question=raw["question"],
                scale_type=scale_type,
                require_evidence=bool(raw.get("require_evidence", False)),
                scale_min=scale.get("min"),
                scale_max=scale.get("max"),
            )
        )

    return Rubric(
        version=int(data["version"]),
        sha256=f"sha256:{hashlib.sha256(raw_bytes).hexdigest()[:16]}",
        dimensions=tuple(dims),
        path=p,
    )
