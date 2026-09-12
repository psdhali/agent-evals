"""Run Supervisor — the control-plane SINGLETON (BUILDER6-SPLIT-SUPERVISOR-
AND-RESULTS-WRITER-2026-08-31.md / CONTROL-PLANE-DECOMPOSITION-DESIGN-
2026-08-31.md).

Relocated out of ``results_writer.py`` verbatim — "a relocation, not a
redesign" — because the component with the largest blast radius in the
system (a 90-second heartbeat: ``control/state.py``'s ``STALE_AFTER_S``) used
to share a process with the bulk data path.  If ``published_at`` stops being
refreshed, ``control_state.read()`` fail-closes to all-paused for every
reader and the whole run halts.  This process does almost nothing else, on
purpose — anything added here later must be judged against that; a loop that
can block for seconds or allocate heavily does not belong here.

Owns:

  - the Aurora→Valkey control publisher / heartbeat (``_maybe_publish_control``,
    rebuilt from Aurora at startup by ``_publish_control_from_aurora``) — must
    never be starved.
  - reaper rule 2 — deadline (``_reap_deadline_rule``): DB-scan-driven,
    in-process timer, singleton-required.
  - reaper rule 3 — never-dispatched (``_reap_never_dispatched``): its
    "consecutive empty" state (``_never_dispatched_empty_since``) must be
    ONE view — two copies would each reach the threshold independently.

Every reap still leaves through the RESULTS queue: ``_emit_reap_result`` and
``is_ready_to_close`` are imported from ``results_writer.py`` rather than
duplicated here — one producer for reaps regardless of which rule (1, 2, or
3) found them, "every reap is recorded, never silent."  No new IPC, no new
Redis coordination: the seam this split rides is the same one that already
existed in-process (results_writer.py's own docstring; different queues,
already-async ``_emit_reap_result``, no shared in-memory state between the
consume loop and these ticks).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from swebench_eval.control import state as control_state
from swebench_eval.orchestrator.control_plane.results_writer import (
    _emit_reap_result,
    _running_instance_ids_for_run,
    is_ready_to_close,
)

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# The publisher tick (ADR-0034 §2 / M1.2)
#
# Aurora is truth; Valkey is the read path; run-supervisor is the
# CONTROL-PLANE SINGLETON — the one process configured in entrypoint.sh's
# run-supervisor case.  It therefore owns the Aurora→Valkey publish tick:
# rebuild state from Aurora on start, then refresh `control:flags` on a ~30s
# cadence WHILE a run is active.
#
# The tick's idle/active gate is the run-activity marker (state.py
# `runs_active_marked`).  It must NOT be "any run with status running/aborting":
# `runs.status` has no normal terminal transition (orchestrator/api/queries.py
# documents that the only move after 'running' is the abort pair), so a status
# query would keep the tick hot forever — the exact Aurora auto-pause failure
# the M1.9 corollary forbids.  Activity (register_run stamping it, results-writer
# re-stamping it for every result a live run produces) expires on its TTL once
# the run's last result lands, and with it the tick goes fully idle: no Aurora
# connection at all.  That is ADR-0034 §2's "go idle between runs" made real.

_CONTROL_TICK_S = 30.0
_control_last_publish_s = 0.0


def _maybe_publish_control() -> None:
    """The publisher tick: heartbeat always, reconcile only while a run is active.

    Split into two legs (the pause-deadlock fix, builder1-hw-rebuild-pause-fix):

    1. **heartbeat** — ``HSET control:flags published_at <now>``, field-level,
       on EVERY tick regardless of run activity.  One Redis write, no Aurora.
       This is what keeps the dispatcher's ``read()`` from failing closed to
       ``paused`` between reconciles.  Crucially it does NOT depend on a run
       being active — the old keep-alive (the run-activity marker) was only
       re-stamped by ``register_run`` and by *results*, which are downstream
       of the gate, so a paused dispatcher produced no results and the marker
       never refreshed → nothing ever republished → permanent pause.  The
       heartbeat breaks that structurally.
    2. **reconcile** — re-read Aurora's control truth and rewrite the flags,
       ONLY while a run is active (``runs_active_marked``).  One Aurora
       connection per tick, never held across the loop; Aurora can still
       auto-pause when idle (M1.9).  Reconcile is compare-and-set: it skips
       the flag write when ``hash.updated_at >= row.updated_at`` so an
       operator pause between the read and the write is never undone.
    """
    global _control_last_publish_s
    now = time.monotonic()
    if now - _control_last_publish_s < _CONTROL_TICK_S:
        return
    _control_last_publish_s = now

    # Leg 1: heartbeat — field-level liveness write, always.
    control_state.heartbeat()

    if not control_state.runs_active_marked():
        return  # idle between runs: no Aurora connection at all
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    try:
        control_state.reconcile_from_db(conn)
    finally:
        conn.close()


def _publish_control_from_aurora() -> None:
    """M1.2: rebuild ``control:flags`` from Aurora on run-supervisor start.

    A gate that reads a stale hash after a restart is a gate that blocks a
    fresh run for no reason (the pause flags were operator intent that
    survived in Aurora; the hash must catch up).  One connection, then the
    tick takes over the cadence.
    """
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    try:
        control_state.publish_from_db(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rule 2 — deadline passed with no sign of life (inferred).
# ---------------------------------------------------------------------------

# D7: provisional.  Must cover queue_wait_s + provision_s + image_pull_s +
# worker_boot_s — recorded per instance (ADR-0037), so after the first real
# run this should be tightened to an observed p99 rather than left as
# folklore.  20 minutes, per the owner's decision, until then.
_REAP_MARGIN_S = 1200

# Harness-phase / eval-phase non-terminal states the deadline rule (rule 2)
# considers a candidate — PENDING is excluded here on purpose: rule 2 needs
# `dispatched_at`/`seeded_at` to measure a deadline FROM, and a row that was
# never even dispatched/enqueued is rule 3's job (queue-drained), not rule 2's.
_RULE2_HARNESS_STATES = ("DISPATCHED", "HARNESS_RUNNING")
_RULE2_EVAL_STATES = ("EVAL_RUNNING",)

# _running_instance_ids_for_run is imported from results_writer.py (top of
# this file) rather than defined here — it has no module-level state and
# abort.py's sweep needs it too; one definition for both consumers.


def _phase_timeout_seconds(conn: Any, run_id: str) -> int | None:
    """``config.timeout_seconds`` from the run's config_snapshot.

    §7: "phase_timeout comes from the run's own config — timeout_seconds for
    harness, the per-eval-task outer timeout (§9.6) for eval." architecture.md
    §9.6 describes that eval-side ceiling conceptually but no run-config field
    or constant for it exists anywhere in the repo today (grepped; the eval
    worker's grading path is explicitly out of this build's scope, §10) — so
    this reuses ``timeout_seconds`` for BOTH phases rather than inventing a
    new constant.  Flagged in builder4-run-launch-response.md: an eval-
    specific field is the correct fix once someone implements the ECS-level
    eval watchdog §9.6 describes.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT config_snapshot->>'timeout_seconds' FROM runs WHERE run_id = %s", (run_id,)
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    try:
        return int(float(row[0]))
    except (TypeError, ValueError):
        return None


