"""``run_events`` — the operator moments nothing else records (2026-09-04, timeline plan §4.3).

Pause / resume only keep a CURRENT state row (``control_state``); the publication site's
timeline needs the moment they happened. Launch, dispatch, abort and finalise already stamp
``runs.*_at``; limit edits are in ``operator_limit_edits``; discovery steps in
``model_tpm_observations`` — none of those are duplicated here.

``record`` never raises: an audit insert must not be able to fail the control action it
describes (the same rule operator_limits._audit follows).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def record(
    conn: Any,
    kind: str,
    *,
    run_id: str | None = None,
    actor: str = "operator",
    reason: str = "",
    detail: dict[str, Any] | None = None,
) -> bool:
    """Insert one event on *conn* (caller commits). Returns False on failure, never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO run_events (run_id, kind, actor, reason, detail)
                   VALUES (%s, %s, %s, %s, %s::jsonb)""",
                (run_id, kind, actor, reason, json.dumps(detail or {})),
            )
        return True
    except Exception:
        logger.warning("run_events: could not record %s (run=%s)", kind, run_id, exc_info=True)
        return False
