"""Eval autoscaler — BUILDER4-EVAL-AUTOSCALER-2026-08-31.md, as revised and owner-approved
2026-09-01 (the DECISIONS block in that doc).

Eval is the anti-harness (verified, not assumed): EC2 not Fargate (privileged DinD — can never
move), a plain ECS service actuated by ``UpdateService(desiredCount)`` (the terraform already
carries ``ignore_changes = [desired_count]`` for exactly this), zero tokens, zero provider
interaction — so there is NO ceiling discovery, NO forecast machinery, NO ramp here. The control
law is queue depth plus a known-future feed-forward, and the actual work is the two-level
host/task problem.

**Decision B (owner-approved, reversing the reviewer's lean):** this module owns BOTH levels —
``SetDesiredCapacity`` for hosts, ``UpdateService`` for tasks; ECS managed scaling stays
DISABLED (the one live measurement of it: a held host at ~$81/month silently overriding
``asg_desired=0``). Scale-in safety is hand-rolled and deterministic: only hosts with
``runningTasksCount == 0`` are ever terminated (targeted
``TerminateInstanceInAutoScalingGroup(ShouldDecrementDesiredCapacity=True)``); when every host
is busy the tick holds (``scale_in_blocked_busy``) — a killed grade would only cost one
redelivered re-grade (delete-on-success + 300s visibility, verified), but not killing it at all
is ~10 lines.

The control law (revision R1 — the feed-forward is IN the formula, not just argued for):

    expected_soon  = beta * count(live instance_progress keys)     # completions within the
    desired_tasks  = clamp(ceil(visible + not_visible + expected_soon), min, max_workers)
    desired_hosts  = ceil(desired_tasks / tasks_per_host)          # host-boot dead time

- ``not_visible`` is NOT optional: a grade in progress is invisible to the visible count for up
  to 300s — depth alone scales in under running work.
- beta defaults to 0.35: the fraction of running harness instances expected to COMPLETE within
  the ~5-minute host-boot horizon (from the measured ~22%-per-3-min completion rate). NOT the
  full running count — that would over-provision hosts 3-5x for instances that finish much
  later than a host takes to boot.
- ``tasks_per_host`` is static config (``EVAL_TASKS_PER_HOST``). 1 on the original c5d.large
  (~3.4 GB usable vs the task's 2048 MB); **4 since BUILDER4-EVAL-PACKING-2026-09-03** on the
  c5d.2xlarge — measured 0.2–1.2 cores and ≤ 1.8 GB per grade, the task reservation
  (2048/3584) makes ECS place exactly four, and the grading container carries a 3 GiB cgroup
  cap so one runaway grade cannot take the host. It must always equal what the eval task's
  reservation lets ECS pack: the scaler turns tasks into hosts with it, ECS turns hosts into
  tasks with the reservation, and the two disagreeing is the packing bug this fixed.
- Scale-out is immediate; scale-in waits for 2 consecutive lower ticks (ADR-0006's dampening).

Placement (decision 3): a rate-limited sub-tick at the END of the run-supervisor loop, bounded
botocore timeouts (5s connect / 10s read / 1 attempt) instead of threads — worst-case tick stays
far inside the 90s heartbeat budget, matching the supervisor's existing inline-with-try/except
discipline. ``maybe_tick`` never raises.

Modes (decision 5): ``EVAL_AUTOSCALER_MODE`` off | observe (default) | live. Observe publishes
the decision record every tick (shared contract, ``decision_record.py``) and actuates NOTHING —
intended actions land in ``would_set``. ``EVAL_MAX_WORKERS`` is REQUIRED when live (refuse to
start unset — ADR-0006: never inferred; H4 §4.1: a default is how an unbounded scaler ships
looking configured); observe mode may default, because observing cannot be unbounded.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from swebench_eval import aws_names
from swebench_eval.orchestrator.control_plane.decision_record import publish as _publish_record

logger = logging.getLogger(__name__)

_STALE_PROGRESS_S = 120.0
_OBSERVE_DEFAULT_MAX_WORKERS = 4
_SCALE_IN_CONSECUTIVE_TICKS = 2  # hosts (ADR-0006's dampening), and the floor for tasks
# F7 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): task-level scale-in after 2 lower ticks
# (60 s) killed and re-created idle eval workers three times in 30 minutes while the host
# stayed. Tasks now need ~EVAL_TASK_SCALE_IN_S of consecutive lower ticks (default 300 s);
# the host rule is unchanged (an idle host beyond what the held tasks need still goes).
_TASK_SCALE_IN_DEFAULT_S = 300.0


class EvalAutoscalerConfigError(RuntimeError):
    """Live mode with no explicit max_workers — refuse, never infer (ADR-0006)."""


def _bounded_boto(service: str) -> Any:
    """Bounded-timeout client (decision 3): the supervisor's heartbeat must never starve behind
    a hung AWS call. 5s connect / 10s read / 1 attempt — a failed tick just retries next tick."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        service,
        region_name=aws_names.region(),
        config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}),
    )


