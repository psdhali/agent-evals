"""Operator-adjustable limits (owner request 2026-09-04: "a way for me to up the various
numbers, or bring them down, via the UI").

Three scopes, one audit trail:

* **run** — the fields the dispatcher already re-reads from ``autoscaler:run_overrides``
  every tick (``max_parallel``, ``ramp_step_pct``, ``cooldown_s``, ``enabled``) plus
  ``ceiling_override`` (new): the planner's computed ceiling is replaced by the operator's
  number. Still min-wins with the static env cap and the per-run cap, still held by
  cooldown / paced. This is the honest "raise the ceiling" knob — raising the cap alone
  does nothing while the projection binds (it did at 5 with 13 queued).
* **global** — ``operator:limits`` (a Valkey hash the dispatcher and the eval scaler read
  every tick): the borrowed-curve cap, the growth clamp factor, the planner utilisation,
  the eval fleet's max workers and its task scale-in delay. Each falls back to the
  module constant / task-def env when the field is absent.
* **pacer** — ``pacer:cfg:{alias}`` fields (r_tok, k_inflight, r_qps, c_burst, c_req,
  cached_weight), live in the pacer's Lua on the next admission. An edit of one of the
  three seed-relative fields re-bases its ``_seed`` (the 1.5x growth clamp and the 0.5x
  recovery floor follow the operator's number) and re-stamps ``seeded_at`` (an operator
  edit IS fresh evidence — full planner margins). Written to the run alias; with
  ``also_pool`` also to the pool key and persisted as a ``pacer_cfg_seeds`` row tagged
  ``operator:<actor>`` so the next launch and the next bring-up inherit it.

Every write appends one ``operator_limit_edits`` row (scope, target, field, old, new, actor,
reason). Aurora is truth for the global scope: ``rehydrate_global`` restores the latest
value per field into the hash at run-supervisor start (the eval tier's Valkey is wiped by
every destroy), the same pattern as the pacer seeds. The static hard cap
(``MAX_CONCURRENT_HARNESS_TASKS``), the 5 % growth-step hard max and the ASG rails stay
terraform-only on purpose — they are the fail-safes every mode is min-wins with.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass
from typing import Any

from swebench_eval.gateway.pacer import pacer_cfg_key

logger = logging.getLogger(__name__)

OPERATOR_LIMITS_KEY = "operator:limits"
_OP_MARK = "_op:"  # per-field "set by an operator" marker inside the run-overrides hash

# The seed-relative pacer fields: an operator edit re-bases the matching ``_seed``.
_SEEDED_PACER_FIELDS = frozenset({"r_tok", "k_inflight", "r_qps"})


@dataclass(frozen=True)
class KnobSpec:
    """One adjustable field — what the UI renders and what the writers validate."""

    field: str
    scope: str  # run | global | pacer
    kind: str  # int | float | bool
    label: str
    description: str
    lo: float | None = None
    hi: float | None = None
    default: float | None = None  # None = "whatever the task-def env / probe says"
    default_note: str = ""
    unit: str = ""
    read_by: str = ""  # who consumes it and how fast the edit lands


RUN_KNOBS: tuple[KnobSpec, ...] = (
    KnobSpec(
        "max_parallel",
        "run",
        "int",
        "per-run task cap",
        "Min-wins with the static env cap. Raise or drop; running tasks are never killed.",
        lo=1,
        hi=5000,
        default=None,
        default_note="launch config (150) or unset = static cap only",
        unit="tasks",
        read_by="dispatcher, every ground-truth refresh (10 s)",
    ),
    KnobSpec(
        "ceiling_override",
        "run",
        "int",
        "planner ceiling override",
        "Replaces the planner's projected ceiling with this number. Still min-wins with "
        "both caps and still held by cooldown / paced. Clear to hand control back.",
        lo=0,
        hi=5000,
        default=None,
        default_note="unset = the planner's projection",
        unit="tasks",
        read_by="dispatcher planner, every tick (15 s)",
    ),
    KnobSpec(
        "ramp_step_pct",
        "run",
        "float",
        "growth step",
        "+X% per clean predicted-peak reconciliation. 5 is the hard max (owner-fixed); "
        "an override can only shrink it.",
        lo=0,
        hi=5,
        default=5.0,
        unit="%",
        read_by="dispatcher planner, every tick",
    ),
    KnobSpec(
        "cooldown_s",
        "run",
        "float",
        "overload cooldown",
        "How long new launches stay frozen after a provider overload (429) before anything "
        "resumes; also the growth stabilisation window.",
        lo=1,
        hi=3600,
        default=60.0,
        unit="s",
        read_by="dispatcher planner, every tick",
    ),
    KnobSpec(
        "enabled",
        "run",
        "bool",
        "planner gate enabled",
        "Off = the L2 dynamic ceiling never blocks; the static cap, the per-run cap and "
        "the L1 pacer stay in force.",
        default=1.0,
        read_by="dispatcher planner, every tick",
    ),
)

GLOBAL_KNOBS: tuple[KnobSpec, ...] = (
    KnobSpec(
        "borrowed_curve_cap",
        "global",
        "int",
        "borrowed-curve cap",
        "A pool running on another pool's fitted curve is not sized past this many tasks "
        "until a refit gives it its own curve.",
        lo=1,
        hi=5000,
        default=None,
        default_note="AUTOSCALER_BORROWED_CURVE_CAP on the dispatcher task def (30)",
        unit="tasks",
        read_by="dispatcher planner, every tick",
    ),
    KnobSpec(
        "growth_cap_factor",
        "global",
        "float",
        "growth clamp",
        "Growth stops once r_tok would exceed this multiple of the discovered (or "
        "operator-set) seed.",
        lo=1.0,
        hi=20.0,
        default=1.5,
        unit="x seed",
        read_by="dispatcher planner, every tick",
    ),
    KnobSpec(
        "utilization",
        "global",
        "float",
        "planner utilisation",
        "The share of r_tok / k_inflight the projection may fill (halved on a stale cfg).",
        lo=0.3,
        hi=1.0,
        default=0.9,
        read_by="dispatcher planner, every tick",
    ),
    KnobSpec(
        "eval_max_workers",
        "global",
        "int",
        "eval max workers",
        "Upper bound on eval-worker tasks. The ASG max x tasks-per-host rail still binds.",
        lo=0,
        hi=5000,
        default=None,
        default_note="EVAL_MAX_WORKERS on the run-supervisor task def",
        unit="tasks",
        read_by="eval scaler, every tick",
    ),
    KnobSpec(
        "eval_task_scale_in_s",
        "global",
        "float",
        "eval task scale-in delay",
        "Consecutive seconds of lower demand before eval tasks are scaled in.",
        lo=0,
        hi=7200,
        default=None,
        default_note="EVAL_TASK_SCALE_IN_S on the run-supervisor task def (300)",
        unit="s",
        read_by="eval scaler, every tick",
    ),
)

PACER_KNOBS: tuple[KnobSpec, ...] = (
    KnobSpec(
        "r_tok",
        "pacer",
        "float",
        "r_tok",
        "Arrival bucket refill, weighted tokens/s. Re-bases r_tok_seed.",
        lo=1,
        unit="tok/s",
        read_by="pacer, next admission; planner, next tick",
    ),
    KnobSpec(
        "k_inflight",
        "pacer",
        "float",
        "k_inflight",
        "In-flight token cap. Re-bases k_inflight_seed.",
        lo=1,
        unit="tok",
        read_by="pacer, next admission; planner, next tick",
    ),
    KnobSpec(
        "r_qps",
        "pacer",
        "float",
        "r_qps",
        "Request bucket refill, call starts/s. Re-bases r_qps_seed.",
        lo=0.01,
        unit="req/s",
        read_by="pacer, next admission; planner, next tick",
    ),
    KnobSpec(
        "c_burst",
        "pacer",
        "float",
        "c_burst",
        "Arrival bucket capacity, weighted tokens.",
        lo=1,
        unit="tok",
        read_by="pacer, next admission",
    ),
    KnobSpec(
        "c_req",
        "pacer",
        "float",
        "c_req",
        "Request bucket capacity.",
        lo=1,
        unit="req",
        read_by="pacer, next admission",
    ),
    KnobSpec(
        "cached_weight",
        "pacer",
        "float",
        "cached-token weight",
        "What a cached prompt token costs the pool relative to an uncached one "
        "(1.0 = full price; deepseek's real traffic runs ~65% cache hits).",
        lo=0.05,
        hi=1.0,
        default=1.0,
        read_by="pacer, next admission; planner, next tick",
    ),
)

_RUN_BY_FIELD = {k.field: k for k in RUN_KNOBS}
_GLOBAL_BY_FIELD = {k.field: k for k in GLOBAL_KNOBS}
_PACER_BY_FIELD = {k.field: k for k in PACER_KNOBS}


class LimitError(ValueError):
    """An edit the specs refuse (unknown field, out of range, wrong type)."""


def specs() -> list[dict[str, Any]]:
    return [asdict(k) for k in (*RUN_KNOBS, *GLOBAL_KNOBS, *PACER_KNOBS)]


def _s(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _hash(client: Any, key: str) -> dict[str, str]:
    raw = client.hgetall(key) or {}
    return {_s(k): _s(v) for k, v in raw.items()}


def _coerce(spec: KnobSpec, value: Any) -> float:
    """Validate against the spec; returns the numeric value (bools as 0/1)."""
    if spec.kind == "bool":
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("1", "true", "on", "yes"):
                return 1.0
            if v in ("0", "false", "off", "no"):
                return 0.0
            raise LimitError(f"{spec.field}: not a boolean: {value!r}")
        return 1.0 if bool(value) else 0.0
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise LimitError(f"{spec.field}: not a number: {value!r}") from exc
    if math.isnan(num) or math.isinf(num):
        raise LimitError(f"{spec.field}: not a number")
    if spec.kind == "int":
        if num != int(num):
            raise LimitError(f"{spec.field}: must be an integer")
        num = float(int(num))
    if spec.lo is not None and num < spec.lo:
        raise LimitError(f"{spec.field}: {num:g} is below the minimum {spec.lo:g}")
    if spec.hi is not None and num > spec.hi:
        raise LimitError(f"{spec.field}: {num:g} is above the maximum {spec.hi:g}")
    return num


def _encode_run_value(spec: KnobSpec, num: float) -> str:
    """The run-overrides hash uses the launch's own encodings (run_launch)."""
    if spec.kind == "bool":
        return "1" if num else "0"
    if spec.kind == "int":
        return str(int(num))
    return repr(float(num))


