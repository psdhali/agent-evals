"""H4 + M1 gate (ADR-0034 §4 / H4 §8) — admission control and pause-gate tests.

The load-bearing guard: the gate lives BEFORE ``receive_message``, never after.
A paused or at-capacity consumer that receives a message and returns it spends a
``maxReceiveCount``; a poll loop does this in a tight cycle so the whole backlog
DLQs.  H4 §8's named tests, plus the generalised reviewer lesson: a "does not
launch when at capacity" test must ALSO assert the capacity check ran — a check
that never executes passes.
"""

from __future__ import annotations

from unittest import mock

import pytest

from swebench_eval.control import state as control_state
from swebench_eval.orchestrator.control_plane import harness_dispatcher as disp
from swebench_eval.queue.schemas import HarnessJob

_DispatcherAdmission = disp._DispatcherAdmission
_max_concurrent_from_env = disp._max_concurrent_from_env


def _job(instance_id: str = "astropy__astropy-12907") -> HarnessJob:
    return HarnessJob(
        run_id="run-1",
        instance_id=instance_id,
        repo_url="https://github.com/astropy/astropy",
        base_commit="c",
        problem_statement="Fix the bug.",
        attempt_number=1,
        harness_name="custom_minimal",
        model_alias="cheap-oss-model",
        env_image_key="sweb.env.py.x86_64.428468730904ff6b4232aa:latest",
    )


def _isolated_ec2(monkeypatch: pytest.MonkeyPatch, *, leaky: bool = False) -> mock.Mock:
    """Fake EC2 describing an isolated harness network (A1-7 gate)."""
    ec2 = mock.Mock()
    ec2.describe_subnets.return_value = {"Subnets": [{"SubnetId": "s1", "VpcId": "v"}]}
    routes = [{"DestinationCidrBlock": "10.0.0.0/16"}]
    if leaky:
        routes.append({"DestinationCidrBlock": "0.0.0.0/0"})
    ec2.describe_route_tables.return_value = {"RouteTables": [{"Routes": routes}]}
    egress_ranges = [{"CidrIp": "10.0.0.0/16"}] if not leaky else [{"CidrIp": "0.0.0.0/0"}]
    ec2.describe_security_groups.return_value = {
        "SecurityGroups": [{"GroupId": "sg1", "IpPermissionsEgress": [{"IpRanges": egress_ranges}]}]
    }

    def _client(service: str, **_: object):
        if service == "ec2":
            return ec2
        raise AssertionError(f"unexpected boto3 service {service}")

    monkeypatch.setattr("boto3.client", _client)
    return ec2


def _harness_ecs(monkeypatch: pytest.MonkeyPatch, groups: list[str | None]) -> mock.Mock:
    """Fake ECS client: ListTasks returns one ARN per entry of *groups*, DescribeTasks
    reports that entry as the task's ``group`` (the shape the real API returns:
    "family:<family>" for RunTask launches, "service:<name>" for service tasks)."""
    ecs = mock.Mock()
    arns = [f"arn:task-{i}" for i in range(len(groups))]
    ecs.list_tasks.return_value = {"taskArns": arns, "nextToken": None}

    def _describe(*, cluster: str, tasks: list[str]) -> dict[str, object]:
        return {"tasks": [{"taskArn": arn, "group": groups[arns.index(arn)]} for arn in tasks]}

    ecs.describe_tasks.side_effect = _describe
    monkeypatch.setattr(disp, "_ecs_client", lambda: ecs)
    return ecs


def _noop_ecs(monkeypatch: pytest.MonkeyPatch, *, running: int = 0) -> mock.Mock:
    """Fake ECS client returning *running* RUNNING harness tasks (H4 ground truth)."""
    return _harness_ecs(monkeypatch, [f"family:eval-dev-harness-{i:022x}" for i in range(running)])


