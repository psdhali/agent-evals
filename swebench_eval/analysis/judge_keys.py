"""Mint/rotate/finalise a judge pass's OpenRouter + LiteLLM keys
(offline-analysis-design.md §10.2).

Reuses ADR-0035's exact key lifecycle — ``openrouter_admin.mint_key`` +
``gateway_admin.generate_key``/``ensure_model_registered``/``rotate_model_key``
+ ``gateway_admin.delete_key``/``openrouter_admin.disable_key`` — the same
machinery ``run_launch._provision_keys``/``revoke_run_keys`` use for a
harness run. Not a new mechanism: a judge pass gets its own minted,
budget-capped keys the same way a run does, so judge spend is auditable per
pass exactly as run spend is auditable per run (§10.6).

A judge pass is not a run, so it has no ``runs.active_key`` row to piggyback
a mutex on. ``judge_pass_lock`` is its own real UNIQUE-constraint mutex
(§10.2 point 4) — two concurrent passes rotating the single ``judge-model``
alias would otherwise race the same way two concurrent harness runs sharing
a ``model_alias`` would.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg2.errors

from swebench_eval.gateway import admin as gateway_admin
from swebench_eval.gateway import openrouter_admin
from swebench_eval.gateway.rotatable_models import ROTATABLE_MODELS
from swebench_eval.harnesses.routing import gateway_api_key, gateway_base_url
from swebench_eval.orchestrator.control_plane.run_launch import (
    NoProvisioningKeyError,
    _fetch_openrouter_provisioning_key,
)

logger = logging.getLogger(__name__)

JUDGE_MODEL_ALIAS = "judge-model"


class JudgePassLockedError(RuntimeError):
    """Another judge pass currently holds the judge-model key."""


def claim_pass_lock(conn: Any, pass_id: str) -> None:
    """Acquire the judge-model mutex for *pass_id*. A real UNIQUE constraint,
    not a check-then-act race — the same principle as ``uq_runs_active_key``.
    Raises :class:`JudgePassLockedError` if another pass already holds it."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO judge_pass_lock (model_alias, pass_id) VALUES (%s, %s)",
                (JUDGE_MODEL_ALIAS, pass_id),
            )
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise JudgePassLockedError(
            f"another judge pass is already using {JUDGE_MODEL_ALIAS!r} — refusing to start "
            "a concurrent pass and rotate its key out from under it"
        ) from None


