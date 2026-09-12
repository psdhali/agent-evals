"""ADR-0030 H3 dispatcher — one Fargate task per job, family from the env hash.

Two-sided:
  Side 1 (can pass by accident): two instances with DIFFERENT env images each
    launch against their matching `-hw` family, carrying the fixed-width
    ADR-0032 job reference.
  Side 2 (the one that matters): a job whose env hash has no registered family
    is REFUSED and NAMED — never launched against a default image.

A1 / ADR-0033 (the third side that matters): the launched task goes into the
HARNESS-ISOLATED network (HARNESS_SUBNET_IDS + HARNESS_SECURITY_GROUP_IDS — not
the dispatcher's own network), and the A1-7 startup gate asserts against LIVE
EC2 state that neither carries a 0.0.0.0/0 and refuses to launch otherwise.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from swebench_eval.orchestrator.control_plane import harness_dispatcher as disp
from swebench_eval.orchestrator.run_config import DEFAULT_MAX_TOKENS_PER_INSTANCE
from swebench_eval.queue.schemas import HarnessJob, JobReference

# ADR-0043: env_image_key is a legacy field the dispatcher no longer reads (it
# always sends ""); jobs in these tests carry a value only to prove it is ignored.
_ASTROPY_KEY = "sweb.env.py.x86_64.428468730904ff6b4232aa:latest"


def _job(
    instance_id: str = "astropy__astropy-12907",
    env_key: str = _ASTROPY_KEY,
    problem_statement: str = "Fix the bug.",
) -> HarnessJob:
    return HarnessJob(
        run_id="run-1",
        instance_id=instance_id,
        repo_url="https://github.com/astropy/astropy",
        base_commit="c",
        problem_statement=problem_statement,
        attempt_number=1,
        harness_name="custom_minimal",
        model_alias="cheap-oss-model",
        env_image_key=env_key,
    )


def _isolated_ec2(
    monkeypatch: pytest.MonkeyPatch,
    *,
    leaky_rt: bool = False,
    leaky_sg: bool = False,
    unresolvable: bool = False,
) -> mock.Mock:
    """A fake EC2 client describing an ISOLATED harness network for the A1-7 gate.

    Mirrors the LIVE API shape (R1): ``describe_subnets`` does NOT return
    ``RouteTableId`` — the gate resolves each subnet's route table via the
    ``association.subnet-id`` filter. The table either has ONLY the VPC local/S3
    routes (isolated) or an extra 0.0.0.0/0 when ``leaky_rt``. ``unresolvable``
    returns no route table at all (the gate must refuse, never report clean).
    """
    ec2 = mock.Mock()
    ec2.describe_subnets.return_value = {
        "Subnets": [
            {"SubnetId": "s1", "VpcId": "v"},
            {"SubnetId": "s2", "VpcId": "v"},
        ]
    }
    routes: list[dict[str, Any]] = [
        {"DestinationCidrBlock": "10.0.0.0/16"},
        {"DestinationPrefixListId": "pl-s3"},  # the S3 gateway route — expected, NOT a leak
    ]
    if leaky_rt:
        routes.append({"DestinationCidrBlock": "0.0.0.0/0"})
    ec2.describe_route_tables.return_value = (
        {"RouteTables": [{"Routes": routes}]} if not unresolvable else {"RouteTables": []}
    )
    egress_ranges = [{"CidrIp": "10.0.0.0/16"}] if not leaky_sg else [{"CidrIp": "0.0.0.0/0"}]
    ec2.describe_security_groups.return_value = {
        "SecurityGroups": [{"GroupId": "sg1", "IpPermissionsEgress": [{"IpRanges": egress_ranges}]}]
    }

    def _client(service: str, **_: Any) -> Any:
        if service == "ec2":
            return ec2
        raise AssertionError(f"unexpected boto3 service {service} in the A1-7 gate")

    monkeypatch.setattr("boto3.client", _client)
    return ec2


def _launch(
    monkeypatch: pytest.MonkeyPatch,
    job: HarnessJob,
    receipt: str = "r",
    *,
    per_instance_family_registered: bool = True,
) -> dict[str, Any]:
    """Run _launch_task with a recording fake ECS client; returns the run_task kwargs.

    ``per_instance_family_registered`` simulates a registered
    ``eval-dev-harness-<instance_id>`` family — the ONLY family shape since
    ADR-0043 (no env-hash families exist to fall back to).  When False the
    describe raises the ECS not-found signature, and the dispatcher must refuse
    the job by name.
    """
    calls: list[dict[str, Any]] = []

    client = mock.Mock()

    def _record(**kw: Any) -> dict[str, list[Any]]:
        calls.append(kw)
        return {"failures": []}

    def _describe(**kw: Any) -> dict[str, Any]:
        if per_instance_family_registered:
            return {"taskDefinition": {"family": f"eval-dev-harness-{job.instance_id}"}}
        # ECS raises ClientException ("Unable to describe task definition") for a
        # missing family; the dispatcher matches by MESSAGE text, so a plain
        # error with the same signature exercises the real fallback path.
        raise RuntimeError(
            "ClientException: User: arn:aws:iam:foo is not authorized to perform: "
            "ecs:DescribeTaskDefinition on resource: arn:aws:ecs:...:task-definition/"
            f"eval-dev-harness-{job.instance_id}: Unable to describe task definition"
        )

    client.run_task.side_effect = _record
    client.describe_task_definition.side_effect = _describe
    monkeypatch.setattr(disp, "_ecs_client", lambda: client)
    monkeypatch.setenv("CLUSTER", "eval-dev-cluster")
    # A1/ADR-0033: the launched task's network is the HARNESS-ISOLATED one, NOT
    # the dispatcher's own subnets/SGs.
    monkeypatch.setenv("HARNESS_SUBNET_IDS", '["s1", "s2"]')
    monkeypatch.setenv("HARNESS_SECURITY_GROUP_IDS", '["sg1"]')
    _isolated_ec2(monkeypatch)
    disp._launch_task(job, receipt)
    assert calls, "run_task was not called"
    return calls[0]


def _override_env(call: dict[str, Any]) -> dict[str, str]:
    container = call["overrides"]["containerOverrides"][0]
    return {e["name"]: e["value"] for e in container["environment"]}


def test_two_instances_launch_their_own_families(monkeypatch: pytest.MonkeyPatch) -> None:
    """Side 1: each instance's task runs its MATCHING per-instance family (ADR-0043)."""
    call_a = _launch(monkeypatch, _job(instance_id="astropy__astropy-12907", env_key=""))
    call_b = _launch(monkeypatch, _job(instance_id="django__django-10914", env_key=""))

    assert call_a["taskDefinition"] == "eval-dev-harness-astropy__astropy-12907"
    assert call_b["taskDefinition"] == "eval-dev-harness-django__django-10914"
    assert call_a["launchType"] == "FARGATE"
    assert call_a["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"] == "DISABLED"
    # The launched task must be in the ISOLATED harness network (A1/ADR-0033).
    aws = call_a["networkConfiguration"]["awsvpcConfiguration"]
    assert aws["subnets"] == ["s1", "s2"]
    assert aws["securityGroups"] == ["sg1"]
    # The command override selects the single-job entrypoint.
    container = call_a["overrides"]["containerOverrides"][0]
    assert container["command"] == ["harness-worker-job"]
    # The task carries the receipt handle + instance.
    env = _override_env(call_a)
    assert env["INSTANCE_ID"] == "astropy__astropy-12907"
    assert env["RECEIPT_HANDLE"] == "r"


