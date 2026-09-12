"""The observation tick — the ONE writer of ``capacity_snapshot``.

CAPACITY-AND-PIPELINE-VIEW-DESIGN-2026-08-31.md §3.1, made real. Both autoscalers publish
``autoscaler:last_decision:{pool}`` every tick and, until this module, nothing read them —
the same shape as the Redis progress keys before the run-progress view: written faithfully,
thrown away. This tick is also THE gate on live mode: "compare decision records against a real
run, then flip" requires the records to exist somewhere an operator can see.

Two rules from the design are non-negotiable and shape everything here:

  1. **Reads Redis (and SQS/ECS), writes Aurora. Decides nothing, actuates nothing.**
  2. **Runs with or without an autoscaler.** ``capacity_snapshot`` was empty for weeks
     precisely because its only intended writer was a component that did not exist. Every
     autoscaler-derived field (desired, binding_constraint, decision age) is an optional
     fold-in; every measured field (queue depths, workers, ceiling utilisation) is read
     directly from the source the autoscalers themselves read.

Placement: its own DAEMON THREAD in the run-supervisor process — not inline in the loop
(the eval autoscaler's F1 arithmetic applies identically: 2 SQS + 1 ECS bounded calls can sum
to ~45s in the tail, and the heartbeat's stall halts the whole run) and not piggybacked on the
reaper's pass (design §3.1: "a reaper failure must not silently take observability with it").

What it records, per pool, every ~30s:

  - ``queue_depth`` / ``not_visible`` — SQS visible AND in-flight, both queues. Not-visible is
    not optional: a 300s visibility timeout means "empty queue" and "50 grades in flight" look
    identical without it (the incident-vs-capacity signal).
  - ``current_workers`` — harness: live (non-stale) ``instance_progress:*`` keys, the same
    fleet signal both autoscalers already use (harness tasks are RunTask-launched across six
    families; there is no service to describe). eval: the ECS service's ``runningCount``.
  - ``desired`` / ``binding_constraint`` / ``decision_age_s`` — folded from the pool's decision
    record when one exists; NULL (never zero) when absent. Unknown must never render healthy —
    the age is what lets a dead autoscaler's record go visibly stale on the chart.
  - ``ceiling_utilization`` — harness only: burst-bucket depletion (1 - level/c_burst) across
    recently-active pacer aliases, max — how close the fleet is to the admission ceiling,
    measured at the pacer that enforces it, autoscaler or not.
  - ``eta_low_s`` / ``eta_high_s`` — §5: a median–p90 RANGE, never a point, labelled an
    estimate downstream. Eval's backlog includes jobs that DO NOT EXIST YET (every live
    harness instance becomes an eval job) — count only the current queue depth and the view
    reports a five-minute eval ETA while four hundred instances are still to arrive.
  - ``gateway_headroom`` — written NULL, permanently: the column must never carry a scraped
    LiteLLM number (design §4.1 / the builder-4 addendum); ``ceiling_utilization`` replaces it.

Idle behaviour: rows are written while a run is active (``runs_active_marked``) OR while any
activity is observed (non-empty queues, live workers) — the drain tail after the activity
marker expires is exactly when the eval backlog is worth watching. A quiet system writes
nothing: a gap in the chart is correct (design §6), and an unbounded march of zero rows is not
a time series anyone charts. Aurora idle cost is unchanged either way — the supervisor's
reaper already opens a connection every 30s while the process runs.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from swebench_eval import aws_names
from swebench_eval.gateway.pacer import (
    alias_from_bucket_key,
    paced_key,
    pacer_cfg_key,
    pacer_waitq_key,
)
from swebench_eval.orchestrator.control_plane.decision_record import decision_key

logger = logging.getLogger(__name__)

# A progress key not refreshed within this window is a dead/dying instance, not a worker —
# the same threshold the eval autoscaler's feed-forward uses (_STALE_PROGRESS_S there).
_STALE_PROGRESS_S = 120.0

# A pacer burst bucket whose `upd` stamp is older than this belongs to an alias with no
# recent traffic — its depletion is history, not utilisation. 120s matches the wait-stats
# and overload counters' own expiry horizon.
_STALE_BUCKET_S = 120.0

# ETA per-job durations, seconds. Median/p90 pairs — §5's range, never a point.
# Provenance: harness = the measured per-instance trajectory at concurrency ~1 (~63 calls
# median x 9.1s median turn ≈ 570s; p90 177 calls ≈ 1610s — a 5-6x extrapolation at fleet
# width, which is WHY the output is a range). Eval = provisional folklore ("grades take
# minutes"); both sides get re-derived from the first real concurrent run's recorded
# durations. Env-overridable so the re-derivation is a config change, not a deploy.
_ETA_DEFAULTS = {
    "harness": (570.0, 1610.0),
    "eval": (240.0, 900.0),
}


def _eta_bounds(pool: str) -> tuple[float, float]:
    med_default, p90_default = _ETA_DEFAULTS[pool]
    med = float(os.environ.get(f"CAP_ETA_{pool.upper()}_MEDIAN_S", med_default))
    p90 = float(os.environ.get(f"CAP_ETA_{pool.upper()}_P90_S", p90_default))
    return med, p90


def _eta_s(backlog: int, workers: int, per_job_s: float) -> int | None:
    """Wave model: the backlog drains in ceil(backlog/workers) waves of one job-duration each,
    plus half a duration for the in-flight wave (assumed half done on average). Zero workers
    with a real backlog is UNKNOWN (None — an unstaffed queue has no drain rate and must not
    render as healthy), not infinity and not zero."""
    if backlog <= 0 and workers <= 0:
        return 0
    if workers <= 0:
        return None
    waves = math.ceil(backlog / workers)
    return round(waves * per_job_s + 0.5 * per_job_s)


@dataclass(frozen=True)
class PoolObservation:
    """One pool's row, exactly as written. None = not measured, never zero."""

    pool: str
    queue_depth: int | None
    not_visible: int | None
    current_workers: int | None
    desired: int | None
    binding_constraint: str | None
    ceiling_utilization: float | None
    decision_age_s: float | None
    eta_low_s: int | None
    eta_high_s: int | None
    # §2.4 provenance: the planner's budget constants' origin ('pacer_cfg' |
    # 'pacer_cfg_stale' | 'defaults'), from the harness decision record's budgets.source.
    # None for eval rows and when no record exists. 'defaults' must render as a visible
    # degraded state downstream — never as a clean record.
    constants_source: str | None = None
    # BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5 (harness rows): live pacer pressure —
    # max wait-queue depth over active aliases; share of the last 60 s of admissions that
    # waited > 2 s (the design's p95 back-pressure proxy). None = not measured, never 0.
    pacer_queue_len: int | None = None
    paced_over_2s_share: float | None = None
    # Forecast review 2026-09-03 (owner: "enough to reason about what happened"): the pool's
    # FULL decision record as published — per-alias ceilings/bindings/curve sources, booting
    # tasks, timeouts, queue depth — persisted per tick. None when no record exists.
    decision: dict[str, Any] | None = None


