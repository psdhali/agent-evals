"""Database module — Postgres connection, models, and state machine; plus the
Redis live-progress client (ADR-0018).

In Phase 2, writes happened directly from the smoke test (the Results Writer and
SQS queues arrived in Phase 3).  The Results Writer uses this module unchanged.
Live in-flight progress lives on Redis, not Postgres, so the harness worker has
no Postgres write path (ADR-0007 now has zero exceptions).
"""

from swebench_eval.database.connection import (
    ensure_additional_databases,
    get_connection,
    run_migrations,
)
from swebench_eval.database.models import InstanceResult, Run
from swebench_eval.database.redis_client import (
    PROGRESS_TTL_SECONDS,
    read_progress,
    write_progress,
)
from swebench_eval.database.state_machine import (
    ErrorCategory,
    EvalState,
    HarnessState,
    map_terminated_reason_to_error_category,
    map_terminated_reason_to_state,
)

__all__ = [
    "PROGRESS_TTL_SECONDS",
    "ErrorCategory",
    "EvalState",
    "HarnessState",
    "InstanceResult",
    "Run",
    "ensure_additional_databases",
    "get_connection",
    "map_terminated_reason_to_error_category",
    "map_terminated_reason_to_state",
    "read_progress",
    "run_migrations",
    "write_progress",
]