def test_refuses_job_without_per_instance_family_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Side 2a: the per-instance family is not registered → refused, NAMING the
    family (ADR-0043: there is no env-hash fallback, and never a default image)."""
    client = mock.Mock()

    def _describe(**kw: Any) -> dict[str, Any]:
        raise RuntimeError("ClientException: Unable to describe task definition")

    client.describe_task_definition.side_effect = _describe
    monkeypatch.setattr(disp, "_ecs_client", lambda: client)
    monkeypatch.setenv("CLUSTER", "eval-dev-cluster")
    monkeypatch.setenv("HARNESS_SUBNET_IDS", '["s1"]')
    monkeypatch.setenv("HARNESS_SECURITY_GROUP_IDS", '["sg1"]')
    _isolated_ec2(monkeypatch)

    with pytest.raises(disp.DispatchRefusedError, match="eval-dev-harness-astropy__astropy-12907"):
        disp._launch_task(_job(env_key=""), "r")
    client.run_task.assert_not_called()


def test_refuses_without_harness_network_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Side 2b: harness network unset → refused with a NAME (no silent default)."""
    client = mock.Mock()
    monkeypatch.setattr(disp, "_ecs_client", lambda: client)
    monkeypatch.setenv("CLUSTER", "eval-dev-cluster")
    monkeypatch.delenv("HARNESS_SUBNET_IDS", raising=False)
    monkeypatch.delenv("HARNESS_SECURITY_GROUP_IDS", raising=False)
    _isolated_ec2(monkeypatch)

    with pytest.raises(RuntimeError, match="HARNESS_SUBNET_IDS"):
        disp._launch_task(_job(), "r")
    client.run_task.assert_not_called()


