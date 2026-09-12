"""Eval autoscaler — BUILDER4-EVAL-AUTOSCALER-2026-08-31.md as revised + owner decisions
(2026-09-01). Every AWS/Redis boundary faked; the law, damping, pause, busy-host protection,
observe-vs-live and actuation ORDER are the things under test."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from swebench_eval.orchestrator.control_plane.decision_record import (
    decision_key,
    validate_record,
)
from swebench_eval.orchestrator.control_plane.eval_autoscaler import (
    EvalAutoscaler,
    EvalAutoscalerConfigError,
)


class FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}

    def scan_iter(self, pattern: str, count: int = 100):
        return iter([k for k in self.strings if k.startswith(pattern.rstrip("*"))])

    def get(self, key: str):
        v = self.strings.get(key)
        return v.encode() if v is not None else None

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.strings[key] = value


class FakeEcs:
    def __init__(self, running=0, desired=0, hosts=None) -> None:
        self.running, self.desired = running, desired
        self.hosts = hosts or []  # [{"ec2_instance_id", "running_tasks"}]
        self.calls: list[tuple[Any, ...]] = []

    def describe_services(self, cluster, services):
        return {"services": [{"runningCount": self.running, "desiredCount": self.desired}]}

    def list_container_instances(self, cluster, maxResults=100, nextToken=None):
        return {"containerInstanceArns": [f"arn{i}" for i in range(len(self.hosts))]}

    def describe_container_instances(self, cluster, containerInstances):
        return {
            "containerInstances": [
                {
                    "ec2InstanceId": h["ec2_instance_id"],
                    "runningTasksCount": h["running_tasks"],
                    "status": "ACTIVE",
                }
                for h in self.hosts
            ]
        }

    def update_service(self, cluster, service, desiredCount):
        self.calls.append(("update_service", desiredCount))


class FakeAsg:
    def __init__(self, desired=0, max_size=4, waiting=()) -> None:
        self.desired, self.max_size = desired, max_size
        self.calls: list[tuple[Any, ...]] = []
        # finding 17: instances parked in the ECS draining hook (Terminating:Wait)
        self.waiting = list(waiting)

    def describe_auto_scaling_groups(self, AutoScalingGroupNames):
        return {
            "AutoScalingGroups": [
                {
                    "DesiredCapacity": self.desired,
                    "MaxSize": self.max_size,
                    "Instances": [
                        {"InstanceId": i, "LifecycleState": "Terminating:Wait"}
                        for i in self.waiting
                    ],
                }
            ]
        }

    def describe_lifecycle_hooks(self, AutoScalingGroupName):
        return {
            "LifecycleHooks": [
                {
                    "LifecycleHookName": "ecs-managed-draining-termination-hook",
                    "LifecycleTransition": "autoscaling:EC2_INSTANCE_TERMINATING",
                }
            ]
        }

    def complete_lifecycle_action(
        self, AutoScalingGroupName, LifecycleHookName, InstanceId, LifecycleActionResult
    ):
        self.calls.append(("complete_lifecycle", InstanceId, LifecycleActionResult))

    def set_desired_capacity(self, AutoScalingGroupName, DesiredCapacity, HonorCooldown):
        self.calls.append(("set_desired_capacity", DesiredCapacity))

    def terminate_instance_in_auto_scaling_group(self, InstanceId, ShouldDecrementDesiredCapacity):
        self.calls.append(("terminate", InstanceId, ShouldDecrementDesiredCapacity))


def _scaler(
    monkeypatch,
    *,
    mode="observe",
    depth=(0, 0),
    harness_live=0,
    ecs=None,
    asg=None,
    max_workers="8",
    beta="0.35",
    paused=False,
):
    from swebench_eval.control import state as control_state

    monkeypatch.setattr(control_state, "is_paused", lambda pool: paused)
    monkeypatch.setenv("EVAL_MAX_WORKERS", max_workers)
    monkeypatch.setenv("EVAL_FEEDFORWARD_BETA", beta)
    monkeypatch.setenv("EVAL_ASG_NAME", "eval-asg")
    monkeypatch.setenv("CLUSTER", "c")
    r = FakeRedis()
    for i in range(harness_live):
        r.strings[f"instance_progress:r:{i}:1"] = json.dumps(
            {"turn_number": 5, "updated_at": time.time()}
        )
    s = EvalAutoscaler(
        mode=mode,
        redis_client=r,
        ecs_client=ecs or FakeEcs(),
        asg_client=asg or FakeAsg(),
        sqs_depth_fn=lambda: depth,
        tick_interval_s=0.0,
    )
    return s, r


class TestLaw:
    def test_depth_plus_not_visible_plus_feedforward(self, monkeypatch) -> None:
        """R1: the law is (visible + not_visible + beta*live) — not_visible is NOT optional
        (300s of in-flight grades are invisible to the visible count), and the feed-forward is
        IN the formula, not just argued for."""
        s, _ = _scaler(
            monkeypatch,
            depth=(3, 2),
            harness_live=10,
            beta="0.35",
            max_workers="16",
            asg=FakeAsg(desired=0, max_size=16),
        )
        d = s.maybe_tick()
        assert d is not None
        assert d.feedforward == pytest.approx(3.5)
        assert d.desired_ceiling == 9  # ceil(3 + 2 + 0.35*10) — all three terms present
        assert d.binding_constraint == "none"

    def test_clamped_at_max_workers_and_says_so(self, monkeypatch) -> None:
        s, _ = _scaler(
            monkeypatch, depth=(50, 10), max_workers="8", asg=FakeAsg(desired=0, max_size=16)
        )
        d = s.maybe_tick()
        assert d.desired_ceiling == 8
        assert d.binding_constraint == "at_max_workers"

    def test_queue_empty_reads_as_queue_empty_not_healthy_silence(self, monkeypatch) -> None:
        s, _ = _scaler(monkeypatch, depth=(0, 0), harness_live=0)
        d = s.maybe_tick()
        assert d.desired_ceiling == 0
        assert d.binding_constraint == "queue_empty"

    def test_asg_max_caps_hosts_and_tasks(self, monkeypatch) -> None:
        """Today's real state: asg_max_size=1 — any depth>tasks_per_host binds on asg_at_max,
        which is the honest constraint until the owner raises the rail."""
        s, _ = _scaler(monkeypatch, depth=(6, 0), asg=FakeAsg(desired=0, max_size=1))
        d = s.maybe_tick()
        assert d.desired_hosts == 1
        assert d.desired_ceiling == 1  # tasks capped to hosts * tasks_per_host(1)
        assert d.binding_constraint == "asg_at_max"

    def test_paused_holds_never_scales_out_into_a_paused_pool(self, monkeypatch) -> None:
        """The workers gate on the eval pause — scaling out would buy idle hosts."""
        s, _ = _scaler(monkeypatch, depth=(20, 0), paused=True, ecs=FakeEcs(desired=2))
        d = s.maybe_tick()
        assert d.binding_constraint == "paused"
        assert d.desired_ceiling <= 2  # held at current, not scaled toward 20


class TestDamping:
    def test_scale_in_waits_two_consecutive_ticks(self, monkeypatch) -> None:
        """ADR-0006 dampening: out immediately, in only after 2 consecutive lower ticks."""
        ecs = FakeEcs(running=4, desired=4)
        s, _ = _scaler(monkeypatch, depth=(0, 0), ecs=ecs, asg=FakeAsg(desired=4))
        d1 = s.maybe_tick()
        assert d1.binding_constraint == "scale_in_damped"
        assert d1.desired_ceiling == 4  # held
        s._last_tick_at = 0.0
        d2 = s.maybe_tick()
        assert d2.desired_ceiling == 0  # second consecutive low tick — allowed through
        assert d2.binding_constraint == "queue_empty"

    def test_a_demand_spike_resets_the_damping_counter(self, monkeypatch) -> None:
        ecs = FakeEcs(running=4, desired=4)
        s, _ = _scaler(monkeypatch, depth=(0, 0), ecs=ecs, asg=FakeAsg(desired=4))
        s.maybe_tick()  # tick 1: damped
        s._sqs_depth_fn = lambda: (6, 0)  # demand returns
        s._last_tick_at = 0.0
        s.maybe_tick()
        assert s._scale_in_ticks == 0  # reset — the next quiet spell starts damping over


class TestModes:
    def test_observe_publishes_but_never_actuates(self, monkeypatch) -> None:
        ecs, asg = FakeEcs(), FakeAsg()
        s, r = _scaler(monkeypatch, mode="observe", depth=(5, 1), ecs=ecs, asg=asg)
        d = s.maybe_tick()
        assert d.would_set  # intended actions are visible...
        assert ecs.calls == [] and asg.calls == []  # ...and nothing happened
        record = json.loads(r.strings[decision_key("eval")])
        validate_record(record)  # the SHARED envelope holds
        assert record["mode"] == "observe"

    def test_decision_published_every_tick_including_no_change(self, monkeypatch) -> None:
        s, r = _scaler(monkeypatch, depth=(0, 0))
        s.maybe_tick()
        assert decision_key("eval") in r.strings  # the no-change tick still emits

    def test_live_without_max_workers_refuses_to_construct(self, monkeypatch) -> None:
        monkeypatch.delenv("EVAL_MAX_WORKERS", raising=False)
        with pytest.raises(EvalAutoscalerConfigError):
            EvalAutoscaler(mode="live", redis_client=FakeRedis())

    def test_live_without_asg_name_refuses_to_construct(self, monkeypatch) -> None:
        """§2.3 (wiring review): a live scaler with no ASG name would run, publish, and
        actuate nothing at the host level — a scaler that LOOKS live and cannot scale.
        Same refusal rule as EVAL_MAX_WORKERS; observe mode may run without one."""
        monkeypatch.setenv("EVAL_MAX_WORKERS", "8")
        monkeypatch.delenv("EVAL_ASG_NAME", raising=False)
        with pytest.raises(EvalAutoscalerConfigError, match="EVAL_ASG_NAME"):
            EvalAutoscaler(mode="live", redis_client=FakeRedis())
        EvalAutoscaler(mode="observe", redis_client=FakeRedis())  # observe is fine

    def test_tick_failure_holds_and_never_raises(self, monkeypatch) -> None:
        s, _ = _scaler(monkeypatch, depth=(1, 0))
        first = s.maybe_tick()
        s._sqs_depth_fn = lambda: 1 / 0
        s._last_tick_at = 0.0
        assert s.maybe_tick() is first  # held, not raised — the heartbeat is never at risk


class TestLiveActuation:
    def test_scale_out_orders_hosts_before_tasks(self, monkeypatch) -> None:
        """Decision B ordering: tasks with no hosts sit in PROVISIONING forever — the ASG rises
        first, then the service."""
        ecs, asg = FakeEcs(running=0, desired=0), FakeAsg(desired=0, max_size=4)
        s, _ = _scaler(monkeypatch, mode="live", depth=(3, 0), ecs=ecs, asg=asg, max_workers="8")
        s.maybe_tick()
        assert asg.calls == [("set_desired_capacity", 3)]
        assert ecs.calls == [("update_service", 3)]

    def test_scale_in_terminates_only_idle_hosts(self, monkeypatch) -> None:
        """The whole point of owning scale-in: a busy host is NEVER an ASG victim. One idle,
        one busy, two to remove -> the idle one goes, the busy one survives, the tick reports
        scale_in_blocked_busy."""
        hosts = [
            {"ec2_instance_id": "i-idle", "running_tasks": 0},
            {"ec2_instance_id": "i-busy", "running_tasks": 1},
        ]
        ecs = FakeEcs(running=1, desired=2, hosts=hosts)
        asg = FakeAsg(desired=2, max_size=4)
        s, _ = _scaler(monkeypatch, mode="live", depth=(0, 0), ecs=ecs, asg=asg)
        s.maybe_tick()  # damped (tick 1)
        s._last_tick_at = 0.0
        d = s.maybe_tick()  # tick 2: scale-in proceeds
        terminations = [c for c in asg.calls if c[0] == "terminate"]
        assert terminations == [("terminate", "i-idle", True)]
        assert d.binding_constraint == "scale_in_blocked_busy"

    def test_scale_in_lowers_tasks_before_hosts(self, monkeypatch) -> None:
        ecs = FakeEcs(running=2, desired=2, hosts=[{"ec2_instance_id": "i-1", "running_tasks": 0}])
        asg = FakeAsg(desired=1, max_size=4)
        s, _ = _scaler(monkeypatch, mode="live", depth=(0, 0), ecs=ecs, asg=asg)
        s.maybe_tick()
        s._last_tick_at = 0.0
        s.maybe_tick()
        assert ("update_service", 0) in ecs.calls
        assert ("terminate", "i-1", True) in asg.calls


class TestBackgroundThread:
    def test_ticks_happen_on_the_thread_never_the_caller(self, monkeypatch) -> None:
        """F1 (exact-design review): the supervisor loop must never wait on this scaler's AWS
        calls — the thread does the ticking, start_background returns immediately."""
        s, r = _scaler(monkeypatch, depth=(2, 0))
        s._tick_interval_s = 0.01
        t = s.start_background()
        try:
            deadline = time.time() + 2.0
            while decision_key("eval") not in r.strings and time.time() < deadline:
                time.sleep(0.01)
            assert decision_key("eval") in r.strings  # a decision landed, driven by the thread
            assert t.daemon is True  # never blocks process exit
            assert t is s.start_background()  # idempotent — one thread, not one per call
        finally:
            s.stop_background()
        assert not t.is_alive()


class TestRailConsistency:
    def test_max_workers_above_the_rail_warns_once_naming_all_three_numbers(
        self, monkeypatch, caplog
    ) -> None:
        """Eval scaling review F4: EVAL_MAX_WORKERS > asg_max x EVAL_TASKS_PER_HOST renders
        exactly like real saturation (asg_at_max). Warn once, from the tick's own ASG read."""
        monkeypatch.setenv("EVAL_TASKS_PER_HOST", "4")
        s, _ = _scaler(monkeypatch, max_workers="48", asg=FakeAsg(desired=0, max_size=4))
        with caplog.at_level("WARNING"):
            s.maybe_tick()
            s._last_tick_at = 0.0
            s.maybe_tick()
        hits = [r for r in caplog.records if "exceeds asg_max" in r.message]
        assert len(hits) == 1
        assert "EVAL_MAX_WORKERS=48" in hits[0].message
        assert "asg_max 4 x EVAL_TASKS_PER_HOST 4 = 16" in hits[0].message

    def test_a_consistent_rail_does_not_warn(self, monkeypatch, caplog) -> None:
        monkeypatch.setenv("EVAL_TASKS_PER_HOST", "4")
        s, _ = _scaler(monkeypatch, max_workers="16", asg=FakeAsg(desired=0, max_size=4))
        with caplog.at_level("WARNING"):
            s.maybe_tick()
        assert not [r for r in caplog.records if "exceeds asg_max" in r.message]