def _reap_deadline_rule(conn: Any, run_id: str) -> None:
    """§7 rule 2: all four conditions must hold (dispatched/seeded + timeout +
    margin < now(); no live progress key; no running ECS task; run not
    paused/aborting for that phase's pool) before a row is marked ABANDONED.
    """
    phase_timeout = _phase_timeout_seconds(conn, run_id)
    if phase_timeout is None:
        return
    cutoff = _now_utc() - timedelta(seconds=phase_timeout + _REAP_MARGIN_S)
    running: set[str] | None = None  # computed lazily, once, only if needed

    for phase, states, time_column, pool in (
        ("harness", _RULE2_HARNESS_STATES, "dispatched_at", "harness"),
        ("eval", _RULE2_EVAL_STATES, "seeded_at", "eval"),
    ):
        if control_state.is_paused(pool):
            continue  # §7 precondition: a paused pool legitimately produces nothing
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT instance_id, attempt_number FROM instance_results
                    WHERE run_id = %s AND phase = %s AND state IN %s
                      AND {time_column} IS NOT NULL AND {time_column} < %s""",
                (run_id, phase, states, cutoff),
            )
            candidates = cur.fetchall()
        if not candidates:
            continue
        if running is None:
            running = _running_instance_ids_for_run(run_id)
        from swebench_eval.database.redis_client import read_progress

        for instance_id, attempt_number in candidates:
            if instance_id in running:
                continue
            if read_progress(run_id, instance_id, attempt_number) is not None:
                continue
            _emit_reap_result(
                run_id,
                instance_id,
                attempt_number,
                phase,
                "ABANDONED",
                f"deadline_passed: no result within {phase_timeout}+{_REAP_MARGIN_S}s of "
                f"{time_column}, no live ECS task (startedBy={run_id}), no live progress key",
            )


# ---------------------------------------------------------------------------
# Rule 3 — never picked up, and the queue is drained.
# ---------------------------------------------------------------------------

# 2026-08-29 hardening, after a live false positive: a freshly-dispatched
# instance (a real RunTask had just succeeded, task still provisioning) got
# reaped 21s after dispatch because a single get_queue_depth() read came back
# (0, 0) — AWS documents ApproximateNumberOfMessages*  as eventually
# consistent, not exact, and the old rule trusted one instantaneous read with
# no corroboration. (Root cause of the specific incident was separate and is
# already fixed — the deployed harness-dispatcher was stale and never emitted
# DISPATCHED at all, so dispatched_at was permanently NULL, not just briefly —
# but rule 3 gets its own defenses regardless, matching rule 2's shape, since
# a bad SQS read is a real, independent risk on its own.) Provisional, like
# _REAP_MARGIN_S — tighten from observed data once real dispatch-to-DISPATCHED
# latency is on hand.
_NEVER_DISPATCHED_MIN_ROW_AGE_S = 180
_NEVER_DISPATCHED_MIN_CONSECUTIVE_EMPTY_S = 90

# (run_id, instance_id, attempt_number) -> monotonic time first observed with
# an empty harness-jobs queue.  Reset to "not observed" whenever the queue
# reads non-empty for that run, or the row stops being a rule-3 candidate at
# all (advanced past PENDING some other way) — a resumed count would let a
# stale, unrelated observation stand in for a fresh one.  Process-local
# (run-supervisor is a SINGLETON — the whole reason rules 2/3 live here and
# not in the N-replica results-writer); a restart just means rule 3 needs one
# more consecutive-empty window before it can fire again, the same safe
# direction as everything else here.
_never_dispatched_empty_since: dict[tuple[str, str, int], float] = {}


def _reap_never_dispatched(conn: Any, run_id: str) -> None:
    """§7 rule 3: PENDING, dispatched_at IS NULL, queue globally drained ->
    NEVER_DISPATCHED.  Queue depth is global, not per-run (§7: "with two runs
    live this simply will not fire. That is the correct direction to be
    wrong in.").

    Three independent defenses before anything actually reaps, mirroring
    rule 2's shape:
      1. the row itself must be at least _NEVER_DISPATCHED_MIN_ROW_AGE_S old
         (created_at) — never conclude never-dispatched off a row that was
         seeded moments ago.
      2. the queue must have read empty on EVERY tick for at least
         _NEVER_DISPATCHED_MIN_CONSECUTIVE_EMPTY_S — one stale/approximate
         SQS read can no longer reap anything by itself.
      3. even once 1 and 2 both hold, a live ECS task (startedBy=run_id) or a
         live Redis progress key for that instance still blocks the reap —
         the same two checks rule 2 already requires.
    """
    if control_state.is_paused("harness"):
        return
    cutoff = _now_utc() - timedelta(seconds=_NEVER_DISPATCHED_MIN_ROW_AGE_S)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT instance_id, attempt_number FROM instance_results
               WHERE run_id = %s AND phase = 'harness' AND state = 'PENDING'
                 AND dispatched_at IS NULL AND created_at < %s""",
            (run_id, cutoff),
        )
        candidates = cur.fetchall()
    if not candidates:
        return
    from swebench_eval.queue import client as queue_client

    depth = queue_client.get_queue_depth("harness-jobs")
    now = time.monotonic()
    current_keys = {(run_id, str(iid), int(att)) for iid, att in candidates}

    if depth.visible > 0 or depth.not_visible > 0:
        # Queue has real work right now — nothing is confirmed drained. Drop
        # any in-progress observation for this run's candidates; a later
        # empty read starts the consecutive-window count over, never resumes
        # a stale one.
        for key in current_keys:
            _never_dispatched_empty_since.pop(key, None)
        return  # queue not drained yet — too early to conclude never-dispatched

    # Bounded memory: forget tracking for anything that stopped being a
    # candidate for this run since the last tick (advanced past PENDING some
    # other way, run finished, etc.).
    for key in [k for k in _never_dispatched_empty_since if k[0] == run_id]:
        if key not in current_keys:
            del _never_dispatched_empty_since[key]

    running: set[str] | None = None  # computed lazily, once, only if needed
    from swebench_eval.database.redis_client import read_progress

    for instance_id, attempt_number in candidates:
        key = (run_id, str(instance_id), int(attempt_number))
        first_seen = _never_dispatched_empty_since.get(key)
        if first_seen is None:
            _never_dispatched_empty_since[key] = now
            continue  # first empty-queue sighting for this row — not enough evidence yet
        if now - first_seen < _NEVER_DISPATCHED_MIN_CONSECUTIVE_EMPTY_S:
            continue  # queue has read empty, but not continuously long enough yet

        if running is None:
            running = _running_instance_ids_for_run(run_id)
        if instance_id in running:
            continue
        if read_progress(run_id, instance_id, attempt_number) is not None:
            continue

        _emit_reap_result(
            run_id,
            instance_id,
            attempt_number,
            "harness",
            "NEVER_DISPATCHED",
            f"harness-jobs queue drained (0 visible, 0 in-flight) continuously for "
            f">={_NEVER_DISPATCHED_MIN_CONSECUTIVE_EMPTY_S}s, row >= "
            f"{_NEVER_DISPATCHED_MIN_ROW_AGE_S}s old, no live ECS task "
            f"(startedBy={run_id}), no live progress key — never dispatched",
        )
        del _never_dispatched_empty_since[key]