def test_refuses_when_harness_route_table_still_has_default_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A1-7: the gate reads LIVE state — the realistic failure is a value that is
    SET and WRONG (a copy-paste of the private subnets), which no env check can
    catch. A 0.0.0.0/0 route on the configured subnets' route table → REFUSE."""
    client = mock.Mock()
    monkeypatch.setattr(disp, "_ecs_client", lambda: client)
    monkeypatch.setenv("CLUSTER", "c")
    monkeypatch.setenv("HARNESS_SUBNET_IDS", '["s1"]')
    monkeypatch.setenv("HARNESS_SECURITY_GROUP_IDS", '["sg1"]')
    _isolated_ec2(monkeypatch, leaky_rt=True)

    with pytest.raises(RuntimeError, match="A1 gate REFUSES"):
        disp._launch_task(_job(), "r")
    client.run_task.assert_not_called()


def test_refuses_when_harness_sg_has_open_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    """A1-7: a 0.0.0.0/0 egress rule on the configured SG → REFUSE."""
    client = mock.Mock()
    monkeypatch.setattr(disp, "_ecs_client", lambda: client)
    monkeypatch.setenv("CLUSTER", "c")
    monkeypatch.setenv("HARNESS_SUBNET_IDS", '["s1"]')
    monkeypatch.setenv("HARNESS_SECURITY_GROUP_IDS", '["sg1"]')
    _isolated_ec2(monkeypatch, leaky_sg=True)

    with pytest.raises(RuntimeError, match="A1 gate REFUSES"):
        disp._launch_task(_job(), "r")
    client.run_task.assert_not_called()


def test_gate_actually_consults_route_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """R1: the route-table check must genuinely RUN — never just 'not fire'.

    The 2026-08-19 regression read a field (RouteTableId) the real DescribeSubnets
    does not return, so the check silently iterated over nothing. An outcome
    assertion ('did not raise') cannot distinguish 'checked and found clean' from
    'never checked'. Asserting the call is the distinction — the reviewer's
    generalisable guard: when a safety check does not fire, also assert it ran.
    """
    ec2 = _isolated_ec2(monkeypatch)
    disp._assert_isolated_network(
        {"subnets": ["s1"], "security_groups": ["sg1"]}
    )  # clean network → no raise
    assert ec2.describe_subnets.called
    assert ec2.describe_route_tables.called, "the route-table check never ran"


