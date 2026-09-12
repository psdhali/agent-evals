"""The ONE autoscaler decision-record contract — shared by harness and eval.

BUILDER4-EVAL-AUTOSCALER-2026-08-31.md §5 + owner-approved decision 4 (2026-09-01): the two
autoscalers were deliberately assigned to one builder so they publish the SAME field, same shape,
same staleness semantics — this module is what makes that structural instead of aspirational.
The observation tick / capacity view reads ``autoscaler:last_decision:{pool}`` and ages it on
display; a dead autoscaler's record goes visibly stale (the TTL is a backstop, ``decided_at`` is
the staleness signal — "holding steady" and "died 20 minutes ago" must never look the same).

The pools' *extra* fields legitimately differ (harness has token budgets, eval has host counts);
what is shared and REQUIRED is the envelope below. ``validate_record`` is the contract's test
hook — both sides' suites assert their published records pass it.

binding_constraint vocabularies (union, per the reviewer's doc + approved additions):
  harness: queue_empty | arrival_budget | inflight_budget | qps_budget | at_capacity |
           ecs_quota | paused | stalled | ramp_limited | cooldown | paced | none
  eval:    queue_empty | at_max_workers | no_hosts | asg_at_max | scale_in_damped |
           scale_in_blocked_busy | paused | none
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Every published record MUST carry these; anything else is pool-specific extra.
REQUIRED_FIELDS = ("decided_at", "mode", "desired_ceiling", "binding_constraint")

_KEY_TEMPLATE = "autoscaler:last_decision:{pool}"
_TTL_S = 3600


def decision_key(pool: str) -> str:
    return _KEY_TEMPLATE.format(pool=pool)


def validate_record(record: dict[str, Any]) -> None:
    """Raise ValueError on a record that violates the shared envelope."""
    missing = [f for f in REQUIRED_FIELDS if f not in record]
    if missing:
        raise ValueError(f"decision record missing required fields: {missing}")
    if not isinstance(record["decided_at"], (int, float)):
        raise TypeError("decided_at must be epoch seconds (the staleness signal)")


def publish(redis_client: Any, pool: str, record: dict[str, Any]) -> None:
    """One-way broadcast — read by the observation tick / capacity view, NEVER read back by any
    admission decision. Emitted every tick including no-change ticks (the no-change tick is the
    most informative one — capacity-emission doc). A failed publish never affects scaling."""
    try:
        validate_record(record)
        redis_client.set(decision_key(pool), json.dumps(record), ex=_TTL_S)
    except Exception:
        logger.debug("decision-record publish failed for pool=%s", pool, exc_info=True)