class CapacityObserver:
    """Owned by the run-supervisor; every read individually guarded — a failed source yields
    NULL for its fields and the rest of the row still lands. Never raises out of a tick."""

    def __init__(
        self,
        redis_client: Any = None,
        ecs_client: Any = None,
        sqs_depth_fn: Any = None,
        conn_factory: Any = None,
        tick_interval_s: float = 30.0,
    ) -> None:
        self.enabled = os.environ.get("CAPACITY_OBSERVER_ENABLED", "1") != "0"
        self.cluster = os.environ.get("CLUSTER", "")
        self.eval_service = os.environ.get("EVAL_SERVICE_NAME", "eval-worker")
        self._redis = redis_client
        self._ecs = ecs_client
        self._sqs_depth_fn = sqs_depth_fn
        self._conn_factory = conn_factory
        self._tick_interval_s = tick_interval_s
        # -inf, not 0.0: time.monotonic() has an arbitrary epoch (seconds since
        # boot on Linux) — on a freshly booted host monotonic() < interval, and
        # a 0.0 sentinel would silently swallow the first tick(s).
        self._last_tick_at = float("-inf")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Timeline plan §4.2: the per-run sampler rides this tick. Same Redis client, same
        # connection factory; the scan's per-run aggregates are handed over after each pass.
        self._per_run: dict[str, Any] = {}
        from swebench_eval.orchestrator.control_plane.run_timeline import RunTimelineSampler

        self.run_sampler = RunTimelineSampler(redis_client=redis_client, conn_factory=conn_factory)

    # -- sources (each guarded; a failed read is None, never a guess) --------------------------

    def _redis_client(self) -> Any:
        if self._redis is None:
            from swebench_eval.database.redis_client import _get_client

            self._redis = _get_client()
        return self._redis

    def _ecs_client(self) -> Any:
        if self._ecs is None:
            import boto3
            from botocore.config import Config

            self._ecs = boto3.client(
                "ecs",
                region_name=aws_names.region(),
                config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}),
            )
        return self._ecs

    def _queue_depth(self, queue: str) -> tuple[int | None, int | None]:
        """(visible, not_visible) via GetQueueAttributes only — deliberately NOT
        queue.client.get_queue_depth, which also polls CloudWatch for oldest-age (the eval
        autoscaler's reasoning, reused)."""
        try:
            if self._sqs_depth_fn is not None:
                visible, not_visible = self._sqs_depth_fn(queue)
                return int(visible), int(not_visible)
            from swebench_eval.queue.client import get_queue_url, get_sqs_client

            attrs = get_sqs_client().get_queue_attributes(
                QueueUrl=get_queue_url(queue),
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )["Attributes"]
            return (
                int(attrs.get("ApproximateNumberOfMessages", 0)),
                int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
            )
        except Exception:
            logger.debug("capacity-observer: %s depth read failed", queue, exc_info=True)
            return None, None

    def _live_harness_count(self) -> int | None:
        """Live (non-stale) instance_progress keys — the harness fleet's in-flight count.
        The same signal both autoscalers use; no per-family ECS list_tasks fan-out.

        2026-09-04 (timeline plan §4.2): the ONE scan also yields the per-run aggregates the
        run-timeline sampler writes, cached on ``self._per_run`` for this tick — the per-run
        split costs no extra Redis read."""
        try:
            from swebench_eval.orchestrator.control_plane.run_timeline import scan_progress

            count, self._per_run = scan_progress(
                self._redis_client(), now=time.time(), stale_s=_STALE_PROGRESS_S
            )
            return count
        except Exception:
            logger.debug("capacity-observer: progress-key scan failed", exc_info=True)
            self._per_run = {}
            return None

    def _eval_running_count(self) -> int | None:
        if not self.cluster:
            return None  # local dev / unset — not measured, never zero
        try:
            resp = self._ecs_client().describe_services(
                cluster=self.cluster, services=[self.eval_service]
            )
            svc = (resp.get("services") or [{}])[0]
            return int(svc.get("runningCount", 0))
        except Exception:
            logger.debug("capacity-observer: eval service read failed", exc_info=True)
            return None

    def _ceiling_utilization(self) -> float | None:
        """Burst-bucket depletion at the pacer: max over recently-active aliases of
        1 - level/c_burst, clamped to [0, 1]. Measured at the component that enforces the
        ceiling, so it exists with or without an autoscaler. None when no alias has fresh
        traffic — idle is "not measured", not "0% utilised"."""
        try:
            client = self._redis_client()
            now = time.time()
            best: float | None = None
            for key in client.scan_iter("pacer:bucket:*", count=100):
                key_s = key.decode() if isinstance(key, bytes) else str(key)
                # alias_from_bucket_key strips the cluster hash-tag braces the
                # pacer wraps the alias in (2026-09-02 CROSSSLOT fix).
                alias = alias_from_bucket_key(key_s)
                if alias is None:
                    continue
                bucket = client.hgetall(key)
                cfg = client.hgetall(pacer_cfg_key(alias))
                try:
                    b = {
                        (k.decode() if isinstance(k, bytes) else k): (
                            v.decode() if isinstance(v, bytes) else v
                        )
                        for k, v in bucket.items()
                    }
                    c = {
                        (k.decode() if isinstance(k, bytes) else k): (
                            v.decode() if isinstance(v, bytes) else v
                        )
                        for k, v in cfg.items()
                    }
                    upd = float(b["upd"])
                    level = float(b["level"])
                    c_burst = float(c["c_burst"])
                except (KeyError, ValueError, TypeError):
                    continue  # no cfg (defaults-only alias) or malformed — skip, not 0
                if now - upd > _STALE_BUCKET_S or c_burst <= 0:
                    continue
                util = min(1.0, max(0.0, 1.0 - level / c_burst))
                best = util if best is None else max(best, util)
            return round(best, 4) if best is not None else None
        except Exception:
            logger.debug("capacity-observer: pacer bucket scan failed", exc_info=True)
            return None

    def _pacer_pressure(self) -> tuple[int | None, float | None]:
        """(max wait-queue depth, max share of admissions in the last 60 s that waited > 2 s)
        over aliases with a fresh burst bucket — the L1 pacer's live pressure, measured at the
        component that enforces it (design doc §2.5). Both None when no alias has fresh
        traffic; each guarded separately so a client without ZCARD (or a missing counter)
        yields None for that field, never 0."""
        try:
            client = self._redis_client()
            now = time.time()
            queue_max: int | None = None
            share_max: float | None = None
            for key in client.scan_iter("pacer:bucket:*", count=100):
                key_s = key.decode() if isinstance(key, bytes) else str(key)
                alias = alias_from_bucket_key(key_s)
                if alias is None:
                    continue
                bucket = client.hgetall(key)
                try:
                    upd = float(
                        next(
                            (v.decode() if isinstance(v, bytes) else v)
                            for k, v in bucket.items()
                            if (k.decode() if isinstance(k, bytes) else k) == "upd"
                        )
                    )
                except (StopIteration, ValueError, TypeError):
                    continue
                if now - upd > _STALE_BUCKET_S:
                    continue
                zcard = getattr(client, "zcard", None)
                if callable(zcard):
                    try:
                        depth = int(zcard(pacer_waitq_key(alias)))
                        queue_max = depth if queue_max is None else max(queue_max, depth)
                    except Exception:
                        logger.debug("capacity-observer: waitq read failed", exc_info=True)
                n = over = 0
                seen = False
                b = int(now // 10)
                for i in range(6):
                    h = client.hgetall(paced_key(alias, b - i)) or {}
                    if h:
                        seen = True
                        hd = {
                            (k.decode() if isinstance(k, bytes) else k): (
                                v.decode() if isinstance(v, bytes) else v
                            )
                            for k, v in h.items()
                        }
                        n += int(float(hd.get("n", 0)))
                        over += int(float(hd.get("n_over_2s", 0)))
                if seen and n > 0:
                    share = over / n
                    share_max = share if share_max is None else max(share_max, share)
            return queue_max, (round(share_max, 4) if share_max is not None else None)
        except Exception:
            logger.debug("capacity-observer: pacer pressure scan failed", exc_info=True)
            return None, None

    def _decision(
        self, pool: str
    ) -> tuple[int | None, str | None, float | None, str | None, dict[str, Any] | None]:
        """(desired, binding_constraint, age_s, constants_source, record) from the pool's
        decision record; all None when no record exists (no autoscaler running, or its TTL
        expired). The AGE is the point: 'holding steady' and 'died 20 minutes ago' must never
        look the same. constants_source (harness records only — ``budgets.source``) is the
        §2.4 provenance: a full chart of decisions computed from GENERIC DEFAULTS instead of
        measured constants must say so on every tick, or the observe phase silently collects
        data that cannot support the live-flip comparison. The raw record is returned whole
        so it can be persisted per tick (forecast review 2026-09-03)."""
        try:
            raw = self._redis_client().get(decision_key(pool))
            if not raw:
                return None, None, None, None, None
            record = json.loads(raw)
            decided_at = float(record["decided_at"])
            age = max(0.0, time.time() - decided_at)
            desired = record.get("desired_ceiling")
            binding = record.get("binding_constraint")
            budgets = record.get("budgets")
            source = budgets.get("source") if isinstance(budgets, dict) else None
            return (
                int(desired) if desired is not None else None,
                str(binding) if binding is not None else None,
                round(age, 1),
                str(source) if source is not None else None,
                record if isinstance(record, dict) else None,
            )
        except Exception:
            logger.debug("capacity-observer: %s decision read failed", pool, exc_info=True)
            return None, None, None, None, None

    # -- the tick ------------------------------------------------------------------------------

    def observe(self) -> list[PoolObservation]:
        """One full read pass — pure observation, no write. Exposed separately so tests (and a
        future on-demand endpoint) can exercise the read side without Aurora."""
        h_visible, h_not_visible = self._queue_depth("harness-jobs")
        e_visible, e_not_visible = self._queue_depth("eval-jobs")
        harness_workers = self._live_harness_count()
        eval_workers = self._eval_running_count()
        util = self._ceiling_utilization()
        pacer_queue_len, paced_over_2s_share = self._pacer_pressure()
        h_desired, h_binding, h_age, h_constants, h_record = self._decision("harness")
        e_desired, e_binding, e_age, _e_constants, e_record = self._decision("eval")

        h_med, h_p90 = _eta_bounds("harness")
        h_backlog = (h_visible or 0) + (h_not_visible or 0)
        h_eta_low = _eta_s(h_backlog, harness_workers or 0, h_med)
        h_eta_high = _eta_s(h_backlog, harness_workers or 0, h_p90)

        # §2's rule: eval's backlog includes jobs that do not exist yet — every live harness
        # instance becomes an eval job.
        e_med, e_p90 = _eta_bounds("eval")
        e_backlog = (e_visible or 0) + (e_not_visible or 0) + (harness_workers or 0)
        e_eta_low = (
            _eta_s(e_backlog, eval_workers or 0, e_med) if eval_workers is not None else None
        )
        e_eta_high = (
            _eta_s(e_backlog, eval_workers or 0, e_p90) if eval_workers is not None else None
        )

        return [
            PoolObservation(
                pool="harness",
                queue_depth=h_visible,
                not_visible=h_not_visible,
                current_workers=harness_workers,
                desired=h_desired,
                binding_constraint=h_binding,
                ceiling_utilization=util,
                decision_age_s=h_age,
                eta_low_s=h_eta_low,
                eta_high_s=h_eta_high,
                constants_source=h_constants,
                pacer_queue_len=pacer_queue_len,
                paced_over_2s_share=paced_over_2s_share,
                decision=h_record,
            ),
            PoolObservation(
                pool="eval",
                queue_depth=e_visible,
                not_visible=e_not_visible,
                current_workers=eval_workers,
                desired=e_desired,
                binding_constraint=e_binding,
                ceiling_utilization=None,  # tokens are a harness-pool concept
                decision_age_s=e_age,
                eta_low_s=e_eta_low,
                eta_high_s=e_eta_high,
                decision=e_record,
            ),
        ]

    def _should_write(self, observations: list[PoolObservation]) -> bool:
        """Active run OR observed activity -> record; quiet idle -> a correct gap. The
        activity term matters for the drain tail: the run-activity marker expires on TTL
        while the eval queue can still be full."""
        try:
            from swebench_eval.control import state as control_state

            if control_state.runs_active_marked():
                return True
        except Exception:
            logger.debug("capacity-observer: activity-marker read failed", exc_info=True)
        return any(
            (o.queue_depth or 0) > 0 or (o.not_visible or 0) > 0 or (o.current_workers or 0) > 0
            for o in observations
        )

    def _write(self, observations: list[PoolObservation]) -> None:
        if self._conn_factory is not None:
            conn = self._conn_factory()
        else:
            from swebench_eval.database.connection import get_connection

            conn = get_connection()
        try:
            with conn.cursor() as cur:
                for o in observations:
                    cur.execute(
                        """INSERT INTO capacity_snapshot
                               (ts, pool, queue_depth, not_visible, current_workers, desired,
                                binding_constraint, ceiling_utilization, decision_age_s,
                                eta_low_s, eta_high_s, constants_source,
                                pacer_queue_len, paced_over_2s_share, decision)
                           VALUES (now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                   %s::jsonb)""",
                        (
                            o.pool,
                            o.queue_depth,
                            o.not_visible,
                            o.current_workers,
                            o.desired,
                            o.binding_constraint,
                            o.ceiling_utilization,
                            o.decision_age_s,
                            o.eta_low_s,
                            o.eta_high_s,
                            o.constants_source,
                            o.pacer_queue_len,
                            o.paced_over_2s_share,
                            json.dumps(o.decision) if o.decision is not None else None,
                        ),
                    )
            conn.commit()
        finally:
            conn.close()

    def maybe_tick(self) -> list[PoolObservation] | None:
        """Rate-limited full pass: observe, gate, write. NEVER raises — the same
        exception-isolation contract as every other control-plane tick."""
        if time.monotonic() - self._last_tick_at < self._tick_interval_s:
            return None
        self._last_tick_at = time.monotonic()
        try:
            observations = self.observe()
            active = self._should_write(observations)
            if active:
                self._write(observations)
        except Exception:
            logger.exception("capacity-observer tick failed (transient); continues next tick")
            return None
        # The per-run rows land on the same tick, after the pool rows, under the same
        # active-or-observed-activity gate (a quiet system opens no connection at all), and
        # isolated from the pool write: a failed capacity write must not lose the run rows.
        if active:
            try:
                self.run_sampler.tick(self._per_run)
            except Exception:
                logger.exception("run-timeline tick failed (transient); continues next tick")
        return observations

    # -- the thread ----------------------------------------------------------------------------

    def start_background(self) -> threading.Thread:
        """Dedicated daemon thread — the supervisor heartbeat must never wait on this tick's
        SQS/ECS calls (the eval autoscaler's F1 arithmetic, identically), and the reaper must
        not be able to take observability down with it (design §3.1)."""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                self.maybe_tick()
                self._stop.wait(min(5.0, self._tick_interval_s))

        self._thread = threading.Thread(target=_loop, name="capacity-observer", daemon=True)
        self._thread.start()
        return self._thread

    def stop_background(self, timeout_s: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