def test_gate_refuses_when_route_table_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1: an unresolvable subnet must REFUSE, never report clean."""
    _isolated_ec2(monkeypatch, unresolvable=True)
    with pytest.raises(RuntimeError, match="cannot resolve a route table"):
        disp._assert_isolated_network({"subnets": ["s1"], "security_groups": ["sg1"]})


def test_overrides_carry_fixed_reference_not_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0032: containerOverrides is the FIXED-WIDTH reference — the 26 KB
    problem_statement must NOT travel."""
    call = _launch(monkeypatch, _job(problem_statement="x" * 30_000))
    env = _override_env(call)
    assert env["INSTANCE_ID"] == "astropy__astropy-12907"
    lower = {k.lower() for k in env}
    assert "problem_statement" not in lower
    assert "repo_url" not in lower
    assert "base_commit" not in lower
    assert len(env) == len(JobReference.ENV_NAMES)


def test_context_window_token_reaches_task_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolved context window MUST travel to the launched task.

    Regression for the real bug (BUILDER1-CONSOLIDATED-REVIEW §3): _reference_for()
    omitted context_window_tokens, so every harness read None -> compaction silently
    disabled (compute_threshold(0) -> 0) and instance_results.context_window_tokens
    stayed NULL no matter what was deployed. The old "count of env vars == ENV_NAMES"
    check could not catch it — it never asserts a specific value survived.

    Mutation: delete `context_window_tokens=job.context_window_tokens,` from
    _reference_for(); this test fails (CONTEXT_WINDOW_TOKENS absent from the task env)."""
    # the wire contract must carry the field (else the assertion below is vacuous)
    assert "context_window_tokens" in JobReference.ENV_NAMES

    # a job WITH an explicit resolved window -> the env must carry it verbatim
    job = _job()
    job.context_window_tokens = 82_768
    env = _override_env(_launch(monkeypatch, job))
    assert env["CONTEXT_WINDOW_TOKENS"] == "82768"

    # the default job's window (DEFAULT_CONTEXT_WINDOW_TOKENS = 262144) must
    # also survive — this is the value the bug silently dropped on every dispatch.
    env_default = _override_env(_launch(monkeypatch, _job()))
    assert env_default.get("CONTEXT_WINDOW_TOKENS") == "262144"

    # a job with NO window (explicit None = compaction disabled) must stay absent
    job_none = _job()
    job_none.context_window_tokens = None
    env_none = _override_env(_launch(monkeypatch, job_none))
    assert env_none.get("CONTEXT_WINDOW_TOKENS", "") == ""


def test_run_task_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed RunTask raises (the loop's except leaves the message for retry)."""
    client = mock.Mock()
    client.run_task.return_value = {
        "failures": [{"reason": "CLIENT_ERROR", "detail": "bad task def"}]
    }
    monkeypatch.setattr(disp, "_ecs_client", lambda: client)
    monkeypatch.setenv("CLUSTER", "c")
    monkeypatch.setenv("HARNESS_SUBNET_IDS", '["s1"]')
    monkeypatch.setenv("HARNESS_SECURITY_GROUP_IDS", '["sg1"]')
    _isolated_ec2(monkeypatch)

    with pytest.raises(RuntimeError, match="CLIENT_ERROR"):
        disp._launch_task(_job(), "r")


def test_reference_round_trips_to_env() -> None:
    ref = JobReference(
        run_id="run-1",
        instance_id="astropy__astropy-12907",
        attempt_number=1,
        receipt_handle="r",
        harness_name="custom_minimal",
        model_alias="cheap-oss-model",
        timeout_seconds=600,
        max_tokens_per_instance=DEFAULT_MAX_TOKENS_PER_INSTANCE,
        max_cost_usd_per_instance=5.0,
    )
    assert JobReference.from_env({e["name"]: e["value"] for e in ref.to_env()}) == ref


