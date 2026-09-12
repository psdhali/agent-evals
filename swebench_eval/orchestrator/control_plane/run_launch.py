"""The run-launch sequence (BUILDER4-RUN-LAUNCH-ORCHESTRATOR-2026-08-26 §4).

CLAIM -> PROVISION -> SEED -> DISPATCH -> RUNNING.  The one function both
``POST /runs`` and the ADR-0024 S3-dispatch trigger call (D2: "both triggers
share one claim path... the lock cannot be bypassed by dropping a file").

Order matters and is not refactored away: each step's status transition is
committed on its own, so a crash mid-launch leaves ``runs.status`` naming
exactly how far it got (§4 "Failure semantics") — 'claimed' means nothing
external exists yet; 'provisioning' means keys may exist and need revoking;
'dispatching' means some jobs may already be live and the run must be
*aborted*, not retried.  This module does not attempt automatic rollback on a
mid-sequence failure (a judgement call — see
builder4-run-launch-response.md): whether a key was actually minted before an
exception is ambiguous from here, and a blind auto-release could race a
still-succeeding async call.  The visible status plus this module's
:func:`finalise_run` (idempotent) is the recovery path.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import psycopg2
from psycopg2.extras import execute_values

from swebench_eval import aws_names
from swebench_eval.control import state as control_state
from swebench_eval.database.redis_client import write_harness_instructions
from swebench_eval.gateway import admin as gateway_admin
from swebench_eval.gateway import openrouter_admin
from swebench_eval.gateway.pacer import pacer_cfg_key
from swebench_eval.gateway.rotatable_models import ROTATABLE_MODELS
from swebench_eval.harnesses.routing import gateway_api_key, gateway_base_url
from swebench_eval.orchestrator.control_plane import run_key_cache
from swebench_eval.orchestrator.control_plane.dispatcher import dispatch_run

if TYPE_CHECKING:
    from swebench_eval.dataset.base import Instance
    from swebench_eval.orchestrator.run_config import RunConfig

logger = logging.getLogger(__name__)


class DuplicateRunError(RuntimeError):
    """§4 CLAIM: a run for this (harness, model_alias) pair is already active.

    Carries the existing run_id — "the 409 must carry the existing run_id...
    the id is the payload, not decoration" (§3.1).
    """

    def __init__(self, existing_run_id: str, harness: str, model_alias: str) -> None:
        self.existing_run_id = existing_run_id
        self.harness = harness
        self.model_alias = model_alias
        super().__init__(
            f"a run for harness={harness!r} model_alias={model_alias!r} is already "
            f"active: {existing_run_id}"
        )


class NoProvisioningKeyError(RuntimeError):
    """D4: no OpenRouter provisioning key available — refuse to launch (fail closed)."""


class GatewayPausedError(RuntimeError):
    """BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §4: the gateway
    pool is globally paused — refuse the launch rather than mint keys for a
    run that would start blocked. Owner decision: refuse, don't silently
    launch-and-block (same posture as any other "don't start new work during
    an incident" gate in this system)."""


@dataclass(frozen=True)
class LaunchResult:
    run_id: str
    dispatched: int
    seeded: int


def _db() -> Any:
    from swebench_eval.database.connection import get_connection

    return get_connection()


# ---------------------------------------------------------------------------
# STEP 1 — CLAIM
# ---------------------------------------------------------------------------


def _claim(
    run_id: str,
    harness: str,
    model_alias: str,
    config_snapshot: dict[str, object],
    budget_cap_usd: float | None,
) -> None:
    """The mutex insert.  Raises :class:`DuplicateRunError` on a pair collision.

    "Why CLAIM is first": the unique partial index on ``active_key`` is the
    mutex.  A check-then-act would let two concurrent submits both pass the
    check and both mint keys; the database is the only thing here that can be
    atomic, and the insert is the cheapest thing to make atomic (§4).
    """
    active_key = f"{harness}:{model_alias}"
    conn = _db()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """INSERT INTO runs
                       (run_id, config_snapshot, status, active_key, budget_cap_usd)
                       VALUES (%s, %s, 'claimed', %s, %s)""",
                    (run_id, json.dumps(config_snapshot), active_key, budget_cap_usd),
                )
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                with conn.cursor() as cur2:
                    cur2.execute("SELECT run_id FROM runs WHERE active_key = %s", (active_key,))
                    row = cur2.fetchone()
                existing = str(row[0]) if row else "unknown"
                raise DuplicateRunError(existing, harness, model_alias) from None
        conn.commit()
        # ADR-0034 §2 / M1.2: open the gates immediately, same as register_run
        # — a claim IS a run starting, and the publisher tick may be idle.
        control_state.publish_from_db(conn)
        control_state.mark_runs_active()
    finally:
        conn.close()


