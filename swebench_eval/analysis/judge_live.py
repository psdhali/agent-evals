"""Live progress of one judge pass — a single TTL'd Redis key per run.

Pass B (:mod:`judge`) persists its summary row only when the pass ENDS (judge_sampling), so an
in-progress pass was invisible everywhere: the run screen showed "no judge pass has run yet"
for the whole hour. This is the supplement, in the exact shape of the harness/eval live
progress (ADR-0018: Redis, TTL'd, best-effort, never the source of truth): the pass's one
DB-writer thread publishes a snapshot after every submission and every completion, and
``GET /runs/{run_id}/judge/live`` reads it back. When the pass finishes the last snapshot
(status ``done``/``failed``) lingers for :data:`JUDGE_LIVE_FINAL_TTL_S` so the operator sees
the finish, then the persisted pass summary is the only record — as before.

Every write here swallows its errors: a judge pass must never fail because Valkey is
unreachable (the judge task ran without REDIS_URL at all until 2026-09-07).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

JUDGE_LIVE_TTL_S = 900  # a pass that stops publishing reads as gone after 15 min
JUDGE_LIVE_FINAL_TTL_S = 600  # the done/failed snapshot lingers 10 min, then the DB row is it


def judge_live_key(run_id: str) -> str:
    return f"judge:live:{run_id}"


@dataclass
class InFlight:
    instance_id: str
    attempt_number: int
    started_at: float


@dataclass
class JudgeLiveState:
    run_id: str
    pass_id: str
    status: str  # running | synthesizing | done | failed | stopped (operator stop-task)
    workers: int
    selected: int  # candidates the stratifier picked for this pass
    judged: int = 0
    skipped_over_budget: int = 0
    parse_failed: int = 0
    skipped_artifacts: int = 0  # artifact fetch failed -> not judged, not an error
    # 2026-09-08: a candidate whose judge call never got an answer (429/5xx/connection,
    # retries exhausted) — recorded, the pass continues, the candidate stays unjudged so a
    # later pass picks it up. Not a parse failure (the judge never answered).
    call_failed: int = 0
    # 2026-09-08: judge still generating at the 10-min ceiling — recorded as a judgment
    # with no verdict (judge_method "timeout"); a resume skips it.
    timed_out: int = 0
    # candidates the pass skipped because a judge_results row already exists for them
    # (resume semantics; 0 when launched with rejudge=true).
    already_judged: int = 0
    spend_usd: float = 0.0
    max_spend_usd: float = 0.0
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    in_flight: list[InFlight] = field(default_factory=list)
    last_error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        d = asdict(self)
        done = (
            self.judged
            + self.skipped_over_budget
            + self.skipped_artifacts
            + self.call_failed
            + self.timed_out
        )
        elapsed = max(0.0, (self.finished_at or time.time()) - self.started_at)
        # ETA from the running average of completions — None until something completed.
        remaining = max(0, self.selected - done - len(self.in_flight))
        rate = (self.judged / elapsed) if (self.judged and elapsed > 0) else 0.0
        d["elapsed_s"] = round(elapsed, 1)
        d["eta_s"] = round((remaining + len(self.in_flight)) / rate, 1) if rate > 0 else None
        d["in_flight_count"] = len(self.in_flight)
        return d


def write_judge_live(state: JudgeLiveState, *, client: Any | None = None) -> bool:
    """Publish *state*; True when written. Never raises."""
    state.updated_at = time.time()
    ttl = JUDGE_LIVE_FINAL_TTL_S if state.status in ("done", "failed") else JUDGE_LIVE_TTL_S
    try:
        if client is None:
            from swebench_eval.database.redis_client import _get_client

            client = _get_client()
        client.set(judge_live_key(state.run_id), json.dumps(state.to_payload()), ex=ttl)
        return True
    except Exception as exc:  # noqa: BLE001 — live progress is a supplement, never a gate
        logger.debug("judge live: publish skipped (%s)", exc)
        return False


def read_judge_live(run_id: str, *, client: Any | None = None) -> dict[str, Any] | None:
    """The latest snapshot for *run_id*, or None (no pass, expired, or Redis unreachable)."""
    try:
        if client is None:
            from swebench_eval.database.redis_client import _get_client

            client = _get_client()
        raw = client.get(judge_live_key(run_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("judge live: read skipped (%s)", exc)
        return None
    if not raw:
        return None
    try:
        doc = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except (ValueError, AttributeError):
        return None
    return doc if isinstance(doc, dict) else None