class TestTaskScaleInDelay:
    """F7 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): task scale-in waits ~EVAL_TASK_SCALE_IN_S
    of consecutive lower ticks; hosts keep ADR-0006's two-tick rule."""

    def _live_interval_scaler(self, monkeypatch, *, seconds: str, tick_s: float, ecs, asg):
        from swebench_eval.control import state as control_state

        monkeypatch.setattr(control_state, "is_paused", lambda pool: False)
        monkeypatch.setenv("EVAL_MAX_WORKERS", "8")
        monkeypatch.setenv("EVAL_ASG_NAME", "eval-asg")
        monkeypatch.setenv("EVAL_TASKS_PER_HOST", "4")
        monkeypatch.setenv("EVAL_TASK_SCALE_IN_S", seconds)
        return EvalAutoscaler(
            mode="observe",
            redis_client=FakeRedis(),
            ecs_client=ecs,
            asg_client=asg,
            sqs_depth_fn=lambda: (0, 0),
            tick_interval_s=tick_s,
        )

    def _tick(self, s):
        s._last_tick_at = 0.0
        return s.maybe_tick()

    def test_tasks_hold_until_the_nth_tick_and_the_decision_says_how_many(self, monkeypatch):
        s = self._live_interval_scaler(
            monkeypatch,
            seconds="300",
            tick_s=30.0,
            ecs=FakeEcs(running=4, desired=4),
            asg=FakeAsg(desired=1),
        )
        assert s._task_scale_in_ticks == 10  # 300 s / 30 s
        for n in range(1, 10):
            d = self._tick(s)
            assert d.desired_ceiling == 4, n  # held
            assert d.binding_constraint == "scale_in_damped"
            assert d.scale_in_pending_ticks == n
            assert d.scale_in_ticks_needed == 10 and d.tick_interval_s == 30.0
        d = self._tick(s)
        assert d.desired_ceiling == 0  # the 10th consecutive lower tick acts
        assert d.would_set["service_desired_count"] == 0

    def test_hosts_the_held_tasks_still_need_are_kept(self, monkeypatch):
        """4 tasks held on 1 host: the host rule (2 ticks) must not pull the host from under
        them — desired_hosts follows the HELD task count."""
        s = self._live_interval_scaler(
            monkeypatch,
            seconds="300",
            tick_s=30.0,
            ecs=FakeEcs(running=4, desired=4),
            asg=FakeAsg(desired=1),
        )
        for _ in range(3):
            d = self._tick(s)
        assert d.desired_ceiling == 4 and d.desired_hosts == 1
        assert "asg_desired_capacity" not in d.would_set

    def test_an_idle_host_beyond_the_held_tasks_goes_on_the_host_rule(self, monkeypatch):
        """4 tasks held (1 host's worth) but 2 hosts up: the spare host is scaled in after the
        two-tick host rule while the tasks are still held."""
        s = self._live_interval_scaler(
            monkeypatch,
            seconds="300",
            tick_s=30.0,
            ecs=FakeEcs(running=4, desired=4),
            asg=FakeAsg(desired=2),
        )
        d1 = self._tick(s)
        assert d1.desired_hosts == 2  # first lower tick: hosts damped too
        d2 = self._tick(s)
        assert d2.desired_ceiling == 4 and d2.desired_hosts == 1
        assert d2.would_set == {"asg_desired_capacity": 1}

    def test_a_zero_tick_interval_keeps_the_two_tick_floor(self, monkeypatch):
        s = self._live_interval_scaler(
            monkeypatch, seconds="300", tick_s=0.0, ecs=FakeEcs(), asg=FakeAsg()
        )
        assert s._task_scale_in_ticks == 2

    def test_stale_progress_keys_do_not_feed_forward(self, monkeypatch):
        """Item 19: a progress key older than _STALE_PROGRESS_S (120 s) is a dead task, not
        coming eval demand — it must not pre-warm a host."""
        s, r = _scaler(monkeypatch, depth=(0, 0), harness_live=0)
        r.strings["instance_progress:r:old:1"] = json.dumps(
            {"turn_number": 5, "updated_at": time.time() - 200}
        )
        r.strings["instance_progress:r:new:1"] = json.dumps(
            {"turn_number": 5, "updated_at": time.time() - 5}
        )
        assert s._live_harness_count() == 1
        d = s.maybe_tick()
        assert d.feedforward == pytest.approx(0.35)