# --- audit -----------------------------------------------------------------------------------


def _audit(
    scope: str,
    target: str,
    field: str,
    old: str | None,
    new: str | None,
    actor: str,
    reason: str,
) -> None:
    """Append one ``operator_limit_edits`` row. Never raises — the live write already
    landed; a lost audit row is logged loudly, not turned into a failed edit."""
    try:
        from swebench_eval.database.connection import get_connection

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO operator_limit_edits
                           (scope, target, field, old_value, new_value, actor, reason)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (scope, target, field, old, new, actor, reason),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception(
            "operator limits: AUDIT ROW LOST for %s/%s %s: %r -> %r by %s",
            scope,
            target,
            field,
            old,
            new,
            actor,
        )


# --- global scope -----------------------------------------------------------------------------


def read_global(client: Any) -> dict[str, float]:
    """The operator's global knobs as floats; {} on any failure (defaults stay in force)."""
    try:
        out: dict[str, float] = {}
        for k, v in _hash(client, OPERATOR_LIMITS_KEY).items():
            if k in _GLOBAL_BY_FIELD:
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    continue
        return out
    except Exception:
        logger.debug("operator limits: global read failed (defaults stay in force)", exc_info=True)
        return {}


def set_global(client: Any, field: str, value: Any, actor: str, reason: str = "") -> dict[str, Any]:
    """Set (or clear, ``value=None``) one global knob. Returns {field, old, new}."""
    spec = _GLOBAL_BY_FIELD.get(field)
    if spec is None:
        raise LimitError(f"unknown global limit: {field}")
    current = _hash(client, OPERATOR_LIMITS_KEY)
    old = current.get(field)
    if value is None:
        client.hdel(OPERATOR_LIMITS_KEY, field)
        new: str | None = None
    else:
        num = _coerce(spec, value)
        new = str(int(num)) if spec.kind == "int" else repr(float(num))
        client.hset(OPERATOR_LIMITS_KEY, mapping={field: new})
    _audit("global", "", field, old, new, actor, reason)
    logger.warning(
        "operator limits: global %s %r -> %r by %s (%s)", field, old, new, actor, reason or "-"
    )
    return {"field": field, "old": old, "new": new}