@dataclass(frozen=True)
class EvalDecision:
    """One tick's answer — published verbatim through the shared envelope."""

    decided_at: float
    mode: str
    desired_ceiling: int  # desired TASKS (the shared envelope's common field)
    binding_constraint: str
    desired_hosts: int
    visible: int
    not_visible: int
    feedforward: float
    running_tasks: int
    running_hosts: int
    idle_hosts: int
    asg_desired: int
    asg_max: int
    scale_in_pending_ticks: int
    would_set: dict[str, int] = field(default_factory=dict)  # observe-mode intended actions
    # F7: how many consecutive lower ticks TASK scale-in needs (hosts: the ADR-0006 two), and
    # the tick cadence — together the UI's scale-in countdown.
    scale_in_ticks_needed: int = _SCALE_IN_CONSECUTIVE_TICKS
    tick_interval_s: float = 0.0
    # Operator limits 2026-09-04: the max_workers in force this tick (env, or the operator's
    # eval_max_workers) and the raw operator overrides the tick read.
    max_workers: int = 0
    operator_limits: dict[str, float] = field(default_factory=dict)


class EvalAutoscaler:
    """Owned and ticked by the run-supervisor. All failures degrade to 'hold', never raise."""

    def __init__(
        self,
        mode: str | None = None,
        redis_client: Any = None,
        ecs_client: Any = None,
        asg_client: Any = None,
        sqs_depth_fn: Any = None,
        tick_interval_s: float = 30.0,
    ) -> None:
        self.mode = mode or os.environ.get("EVAL_AUTOSCALER_MODE", "observe")
        raw_max = os.environ.get("EVAL_MAX_WORKERS", "")
        if self.mode == "live" and not raw_max:
            raise EvalAutoscalerConfigError(
                "EVAL_AUTOSCALER_MODE=live requires an explicit EVAL_MAX_WORKERS "
                "(ADR-0006: max_workers is never inferred; H4 §4.1: no silent default "
                "on anything that bounds real capacity)"
            )
        self.max_workers = int(raw_max) if raw_max else _OBSERVE_DEFAULT_MAX_WORKERS
        self.min_workers = int(os.environ.get("EVAL_MIN_WORKERS", "0"))
        self.beta = float(os.environ.get("EVAL_FEEDFORWARD_BETA", "0.35"))
        self.tasks_per_host = max(1, int(os.environ.get("EVAL_TASKS_PER_HOST", "1")))
        self.cluster = os.environ.get("CLUSTER", "")
        self.service = os.environ.get("EVAL_SERVICE_NAME", "eval-worker")
        self.asg_name = os.environ.get("EVAL_ASG_NAME", "")
        if self.mode == "live" and not self.asg_name:
            # §2.3 (wiring review 2026-09-01), same rule as EVAL_MAX_WORKERS: a live scaler
            # with no ASG name would run, publish records, and actuate nothing at the host
            # level — a scaler that LOOKS live and cannot scale. Refuse loudly instead.
            raise EvalAutoscalerConfigError(
                "EVAL_AUTOSCALER_MODE=live requires EVAL_ASG_NAME — a live scaler with no "
                "ASG name cannot scale hosts (it would silently hold at whatever host count "
                "exists); observe mode may run without one"
            )
        self._redis = redis_client
        self._ecs = ecs_client
        self._asg = asg_client
        self._sqs_depth_fn = sqs_depth_fn
        self._tick_interval_s = tick_interval_s
        self._last_tick_at = 0.0
        self._last_decision: EvalDecision | None = None
        # ADR-0006 dampening: scale-in requires this many CONSECUTIVE lower ticks.
        self._scale_in_ticks = 0
        # F7: the task-level requirement in ticks — EVAL_TASK_SCALE_IN_S over the tick
        # interval, never below the host rule's two. A zero tick interval (tests) keeps two.
        self.task_scale_in_s = float(
            os.environ.get("EVAL_TASK_SCALE_IN_S", str(_TASK_SCALE_IN_DEFAULT_S))
        )
        self._task_scale_in_ticks = _SCALE_IN_CONSECUTIVE_TICKS
        if tick_interval_s > 0:
            self._task_scale_in_ticks = max(
                _SCALE_IN_CONSECUTIVE_TICKS, math.ceil(self.task_scale_in_s / tick_interval_s)
            )
        # F1 (exact-design review): the background thread + its stop signal.
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._config_warned = False  # F4: the max_workers-vs-rail WARNING fires once

    # -- data plumbing (each read guarded; a failed read = hold, never a guess) ----------------

    def _redis_client(self) -> Any:
        if self._redis is None:
            from swebench_eval.database.redis_client import _get_client

            self._redis = _get_client()
        return self._redis

    def _ecs_client(self) -> Any:
        if self._ecs is None:
            self._ecs = _bounded_boto("ecs")
        return self._ecs

    def _asg_client(self) -> Any:
        if self._asg is None:
            self._asg = _bounded_boto("autoscaling")
        return self._asg

    def _queue_depth(self) -> tuple[int, int]:
        """(visible, not_visible) for eval-jobs — GetQueueAttributes only, deliberately NOT
        queue.client.get_queue_depth (that also polls CloudWatch for oldest-age; the supervisor
        tick doesn't need it and shouldn't pay for it)."""
        if self._sqs_depth_fn is not None:
            visible, not_visible = self._sqs_depth_fn()
            return int(visible), int(not_visible)
        from swebench_eval.queue.client import get_queue_url, get_sqs_client

        attrs = get_sqs_client().get_queue_attributes(
            QueueUrl=get_queue_url("eval-jobs"),
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        return (
            int(attrs.get("ApproximateNumberOfMessages", 0)),
            int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
        )

    def _live_harness_count(self) -> int:
        """R1's feed-forward source: live (non-stale) instance_progress keys — the same signal
        the harness planner scans, read cheaply, no Aurora query in the supervisor tick."""
        client = self._redis_client()
        now = time.time()
        count = 0
        for key in client.scan_iter("instance_progress:*", count=200):
            raw = client.get(key)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                if now - float(payload.get("updated_at", 0)) <= _STALE_PROGRESS_S:
                    count += 1
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        return count

    def _service_counts(self) -> tuple[int, int]:
        """(runningCount, desiredCount) of the eval service."""
        resp = self._ecs_client().describe_services(cluster=self.cluster, services=[self.service])
        svc = (resp.get("services") or [{}])[0]
        return int(svc.get("runningCount", 0)), int(svc.get("desiredCount", 0))

    def _hosts(self) -> list[dict[str, Any]]:
        """Container instances with their running-task counts and EC2 ids (scale-in victims are
        chosen from these — idle only)."""
        ecs = self._ecs_client()
        arns: list[str] = []
        token: str | None = None
        while True:
            kw: dict[str, Any] = {"cluster": self.cluster, "maxResults": 100}
            if token:
                kw["nextToken"] = token
            resp = ecs.list_container_instances(**kw)
            arns += resp.get("containerInstanceArns", [])
            token = resp.get("nextToken")
            if not token:
                break
        if not arns:
            return []
        detail = ecs.describe_container_instances(cluster=self.cluster, containerInstances=arns)
        return [
            {
                "ec2_instance_id": ci.get("ec2InstanceId"),
                "running_tasks": int(ci.get("runningTasksCount", 0)),
            }
            for ci in detail.get("containerInstances", [])
            if ci.get("status") == "ACTIVE"
        ]

    def _release_drained_terminations(self) -> int:
        """Complete the termination lifecycle hook for ASG instances stuck in Terminating:Wait
        whose container instance runs no tasks. Returns how many were released. Best-effort:
        a failure here must never stop the scale-up that follows."""
        try:
            asg = self._asg_client()
            group = (
                asg.describe_auto_scaling_groups(AutoScalingGroupNames=[self.asg_name]).get(
                    "AutoScalingGroups"
                )
                or [{}]
            )[0]
            waiting = [
                str(i["InstanceId"])
                for i in group.get("Instances", [])
                if str(i.get("LifecycleState", "")).startswith("Terminating:Wait")
            ]
            if not waiting:
                return 0
            busy = self._draining_hosts_with_tasks()
            hooks = [
                str(h["LifecycleHookName"])
                for h in asg.describe_lifecycle_hooks(AutoScalingGroupName=self.asg_name).get(
                    "LifecycleHooks", []
                )
                if str(h.get("LifecycleTransition", "")).endswith("TERMINATING")
            ]
            released = 0
            for instance_id in waiting:
                if instance_id in busy:
                    continue
                for hook in hooks:
                    asg.complete_lifecycle_action(
                        AutoScalingGroupName=self.asg_name,
                        LifecycleHookName=hook,
                        InstanceId=instance_id,
                        LifecycleActionResult="CONTINUE",
                    )
                released += 1
                logger.info(
                    "eval autoscaler: released drained host %s from its termination hook",
                    instance_id,
                )
            return released
        except Exception:
            logger.warning("eval autoscaler: could not release drained hosts", exc_info=True)
            return 0

    def _draining_hosts_with_tasks(self) -> set[str]:
        """EC2 ids of DRAINING container instances that still run tasks (never release those)."""
        ecs = self._ecs_client()
        arns = ecs.list_container_instances(cluster=self.cluster, status="DRAINING").get(
            "containerInstanceArns", []
        )
        if not arns:
            return set()
        detail = ecs.describe_container_instances(cluster=self.cluster, containerInstances=arns)
        return {
            str(ci.get("ec2InstanceId"))
            for ci in detail.get("containerInstances", [])
            if int(ci.get("runningTasksCount", 0)) > 0
        }

    def _asg_state(self) -> tuple[int, int]:
        """(desired, max) of the eval host ASG."""
        resp = self._asg_client().describe_auto_scaling_groups(
            AutoScalingGroupNames=[self.asg_name]
        )
        group = (resp.get("AutoScalingGroups") or [{}])[0]
        return int(group.get("DesiredCapacity", 0)), int(group.get("MaxSize", 0))

    # -- the background thread (reviewer F1) ---------------------------------------------------

    def start_background(self) -> threading.Thread:
        """Run the tick loop on a dedicated daemon thread — the supervisor's heartbeat loop must
        NEVER wait on this scaler's AWS calls (F1: ~8 bounded calls can sum past the 90s
        heartbeat budget in the tail; the heartbeat's stall halts the whole run). Bounded client
        timeouts stay — a thread that hangs forever is still a leak, just a quieter one."""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                self.maybe_tick()  # rate-limits itself to tick_interval_s; never raises
                self._stop.wait(min(5.0, self._tick_interval_s))

        self._thread = threading.Thread(target=_loop, name="eval-autoscaler", daemon=True)
        self._thread.start()
        return self._thread

    def stop_background(self, timeout_s: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)

    # -- the tick ------------------------------------------------------------------------------

    def maybe_tick(self) -> EvalDecision | None:
        """Called by the supervisor loop; runs at most every tick_interval_s. NEVER raises —
        same exception-isolation contract as every other control-loop tick in this repo."""
        if time.monotonic() - self._last_tick_at < self._tick_interval_s:
            return self._last_decision
        self._last_tick_at = time.monotonic()
        try:
            self._last_decision = self._tick()
        except Exception:
            logger.exception("eval-autoscaler tick failed; holding")
        return self._last_decision

    def _operator_limits(self) -> tuple[int, int, dict[str, float]]:
        """Operator limits 2026-09-04: ``eval_max_workers`` / ``eval_task_scale_in_s`` from
        the operator:limits hash, re-read every tick; the env values stay the defaults. A
        read failure keeps the env values (the safe direction, same as every other guarded
        read here). Returns (max_workers, task_scale_in_ticks, the raw overrides)."""
        from swebench_eval.orchestrator.control_plane import operator_limits

        max_workers = self.max_workers
        task_ticks = self._task_scale_in_ticks
        try:
            lim = operator_limits.read_global(self._redis_client())
        except Exception:  # noqa: BLE001 — a failed read keeps the env values
            return max_workers, task_ticks, {}
        if "eval_max_workers" in lim:
            max_workers = max(self.min_workers, int(lim["eval_max_workers"]))
        if "eval_task_scale_in_s" in lim and self._tick_interval_s > 0:
            task_ticks = max(
                _SCALE_IN_CONSECUTIVE_TICKS,
                math.ceil(float(lim["eval_task_scale_in_s"]) / self._tick_interval_s),
            )
        return max_workers, task_ticks, lim

    def _tick(self) -> EvalDecision:
        from swebench_eval.control import state as control_state

        max_workers, task_scale_in_ticks, operator = self._operator_limits()
        visible, not_visible = self._queue_depth()
        feedforward = self.beta * self._live_harness_count()
        running_tasks, current_desired_tasks = self._service_counts()
        hosts = self._hosts()
        idle_hosts = sum(1 for h in hosts if h["running_tasks"] == 0)
        asg_desired, asg_max = self._asg_state() if self.asg_name else (len(hosts), len(hosts))
        if (
            self.asg_name
            and not self._config_warned
            and max_workers > asg_max * self.tasks_per_host
        ):
            # Eval scaling review F4: a max_workers the rail cannot hold renders exactly like
            # real saturation (`asg_at_max`). Name all three numbers once, from the tick's own
            # DescribeAutoScalingGroups read (no extra call, nothing at construction).
            self._config_warned = True
            logger.warning(
                "eval-autoscaler: EVAL_MAX_WORKERS=%d exceeds asg_max %d x EVAL_TASKS_PER_HOST %d "
                "= %d — the fleet caps at %d tasks and will report asg_at_max; fix the rail or "
                "the packing constant",
                max_workers,
                asg_max,
                self.tasks_per_host,
                asg_max * self.tasks_per_host,
                asg_max * self.tasks_per_host,
            )

        demand = visible + not_visible + feedforward
        desired_tasks = max(self.min_workers, min(max_workers, math.ceil(demand)))
        desired_hosts = math.ceil(desired_tasks / self.tasks_per_host)

        binding = "none"
        paused = control_state.is_paused("eval")
        if paused:
            # Scaling OUT into a paused pool buys idle hosts (the workers gate on the pause and
            # receive nothing). Hold at current size; scale-in damping still applies.
            binding = "paused"
            desired_tasks = min(desired_tasks, current_desired_tasks)
            desired_hosts = min(desired_hosts, asg_desired)
        elif demand == 0:
            binding = "queue_empty"
        elif math.ceil(demand) > max_workers:
            binding = "at_max_workers"
        if desired_hosts > asg_max:
            desired_hosts = asg_max
            desired_tasks = min(desired_tasks, asg_max * self.tasks_per_host)
            binding = "asg_at_max"
        if desired_tasks > running_tasks and len(hosts) == 0 and asg_desired > 0:
            binding = "no_hosts"  # capacity requested, hosts still booting — the dead time

        # ADR-0006 dampening: out immediately, in only after N consecutive lower ticks —
        # F7: N is the task rule (~EVAL_TASK_SCALE_IN_S) for tasks and the host rule (two)
        # for hosts. Hosts held by the still-desired tasks stay; an idle host beyond that
        # need goes on the host rule.
        tasks_down = desired_tasks < current_desired_tasks and not paused
        hosts_down = desired_hosts < asg_desired and not paused
        if tasks_down or hosts_down:
            self._scale_in_ticks += 1
            if tasks_down and self._scale_in_ticks < task_scale_in_ticks:
                binding = "scale_in_damped"
                desired_tasks = current_desired_tasks
                desired_hosts = max(desired_hosts, math.ceil(desired_tasks / self.tasks_per_host))
            if hosts_down and self._scale_in_ticks < _SCALE_IN_CONSECUTIVE_TICKS:
                binding = "scale_in_damped"
                desired_hosts = asg_desired
        else:
            self._scale_in_ticks = 0

        would_set: dict[str, int] = {}
        if desired_tasks != current_desired_tasks:
            would_set["service_desired_count"] = desired_tasks
        if desired_hosts != asg_desired:
            would_set["asg_desired_capacity"] = desired_hosts

        if self.mode == "live" and would_set:
            binding = self._actuate(
                desired_tasks, desired_hosts, current_desired_tasks, asg_desired, hosts, binding
            )

        decision = EvalDecision(
            decided_at=time.time(),
            mode=self.mode,
            desired_ceiling=desired_tasks,
            binding_constraint=binding,
            desired_hosts=desired_hosts,
            visible=visible,
            not_visible=not_visible,
            feedforward=round(feedforward, 2),
            running_tasks=running_tasks,
            running_hosts=len(hosts),
            idle_hosts=idle_hosts,
            asg_desired=asg_desired,
            asg_max=asg_max,
            scale_in_pending_ticks=self._scale_in_ticks,
            would_set=would_set,
            scale_in_ticks_needed=task_scale_in_ticks,
            tick_interval_s=self._tick_interval_s,
            max_workers=max_workers,
            operator_limits=operator,
        )
        _publish_record(self._redis_client(), "eval", dict(decision.__dict__))
        return decision

    def _actuate(
        self,
        desired_tasks: int,
        desired_hosts: int,
        current_tasks: int,
        current_hosts: int,
        hosts: list[dict[str, Any]],
        binding: str,
    ) -> str:
        """Decision B's ordering. OUT: hosts before tasks (tasks with no hosts sit in
        PROVISIONING forever). IN: tasks down first, then ONLY idle hosts terminated —
        targeted, decrementing — never an arbitrary ASG victim that might hold a grade."""
        if desired_hosts > current_hosts:
            # Fresh-account finding 17 (2026-09-11): the hosts THIS scaler terminated on the
            # previous scale-in sit in the ECS managed-draining hook (Terminating:Wait, up to
            # an hour) with zero tasks, still counting against the Spot vCPU quota; in a small
            # account that starved every new launch (UnfulfillableCapacity) while six patches
            # waited in the eval queue. Release the drained ones before asking for more.
            self._release_drained_terminations()
            self._asg_client().set_desired_capacity(
                AutoScalingGroupName=self.asg_name,
                DesiredCapacity=desired_hosts,
                HonorCooldown=False,
            )
        if desired_tasks != current_tasks:
            self._ecs_client().update_service(
                cluster=self.cluster, service=self.service, desiredCount=desired_tasks
            )
        if desired_hosts < current_hosts:
            to_remove = current_hosts - desired_hosts
            idle = [h for h in hosts if h["running_tasks"] == 0 and h["ec2_instance_id"]]
            for host in idle[:to_remove]:
                self._asg_client().terminate_instance_in_auto_scaling_group(
                    InstanceId=host["ec2_instance_id"],
                    ShouldDecrementDesiredCapacity=True,
                )
            if len(idle) < to_remove:
                return "scale_in_blocked_busy"  # every remaining candidate holds a grade — hold
        return binding