def test_enforce_per_run_key_refuses_launch_with_no_cached_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-08-29: with no per-run key cached (run_key_cache.fetch -> None,
    the real behavior in a test env with no Redis) and ENFORCE_PER_RUN_KEY=1,
    the dispatcher must refuse the launch closed rather than fall back to
    LITELLM_MASTER_KEY (routing.gateway_api_key) — the exact live incident
    this pins: a stale SQS redelivery of an already-finalised run's job
    silently ran against the shared master key with no per-run budget cap.
    Mutation: drop the `if _enforce_per_run_key():` check in _reference_for;
    this test fails (no DispatchRefusedError, run_task gets called)."""
    monkeypatch.setenv("ENFORCE_PER_RUN_KEY", "1")
    monkeypatch.setenv("CLUSTER", "eval-dev-cluster")
    monkeypatch.setenv("HARNESS_SUBNET_IDS", '["s1", "s2"]')
    monkeypatch.setenv("HARNESS_SECURITY_GROUP_IDS", '["sg1"]')
    _isolated_ec2(monkeypatch)
    client = mock.Mock()
    monkeypatch.setattr(disp, "_ecs_client", lambda: client)

    with pytest.raises(disp.DispatchRefusedError, match="ENFORCE_PER_RUN_KEY=1"):
        disp._launch_task(_job(), "r")
    client.run_task.assert_not_called()


def test_enforce_per_run_key_off_still_launches_with_master_key_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default (unset / '0') path is unchanged: no cached key still
    launches, just with an empty LITELLM_API_KEY override (the worker's own
    gateway_api_key() resolves the fallback, not the dispatcher)."""
    monkeypatch.delenv("ENFORCE_PER_RUN_KEY", raising=False)
    env = _override_env(_launch(monkeypatch, _job()))
    assert env.get("LITELLM_API_KEY", "") == ""


def test_reference_none_max_tokens_round_trips() -> None:
    ref = JobReference(
        run_id="run-1",
        instance_id="i",
        attempt_number=1,
        receipt_handle="r",
        harness_name="custom_minimal",
        model_alias="cheap-oss-model",
        timeout_seconds=600,
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=5.0,
    )
    assert JobReference.from_env({e["name"]: e["value"] for e in ref.to_env()}) == ref


def test_reference_from_env_fails_closed_on_missing_field() -> None:
    env = {
        "RUN_ID": "r",
        "INSTANCE_ID": "i",
        "ATTEMPT_NUMBER": "1",
        "HARNESS_NAME": "custom_minimal",
        "MODEL_ALIAS": "cheap-oss-model",
        "TIMEOUT_SECONDS": "600",
        "MAX_TOKENS_PER_INSTANCE": "",
        "MAX_COST_USD_PER_INSTANCE": "5.0",
    }
    with pytest.raises(KeyError):
        JobReference.from_env(env)  # missing RECEIPT_HANDLE


def test_per_instance_family_is_selected_from_the_instance_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0043: the registered ``eval-dev-harness-<instance_id>`` family (pointing
    at the -inst image with the baked testbed sentinel) is selected from the
    instance id alone; a stale ``env_image_key`` on the job is ignored."""
    call = _launch(
        monkeypatch,
        _job(instance_id="astropy__astropy-12907", env_key=_ASTROPY_KEY),
        per_instance_family_registered=True,
    )
    assert call["taskDefinition"] == "eval-dev-harness-astropy__astropy-12907"


def test_missing_per_instance_family_is_refused_not_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No registered per-instance family → refused by name (ADR-0043: no env-hash
    family exists to fall back to, and never a silent default image)."""
    with pytest.raises(disp.DispatchRefusedError, match="eval-dev-harness-astropy__astropy-12907"):
        _launch(
            monkeypatch,
            _job(instance_id="astropy__astropy-12907", env_key=_ASTROPY_KEY),
            per_instance_family_registered=False,
        )