def _not_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the fail-closed pause read so the H4-terms unit tests can isolate
    the capacity gate (the real call reads Redis and fail-closes to paused)."""
    monkeypatch.setattr(control_state, "is_paused", lambda pool: False)


# ---------------------------------------------------------------------------


def test_at_capacity_does_not_receive(monkeypatch: pytest.MonkeyPatch) -> None:
    """H4 §8 row 1: at the ceiling, receive_message is NOT called.

    This is the gate-before-receive invariant — the DLQ is protected because a
    message is never spent on a refusal.
    """
    monkeypatch.setenv("MAX_CONCURRENT_HARNESS_TASKS", "2")
    _noop_ecs(monkeypatch, running=2)  # ground truth already at the ceiling
    _not_paused(monkeypatch)
    admission = _DispatcherAdmission(ceiling=2)

    # Ground-truth already reports 2 running; the gate must refuse.
    ecs = _noop_ecs(monkeypatch, running=2)
    decision = admission.may_launch()
    assert decision.allowed is False
    assert "at_capacity" in decision.reason
    # The enforce check ran: ListTasks was actually consulted.
    ecs.list_tasks.assert_called()


def test_capacity_check_runs_not_just_not_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reviewer's generalised guard: when a safety gate doesn't fire, the
    test must ALSO assert it ran — otherwise a check never executing passes."""
    monkeypatch.setenv("MAX_CONCURRENT_HARNESS_TASKS", "100")
    ecs = _noop_ecs(monkeypatch, running=1)
    _not_paused(monkeypatch)
    admission = _DispatcherAdmission(ceiling=100)
    assert admission.may_launch().allowed is True
    assert ecs.list_tasks.called, "the capacity check never ran (even when it permits)"


def test_unset_ceiling_refuses_start() -> None:
    """H4 §8: absent configuration must refuse to start, not pick a number."""
    with pytest.raises(RuntimeError, match="refuses to start"):
        _max_concurrent_from_env()


def test_ceiling_ignored_if_non_integer() -> None:
    with (
        pytest.raises(RuntimeError, match="not an integer"),
        mock.patch.dict("os.environ", {"MAX_CONCURRENT_HARNESS_TASKS": "lots"}),
    ):
        _max_concurrent_from_env()


def test_listtasks_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """H4 §4/§5: ListTasks raising → no launch, ERROR logged, reason set.

    Killing ListTasks (IAM deny) must stall dispatch with an ERROR rather than
    flooding.  Fail-closed: an unresolvable count blocks dispatch.
    """
    monkeypatch.setenv("MAX_CONCURRENT_HARNESS_TASKS", "100")
    ecs = mock.Mock()
    ecs.list_tasks.side_effect = Exception("AccessDenied")
    monkeypatch.setattr(disp, "_ecs_client", lambda: ecs)
    _not_paused(monkeypatch)
    admission = _DispatcherAdmission(ceiling=100)
    admission._refresh()
    decision = admission.may_launch()
    assert decision.allowed is False
    assert "ListTasks" in decision.reason


def test_local_counter_at_ceiling_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """H4: launches since the last refresh can block even before ground truth
    reflects the completions — the over-count is the safe direction."""
    import time

    monkeypatch.setenv("MAX_CONCURRENT_HARNESS_TASKS", "3")
    _noop_ecs(monkeypatch, running=0)
    _not_paused(monkeypatch)
    admission = _DispatcherAdmission(ceiling=3)
    admission._last_gt_at = time.time()  # fresh — no refresh on this may_launch
    for _ in range(3):
        admission.note_launch()
    decision = admission.may_launch()
    assert decision.allowed is False
    assert "at_capacity" in decision.reason


def test_ground_truth_refresh_corrects_drifted_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """H4 §8: completions/terminations are learned only from ground truth — a
    stale local count (launches since refresh) is pulled back to ListTasks truth
    on refresh (ADR-0030: bias reconcile toward over-count, but the window is
    one refresh, not forever)."""
    monkeypatch.setenv("MAX_CONCURRENT_HARNESS_TASKS", "10")

    # Ground truth reports a single RUNNING task (one of the "launched" ones has
    # already completed or crashed and is gone from ListTasks).
    _harness_ecs(monkeypatch, ["family:eval-dev-harness-aa92880033da20ca313928"])
    _not_paused(monkeypatch)
    admission = _DispatcherAdmission(ceiling=10)
    admission._launches_since_gt = 9  # many launches since the last refresh
    admission._last_gt_at = 0.0  # stale -> may_launch forces a refresh
    admission.may_launch()
    # After the refresh, the count is ground truth (1) + 0 launches since it —
    # the stale local over-count is gone instead of persisting forever.
    assert admission.in_flight() == 1