# ---------------------------------------------------------------------------
# The reaper tick itself.
# ---------------------------------------------------------------------------

_REAPER_TICK_S = 30.0
_reaper_last_run_s = 0.0


def _maybe_run_reaper() -> None:
    """§7's timer.  Gated on ``runs.status = 'running'`` existing at all — "the
    reaper's gate... is now legal": once runs reach a terminal status this
    query is idle between runs and hot while one is live, which is exactly
    what M1.9 requires and what §7 says run-launch's finalisation unblocks.
    """
    global _reaper_last_run_s
    now = time.monotonic()
    if now - _reaper_last_run_s < _REAPER_TICK_S:
        return
    _reaper_last_run_s = now

    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT run_id FROM runs WHERE status = 'running'")
            run_ids = [str(r[0]) for r in cur.fetchall()]
        if not run_ids:
            return  # idle: no Aurora work beyond the one SELECT above
        for run_id in run_ids:
            if control_state.is_run_aborted(run_id):
                continue  # §7 precondition: not aborting
            try:
                _reap_deadline_rule(conn, run_id)
                _reap_never_dispatched(conn, run_id)
                if is_ready_to_close(conn, run_id):
                    logger.info("run %s: zero non-terminal rows — ready for operator close", run_id)
            except Exception:
                # One run's reaper failure must not block every other run's
                # pass — log and continue to the next run_id.
                logger.exception("reaper pass failed for run %s", run_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entrypoint — the singleton loop.
# ---------------------------------------------------------------------------

# How often the outer loop wakes to re-check the two ticks above.  Both ticks
# self-gate on their own ~30s monotonic timers (_CONTROL_TICK_S,
# _REAPER_TICK_S); this only bounds how promptly a just-opened window is
# noticed, and keeps the heartbeat's WALL-CLOCK freshness comfortably inside
# control/state.py's STALE_AFTER_S=90 even under a slow tick.  Unlike
# results_writer's loop this process has no blocking SQS receive to ride, so
# it sleeps explicitly.
_TICK_SLEEP_S = 5.0

# One liveness INFO every N ticks (N x _TICK_SLEEP_S ≈ 5 minutes) — see the
# heartbeat-log block in run_run_supervisor.
_HEARTBEAT_LOG_EVERY = 60


def run_run_supervisor() -> None:
    """The control-plane singleton: heartbeat + reaper, forever.

    Deliberately the whole function — no message consumption, no bulk work.
    Every tick is wrapped in its own try/except (same discipline
    results_writer.py used before the split): a transient Aurora error must
    not be able to kill the heartbeat that everything else's liveness depends
    on ("no heartbeat -> published_at stale -> read() fails closed to paused
    -> the deadlock this heartbeat exists to break").
    """
    from swebench_eval.database.connection import ensure_additional_databases, run_migrations
    from swebench_eval.logging_bootstrap import configure_logging

    configure_logging()
    logger.info("run-supervisor starting (heartbeat + reaper)")

    run_migrations()
    ensure_additional_databases()
    # ADR-0034 §2 / M1.2: rebuild control state from Aurora on start (the
    # control-plane singleton).  A stale key after a restart would read as
    # paused and block a fresh run for no reason.  Guarded (2026-09-02): this
    # crash-looped the whole service at boot when Redis was unreachable/
    # misconfigured, taking the reaper and heartbeat down with it — but a
    # failed publish only means readers stay fail-closed (paused), and the
    # 5s tick below retries the publish anyway.  Degrade, don't die.
    try:
        _publish_control_from_aurora()
    except Exception:
        logger.exception(
            "startup control-state publish failed — readers stay fail-closed (paused) "
            "until the tick's republish succeeds; supervisor continues"
        )

    # 2026-09-04 (owner): the pacer/planner seeds live in Valkey (eval tier) and die with every
    # eval destroy; the last probe's seeds are persisted in Aurora (pacer_cfg_seeds). Restore
    # every known pool whose hash is empty, here in the planner's own process, so the API's
    # launch-screen consistency line and the first launch both see them. Best-effort: a failure
    # leaves the pacer on defaults, as before. Same degrade-don't-die rule as above.
    try:
        from swebench_eval.orchestrator.control_plane import pacer_seeds

        restored = pacer_seeds.rehydrate_known_pools()
        logger.info("startup pacer-seed rehydration: %s", restored or "nothing to restore")
    except Exception:
        logger.exception("startup pacer-seed rehydration failed — pacer stays on defaults")
    # 2026-09-08: the same cycle also wipes what LAUNCH wrote per run — the raw LiteLLM key
    # (run_key_cache; without it the dispatcher refuses every restarted job, ADR-0035) and the
    # run alias's pacer:cfg copy (without it the gateway admits the run on its defaults). Both
    # were found live on the first restart across a Valkey recreate. Re-mint / re-fill for
    # every run still open; per-run best-effort, a failure leaves that run as it was.
    try:
        from swebench_eval.orchestrator.control_plane import open_run_recovery

        open_run_recovery.recover_open_runs()
    except Exception:
        logger.exception("startup open-run recovery failed — restarts of open runs may refuse")
    # Same for the operator's global limits (operator:limits — borrowed-curve cap, growth
    # clamp, utilisation, eval max workers / scale-in): Aurora's audit table is the truth.
    try:
        from swebench_eval.database.redis_client import _get_client
        from swebench_eval.orchestrator.control_plane import operator_limits

        n = operator_limits.rehydrate_global(_get_client())
        logger.info("startup operator-limits rehydration: %d field(s) restored", n)
    except Exception:
        logger.exception("startup operator-limits rehydration failed — defaults stay in force")

    # Eval autoscaler (BUILDER4-EVAL-AUTOSCALER-2026-08-31.md; reviewer F1 of the exact-design
    # review, 2026-09-01): runs on its OWN DAEMON THREAD, never inline in this loop. The earlier
    # bounded-timeouts-only placement was arithmetically wrong: a tick makes up to ~8 AWS calls,
    # and at the bounded worst case (5s connect + 10s read each) that sums past the 90s heartbeat
    # budget — and the heartbeat is the component whose stall halts the entire run at T+90s (the
    # reason this service exists). The bounded clients STAY (a thread that hangs forever is still
    # a leak); the thread means the main loop's cadence never depends on them at all.
    # Construction stays here, before the loop: a misconfigured scaler (live with no
    # EVAL_MAX_WORKERS) must fail LOUDLY at startup, not silently skip.
    from swebench_eval.orchestrator.control_plane.eval_autoscaler import EvalAutoscaler

    eval_autoscaler = EvalAutoscaler()
    logger.info(
        "eval-autoscaler: mode=%s max_workers=%d", eval_autoscaler.mode, eval_autoscaler.max_workers
    )
    if eval_autoscaler.mode != "off":
        eval_autoscaler.start_background()

    # The observation tick — the ONE writer of capacity_snapshot (CAPACITY-AND-PIPELINE-
    # VIEW-DESIGN-2026-08-31.md §3.1). Its own daemon thread, for the same F1 arithmetic as
    # the eval autoscaler AND because a reaper failure must not take observability with it.
    # Deliberately NOT gated on any autoscaler mode: it must run with or without one —
    # capacity_snapshot sat empty precisely because its only intended writer was a component
    # that did not exist. CAPACITY_OBSERVER_ENABLED=0 is the test/emergency kill switch.
    from swebench_eval.orchestrator.control_plane.capacity_observer import CapacityObserver

    capacity_observer = CapacityObserver()
    if capacity_observer.enabled:
        capacity_observer.start_background()
        logger.info("capacity-observer: started (30s tick)")
    else:
        logger.info("capacity-observer: disabled (CAPACITY_OBSERVER_ENABLED=0)")

    # Liveness heartbeat log (owner request 2026-09-02): after startup this
    # process logged only failures, so a healthy supervisor and a wedged one
    # looked identical in the log group — health was only provable through
    # /capacity side-effects.  One INFO per _HEARTBEAT_LOG_EVERY ticks (~5 min
    # at the 5s tick) with ok/fail counters; cheap, greppable, and its absence
    # for >2 intervals now MEANS something.
    _tick_n = 0
    _pub_ok = _pub_fail = _reap_ok = _reap_fail = 0
    _started = time.monotonic()
    while True:
        try:
            _maybe_publish_control()
            _pub_ok += 1
        except Exception:  # the tick is best-effort liveness
            _pub_fail += 1
            logger.exception("publisher tick failed (transient); heartbeat continues next tick")

        try:
            _maybe_run_reaper()
            _reap_ok += 1
        except Exception:
            _reap_fail += 1
            logger.exception("reaper tick failed (transient); continues next tick")

        _tick_n += 1
        if _tick_n % _HEARTBEAT_LOG_EVERY == 0:
            logger.info(
                "run-supervisor alive: tick=%d uptime_s=%d publish ok/fail=%d/%d "
                "reaper ok/fail=%d/%d",
                _tick_n,
                int(time.monotonic() - _started),
                _pub_ok,
                _pub_fail,
                _reap_ok,
                _reap_fail,
            )

        time.sleep(_TICK_SLEEP_S)