def _set_status(run_id: str, status: str) -> None:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE runs SET status = %s WHERE run_id = %s", (status, run_id))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# STEP 2 — PROVISION
# ---------------------------------------------------------------------------


def _fetch_openrouter_provisioning_key() -> str:
    """D4/D5: the out-of-band provisioning key, orchestrator-task-role only.

    Local dev / tests: ``OPENROUTER_PROVISIONING_KEY`` env var.  Deployed: read
    the secret named by ``OPENROUTER_MANAGEMENT_SECRET_ARN`` (wired by the
    terraform data source in D5) via Secrets Manager.  Raises
    :class:`NoProvisioningKeyError` on any absence/failure — D4 is explicit:
    "no provisioning key => refuse to launch. Fail closed."
    """
    direct = os.environ.get("OPENROUTER_PROVISIONING_KEY")
    if direct:
        return direct
    secret_arn = os.environ.get("OPENROUTER_MANAGEMENT_SECRET_ARN")
    if not secret_arn:
        raise NoProvisioningKeyError(
            "no OpenRouter provisioning key available "
            "(OPENROUTER_PROVISIONING_KEY and OPENROUTER_MANAGEMENT_SECRET_ARN both unset) "
            "— refusing to launch (D4: fail closed)"
        )
    import boto3

    secrets = boto3.client("secretsmanager", region_name=aws_names.region())
    try:
        resp = secrets.get_secret_value(SecretId=secret_arn)
    except Exception as exc:
        raise NoProvisioningKeyError(f"could not read OpenRouter provisioning key: {exc}") from exc
    secret_string = str(resp.get("SecretString") or "")
    if not secret_string:
        raise NoProvisioningKeyError("OpenRouter provisioning key secret has no SecretString")
    # The secret's exact shape was not verifiable in this session (no AWS
    # credentials) — accept either a bare key string or a JSON object
    # carrying it under a plausible field name, matching the Docker Hub PAT
    # precedent's {"username":...,"accessToken":...} shape.
    try:
        parsed = json.loads(secret_string)
    except (TypeError, ValueError):
        return secret_string
    if isinstance(parsed, dict):
        for field_name in ("api_key", "key", "provisioning_key", "accessToken"):
            if field_name in parsed:
                return str(parsed[field_name])
    return secret_string