def test_pause_gate_blocks_with_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """M1 + H4 shared gate: pause blocks with reason 'paused' before receive."""
    monkeypatch.setenv("MAX_CONCURRENT_HARNESS_TASKS", "100")
    _noop_ecs(monkeypatch, running=0)
    admission = _DispatcherAdmission(ceiling=100)

    # Control read fails closed -> is_paused("harness") is True.
    class _Flaky:
        def hgetall(self, _k):
            raise RuntimeError("redis down")

        def smembers(self, _k):
            raise RuntimeError("redis down")

    monkeypatch.setattr(control_state, "_redis", lambda: _Flaky())
    decision = admission.may_launch()
    assert decision.allowed is False
    assert decision.reason == "paused"


def test_classify_run_task_failures() -> None:
    """H4 §6: four RunTask failure classes take distinct paths."""
    assert isinstance(disp._classify_run_task_failure("CAPACITY", ""), disp.RunTaskCapacityError)
    assert isinstance(
        disp._classify_run_task_failure("QuotaExceededException", ""), disp.RunTaskQuotaError
    )
    assert isinstance(
        disp._classify_run_task_failure("ThrottlingException", ""), disp.RunTaskThrottleError
    )
    assert isinstance(
        disp._classify_run_task_failure("CLIENT_ERROR", "bad task def"), disp.RunTaskPermanentError
    )


def test_never_dispatched_discard_sends_and_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate #2: an aborted run's message is NEVER_DISPATCHED and deleted, never launched."""
    sent = []
    deleted = []
    monkeypatch.setattr(disp, "send_message", lambda q, body: sent.append((q, body)))
    monkeypatch.setattr(disp, "delete_message", lambda q, rh: deleted.append(rh))

    disp._discard_aborted(_job(), "receipt-1")
    assert deleted == ["receipt-1"]
    assert any(q == "results" for q, _ in sent)
    verdict = sent[0][1]["state"]
    assert verdict == "NEVER_DISPATCHED"


# ---------------------------------------------------------------------------
# Bring-up 2026-09-03: running_count must count HARNESS tasks, not the cluster.
# At an idle bring-up ListTasks returned the eight service tasks (api, supervisor,
# 2x writer, 2x gateway, git-mirror, this dispatcher); the live planner read them as
# eight booting harness tasks, set the ceiling to eight, and gated every launch.


def test_running_count_ignores_service_tasks_and_the_dispatcher_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_HARNESS_TASKS", "100")
    ecs = _harness_ecs(
        monkeypatch,
        [
            "service:orchestrator-api",
            "service:run-supervisor",
            "service:results-writer",
            "service:results-writer",
            "service:gateway",
            "service:gateway",
            "service:git-mirror",
            "service:harness-dispatcher",
            "family:eval-dev-harness-dispatcher",  # a RunTask'd dispatcher: same family
            "family:eval-dev-eval-worker",  # the EC2 grading worker
            None,  # a task with no group at all
        ],
    )
    _not_paused(monkeypatch)
    admission = _DispatcherAdmission(ceiling=100)
    assert admission.running_count() == 0
    # DescribeTasks was consulted with the listed ARNs — the filter actually ran.
    assert ecs.describe_tasks.called
    assert ecs.describe_tasks.call_args.kwargs["tasks"] == [f"arn:task-{i}" for i in range(11)]


def test_running_count_counts_env_hash_and_per_instance_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_HARNESS_TASKS", "100")
    _harness_ecs(
        monkeypatch,
        [
            "service:gateway",
            "family:eval-dev-harness-aa92880033da20ca313928",  # -hw env-hash family
            "family:eval-dev-harness-django__django-10097",  # -inst per-instance family
            "family:eval-dev-harness-dispatcher",
            "family:eval-dev-harness-scikit-learn__scikit-learn-25102",
        ],
    )
    _not_paused(monkeypatch)
    admission = _DispatcherAdmission(ceiling=100)
    assert admission.running_count() == 3
    # and the gate uses that number: in_flight is the harness count, not the cluster's.
    admission.may_launch()
    assert admission.in_flight() == 3


