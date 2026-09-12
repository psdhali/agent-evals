"""Redis transport for a run's raw LiteLLM virtual key (rule 3, orchestrator -> dispatcher).

The orchestrator mints a run's LiteLLM virtual key at PROVISION and persists
only its non-secret id to Aurora (``runs.litellm_key_id`` — rule 3: never the
key itself, never in ``config_snapshot``, a log, or a fixture).  The RAW key
still has to reach every task ``containerOverrides`` the harness dispatcher
launches for that run, potentially hours after minting and across many
launches — so it needs a transport between the two processes that is not
Aurora (never store the raw key there) and not a fresh Secrets Manager entry
per run (that IAM surface does not exist for the orchestrator today and adding
it is a bigger change than this run-scoped, already-budget-capped,
independently-revocable credential warrants).

Redis is the pragmatic middle ground: the same store already carries
``control:flags``/``control:aborted`` (ADR-0034) and per-instance live
progress (ADR-0018), both similarly ephemeral and run-scoped.  A LiteLLM
virtual key is not an unscoped admin credential — it is exactly the thing
ADR-0035 was designed to make safe to hold *anywhere* other than the untrusted
agent container: rate-limited, budget-capped, and revocable in one call
(:func:`swebench_eval.gateway.admin.delete_key`).  The TTL is a safety net,
not the primary lifecycle — :func:`clear` is called at finalisation
(§8 step 3) so a normally-completed run leaves nothing behind; the TTL only
protects against a run that crashes before finalising.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Generous safety net: long enough that no real run's dispatch window can
# outlive it (a run at 20k concurrent instances, ADR-0018, is bounded in
# hours not days), short enough that a crashed run's key does not linger in
# Redis forever if finalisation never runs.
_TTL_SECONDS = 7 * 24 * 3600


def _redis() -> Any:
    from swebench_eval.database.redis_client import _get_client

    return _get_client()


def _key(run_id: str) -> str:
    return f"run:{run_id}:litellm_api_key"


def store(run_id: str, raw_key: str) -> None:
    """Cache *raw_key* for *run_id* — the ONLY place the raw key is written."""
    _redis().set(_key(run_id), raw_key, ex=_TTL_SECONDS)


def fetch(run_id: str) -> str | None:
    """Return the cached raw key for *run_id*, or ``None`` if absent/expired.

    ``None`` here means the harness dispatcher must refuse to launch that
    job (fail closed, D4's spirit extended to dispatch: a task must never run
    without its per-run key — falling back to the master key would silently
    reopen the exact hole ADR-0035 closes).
    """
    r = _redis()
    try:
        raw = r.get(_key(run_id))
    except Exception:
        logger.warning("run_key_cache: could not read key for run %s", run_id, exc_info=True)
        return None
    if raw is None:
        return None
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def clear(run_id: str) -> None:
    """Remove the cached key for *run_id* (finalisation §8 step 3, best-effort)."""
    try:
        _redis().delete(_key(run_id))
    except Exception:
        logger.warning("run_key_cache: could not clear key for run %s", run_id, exc_info=True)