def _provision_keys(
    run_id: str,
    harness: str,
    model_alias: str,
    budget_cap_usd: float,
    rpm_limit: int | None,
    tpm_limit: int | None,
) -> tuple[str, str]:
    """Mint the run's LiteLLM + OpenRouter keys; rotate the alias when possible.

    Returns ``(litellm_key_id, openrouter_key_hash)`` — never a raw key
    (rule 3).  D4: called AFTER the provisioning key is confirmed readable, so
    a missing key refuses before any minting happens.
    """
    provisioning_key = _fetch_openrouter_provisioning_key()  # D4: fail closed first

    base = gateway_base_url()
    master = gateway_api_key()
    # Owner decision 2026-09-06 (supersedes ADR-0035's gateway-enforced cap):
    # the LiteLLM virtual key carries NO max_budget.  LiteLLM's spend counter
    # prices from its own table (v1.99.1: MiniMax cache reads at $0.15/M vs the
    # real $0.03/M), so it tripped a $10 cap at $2.90 of real spend.  The
    # OpenRouter per-run key below is the cap: same dollar limit, billed by the
    # provider at the real price ("Key limit exceeded (total limit)" -> 403).
    litellm_raw, litellm_key_id = gateway_admin.generate_key(
        base,
        master,
        key_alias=run_id,
        models=[model_alias],
        max_budget=None,
        rpm_limit=rpm_limit,
        tpm_limit=tpm_limit,
        metadata={"run_id": run_id, "harness": harness},
    )
    run_key_cache.store(run_id, litellm_raw)

    or_raw, or_hash = openrouter_admin.mint_key(
        provisioning_key, name=run_id, limit_usd=budget_cap_usd
    )

    spec = ROTATABLE_MODELS.get(model_alias)
    if spec is not None:
        model_id = gateway_admin.ensure_model_registered(
            base, master, model_alias, spec.litellm_params, spec.model_info
        )
        gateway_admin.rotate_model_key(
            base,
            master,
            model_alias,
            model_id,
            or_raw,
            upstream_model=str(spec.litellm_params["model"]),
            # The wipe fix (measured 2026-09-01): /model/update REPLACES litellm_params, so the
            # rotation must carry the WHOLE spec or every rotation strips the generation params.
            litellm_params=dict(spec.litellm_params),
        )
        # Owner decision 2026-09-03: every key-minting path waits for the rotated key to be
        # served by EVERY gateway replica before it is handed to anything that will call it.
        # Harness workers spend minutes booting, so this path never hit the stale-replica
        # 401 that the judge and discovery did — but "never hit" is timing, not a guarantee,
        # and a run's first call is the one whose failure is most expensive to diagnose.
        gateway_admin.await_alias_served(
            base, litellm_raw, model_alias, what=f"run {run_id} rotated key"
        )
    else:
        # §5.1 finding: model_alias is still config-yaml-declared —
        # /model/update would refuse it (verified locally, see
        # gateway/rotatable_models.py).  The LiteLLM virtual key above still
        # enforces the run's budget/rate limits (unaffected — key scoping is
        # proxy-level, independent of config-vs-db model source); only the
        # OpenRouter credit cap is not wired to the traffic for this alias.
        logger.warning(
            "run %s: model_alias %r is not a rotatable db-model — its OpenRouter "
            "per-run credit cap is minted for audit only and will NOT be enforced "
            "upstream until this alias is migrated (builder4-run-launch-response.md)",
            run_id,
            model_alias,
        )

    return litellm_key_id, or_hash


