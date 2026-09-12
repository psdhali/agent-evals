"""Redis client — live in-flight progress (ADR-0018) + control reads (ADR-0034).

Two consumers.  Live progress stays one TTL'd key per in-flight
``(run_id, instance_id, attempt_number)`` holding the turn-until cumulative
usage; a dead worker's key expires itself.  Live control state (pause/abort,
ADR-0034) is read by every worker at gating points that fire repeatedly, which
is what this module's cached client exists for.

Single client, not a new connection per call: :func:`_redis_from_env` builds a
*new* client every call, which with a control read per poll cycle per worker is
a fresh TCP connection every 20 seconds per task.  This module caches one
client per process (``lru_cache`` over the env factory) and both progress and
control reads use it.  M1 §1.2; ``clear_redis_cache`` exists so tests reach a
fresh client when they manipulate ``REDIS_URL``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _redis_from_env() -> Any:
    """Build a Redis client from REDIS_URL or the local compose defaults.

    ``or`` instead of a ``.get`` default (2026-09-02): a cross-tier terraform
    fallback can render ``REDIS_URL=''`` (set-but-empty), and
    ``redis.from_url("")`` raises ValueError at PARSE time — which crash-looped
    the run-supervisor at boot instead of degrading.  An empty value now falls
    back to the compose default; if that target is unreachable the normal
    fail-closed paths take over (control reads degrade to paused — ADR-0034),
    which is the posture an absent cache is supposed to produce.
    """
    import redis

    return redis.Redis.from_url(os.environ.get("REDIS_URL") or "redis://localhost:6379/0")


@functools.lru_cache(maxsize=1)
def _get_client() -> Any:
    """The process-wide Redis client — ONE connection, not one per call.

    A control read per poll cycle across every worker used to be a new TCP
    connection every 20 seconds, per task.  Cached at module level so all
    readers share one client (M1 §1.2).  Tests call :func:`clear_redis_cache`.
    """
    return _redis_from_env()


def clear_redis_cache() -> None:
    """Drop the cached client (call between tests that change REDIS_URL)."""
    _get_client.cache_clear()


_HARNESS_INSTRUCTIONS_TTL_S = 60 * 24 * 3600  # a run never lives this long; never expire mid-run


def harness_instructions_key(run_id: str) -> str:
    """2026-09-09 efficiency prompt arm: the run's operator instructions, published by
    run_launch (and re-published by restart / open-run recovery from config_snapshot), read
    once per job by the harness worker — the only run-time channel the control plane writes
    and the worker reads (the ADR-0032 job reference carries no prompt text)."""
    return f"run:{run_id}:harness_instructions"


def write_harness_instructions(run_id: str, text: str | None) -> None:
    """Publish (or clear) the run's instructions. Never raises — a Redis failure here must
    not fail a launch; the worker then runs plain and logs that it found none."""
    try:
        client = _get_client()
        key = harness_instructions_key(run_id)
        if text and text.strip():
            client.set(key, text.strip(), ex=_HARNESS_INSTRUCTIONS_TTL_S)
        else:
            client.delete(key)
    except Exception:
        logger.warning("harness_instructions publish failed for run %s", run_id, exc_info=True)


def read_harness_instructions(run_id: str) -> str | None:
    """The run's instructions, or None (none published / Redis unreachable — logged)."""
    try:
        raw = _get_client().get(harness_instructions_key(run_id))
    except Exception:
        logger.warning("harness_instructions read failed for run %s", run_id, exc_info=True)
        return None
    if raw is None:
        return None
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return text.strip() or None


def progress_key(run_id: str, instance_id: str, attempt_number: int) -> str:
    """The Redis key for one in-flight instance's progress."""
    return f"instance_progress:{run_id}:{instance_id}:{attempt_number}"


def is_redis_reachable() -> bool:
    """Can the cached client actually reach Redis right now?

    The /live endpoint uses this to fail closed: when Redis is unreachable the
    WHOLE response is explicitly unknown, never an empty list that reads as
    "nothing is running" (BUILDER1-EXPORT-AND-LIVE-ENDPOINTS §2 — unknown must
    never render as healthy).  A ping on the cached client is cheap; the
    endpoint is polled, not hammered.
    """
    try:
        return bool(_get_client().ping())
    except Exception:  # noqa: BLE001 — reachability is a boolean, never a crash
        return False


# TTL for progress keys — a dead worker's progress disappears on its own.
PROGRESS_TTL_SECONDS = 300