def release_pass_lock(conn: Any, pass_id: str) -> None:
    """Idempotent: releasing a lock this pass doesn't hold is a no-op, not an
    error — finalisation must be safe to retry (same discipline as
    ``revoke_run_keys``)."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM judge_pass_lock WHERE model_alias = %s AND pass_id = %s",
            (JUDGE_MODEL_ALIAS, pass_id),
        )
    conn.commit()


def provision_pass_keys(pass_id: str, budget_cap_usd: float) -> tuple[str, str, str]:
    """Mint the pass's LiteLLM + OpenRouter keys and rotate them onto
    judge-model. Returns ``(raw_litellm_key, litellm_key_id, openrouter_key_hash)``
    — the raw key is for THIS PROCESS'S immediate use only (the judge call,
    §10.5's same-task design means it never crosses a container boundary);
    only the two ids are ever persisted (rule 3: never the raw key)."""
    provisioning_key = _fetch_openrouter_provisioning_key()  # fail closed first

    base = gateway_base_url()
    master = gateway_api_key()
    litellm_raw, litellm_key_id = gateway_admin.generate_key(
        base,
        master,
        key_alias=pass_id,
        models=[JUDGE_MODEL_ALIAS],
        max_budget=budget_cap_usd,
        metadata={"pass_id": pass_id, "kind": "judge"},
    )

    or_raw, or_hash = openrouter_admin.mint_key(
        provisioning_key, name=pass_id, limit_usd=budget_cap_usd
    )

    spec = ROTATABLE_MODELS[JUDGE_MODEL_ALIAS]
    model_id = gateway_admin.ensure_model_registered(
        base, master, JUDGE_MODEL_ALIAS, spec.litellm_params, spec.model_info
    )
    gateway_admin.rotate_model_key(
        base,
        master,
        JUDGE_MODEL_ALIAS,
        model_id,
        or_raw,
        upstream_model=str(spec.litellm_params["model"]),
        litellm_params=dict(spec.litellm_params),
    )
    _await_rotated_key_active(base, litellm_raw)

    return litellm_raw, litellm_key_id, or_hash


def _await_rotated_key_active(base: str, litellm_raw: str, *, timeout_s: float = 120.0) -> None:
    """Block until EVERY gateway replica serves the rotated key (2026-09-02, tightened 2026-09-03).

    ``/model/update`` writes the new upstream key to the gateway's DB, but each gateway REPLICA
    keeps serving its cached router config until its periodic DB reload — and the previous
    pass's finaliser deleted its OpenRouter key, so a completion sent before the reload goes
    upstream with a DELETED key and 401s ("User not found"; proven live, judge task 55ce189a).
    The first version of this returned on the first non-401, which proves ONE replica: judge
    task judge-01788481792836629782 (2026-09-03) logged "active after 2 probes" and 401'd on the
    other replica three seconds later. Now ``gateway_admin.await_alias_served`` requires a run of
    consecutive 200s that spans both replicas and LiteLLM's post-401 cooldown.
    """
    gateway_admin.await_alias_served(
        base, litellm_raw, JUDGE_MODEL_ALIAS, timeout_s=timeout_s, what="rotated judge-model key"
    )


def finalize_pass_keys(
    pass_id: str, litellm_key_id: str | None, openrouter_key_hash: str | None
) -> None:
    """§10.2 point 5: revoke on success, failure, or budget-exhaustion —
    idempotent, mirrors ``revoke_run_keys``."""
    if litellm_key_id:
        gateway_admin.delete_key(gateway_base_url(), gateway_api_key(), pass_id)
    if openrouter_key_hash:
        try:
            provisioning_key = _fetch_openrouter_provisioning_key()
            openrouter_admin.disable_key(provisioning_key, openrouter_key_hash)
        except NoProvisioningKeyError:
            logger.error(
                "judge pass %s: cannot disable OpenRouter key (no provisioning key) — "
                "MANUAL CLEANUP NEEDED for hash=%s",
                pass_id,
                openrouter_key_hash,
            )


def cleanup_pass(conn: Any, pass_id: str) -> dict[str, Any]:
    """2026-09-08: everything ``finalize_pass_keys`` + ``release_pass_lock`` would have
    done for a pass whose process died before its finally ran (an ECS stop on an image
    without the SIGTERM handler, or a stall the operator had to kill). Needs no ids:
    the LiteLLM key is aliased and the OpenRouter key named ``pass_id``. Idempotent —
    safe to run on a pass that finalised itself. Returns what it found/did."""
    report: dict[str, Any] = {"pass_id": pass_id}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM judge_pass_lock WHERE model_alias = %s AND pass_id = %s",
            (JUDGE_MODEL_ALIAS, pass_id),
        )
        report["lock_held"] = cur.fetchone() is not None
    release_pass_lock(conn, pass_id)
    try:
        gateway_admin.delete_key(gateway_base_url(), gateway_api_key(), pass_id)
        report["litellm_key"] = "deleted-or-absent"
    except Exception as exc:  # noqa: BLE001 — report, never abort the rest of the cleanup
        report["litellm_key"] = f"delete failed: {exc}"
    try:
        provisioning_key = _fetch_openrouter_provisioning_key()
        key_hash = openrouter_admin.find_key_hash(provisioning_key, pass_id)
        if key_hash:
            openrouter_admin.disable_key(provisioning_key, key_hash)
            report["openrouter_key"] = f"disabled ({key_hash[:8]}…)"
        else:
            report["openrouter_key"] = "not found (already gone or never minted)"
    except Exception as exc:  # noqa: BLE001
        report["openrouter_key"] = f"disable failed: {exc}"
    logger.info("judge pass cleanup: %s", report)
    return report