def _record_key_ids(run_id: str, litellm_key_id: str, openrouter_key_hash: str) -> None:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE runs SET litellm_key_id = %s, openrouter_key_hash = %s
                   WHERE run_id = %s""",
                (litellm_key_id, openrouter_key_hash, run_id),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# STEP 3 — SEED
# ---------------------------------------------------------------------------


def _seed_instance_rows(run_id: str, instances: list[Instance], attempts_per_instance: int) -> int:
    """One batched INSERT: a PENDING row per (instance, attempt) (§4 step 3).

    Never 500 round trips.  ``ON CONFLICT DO NOTHING`` makes a retried launch
    (the caller crashed after SEED but before DISPATCH) idempotent — a second
    call seeds nothing new, ``seeded`` in the caller's response would then
    honestly read the count THIS call inserted, not what already existed.
    """
    rows = [
        (run_id, instance.instance_id, attempt, "harness", "PENDING")
        for instance in instances
        for attempt in range(1, attempts_per_instance + 1)
    ]
    if not rows:
        return 0
    conn = _db()
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """INSERT INTO instance_results
                   (run_id, instance_id, attempt_number, phase, state, seeded_at)
                   VALUES %s
                   ON CONFLICT (run_id, instance_id, attempt_number, phase) DO NOTHING""",
                rows,
                template="(%s, %s, %s, %s, %s, now())",
            )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


# ---------------------------------------------------------------------------
# STEP 5 — RUNNING
# ---------------------------------------------------------------------------


def _mark_running(run_id: str) -> None:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET status = 'running', dispatched_at = now() WHERE run_id = %s",
                (run_id,),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The full sequence
# ---------------------------------------------------------------------------


def launch_run(
    run_id: str,
    instances: list[Instance],
    config: RunConfig,
    budget_cap_usd: float,
    rpm_limit: int | None = None,
    tpm_limit: int | None = None,
) -> LaunchResult:
    """CLAIM -> PROVISION -> SEED -> DISPATCH -> RUNNING (§4).

    The single function ``POST /runs`` and the ADR-0024 S3 trigger both call
    (D2) — the lock cannot be bypassed by dropping a file.

    BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §4: refuses before
    CLAIM (nothing minted, nothing written to Aurora) when the ``gateway``
    pool is currently globally paused — read fail-closed via
    ``control_state.is_paused``, the same flag ``/control`` shows the
    operator. This is the *global* flag only; a run individually paused via
    ``gateway_key_blocked_by`` has no bearing on whether a NEW run may start.
    """
    if control_state.is_paused("gateway"):
        raise GatewayPausedError(
            "gateway is globally paused — refusing to launch a new run "
            "(resume gateway via /control/resume first)"
        )
    _claim(run_id, config.harness, config.model_alias, dataclasses.asdict(config), budget_cap_usd)

    _set_status(run_id, "provisioning")
    litellm_key_id, openrouter_key_hash = _provision_keys(
        run_id, config.harness, config.model_alias, budget_cap_usd, rpm_limit, tpm_limit
    )
    _record_key_ids(run_id, litellm_key_id, openrouter_key_hash)
    # BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md: closes the race
    # between the refusal check above (a single point-in-time read, before
    # CLAIM) and the global sweep (only touches status='running' rows) — a
    # pause landing in exactly this PROVISION window would otherwise never
    # get applied to this run. See gateway_pause.block_if_globally_paused's
    # docstring for the full account.
    from swebench_eval.orchestrator.control_plane import gateway_pause

    gateway_pause.block_if_globally_paused(run_id, litellm_key_id)

    _set_status(run_id, "seeding")
    seeded = _seed_instance_rows(run_id, instances, config.attempts_per_instance)

    _set_status(run_id, "dispatching")
    # §6.7 per-run autoscaler overrides — published BEFORE the first job can be launched so
    # the dispatcher's admission sees them from launch #1. Guarded: a Redis failure here must
    # never fail the launch (the dispatcher then runs on its static env config, which is the
    # safe direction — the static ceiling is min-wins in every mode).
    _publish_autoscaler_overrides(run_id, config)
    # 2026-09-09 efficiency prompt arm: the worker reads these once per job (the ADR-0032
    # reference carries no prompt text). Never raises.
    write_harness_instructions(run_id, config.harness_instructions)
    # register_run() runs again inside dispatch_run() — harmless: the runs row
    # already exists (CLAIM), so its INSERT's ON CONFLICT overwrites
    # config_snapshot with the RESOLVED one (resolved_models, window,
    # reproducibility facts) and leaves status/active_key alone.
    dispatched = dispatch_run(run_id=run_id, instances=instances, config=config)

    _mark_running(run_id)

    return LaunchResult(run_id=run_id, dispatched=dispatched, seeded=seeded)


# ---------------------------------------------------------------------------
# Finalisation (§8)
# ---------------------------------------------------------------------------


def _fetch_run_keys(run_id: str) -> tuple[str | None, str | None]:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT litellm_key_id, openrouter_key_hash FROM runs WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None, None
    return row[0], row[1]


def _stop_run_tasks(run_id: str) -> int:
    """§8 step 2: StopTask every task still ``startedBy = run_id``.

    Reuses ``abort.py``'s enumeration (§8: "abort.py already enumerates tasks
    by startedBy=run_id — reuse it, do not write a second one") rather than a
    second ListTasks/StopTask implementation.
    """
    import boto3

    from swebench_eval.orchestrator.control_plane.abort import AbortReport, _stop_in_flight

    ecs = boto3.client("ecs", region_name=aws_names.region())
    conn = _db()
    try:
        dummy = AbortReport(
            run_id=run_id,
            scope="all",
            reason="finalisation",
            actor="run_launch",
            requested_at=time.time(),
        )
        result = _stop_in_flight(conn, ecs, dummy)
    finally:
        conn.close()
    return result.in_flight_stopped


def revoke_run_keys(run_id: str) -> None:
    """§8 step 3: delete the LiteLLM key, disable the OpenRouter key (idempotent).

    Factored out of :func:`finalise_run` so ``abort.py`` can call it too —
    §8 is explicit: "Abort must also clear active_key and revoke the keys, or
    an aborted run blocks its pair forever."  Safe to call more than once
    (both ``gateway_admin.delete_key`` and ``openrouter_admin.disable_key``
    treat "already gone" as success, not an error).
    """
    litellm_key_id, openrouter_key_hash = _fetch_run_keys(run_id)
    if litellm_key_id:
        gateway_admin.delete_key(gateway_base_url(), gateway_api_key(), run_id)
    if openrouter_key_hash:
        try:
            provisioning_key = _fetch_openrouter_provisioning_key()
            openrouter_admin.disable_key(provisioning_key, openrouter_key_hash)
        except NoProvisioningKeyError:
            logger.error(
                "revoke %s: cannot disable OpenRouter key (no provisioning key) — "
                "MANUAL CLEANUP NEEDED for hash=%s",
                run_id,
                openrouter_key_hash,
            )
    run_key_cache.clear(run_id)


def release_active_key(run_id: str) -> None:
    """Clear ``runs.active_key`` (frees the (harness, model_alias) pair for reuse).

    Factored out for the same reason as :func:`revoke_run_keys` — abort's own
    finalisation sets ``status='aborted'`` itself and only needs this half.
    """
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE runs SET active_key = NULL WHERE run_id = %s", (run_id,))
        conn.commit()
    finally:
        conn.close()


# §6.7: the per-run autoscaler overrides, published at launch for the dispatcher (a separate
# long-running service that never reads config_snapshot). ONE global key, last-writer-wins,
# stamped with the owning run_id — with two concurrent runs the later launch's overrides win,
# which is flagged as a known simplification (single-run operation is the current reality;
# the static env ceiling stays min-wins regardless). The TTL is a backstop only; finalise_run
# deletes it when its owning run closes.
AUTOSCALER_OVERRIDES_KEY = "autoscaler:run_overrides"
_OVERRIDES_TTL_S = 7 * 24 * 3600

# The pacer-config fields a launch-time initial_budget_override may seed (exact-design §8:
# "sets R/K/C triple", plus the request-axis pair). Anything else in the dict is ignored
# loudly rather than written into the pacer's config hash.
_BUDGET_OVERRIDE_FIELDS = frozenset({"c_burst", "r_tok", "k_inflight", "c_req", "r_qps"})


def _publish_autoscaler_overrides(run_id: str, config: RunConfig) -> None:
    """Write the launch-time overrides where the dispatcher reads them. Never raises."""
    try:
        from swebench_eval.database.redis_client import _get_client

        client = _get_client()
        client.hset(
            AUTOSCALER_OVERRIDES_KEY,
            mapping={
                "run_id": run_id,
                # §2.4 (wiring review): the run is the authoritative source of the model alias —
                # the dispatcher is a static service and AUTOSCALER_MODEL_ALIAS was never set
                # anywhere, which silently ran the planner on generic defaults and disabled
                # observation emission. The launch publishes it; the env var is the fallback.
                "model_alias": config.model_alias,
                "max_parallel": str(int(config.max_parallel_harness_tasks)),
                "ramp_step_pct": repr(float(config.ramp_step_pct)),
                "cooldown_s": repr(float(config.ramp_cooldown_seconds)),
                "enabled": "1" if config.autoscaler_enabled else "0",
                "set_at": repr(time.time()),
            },
        )
        client.expire(AUTOSCALER_OVERRIDES_KEY, _OVERRIDES_TTL_S)

        # E2E-RUN1 finding #2 (2026-09-02): discovery seeds pacer:cfg:{POOL}
        # (constants are a property of the provider pool), but every consumer —
        # the dispatcher's L2 budgets, the shim's L1 pacer, the capacity
        # observer — reads pacer:cfg:{run alias}.  Copy pool -> alias at launch
        # when the alias hash is empty, so one discovery per pool binds for
        # every harness alias (including discoveries run BEFORE this fix).
        # seeded_at is copied as-is: the staleness clock belongs to the
        # discovery, not the launch.  An operator initial_budget_override below
        # lands AFTER this copy and wins field-by-field.
        from swebench_eval.gateway.rotatable_models import pool_alias_for

        pool = pool_alias_for(config.model_alias)
        if pool and pool != config.model_alias:
            alias_key = pacer_cfg_key(config.model_alias)
            if not client.hgetall(alias_key):
                pool_seeds = client.hgetall(pacer_cfg_key(pool))
                if not pool_seeds:
                    # 2026-09-04: the pool hash is empty after every eval-tier destroy (Valkey
                    # goes with it). Restore the last probe's seeds from Aurora before the
                    # copy — original seeded_at, so a stale probe still reads as stale.
                    from swebench_eval.orchestrator.control_plane import pacer_seeds

                    if pacer_seeds.rehydrate_pool(client, pool):
                        pool_seeds = client.hgetall(pacer_cfg_key(pool))
                if pool_seeds:
                    client.hset(alias_key, mapping=pool_seeds)
                    logger.info(
                        "launch %s: pacer:cfg seeded from pool %s -> %s (%d fields)",
                        run_id,
                        pool,
                        config.model_alias,
                        len(pool_seeds),
                    )

        if config.initial_budget_override:
            known = {
                k: float(v)
                for k, v in config.initial_budget_override.items()
                if k in _BUDGET_OVERRIDE_FIELDS
            }
            unknown = set(config.initial_budget_override) - _BUDGET_OVERRIDE_FIELDS
            if unknown:
                logger.warning(
                    "launch %s: ignoring unknown initial_budget_override fields %s "
                    "(allowed: %s)",
                    run_id,
                    sorted(unknown),
                    sorted(_BUDGET_OVERRIDE_FIELDS),
                )
            if known:
                # Same write shape as discovery's seed and L2's growth: values as repr,
                # seeded_at stamped (an operator override IS fresh evidence — the F3
                # staleness policy starts its clock here).
                client.hset(
                    pacer_cfg_key(config.model_alias),
                    mapping={
                        **{k: repr(v) for k, v in known.items()},
                        "seeded_at": repr(time.time()),
                    },
                )
                logger.info(
                    "launch %s: initial_budget_override seeded pacer:cfg:%s with %s",
                    run_id,
                    config.model_alias,
                    known,
                )
    except Exception:
        logger.warning(
            "launch %s: autoscaler-override publish failed (dispatcher will run on its "
            "static env config — the safe direction)",
            run_id,
            exc_info=True,
        )


def _clear_autoscaler_overrides(run_id: str) -> None:
    """Delete the override key IFF this run still owns it — a later run's overrides must
    never be swept by an earlier run's close. Never raises."""
    try:
        from swebench_eval.database.redis_client import _get_client

        client = _get_client()
        owner = client.hget(AUTOSCALER_OVERRIDES_KEY, "run_id")
        owner_s = owner.decode() if isinstance(owner, bytes) else owner
        if owner_s == run_id:
            client.delete(AUTOSCALER_OVERRIDES_KEY)
    except Exception:  # noqa: BLE001 — cleanup is best-effort; the TTL is the backstop
        logger.debug("finalise %s: override cleanup failed (TTL is the backstop)", run_id)


def finalise_run(run_id: str) -> None:
    """§8: stop -> revoke -> release.  Idempotent — safe to call more than once.

    BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md §1: the CALLER is now
    ``control_plane.restart.close_run`` (the ``POST /runs/{id}/close``
    handler), a deliberate operator action — this is no longer called
    automatically by the reaper or by a result landing. The caller is
    responsible for the atomic ``running -> finalising`` claim AND a
    re-check of "zero non-terminal rows" *inside* that same transaction,
    after the flip (M1 — a restart landing between the pre-flip check and
    the flip is exactly the race that re-check exists to catch). This
    function does not re-check any of that itself — it trusts the caller,
    same as ``abort.py``'s executor trusts its caller.
    """
    stopped = _stop_run_tasks(run_id)
    if stopped:
        logger.info("finalise %s: stopped %d straggler task(s)", run_id, stopped)

    revoke_run_keys(run_id)
    _clear_autoscaler_overrides(run_id)

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE runs SET active_key = NULL, status = 'completed', finalised_at = now()
                   WHERE run_id = %s""",
                (run_id,),
            )
        conn.commit()
    finally:
        conn.close()
    logger.info("finalise %s: complete (active_key released)", run_id)