def test_running_count_describes_in_batches_of_100(monkeypatch: pytest.MonkeyPatch) -> None:
    """DescribeTasks takes at most 100 ARNs per call; 145 running harness tasks (the
    static cap) must not raise or be truncated."""
    monkeypatch.setenv("MAX_CONCURRENT_HARNESS_TASKS", "145")
    groups: list[str | None] = [f"family:eval-dev-harness-{i:022x}" for i in range(145)]
    groups.append("service:x")
    ecs = _harness_ecs(monkeypatch, groups)
    _not_paused(monkeypatch)
    admission = _DispatcherAdmission(ceiling=145)
    assert admission.running_count() == 145
    sizes = [len(c.kwargs["tasks"]) for c in ecs.describe_tasks.call_args_list]
    assert sizes == [100, 46]


def test_running_count_describe_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """H4 stays fail-closed through the second call: a DescribeTasks failure refuses
    dispatch exactly as a ListTasks failure did."""
    monkeypatch.setenv("MAX_CONCURRENT_HARNESS_TASKS", "10")
    ecs = _harness_ecs(monkeypatch, ["family:eval-dev-harness-aa92880033da20ca313928"])
    ecs.describe_tasks.side_effect = Exception("AccessDenied")
    _not_paused(monkeypatch)
    admission = _DispatcherAdmission(ceiling=10)
    decision = admission.may_launch()
    assert decision.allowed is False
    assert "ListTasks failed" in decision.reason


@pytest.mark.parametrize(
    ("group", "expected"),
    [
        ("family:eval-dev-harness-aa92880033da20ca313928", True),
        ("family:eval-dev-harness-django__django-10097", True),
        ("family:eval-dev-harness-dispatcher", False),
        ("service:harness-dispatcher", False),
        ("service:eval-dev-harness-aa92880033da20ca313928", False),
        ("family:eval-dev-eval-worker", False),
        ("family:eval-dev-harness", False),
        ("", False),
        (None, False),
    ],
)
def test_is_harness_family_group(group: str | None, expected: bool) -> None:
    assert disp._is_harness_family_group(group) is expected


# ---------------------------------------------------------------------------
# Abort 2026-09-04 (run 01788550040741118596-dd284a4f): the discard path, live.


def test_redelivered_message_of_an_aborted_run_is_aborted_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message on receive #2+ was already received once — the observed case is a launched
    task the abort SIGKILLed before it reported, whose message came back after its
    visibility timeout. Stamping that NEVER_DISPATCHED recorded a 164-turn attempt as
    never having run."""
    sent = []
    deleted = []
    monkeypatch.setattr(disp, "send_message", lambda q, body: sent.append((q, body)))
    monkeypatch.setattr(disp, "delete_message", lambda q, rh: deleted.append(rh))

    disp._discard_aborted(_job(), "receipt-2", {"ApproximateReceiveCount": "2"})
    assert deleted == ["receipt-2"]
    assert sent[0][1]["state"] == "ABORTED_IN_FLIGHT"
    assert "redelivered" in sent[0][1]["error_detail"]
    # first receive (or no attributes at all, e.g. ElasticMQ) keeps the old verdict
    sent.clear()
    disp._discard_aborted(_job(), "receipt-1", {"ApproximateReceiveCount": "1"})
    assert sent[0][1]["state"] == "NEVER_DISPATCHED"


def test_discard_failure_never_escapes_into_the_poll_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live failure: DeleteMessage raised AccessDenied (the task role lacked the
    grant), the exception left the poll loop and ECS restarted the dispatcher every
    visibility timeout. A failed discard is logged; the message returns later."""
    sent = []
    monkeypatch.setattr(disp, "send_message", lambda q, body: sent.append((q, body)))

    def _denied(q: str, rh: str) -> None:
        raise RuntimeError("AccessDenied: sqs:DeleteMessage")

    monkeypatch.setattr(disp, "delete_message", _denied)
    disp._discard_aborted(_job(), "receipt-x", None)  # must not raise
    assert sent and sent[0][1]["state"] == "NEVER_DISPATCHED"