def rehydrate_global(client: Any) -> int:
    """Restore the latest audited value per global field into the hash IF it is empty
    (the eval tier's Valkey is wiped by every destroy). Returns fields restored."""
    try:
        if _hash(client, OPERATOR_LIMITS_KEY):
            return 0
        from swebench_eval.database.connection import get_connection

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT DISTINCT ON (field) field, new_value
                       FROM operator_limit_edits
                       WHERE scope = 'global'
                       ORDER BY field, id DESC""")
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        logger.warning("operator limits: global rehydration failed", exc_info=True)
        return 0
    mapping = {str(f): str(v) for f, v in rows if v is not None and str(f) in _GLOBAL_BY_FIELD}
    if mapping:
        client.hset(OPERATOR_LIMITS_KEY, mapping=mapping)
        logger.info("operator limits: rehydrated %s from Aurora", mapping)
    return len(mapping)


# --- run scope --------------------------------------------------------------------------------


def _run_key() -> str:
    from swebench_eval.orchestrator.control_plane.run_launch import AUTOSCALER_OVERRIDES_KEY

    return AUTOSCALER_OVERRIDES_KEY


def set_run(client: Any, field: str, value: Any, actor: str, reason: str = "") -> dict[str, Any]:
    """Set (or clear) one field of the run-overrides hash the dispatcher re-reads each tick.
    The hash is the one the launch published (last launch wins, §6.7); an edit before any
    launch creates it with no run_id, which the dispatcher reads the same way."""
    spec = _RUN_BY_FIELD.get(field)
    if spec is None:
        raise LimitError(f"unknown run limit: {field}")
    key = _run_key()
    current = _hash(client, key)
    old = current.get(field)
    run_id = current.get("run_id") or ""
    if value is None:
        client.hdel(key, field, f"{_OP_MARK}{field}")
        new: str | None = None
    else:
        new = _encode_run_value(spec, _coerce(spec, value))
        client.hset(key, mapping={field: new, f"{_OP_MARK}{field}": actor})
    if not current:
        client.expire(key, 7 * 24 * 3600)
    _audit("run", run_id, field, old, new, actor, reason)
    logger.warning(
        "operator limits: run %s %s %r -> %r by %s (%s)",
        run_id or "(no run)",
        field,
        old,
        new,
        actor,
        reason or "-",
    )
    return {"field": field, "old": old, "new": new, "run_id": run_id}


# --- pacer scope ------------------------------------------------------------------------------


def set_pacer(
    client: Any,
    alias: str,
    field: str,
    value: Any,
    actor: str,
    reason: str = "",
    *,
    also_pool: bool = False,
) -> dict[str, Any]:
    """Set one ``pacer:cfg:{alias}`` field live (never clears — the pacer needs a number).
    Seed-relative fields re-base their ``_seed``; ``seeded_at`` is re-stamped. With
    ``also_pool`` the pool key gets the same write and a ``pacer_cfg_seeds`` row."""
    spec = _PACER_BY_FIELD.get(field)
    if spec is None:
        raise LimitError(f"unknown pacer limit: {field}")
    if value is None:
        raise LimitError(f"{field}: a pacer field cannot be cleared, only set")
    num = _coerce(spec, value)
    now = time.time()
    mapping = {field: repr(float(num)), "seeded_at": repr(now)}
    if field in _SEEDED_PACER_FIELDS:
        mapping[f"{field}_seed"] = repr(float(num))

    alias_key = pacer_cfg_key(alias)
    old = _hash(client, alias_key).get(field)
    client.hset(alias_key, mapping=mapping)
    new = mapping[field]
    _audit("pacer", alias, field, old, new, actor, reason)
    written = {"alias": alias, "field": field, "old": old, "new": new, "pool": None}

    if also_pool:
        pool = _pool_for(alias)
        if pool and pool != alias:
            pool_key = pacer_cfg_key(pool)
            pool_old = _hash(client, pool_key).get(field)
            client.hset(pool_key, mapping=mapping)
            _audit("pacer", pool, field, pool_old, new, actor, reason)
        else:
            pool = alias
        written["pool"] = pool
        try:
            from swebench_eval.orchestrator.control_plane import pacer_seeds

            # The hash comes back as strings; the row must hold numbers (a rehydration of a
            # string is repr()'d with quotes and nothing can read it — bit live 2026-09-07).
            seeds = pacer_seeds.numeric_seeds(
                _hash(client, pacer_cfg_key(pool)), context=f"operator edit on {pool}"
            )
            pacer_seeds.persist_seeds(
                pool,
                seeds,
                now,
                provider=None,
                triggered_by=f"operator:{actor}",
                report={"operator_edit": {**written, "reason": reason}},
            )
        except Exception:
            logger.exception(
                "operator limits: pacer edit %s=%s on %s persisted to Valkey but NOT to "
                "pacer_cfg_seeds (the next bring-up will not inherit it)",
                field,
                new,
                pool,
            )
    logger.warning(
        "operator limits: pacer %s %s %r -> %r by %s (%s)%s",
        alias,
        field,
        old,
        new,
        actor,
        reason or "-",
        f" [also pool {written['pool']}]" if also_pool else "",
    )
    return written


def _pool_for(alias: str) -> str | None:
    try:
        from swebench_eval.gateway.rotatable_models import pool_alias_for

        return pool_alias_for(alias)
    except Exception:  # noqa: BLE001 — an unknown alias has no pool
        return None


# --- the effective view -----------------------------------------------------------------------


def effective_view(client: Any, aliases: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    """Everything the UI renders: every knob with its effective value and SOURCE
    (``operator`` / ``run_launch`` / ``default``), the static caps from the live decision
    records, and — for the given ``(harness, alias)`` pairs — the pacer cfg of each alias
    and of its pool."""
    run_raw = _hash(client, _run_key())
    run_fields: dict[str, Any] = {}
    for k in RUN_KNOBS:
        raw = run_raw.get(k.field)
        if raw is None:
            source = "default"
        elif run_raw.get(f"{_OP_MARK}{k.field}"):
            source = "operator"
        else:
            source = "run_launch"
        run_fields[k.field] = {
            "value": _display_value(k, raw),
            "source": source,
            "set_by": run_raw.get(f"{_OP_MARK}{k.field}"),
        }
    glob_raw = _hash(client, OPERATOR_LIMITS_KEY)
    global_fields: dict[str, Any] = {}
    for k in GLOBAL_KNOBS:
        raw = glob_raw.get(k.field)
        global_fields[k.field] = {
            "value": _display_value(k, raw),
            "source": "operator" if raw is not None else "default",
            "set_by": None,
        }

    static = _static_caps(client)

    pacer: list[dict[str, Any]] = []
    for harness, alias in aliases or []:
        pool = _pool_for(alias)
        pacer.append(
            {
                "harness": harness,
                "alias": alias,
                "pool": pool if pool and pool != alias else None,
                "cfg": _pacer_cfg_numbers(_hash(client, pacer_cfg_key(alias))),
                "pool_cfg": (
                    _pacer_cfg_numbers(_hash(client, pacer_cfg_key(pool)))
                    if pool and pool != alias
                    else None
                ),
            }
        )
    return {
        "run": {
            "run_id": run_raw.get("run_id") or None,
            "set_at": _float_or_none(run_raw.get("set_at")),
            "fields": run_fields,
        },
        "global": {"fields": global_fields},
        "static": static,
        "pacer": pacer,
        "specs": specs(),
    }


def _display_value(spec: KnobSpec, raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        if spec.kind == "bool":
            return 1.0 if raw.strip() not in ("0", "false", "False") else 0.0
        return float(raw)
    except (TypeError, ValueError):
        return None


def _float_or_none(raw: str | None) -> float | None:
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _pacer_cfg_numbers(cfg: dict[str, str]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for k in ("r_tok", "k_inflight", "r_qps", "c_burst", "c_req", "cached_weight", "seeded_at"):
        out[k] = _float_or_none(cfg.get(k))
    for k in ("r_tok_seed", "k_inflight_seed", "r_qps_seed"):
        out[k] = _float_or_none(cfg.get(k))
    return out


def _static_caps(client: Any) -> dict[str, Any]:
    """The hard rails, read from the live decision records (the API has no other view of
    the dispatcher's env)."""
    import json

    from swebench_eval.orchestrator.control_plane.decision_record import decision_key

    out: dict[str, Any] = {
        "max_concurrent_harness_tasks": None,
        "eval_asg_max": None,
        "eval_max_workers_env": None,
    }
    try:
        raw = client.get(decision_key("harness"))
        if raw:
            rec = json.loads(_s(raw))
            out["max_concurrent_harness_tasks"] = rec.get("static_cap")
            out["harness_ceiling_override"] = rec.get("ceiling_override")
            out["harness_operator_limits"] = rec.get("operator_limits")
        raw = client.get(decision_key("eval"))
        if raw:
            rec = json.loads(_s(raw))
            out["eval_asg_max"] = rec.get("asg_max")
            out["eval_max_workers_env"] = rec.get("max_workers")
    except Exception:
        logger.debug("operator limits: static caps unreadable", exc_info=True)
    return out