def _usage_payload(
    run_id: str,
    instance_id: str,
    attempt_number: int,
    turn_number: int,
    usage: Any,
    *,
    harness: str | None = None,
    model_alias: str | None = None,
) -> dict[str, Any]:
    """The ONE progress payload shape, from a shim :class:`Usage` (duck-typed).

    METERING-COMPLETENESS (2026-08-28): this is the single serializer both
    write_progress callers (the per-turn shim callback and the end-of-run write)
    must use, so they cannot drift.  Prior to this, the two emitted different
    shapes and a finished instance could be erased with turn_number=0.

    The payload carries the shim's cumulative Usage — all seven fields — plus an
    ``updated_at`` wall-clock stamp.  ``updated_at`` is the staleness signal: the
    key is TTL'd and best-effort (write_progress runs inside try/except), so a
    failed write leaves a STALE payload; a UI rendering "turn 31, $0.42" that is
    four minutes cold is indistinguishable from a live instance without it.  The
    300 s TTL expiring is far too coarse to be the only signal.

    ADVISORY data.  Redis progress is a TTL'd, best-effort mid-run snapshot for
    the live dashboard (ADR-0018); ``llm_calls`` recorded by the shim is the
    authoritative record (ADR-0019).  A consumer must never reconcile the two as
    equals — same failure mode as the ``adapter_*`` columns, which are a
    cross-check and never an addend.

    We deliberately do NOT publish the ceilings (``max_turns``,
    ``max_cost_usd_per_instance``): they are static per run and already in
    ``runs.config_snapshot``; repeating them every turn to save the UI one join
    is the wrong trade.  The UI renders "turn N / 500" client-side.
    """
    import time as _time

    return {
        "run_id": run_id,
        "instance_id": instance_id,
        "attempt_number": attempt_number,
        "turn_number": turn_number,
        # Forecast review 2026-09-03 §3: the L2 planner groups the fleet by alias and picks
        # the per-(pool, harness) curve from these — None on older writers reads as "the run's
        # current alias", never as a fabricated one.
        "harness": harness,
        "model_alias": model_alias,
        # The seven Usage fields, cumulative across the run.
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_tokens": usage.cached_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "cost_usd": usage.cost_usd,
        "source": usage.source,
        "retry_count": usage.retry_count,
        # BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5: the L1 pacer's cumulative
        # footprint — so "stuck at turn 2" is explainable LIVE as "held 97s at the pacer,
        # 3 timeouts, last denied on the token axis behind a queue of 4". getattr: any
        # older Usage shape (tests, other callers) reads as not-measured, never 0.
        "paced_wait_ms_total": getattr(usage, "paced_wait_ms_total", None),
        "paced_calls": getattr(usage, "paced_calls", None),
        "pacer_timeouts": getattr(usage, "pacer_timeouts", None),
        "overload_retries_total": getattr(usage, "overload_retries_total", None),
        "pacer_last_deny_axis": getattr(usage, "pacer_last_deny_axis", None),
        "pacer_last_queue_len": getattr(usage, "pacer_last_queue_len", None),
        # Staleness stamp (see docstring).
        "updated_at": _time.time(),
    }


def write_progress(
    run_id: str,
    instance_id: str,
    attempt_number: int,
    turn_number: int,
    usage: Any,
    ttl: int = PROGRESS_TTL_SECONDS,
    *,
    harness: str | None = None,
    model_alias: str | None = None,
) -> None:
    """Upsert one instance's live progress with a TTL (ADR-0018).

    ``usage`` is the shim's cumulative :class:`Usage` (duck-typed — anyone with
    the same attribute surface works, so this module need not import the harness
    package).  Serialized by :func:`_usage_payload` — the ONE shape for both the
    per-turn callback and the end-of-run write.
    """
    r = _get_client()
    key = progress_key(run_id, instance_id, attempt_number)
    payload = json.dumps(
        _usage_payload(
            run_id,
            instance_id,
            attempt_number,
            turn_number,
            usage,
            harness=harness,
            model_alias=model_alias,
        )
    )
    r.set(key, payload, ex=ttl)


def write_eval_progress(
    run_id: str,
    instance_id: str,
    attempt_number: int,
    grade: dict[str, Any],
    ttl: int = PROGRESS_TTL_SECONDS,
) -> None:
    """Upsert an EVAL-phase attempt's live progress (2026-09-06, django-10097).

    The eval worker's heartbeat publishes :class:`grade_progress.GradeProgress`
    snapshots under the SAME progress key the harness shim uses, so one key
    per attempt is the liveness signal for both phases: the run-supervisor's
    deadline rule (``read_progress`` is None → ABANDONED) stops reaping a live
    67-minute grade, and the dashboard's live panel sees the attempt as
    running instead of "stale".  The harness/usage fields are None (not
    measured — the eval phase makes no model calls); the grade fields carry
    what the tee has seen so far.
    """
    import time as _time

    r = _get_client()
    key = progress_key(run_id, instance_id, attempt_number)
    payload = json.dumps(
        {
            "run_id": run_id,
            "instance_id": instance_id,
            "attempt_number": attempt_number,
            "phase": "eval",
            "turn_number": None,
            "harness": None,
            "model_alias": None,
            "input_tokens": None,
            "output_tokens": None,
            "cached_tokens": None,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
            "cost_usd": None,
            "source": None,
            "retry_count": None,
            "eval_elapsed_s": grade.get("elapsed_s"),
            "eval_lines": grade.get("lines"),
            "eval_bytes": grade.get("bytes"),
            "eval_last_line": grade.get("last_line"),
            "eval_silent_s": grade.get("silent_s"),  # 2026-09-08: seconds since the last byte
            "eval_done": grade.get("done"),
            "updated_at": _time.time(),
        }
    )
    r.set(key, payload, ex=ttl)


def read_progress(run_id: str, instance_id: str, attempt_number: int) -> dict[str, Any] | None:
    """Read an instance's live progress (or None if expired / never written)."""
    r = _get_client()
    raw = r.get(progress_key(run_id, instance_id, attempt_number))
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed
