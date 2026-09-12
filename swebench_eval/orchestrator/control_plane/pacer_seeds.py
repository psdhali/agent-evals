"""Discovery seeds, persisted (owner decision 2026-09-04).

The L1 pacer and the L2 planner run on ``pacer:cfg:{pool}`` — the margined, floored constants
:mod:`ceiling_discovery` derives from a probe. That hash lives in Valkey, and Valkey lives in
the eval tier: every eval destroy wiped it and forced a ~$20 re-probe per pool at the next
bring-up. The probe's observation rows (``model_tpm_observations``) are the *measurements*;
re-deriving the seeds from them is brittle (the derivation changed three times in a week), so
the seeds themselves are persisted verbatim, one JSONB row per probe, and rehydrated when the
hash is empty.

Two rehydration points, both best-effort and both no-ops when the hash already has fields:

* run-supervisor start (:func:`rehydrate_known_pools`) — the planner's own process, and the
  API's ``/models`` consistency line reads the same hash, so the launch screen is truthful
  again as soon as the ui tier is up;
* run launch (:func:`rehydrate_pool`, from ``run_launch._publish_autoscaler_overrides``) — the
  safety net, before the pool -> run-alias copy.

The ORIGINAL ``seeded_at`` is restored, never re-stamped: the planner's staleness policy
(``PACER_CFG_MAX_AGE_S``, default 6 h) halves its utilisation margins on an old seed, which is
exactly the right reaction to a day-old probe. Discovery seeds only — the planner's live growth
values (it rewrites r_tok / k_inflight / r_qps in the same hash) are ephemeral by design.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

from swebench_eval.gateway.pacer import pacer_cfg_key

logger = logging.getLogger(__name__)


def _db() -> Any:
    from swebench_eval.database.connection import get_connection

    return get_connection()


def _as_number(value: Any) -> int | float | None:
    """A seed value as a number. Ints and floats pass through; a numeric string or bytes (a
    ``pacer:cfg`` hash read back from Valkey — every field there is a ``repr()`` of a number)
    parses; anything else — a bool, None, ``'14.0'`` with literal quotes — is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def numeric_seeds(seeds: Mapping[str, Any], *, context: str = "") -> dict[str, int | float]:
    """*seeds* with every value a number and every field that is not one dropped (logged).

    Every consumer of a seed — the pacer's Lua ``tonumber``, the planner, the API's
    consistency line — parses the hash value as a float, so a seed that is not a number is
    worse than a missing one: it reads as "unseeded" everywhere while the row says it is
    seeded. This is the single normalisation point for both directions (Aurora row <->
    Valkey hash). Bit live 2026-09-07: an operator r_qps edit persisted the pool hash's raw
    Redis strings, the next bring-up rehydrated them as ``repr(str)`` (quoted), and the
    launch screen read minimax as unseeded."""
    out: dict[str, int | float] = {}
    dropped: list[str] = []
    for k, v in seeds.items():
        n = _as_number(v)
        if n is None:
            dropped.append(k)
        else:
            out[k] = n
    if dropped:
        logger.warning(
            "pacer seeds: %s: dropped non-numeric field(s) %s", context or "seeds", dropped
        )
    return out


def hash_is_numeric(raw: Mapping[Any, Any]) -> bool:
    """True when every value of a ``pacer:cfg`` hash parses as a number — the invariant every
    reader relies on. An empty hash is vacuously numeric (and 'empty' is the caller's case)."""
    return all(_as_number(v) is not None for v in raw.values())


def persist_seeds(
    model_alias: str,
    seeds: dict[str, Any],
    seeded_at: float,
    *,
    provider: str | None = None,
    triggered_by: str | None = None,
    report: dict[str, Any] | None = None,
) -> None:
    """Append one ``pacer_cfg_seeds`` row: *seeds* exactly as discovery HSETs them (minus
    ``seeded_at``, which has its own column). Raises on a DB failure — the caller decides how
    loud to be (discovery logs an error and still seeds Valkey: the live seed is the point of
    the probe, the row is what survives the night)."""
    payload = numeric_seeds(
        {k: v for k, v in seeds.items() if k != "seeded_at"}, context=f"persist {model_alias}"
    )
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO pacer_cfg_seeds
                       (model_alias, seeds, seeded_at, provider, triggered_by, task_id, report)
                   VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s::jsonb)""",
                (
                    model_alias,
                    json.dumps(payload),
                    float(seeded_at),
                    provider,
                    triggered_by,
                    os.environ.get("ECS_TASK_ARN"),
                    json.dumps(report, default=str) if report is not None else None,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    logger.info(
        "pacer seeds persisted for %s (%d fields, seeded_at=%.0f)",
        model_alias,
        len(payload),
        seeded_at,
    )


def load_latest_seeds(model_alias: str) -> tuple[dict[str, Any], float] | None:
    """The most recent persisted seeds for *model_alias* as ``(seeds, seeded_at)``, or None."""
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT seeds, seeded_at FROM pacer_cfg_seeds
                   WHERE model_alias = %s
                   ORDER BY seeded_at DESC, id DESC LIMIT 1""",
                (model_alias,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    seeds, seeded_at = row
    if isinstance(seeds, str):  # a driver that hands JSONB back as text
        seeds = json.loads(seeds)
    if not isinstance(seeds, dict):
        return None
    return seeds, float(seeded_at)


def rehydrate_pool(client: Any, model_alias: str) -> bool:
    """Write the latest persisted seeds into ``pacer:cfg:{model_alias}`` IF the hash is empty.

    Returns True when the hash was written. Never raises: a Redis or DB failure logs and returns
    False — the pacer then runs on its defaults (the safe direction), exactly as before this
    module existed. An existing hash — a fresh probe, an operator override, a still-warm cache —
    is never touched: it is newer evidence than any row. The one exception is a hash whose
    values do not parse as numbers (a bad earlier rehydration): no reader can use it, so it is
    deleted and rebuilt from the row."""
    key = pacer_cfg_key(model_alias)
    try:
        live = client.hgetall(key) or {}
        if live:
            if hash_is_numeric(live):
                return False
            logger.warning(
                "pacer seeds: pacer:cfg:%s holds non-numeric values (%d fields) — unreadable by "
                "the pacer, the planner and the API; deleting it and rehydrating from Aurora",
                model_alias,
                len(live),
            )
            client.delete(key)
        loaded = load_latest_seeds(model_alias)
    except Exception:  # rehydration is best-effort; defaults are the fallback
        logger.warning("pacer seeds: rehydrate %s failed (pacer stays on defaults)", model_alias)
        logger.debug("pacer seeds: rehydrate %s traceback", model_alias, exc_info=True)
        return False
    if loaded is None:
        logger.info("pacer seeds: no persisted seeds for %s — needs a probe", model_alias)
        return False
    seeds, seeded_at = loaded
    numeric = numeric_seeds(
        {k: v for k, v in seeds.items() if k != "seeded_at"}, context=f"rehydrate {model_alias}"
    )
    if not numeric:
        logger.warning(
            "pacer seeds: persisted row for %s has no numeric field — needs a probe", model_alias
        )
        return False
    mapping = {
        **{k: repr(v) for k, v in numeric.items()},
        "seeded_at": repr(float(seeded_at)),
    }
    try:
        client.hset(key, mapping=mapping)
    except Exception:  # noqa: BLE001
        logger.warning("pacer seeds: rehydrate %s: Redis write failed", model_alias)
        return False
    age_h = max(0.0, time.time() - seeded_at) / 3600.0
    logger.info(
        "pacer seeds: rehydrated pacer:cfg:%s from Aurora (%d fields, probe %.1f h old; the "
        "planner's staleness policy applies past PACER_CFG_MAX_AGE_S)",
        model_alias,
        len(mapping) - 1,
        age_h,
    )
    return True


def seed_alias_from_pool(client: Any, alias: str, *, fill_missing: bool = False) -> int:
    """Copy the POOL's ``pacer:cfg`` into ``pacer:cfg:{alias}`` when the alias hash is empty
    (rehydrating the pool from Aurora first if IT is empty). Returns the number of fields
    written (0 = alias already seeded, no pool, or nothing to copy). Never raises.

    This is the copy run launch does for a run's alias (``run_launch._publish_autoscaler_
    overrides``, finding #2 of run 1); the judge pass needs the same for ``judge-model`` —
    it has no launch step, so without this a parallel pass runs on the gateway pacer's
    DEFAULTS (1M tokens in flight, 30 requests, 20k tok/s, 1.5 req/s) whatever its worker
    count, and calls are held up to the hold cap. An existing alias hash is never touched —
    unless ``fill_missing`` (2026-09-08): then the pool's fields the alias LACKS are added and
    every field the alias already has (an operator's r_qps, a planner-grown value) is kept.
    That is the restart / supervisor-startup case: an eval-tier cycle wipes the launch-time
    copy, an operator r_qps edit recreates the hash with that one field, and the gateway
    admits the run against its defaults (20k tok/s) — 19 of 24 admissions waited > 2 s on
    the opencode restarts of 2026-09-08."""
    try:
        from swebench_eval.gateway.rotatable_models import pool_alias_for

        pool = pool_alias_for(alias)
        if not pool or pool == alias:
            return 0
        alias_key = pacer_cfg_key(alias)
        current = client.hgetall(alias_key) or {}
        if current and not fill_missing:
            return 0
        pool_seeds = client.hgetall(pacer_cfg_key(pool))
        if not pool_seeds and rehydrate_pool(client, pool):
            pool_seeds = client.hgetall(pacer_cfg_key(pool))
        if not pool_seeds:
            logger.info(
                "pacer seeds: pool %s has nothing to copy to %s — needs a probe", pool, alias
            )
            return 0
        if current:
            have = {k.decode() if isinstance(k, bytes) else k for k in current}
            mapping = {
                k: v
                for k, v in pool_seeds.items()
                if (k.decode() if isinstance(k, bytes) else k) not in have
            }
            if not mapping:
                return 0
            client.hset(alias_key, mapping=mapping)
            logger.info(
                "pacer seeds: pacer:cfg:%s was missing %d field(s) — filled from pool %s "
                "(%s); the %d field(s) it had are kept",
                alias,
                len(mapping),
                pool,
                ", ".join(sorted(k.decode() if isinstance(k, bytes) else k for k in mapping)),
                len(current),
            )
            return len(mapping)
        client.hset(alias_key, mapping=pool_seeds)
        logger.info(
            "pacer seeds: pacer:cfg seeded from pool %s -> %s (%d fields)",
            pool,
            alias,
            len(pool_seeds),
        )
        return len(pool_seeds)
    except Exception:  # best-effort, defaults are the fallback
        logger.warning("pacer seeds: seeding %s from its pool failed (pacer on defaults)", alias)
        logger.debug("pacer seeds: seed %s traceback", alias, exc_info=True)
        return 0


def known_pools() -> tuple[str, ...]:
    """The discovery pools — the aliases a probe can seed, hence the ones worth rehydrating."""
    from swebench_eval.orchestrator.control_plane.ceiling_discovery import _UPSTREAM_MODELS

    return tuple(_UPSTREAM_MODELS)


def rehydrate_known_pools(client: Any | None = None) -> list[str]:
    """Rehydrate every known pool whose hash is empty; returns the pools written. Never raises."""
    if client is None:
        try:
            from swebench_eval.database.redis_client import _get_client

            client = _get_client()
        except Exception:  # noqa: BLE001
            logger.warning("pacer seeds: no Redis client — skipping rehydration")
            return []
    written: list[str] = []
    for pool in known_pools():
        if rehydrate_pool(client, pool):
            written.append(pool)
    return written