def test_scale_up_releases_drained_hosts_stuck_in_the_termination_hook(monkeypatch) -> None:
    """Finding 17 (fresh account, 2026-09-11): hosts the scaler terminated earlier sat in the
    ECS draining hook with zero tasks, still counting against the Spot quota, and every new
    launch failed as unfulfillable. On scale-up the scaler completes the hook for drained
    hosts (and never for a draining host that still runs a grade)."""
    asg = FakeAsg(desired=0, max_size=4, waiting=["i-drained", "i-busy"])

    class DrainAwareEcs(FakeEcs):
        def list_container_instances(self, cluster, maxResults=100, nextToken=None, status=None):
            if status == "DRAINING":
                return {"containerInstanceArns": ["arn:busy"]}
            return super().list_container_instances(cluster, maxResults, nextToken)

        def describe_container_instances(self, cluster, containerInstances):
            if containerInstances == ["arn:busy"]:
                return {
                    "containerInstances": [
                        {"ec2InstanceId": "i-busy", "runningTasksCount": 1, "status": "DRAINING"}
                    ]
                }
            return super().describe_container_instances(cluster, containerInstances)

    scaler, _ = _scaler(monkeypatch, ecs=DrainAwareEcs(), asg=asg)
    released = scaler._release_drained_terminations()
    assert released == 1
    assert ("complete_lifecycle", "i-drained", "CONTINUE") in asg.calls
    assert not any(c[0] == "complete_lifecycle" and c[1] == "i-busy" for c in asg.calls)
