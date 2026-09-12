"""Gateway pause/resume via LiteLLM key-block —
BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md.

Closes the gap where the ``gateway`` pool's pause flag round-tripped through
Aurora/Valkey/the UI but nothing downstream ever acted on it (the ADR-0034
§M1.11 ALB toggle was never implemented). This blocks/unblocks the run's own
LiteLLM virtual key (``runs.litellm_key_id``) instead — no AWS work, and it
gives a capability the ALB design never had: pausing one run without
touching others.

Two independent actors touch the same underlying fact ("is this run's key
blocked") — a global sweep (folded into the existing ``/control/pause|resume``
handler for the ``gateway`` pool) and a per-run operator action (this
module's ``pause_gateway``/``resume_gateway``). §2's precedence table is
**most-specific-wins**, tracked in ``runs.gateway_key_blocked_by``
(``NULL`` / ``'global'`` / ``'operator'``):

* Global pause only ever *sets* ``'global'`` on rows currently ``NULL`` — an
  operator-held block is left untouched.
* Global resume only ever *touches* rows currently ``'global'`` — same reason.
* Per-run pause/resume unconditionally overwrite to ``'operator'``/``NULL``
  regardless of what was there: an explicit per-run action is always
  authoritative for that one run.

This is orchestrator-side Aurora bookkeeping only — the shim never reads this
column, it only ever reacts to the block/unblock *effect* on a call (the 401
marker, ``harnesses.routing.is_operator_block_response``).
"""

from __future__ import annotations

import logging
from typing import Any

from swebench_eval.database.state_machine import is_run_closed
from swebench_eval.gateway import admin as gateway_admin
from swebench_eval.harnesses.routing import gateway_api_key, gateway_base_url

logger = logging.getLogger(__name__)


class GatewayPauseError(Exception):
    """A per-run gateway pause/resume request could not be carried out."""


def _db() -> Any:
    from swebench_eval.database.connection import get_connection

    return get_connection()


# ---------------------------------------------------------------------------
# Per-run (new, alongside close/restart/abort) — §5
# ---------------------------------------------------------------------------


