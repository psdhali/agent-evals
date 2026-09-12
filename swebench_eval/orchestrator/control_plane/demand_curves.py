"""Fitted demand curves for the L2 planner — the consumer side of
``scripts/fit_growth_curves.py`` (BUILDER4-DISPATCHER-FORECAST-REVIEW-2026-09-03.md §2).

Until 2026-09-03 the planner had no code path that loaded a refit at all: every decision was
computed from the hard-coded pooled constants (``curve_source=pooled_default``) — the refit
script existed, its output was never read. This module loads the packaged JSON (or an
``AUTOSCALER_CURVES_PATH`` override) and hands out one :class:`DemandModel` per
``(provider pool, harness)`` through an explicit fallback chain, recording the level used in
the model's ``source`` so every decision record says what it was computed from:

    fitted:<pool>|<harness>      the group itself (>= MIN_CALLS calls)
    fitted:harness:<key>         another pool's fit for the SAME harness — token growth per
                                 turn is harness behaviour; latency/period still come from the
                                 pool if it has any group at all
    fitted:pooled                the document's pooled fit
    pooled_default               the hard-coded constants (no usable document)

Per-field fallback within a group: survival needs >= MIN_SURVIVAL_ATTEMPTS attempts behind the
turn-0 window, period needs a measured value — either absent falls to the next level for THAT
field only, and the source string carries a ``+`` suffix naming what fell through.
"""

from __future__ import annotations

import json
import logging
import os
from importlib import resources
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MIN_CALLS = 30
MIN_SURVIVAL_ATTEMPTS = 20
_PACKAGED = "growth_curves.json"


class DemandCurves:
    """The loaded document. ``groups`` keys are ``"<pool>|<harness>"``."""

    def __init__(self, doc: dict[str, Any] | None, *, origin: str = "none") -> None:
        self.doc = doc or {}
        self.origin = origin
        self.groups: dict[str, dict[str, Any]] = dict(self.doc.get("groups") or {})
        self.pooled: dict[str, Any] | None = self.doc.get("pooled")
        self.fitted_at: str | None = self.doc.get("fitted_at")

    @classmethod
    def load(cls, path: str | None = None) -> DemandCurves:
        """``AUTOSCALER_CURVES_PATH`` > the packaged document > empty (pooled defaults)."""
        candidate = path or os.environ.get("AUTOSCALER_CURVES_PATH")
        if candidate:
            try:
                return cls(json.loads(Path(candidate).read_text()), origin=candidate)
            except (OSError, ValueError):
                logger.warning("demand curves: cannot read %s; using packaged", candidate)
        try:
            raw = resources.files("swebench_eval.orchestrator.control_plane").joinpath(
                "data", _PACKAGED
            )
            return cls(json.loads(raw.read_text()), origin="packaged")
        except (OSError, ValueError, ModuleNotFoundError, TypeError):
            logger.warning("demand curves: no packaged document; planner on pooled defaults")
            return cls(None, origin="none")

    # -- selection ----------------------------------------------------------------------------

    def _group(self, pool: str | None, harness: str | None) -> tuple[dict[str, Any] | None, str]:
        if pool and harness:
            g = self.groups.get(f"{pool}|{harness}")
            if g and not g.get("insufficient") and int(g.get("n", 0)) >= MIN_CALLS:
                return g, f"fitted:{pool}|{harness}"
        if harness:
            # Same harness, any pool, the best-populated: token growth per turn is harness
            # behaviour (how much context the agent accumulates), not provider behaviour.
            best_key, best = None, None
            for key, g in self.groups.items():
                if (
                    key.endswith(f"|{harness}")
                    and not g.get("insufficient")
                    and (best is None or int(g.get("n", 0)) > int(best.get("n", 0)))
                ):
                    best_key, best = key, g
            if best is not None:
                return best, f"fitted:harness:{best_key}"
        if self.pooled and int(self.pooled.get("n", 0)) >= MIN_CALLS:
            return self.pooled, "fitted:pooled"
        return None, "pooled_default"

    def _pool_latency_period(self, pool: str | None) -> tuple[float | None, float | None]:
        """Latency / period are provider behaviour: take them from ANY group of this pool
        (the best-populated with a measured period) when the exact group fell through."""
        best = None
        for key, g in self.groups.items():
            if (
                pool
                and key.startswith(f"{pool}|")
                and g.get("turn_period_s") is not None
                and (best is None or int(g.get("n", 0)) > int(best.get("n", 0)))
            ):
                best = g
        if best is None:
            return None, None
        return best.get("latency_s"), best.get("turn_period_s")

    def model_for(self, pool: str | None, harness: str | None) -> Any:
        """A :class:`DemandModel` for this (pool, harness) with the fallback chain applied."""
        from swebench_eval.orchestrator.control_plane.harness_dispatcher import DemandModel

        g, source = self._group(pool, harness)
        if g is None:
            return DemandModel(source="pooled_default")

        fell: list[str] = []
        survival_raw = g.get("survival") or {}
        attempts = g.get("survival_attempts") or {}
        survival: dict[int, float] | None = None
        # F5: the attempts behind the table the planner will use — the group's own count, or
        # 0 for a pooled fallback (borrowed evidence, so the planner applies no discount).
        survival_attempts = 0
        if survival_raw and int(attempts.get("0", 0)) >= MIN_SURVIVAL_ATTEMPTS:
            survival = {int(k): float(v) for k, v in survival_raw.items()}
            survival_attempts = int(attempts.get("0", 0))
        elif self.pooled and (self.pooled.get("survival") or {}):
            survival = {int(k): float(v) for k, v in self.pooled["survival"].items()}
            fell.append("survival:pooled")

        latency_s = g.get("latency_s")
        period_s = g.get("turn_period_s")
        if (latency_s is None or period_s is None) and not source.startswith("fitted:harness"):
            pl, pp = self._pool_latency_period(pool)
            latency_s = latency_s if latency_s is not None else pl
            period_s = period_s if period_s is not None else pp
        if source.startswith("fitted:harness"):
            # Provider behaviour must come from THIS pool if it has any evidence at all.
            pl, pp = self._pool_latency_period(pool)
            if pl is not None:
                latency_s, period_s = pl, pp
            else:
                fell.append("latency/period:other-pool")
        if (latency_s is None or period_s is None) and self.pooled:
            latency_s = latency_s if latency_s is not None else self.pooled.get("latency_s")
            period_s = period_s if period_s is not None else self.pooled.get("turn_period_s")
            fell.append("latency/period:pooled")

        return DemandModel(
            a=float(g["a"]),
            b=float(g["b"]),
            latency_s=float(latency_s) if latency_s is not None else None,
            turn_period_s=float(period_s) if period_s is not None else None,
            survival=survival,
            source=source + (("+" + ",".join(fell)) if fell else ""),
            # F3: the group's own fitted cache-hit share (0 when the fit predates the field).
            cached_share=float(g.get("cached_share") or 0.0),
            survival_attempts=survival_attempts,
        )
