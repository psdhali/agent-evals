"""Operator control plane state — pause and abort as durable, fail-closed state.

Single place that answers "may I do work?".  No component talks to
``control_state`` directly; every consumer reads this module.

Two storage layers (ADR-0034 §2):

* **Aurora is the source of truth.**  ``control_state`` (single-row pause
  flags) and ``runs.stop_*`` (a run's abort intent) live there.  The
  orchestrator is the only writer; it updates Aurora *first*, then publishes
  the same facts to Valkey so the effect is immediate rather than waiting up to
  a 30s tick (``set_pause`` / ``request_abort`` are the break-glass-fast path).

* **Valkey is the read path.**  Workers read ``control:flags`` (a HASH of pool
  paused-flag -> "0"|"1", plus ``published_at``) and ``control:aborted`` (a SET
  of run ids).  This keeps pause reads off Aurora entirely — the reason no
  consumer gains an Aurora connection (M1 §1.9 / ADR-0034 §2).

**Fail-closed is not optional.**  :func:`read` returns *paused* when Valkey is
unreachable, when the key is missing, or when ``published_at`` is older than
:data:`STALE_AFTER_S`.  Losing the cache can then only ever over-stop, which is
recoverable; failing open means a Valkey restart silently resumes spending (the
exact thing pause exists to prevent).  The staleness check is the easy-to-omit
half: a Redis that is *up* but holding a key nobody has refreshed since the
orchestrator died is otherwise indistinguishable from a healthy one.

"Completing" the pause by gating the Results Writer is a defect, not a fix —
see ``control_plane/results_writer.py``'s docstring.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 3 × the 30s publisher tick.  A Redis holding a control hash nobody has
# refreshed for this long is stale — treat it as paused (Trap 2).
STALE_AFTER_S = 90

# Valkey key names (ADR-0034 §M1.2 / observability design §M1.2).  Written only
# by the orchestrator; read by every consumer.
_FLAGS_KEY = "control:flags"
_ABORTED_KEY = "control:aborted"
# When ``updated_at`` is present in the flags hash it is the Aurora
# ``control_state.updated_at`` (epoch) carried into the hash so the reconcile
# leg's compare-and-set can tell a fresh Aurora write from a stale one.
_UPDATED_AT_FIELD = "updated_at"

# The publisher-tick gate in the control plane (results_writer).  While a run is
# active the tick must keep ``control:flags`` fresh; between runs it must go
# fully idle so Aurora (seconds_until_auto_pause = 300) can sleep (M1.9
# corollary — "if it polls Aurora every 30s forever, the tick itself is what
# stops Aurora auto-pausing"; ADR-0034 §2).  A keep-alive key bridges the two:
# `register_run` stamps it when a run starts, and every result the results
# writer processes refreshes it; when no run is live it expires and the tick
# opens NO Aurora connection — so idle the ticker cannot defeat auto-pause by
# itself.  The TTL is deliberately a little longer than the tick so a single
# stamp covers several ticks.
_ACTIVE_RUNS_KEY = "control:any-run-active"
# The run-activity window: the tick stays hot while a run is producing work.
# 15 min comfortably covers a run's longest quiet stretch (first result after
# a long instance, or a run whose remaining instances are all in flight), and
# bounds the post-run tail — after this expires the tick stops entirely (idle)
# and Aurora can auto-pause.  A bounded tail of ~30 tiny SELECTs is a rounding
# error against the $100/quarter budget; an *unbounded* 30s poll is the exact
# defect the brief's M1.9 corollary forbids.
_ACTIVE_RUNS_TTL_S = 900

# The field inside control:flags that proves "this was refreshed by a live
# publisher" — its age is exactly what separates "healthy paused" from
# "publisher died an hour ago".
_PUBLISHED_AT_FIELD = "published_at"

# Pool names understood by pause.  "gateway" is stored so the gateway tier can
# pause too, but harness is the default action (M1 §1.10).
POOLS = ("harness", "eval", "gateway")


@dataclass(frozen=True)
class ControlView:
    """The whole control surface as one immutable snapshot.

    ``aborted_runs`` is the set of run ids currently marked aborted; abort is
    per-run while pause is per-pool, so both travel in the same read.

    ``published_at`` is the publisher's epoch-seconds timestamp; ``stale`` is a
    convenience flag (True when older than :data:`STALE_AFTER_S`).  Note that a
    failing :func:`read` returns ``stale=True`` *and* all pools paused.
    """

    harness_paused: bool = False
    eval_paused: bool = False
    gateway_paused: bool = False
    aborted_runs: frozenset[str] = field(default_factory=frozenset)
    published_at: float = 0.0
    stale: bool = True  # default to stale until proven otherwise
    # Audit of the last operator action (M1.10 / §1.1 signature deviation §2):
    # ``updated_by`` and ``reason`` are written into the control:flags hash by
    # set_pause / request_abort and surfaced here so GET /control can show WHO
    # paused/aborted and WHY.  Empty when no action has been recorded yet, or
    # after a publish from Aurora that predates the audit fields.
    updated_by: str = ""
    reason: str = ""

    def is_paused_impl(self, pool: str) -> bool:
        """The paused flag for *pool* (harness | eval | gateway)."""
        return {
            "harness": self.harness_paused,
            "eval": self.eval_paused,
            "gateway": self.gateway_paused,
        }.get(pool, False)


# The permanent "can't confirm anything, assume the worst" sentinel: unreadable,
# missing, or stale all collapse to this.  Fail-closed means we can only
# over-stop, never under-stop.
_ALL_PAUSED = ControlView(harness_paused=True, eval_paused=True, gateway_paused=True, stale=True)


def _redis() -> Any:
    """The shared cached Redis client (M1 §1.2)."""
    from swebench_eval.database.redis_client import _get_client

    return _get_client()


def read() -> ControlView:
    """Read the live control state, fail-closed.

    Any failure — unreachable Valkey, a missing key, a stale ``published_at`` —
    returns :data:`_ALL_PAUSED`.  See the module docstring for why that default
    is a requirement, not a preference.
    """
    try:
        raw = _redis().hgetall(_FLAGS_KEY)
    except Exception:
        logger.warning("control:read failed (fail-closed -> PAUSED)", exc_info=True)
        return _ALL_PAUSED

    if not raw:
        return _ALL_PAUSED  # missing key

    def _bytes_eq(field_name: str, val: bytes | None) -> bool:
        return val == b"1"

    navigate = {k.decode() if isinstance(k, bytes) else str(k): v for k, v in raw.items()}

    try:
        published_at = float(navigate.get(_PUBLISHED_AT_FIELD, b"0"))
    except (TypeError, ValueError):
        published_at = 0.0

    if time.time() - published_at > STALE_AFTER_S:
        # Publisher dead / leftover key from a destroyed Valkey.  Treat as
        # paused (over-stop is recoverable; under-stop is not).
        return ControlView(
            harness_paused=True,
            eval_paused=True,
            gateway_paused=True,
            published_at=published_at,
            stale=True,
        )

    try:
        members = _redis().smembers(_ABORTED_KEY)
        aborted = frozenset(
            m.decode("utf-8", "replace") if isinstance(m, bytes) else str(m) for m in members
        )
    except Exception:  # noqa: BLE001
        aborted = frozenset()

    def _field(name: str) -> str:
        val = navigate.get(name)
        return val.decode("utf-8", "replace") if isinstance(val, bytes) else str(val or "")

    return ControlView(
        harness_paused=_bytes_eq("harness", navigate.get("harness")),
        eval_paused=_bytes_eq("eval", navigate.get("eval")),
        gateway_paused=_bytes_eq("gateway", navigate.get("gateway")),
        aborted_runs=aborted,
        published_at=published_at,
        stale=False,
        updated_by=_field("updated_by"),
        reason=_field("reason"),
    )


def is_paused(pool: str) -> bool:
    """True if *pool* is paused (harness | eval | gateway), read fail-closed."""
    return read().is_paused_impl(pool)


def is_run_aborted(run_id: str) -> bool:
    """True if ``run_id`` is in the aborted set (read fail-closed).

    An unreadable Redis returns True — "cannot confirm not-aborted" reads as
    aborted, consistent with the door only ever over-stopping.
    """
    try:
        members = _redis().smembers(_ABORTED_KEY)
    except Exception:  # noqa: BLE001
        return True
    return (run_id.encode() if isinstance(run_id, str) else run_id) in members


# ---------------------------------------------------------------------------
# Orchestrator-only writers.  These are the ONLY components that write.  Every
# consumer reads :func:`read` / :func:`is_paused` / :func:`is_run_aborted`.
# ---------------------------------------------------------------------------


def _write_flags(flags: dict[str, int], updated_at: float | None = None) -> None:
    """Write ``control:flags`` for *flags* keyed by pool name -> 0|1.

    ``published_at`` is the timestamp of THIS write, stamped by the publisher.
    When *updated_at* is given (the Aurora row's ``updated_at``), it is carried
    into the hash as ``updated_at`` so the reconcile leg's compare-and-set can
    tell a fresh Aurora write from a stale one.
    """
    data = {k: str(v).encode() for k, v in flags.items()}
    data[_PUBLISHED_AT_FIELD] = str(time.time()).encode()
    if updated_at is not None:
        data[_UPDATED_AT_FIELD] = str(updated_at).encode()
    _redis().hset(_FLAGS_KEY, mapping=data)


def heartbeat() -> None:
    """Refresh ``control:flags.published_at`` — the publisher's liveness.

    Field-level only (HSET on ``published_at``), NEVER a ``_write_flags``:
    that rebuilds the whole hash from an in-memory dict and would let a stale
    view clobber fresh pause/audit fields.  The heartbeat is what keeps the
    dispatcher's ``read()`` from failing closed to ``paused`` between
    reconciles; it has no dependency on a run being active (the pause
    deadlock fix — the keep-alive must not depend on work happening).
    """
    _redis().hset(_FLAGS_KEY, mapping={_PUBLISHED_AT_FIELD: str(time.time()).encode()})


def _read_updated_at(connection: Any) -> float | None:
    """Aurora ``control_state.updated_at`` (epoch) — the CAS guard value.

    Returns ``None`` on ANY failure (dropped connection, missing column) —
    an error MUST be distinguishable from "no timestamp".  A ``0.0`` here
    would make the CAS compare ``0.0 >= 0.0`` (true) and skip the flag write,
    failing OPEN: the heartbeat would hold the gate open over flags that never
    arrive.  ``None`` means "cannot read Aurora, so do not silently decide the
    fleet is fine" — see :func:`reconcile_from_db`.
    """
    cursor: Any = None
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT updated_at FROM control_state WHERE id = TRUE")
        row = cursor.fetchone()
    except Exception:
        logger.warning(
            "control: could not read control_state.updated_at from Aurora", exc_info=True
        )
        return None
    finally:
        if cursor is not None:
            cursor.close()
    if row is None or row[0] is None:
        return None
    return float(row[0].timestamp())


def _hash_updated_at() -> float:
    """The ``control:flags.updated_at`` currently in Valkey (0 when absent)."""
    try:
        raw = _redis().hget(_FLAGS_KEY, _UPDATED_AT_FIELD)
    except Exception:  # noqa: BLE001 - unreadable Redis reads as "absent"
        return 0.0
    if not raw:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def reconcile_from_db(connection: Any) -> None:
    """Compare-and-set reconcile: publish Aurora's control truth into Valkey.

    The flag write is SKIPPED when the hash already carries a ``updated_at``
    >= Aurora's row — an operator pause written between our read and now must
    not be silently undone (lost-update race).  ``published_at`` is refreshed
    REGARDLESS so the liveness heartbeat keeps the gate open.  The aborted-run
    set is refreshed unconditionally (it is a set, not a single row; there is
    no row timestamp to race against).

    **Fail-closed on Aurora error.** When ``_read_updated_at`` cannot read
    Aurora it returns ``None`` — we must NOT take the skip branch (that would
    hold the gate open over flags that never arrive = fail OPEN).  Instead we
    propagate: reconcile writes the flags from whatever Aurora did return
    (which, on a read failure, is the module's fail-closed all-paused default),
    so an operator pause is never silently undone by holding a fresh heartbeat
    over stale flags.
    """
    row_updated_at = _read_updated_at(connection)
    if row_updated_at is not None and _hash_updated_at() >= row_updated_at:
        # A newer operator write is already in the hash — do not clobber it.
        heartbeat()
        return
    flags = _read_control_flags(connection)
    _write_flags(flags, updated_at=row_updated_at)
    _write_aborted(_read_aborted_run_ids(connection))


def _write_aborted(run_ids: set[str]) -> None:
    r = _redis()
    if run_ids:
        r.sadd(_ABORTED_KEY, *run_ids)
    else:
        r.delete(_ABORTED_KEY)


def _read_control_flags(connection: Any) -> dict[str, int]:
    """Read Aurora's ``control_state`` single row into a pool->flag dict."""
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute("""SELECT harness_paused, eval_paused, gateway_paused
               FROM control_state WHERE id = TRUE""")
        row = cursor.fetchone()
    except Exception:
        logger.warning("control: could not read control_state from Aurora", exc_info=True)
        return {"harness": 1, "eval": 1, "gateway": 1}
    finally:
        if cursor is not None:
            cursor.close()
    if row is None:
        return {"harness": 1, "eval": 1, "gateway": 1}
    return {
        "harness": 1 if row[0] else 0,
        "eval": 1 if row[1] else 0,
        "gateway": 1 if row[2] else 0,
    }


def _read_aborted_run_ids(connection: Any) -> set[str]:
    """Read the set of aborted-run ids from Aurora (`runs.status = 'aborting'` plus
    any `stopped_at` terminal rows still carrying the intent).  The abort
    executor keeps `runs.status = 'aborting'` while it drains; the terminal
    `'aborted'` status is written only when the drain completes.  The control
    key must therefore reflect both: a run mid-abort is still gated.
    """
    cursor: Any = None
    try:
        cursor = connection.cursor()
        cursor.execute("""SELECT run_id FROM runs WHERE status IN ('aborting', 'aborted')""")
        return {str(row[0]) for row in cursor.fetchall()}
    except Exception:
        logger.warning("control: could not read aborted runs from Aurora", exc_info=True)
        return set()
    finally:
        if cursor is not None:
            cursor.close()


def _current_pool_flags() -> dict[str, int]:
    """The current ``control:flags`` pool->0|1 dict (defaults all unpaused).

    Read so a mutation can MERGE (leave pools it was not asked about alone)
    instead of rebuilding the whole hash from zero.  Missing key / unreadable
    Redis returns all-0: a flush is repaired by the next :func:`publish_from_db`,
    and pausing on a missing key is the over-stop direction (safe).
    """
    try:
        raw = _redis().hgetall(_FLAGS_KEY)
    except Exception:
        logger.warning("control: could not read current flags for merge", exc_info=True)
        return {p: 0 for p in POOLS}
    out = {p: 0 for p in POOLS}
    for k, v in raw.items():
        name = k.decode() if isinstance(k, bytes) else str(k)
        if name in out:
            out[name] = 1 if v == b"1" else 0
    return out


def set_pause(
    pools: list[str],
    paused: bool,
    actor: str = "operator",
    reason: str = "",
    updated_at: float | None = None,
) -> None:
    """Set the pause flags for *pools* (harness | eval | gateway).

    MERGES the named pools over the current hash — a pool that is *not* in
    *pools* keeps its existing flag (``harness_paused`` must not silently
    resume when the operator pauses *eval*).  Writes Redis directly (the
    operator waiting on a 30s tick is the failure mode this fast path exists
    to remove); publishes ``actor`` / ``reason`` as ``updated_by`` / ``reason``
    in the same atomic hash so ``GET /control`` can return the audit trail
    (M1.10), matching §1.1's ``set_pause(pools, paused, actor, reason)``.

    ``updated_at`` — when called from the API it MUST be the Aurora row's
    ``updated_at`` (see ``_set_pause_in_db``); the reconcile leg compares this
    hash value against the row so a pause is never silently undone by a stale
    reconcile (CAS).  Defaults to ``time.time()`` (local clock) for non-API
    callers — that is fine today because nothing else writes pause in the
    deployed path, but any future caller MUST pass the Aurora timestamp, or
    clock skew silently defeats the CAS guard.
    """
    current = _current_pool_flags()
    for pool in pools:
        if pool not in POOLS:
            raise ValueError(f"unknown pool '{pool}' (expected one of {POOLS})")
        current[pool] = 1 if paused else 0
    data = {k: str(v).encode() for k, v in current.items()}
    data[_PUBLISHED_AT_FIELD] = str(time.time()).encode()
    data[_UPDATED_AT_FIELD] = str(updated_at if updated_at is not None else time.time()).encode()
    data["updated_by"] = str(actor).encode()
    data["reason"] = str(reason).encode()
    _redis().hset(_FLAGS_KEY, mapping=data)


def request_abort(run_id: str, scope: str = "", reason: str = "", actor: str = "operator") -> None:
    """Mark *run_id* aborted in Redis (the fast path).

    The abort executor records the full intent (scope, reason, actor,
    ``runs.status = 'aborting'``) in Aurora first; this publishes it so in-flight
    dispatch gates stop immediately rather than on the next tick.  Mirrors
    §1.1's ``request_abort(run_id, scope, reason, actor)``: the audit fields are
    published in ``control:flags`` (as ``updated_by`` / ``reason``) so
    ``GET /control`` can attribute the last operator action.
    """
    _redis().sadd(_ABORTED_KEY, run_id)
    data = {
        "updated_by": str(actor).encode(),
        "reason": str(reason).encode(),
        _PUBLISHED_AT_FIELD: str(time.time()).encode(),
    }
    _redis().hset(_FLAGS_KEY, mapping=data)


def publish_from_db(connection: Any) -> None:
    """Publish Aurora's control truth into Valkey.

    Called on orchestrator START only (ADR-0034 §2: Aurora is truth; Valkey is
    rebuilt from it so a cache flush or eval-tier teardown loses nothing) and
    by :func:`reconcile_from_db` which splits the tick into a heartbeat +
    CAS reconcile.  Full write carries the row's ``updated_at`` so the CAS
    guard has a baseline from boot.
    """
    flags = _read_control_flags(connection)
    _write_flags(flags, updated_at=_read_updated_at(connection))
    _write_aborted(_read_aborted_run_ids(connection))


def mark_runs_active() -> None:
    """Stamp the publisher tick's "a run is doing work" marker.

    Called when ``register_run`` starts a run (the tick stops being idle and
    re-publishes on cadence) and by the results_writer whenever it processes a
    result from a live run (running work keeps the marker fresh).

    Why a marker and not status IN ('running','aborting')?  ``runs.status``
    has NO normal terminal transition — a finished run stays ``'running'``
    until intentionally aborted (swebench_eval/orchestrator/api/queries.py
    documents exactly this: "the only transition after 'running' is the
    abort pair").  So "is a run active" cannot be read from Aurora; it is a
    property of *run activity*, so a TTL Redis marker it is.  Longer details in
    ``publish_from_db``'s keep-alive comment.
    """
    _redis().set(_ACTIVE_RUNS_KEY, b"1", ex=_ACTIVE_RUNS_TTL_S)


def runs_active_marked() -> bool:
    """True while a live run has been seen/active recently.

    The publisher tick's idle/active gate.  While a run is active the tick must
    keep ``control:flags`` fresh so the harness/eval gates stay open (a stale
    key reads as paused, silently stalling a run mid-dispatch); once the
    marker TTL lapses without a run, the tick goes fully idle — it opens NO
    Aurora connection, so Aurora auto-pause can engage (M1.9 corollary,
    ADR-0034 §2: a tick that polls while idle prevents auto-pause).
    """
    try:
        return bool(_redis().exists(_ACTIVE_RUNS_KEY))
    except Exception:
        # Fail toward "not active": an unreadable Redis must not keep the
        # tick hot (that would defeat Aurora auto-pause); any subsequent
        # publish fails closed anyway, so the only cost of a wrong False is a
        # paused gate, which is the safe direction.
        logger.warning("control: could not read active-runs marker", exc_info=True)
        return False