def pause_gateway(run_id: str, *, actor: str = "operator") -> dict[str, Any]:
    """Block *run_id*'s LiteLLM key; set ``gateway_key_blocked_by = 'operator'``
    unconditionally. Legal any time the run isn't closed (same gate as
    :func:`restart.restart_instances`), regardless of the global flag's
    current state (§2's table: an explicit per-run action always wins).
    """
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, litellm_key_id FROM runs WHERE run_id = %s FOR UPDATE",
                (run_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise GatewayPauseError(f"no such run: {run_id}")
            status, litellm_key_id = row
            if is_run_closed(status):
                raise GatewayPauseError(
                    f"run {run_id} is closed (status={status!r}) — its key is already revoked"
                )
            if litellm_key_id:
                gateway_admin.block_key(gateway_base_url(), gateway_api_key(), litellm_key_id)
            cur.execute(
                "UPDATE runs SET gateway_key_blocked_by = 'operator' WHERE run_id = %s",
                (run_id,),
            )
        conn.commit()
    finally:
        conn.close()
    logger.info("run %s: gateway paused by operator %s", run_id, actor)
    return {"run_id": run_id, "gateway_key_blocked_by": "operator"}


def resume_gateway(run_id: str, *, actor: str = "operator") -> dict[str, Any]:
    """Unblock *run_id*'s LiteLLM key; set ``gateway_key_blocked_by = NULL``
    unconditionally — a carve-out: this run resumes even if a global pause
    is still active for every other run (§2's table).
    """
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, litellm_key_id FROM runs WHERE run_id = %s FOR UPDATE",
                (run_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise GatewayPauseError(f"no such run: {run_id}")
            status, litellm_key_id = row
            if is_run_closed(status):
                raise GatewayPauseError(
                    f"run {run_id} is closed (status={status!r}) — its key is already revoked"
                )
            if litellm_key_id:
                gateway_admin.unblock_key(gateway_base_url(), gateway_api_key(), litellm_key_id)
            cur.execute(
                "UPDATE runs SET gateway_key_blocked_by = NULL WHERE run_id = %s",
                (run_id,),
            )
        conn.commit()
    finally:
        conn.close()
    logger.info("run %s: gateway resumed by operator %s", run_id, actor)
    return {"run_id": run_id, "gateway_key_blocked_by": None}


# ---------------------------------------------------------------------------
# Global sweep (folded into /control/pause|resume for the "gateway" pool) — §5
# ---------------------------------------------------------------------------


def sweep_global_pause() -> int:
    """Block the key of every active, currently-unblocked run.

    F1 (implementation review, 2026-08-31): "active" is NOT just
    ``status = 'running'`` — that population misses a run mid-launch. The
    original version relied on ``block_if_globally_paused``'s one-shot
    re-check (called right after key mint) to close that window, but a
    500-instance run's SEED+DISPATCH phase is hundreds of sequential SQS
    sends — the LONGEST phase of a launch, not an instant — and a pause
    landing anywhere in it slipped through both mechanisms (proved with a
    row-scoped test: rows stuck at 'seeding'/'dispatching' never got
    blocked). Structural fix: widen the population to every in-flight
    pre-running status too, so the sweep itself catches these runs instead
    of depending on launch_run remembering to re-check.

    Only rows with ``gateway_key_blocked_by IS NULL`` are swept — a row
    already ``'operator'`` is left untouched (§2's table: "operator" always
    wins). Returns the count of runs actually blocked (F2, below — NOT the
    count of rows touched; see the marker-stamping note).
    """
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT run_id, litellm_key_id FROM runs
                   WHERE status IN ('provisioning', 'seeding', 'dispatching', 'running')
                     AND gateway_key_blocked_by IS NULL""")
            rows = cur.fetchall()
            base, master = gateway_base_url(), gateway_api_key()
            blocked = 0
            for run_id, litellm_key_id in rows:
                # F2 (implementation review, 2026-08-31): the widened population
                # above includes rows caught between CLAIM and _record_key_ids,
                # where litellm_key_id is still NULL — nothing to block yet.
                # The naive "block conditionally, mark unconditionally" version
                # stamped 'global' on those rows anyway, which then made
                # block_if_globally_paused's own `IS NULL` guard skip them
                # later — an unblocked key that reads as blocked, strictly
                # worse than the original bug (unknown must never render as
                # healthy). Only stamp the marker when a key was ACTUALLY
                # blocked; a no-key row is left NULL so
                # block_if_globally_paused still catches it moments later,
                # once the key exists.
                if not litellm_key_id:
                    continue
                gateway_admin.block_key(base, master, litellm_key_id)
                cur.execute(
                    "UPDATE runs SET gateway_key_blocked_by = 'global' WHERE run_id = %s",
                    (run_id,),
                )
                blocked += 1
        conn.commit()
    finally:
        conn.close()
    logger.info("global gateway pause: blocked %d run key(s)", blocked)
    return blocked


def block_if_globally_paused(run_id: str, litellm_key_id: str | None) -> bool:
    """Belt-and-suspenders catch for the launch-vs-global-pause race — the
    structural fix is :func:`sweep_global_pause`'s widened population (F1,
    implementation review 2026-08-31), not this function alone. An earlier
    version of this docstring claimed this call closed the whole
    PROVISION/SEED/DISPATCH window down to "negligible" — the reviewer
    proved that wrong with a row-scoped test (rows stuck at
    'seeding'/'dispatching' were never blocked): the window between this
    call and ``launch_run`` reaching ``status = 'running'`` is real and can
    span hundreds of sequential SQS sends for a large run, not an instant.

    What THIS function still legitimately closes: the moment between CLAIM
    (before which the run doesn't exist as a row a sweep could ever see) and
    the key actually being recorded — the one gap the widened sweep cannot
    cover no matter how it's widened, since there is genuinely no key to
    block yet. Call it right after the key is minted and recorded
    (:func:`run_launch._record_key_ids`). Re-reads the same global flag
    (:func:`control_state.is_paused`) and, if now paused, blocks the
    just-minted key immediately. Returns True if it blocked (False when not
    paused, or when ``litellm_key_id`` is falsy — nothing to block yet).
    """
    if not litellm_key_id:
        return False
    from swebench_eval.control import state as control_state

    if not control_state.is_paused("gateway"):
        return False
    conn = _db()
    try:
        with conn.cursor() as cur:
            gateway_admin.block_key(gateway_base_url(), gateway_api_key(), litellm_key_id)
            cur.execute(
                "UPDATE runs SET gateway_key_blocked_by = 'global' "
                "WHERE run_id = %s AND gateway_key_blocked_by IS NULL",
                (run_id,),
            )
        conn.commit()
    finally:
        conn.close()
    logger.info(
        "run %s: gateway blocked at provision time (global pause landed mid-launch)", run_id
    )
    return True


def sweep_global_resume() -> int:
    """Unblock the key of every run currently blocked by the global sweep.

    Only rows with ``gateway_key_blocked_by = 'global'`` are touched — a row
    held ``'operator'`` is untouched (§2's table: "'operator' + global resume
    -> 'operator' — not touched — this is the actual fix"). Returns the count
    of runs unblocked.
    """
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT run_id, litellm_key_id FROM runs
                   WHERE gateway_key_blocked_by = 'global'""")
            rows = cur.fetchall()
            base, master = gateway_base_url(), gateway_api_key()
            for run_id, litellm_key_id in rows:
                if litellm_key_id:
                    gateway_admin.unblock_key(base, master, litellm_key_id)
                cur.execute(
                    "UPDATE runs SET gateway_key_blocked_by = NULL WHERE run_id = %s",
                    (run_id,),
                )
        conn.commit()
    finally:
        conn.close()
    logger.info("global gateway resume: unblocked %d run key(s)", len(rows))
    return len(rows)
