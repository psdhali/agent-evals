"""Guard tests for the nightly scale-to-zero Lambda (review item 2 / DoD 10 + S-2).

The whole point of the guard: a ~6h eval run trivially spans the 02:00 cron.
Scaling to zero mid-run destroys sunk inference spend. So the Lambda must
REFUSE (leave services running) unless ALL three idle checks pass, and must
fail-safe toward NOT tearing down when any check errors (Aurora paused, IAM
denied, throttled).

S-2 hybrid: check 1 short-circuits a PAUSED cluster (ServerlessDatabaseCapacity
== 0) to idle WITHOUT a Data API query (a paused cluster errors on it and would
otherwise refuse forever); an AWAKE cluster falls through to the `runs` query.

These tests load the Lambda source directly (no deployment) and drive it with
mocked boto3 clients: idle->scales, paused->scales (no query), active DB->
refuses, in-flight SQS (NotVisible)->refuses, running task->refuses, metric or
query error->refuses.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
from typing import Any
from unittest import mock

import pytest

_LAMBDA_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "infra/terraform/modules/observability/scale_to_zero_lambda/index.py"
)


@pytest.fixture()
def guard() -> Any:
    # The Lambda does boto3.client(...) at module import, which needs a region.
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
    spec = importlib.util.spec_from_file_location("scale_to_zero_lambda", _LAMBDA_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "AWS_DEFAULT_REGION": "us-west-2",  # top-level boto3.client() at import needs a region
        "DB_CLUSTER_ARN": "arn:aws:rds:us-west-2:1:cluster:eval-dev-aurora-v2",
        "DB_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:1:secret:aurora-master-abc",
        "DB_NAME": "app_control_plane",
        "QUEUE_URLS": '["https://sqs.us-west-2.amazonaws.com/1/harness-jobs", "https://sqs.us-west-2.amazonaws.com/1/eval-jobs"]',
        "ECS_CLUSTER": "eval-dev-cluster",
        "ECS_SERVICES": '["arn:aws:ecs:us-west-2:1:service/eval-dev-cluster/eval-dev-gateway"]',
        "SNS_TOPIC_ARN": "arn:aws:sns:us-west-2:1:scale-to-zero",
    }
    env.update(overrides)
    return env


def _patch_clients(
    guard: Any,
    db_count: int = 0,
    sqs_attr: dict[str, str] | None = None,
    list_tasks: tuple[str, ...] = (),
    capacity: float | None = 1.0,
    metric_points: list[dict[str, float]] | None = None,
) -> dict[str, mock.Mock]:
    """Patch the Lambda's module-level boto3 clients.

    Defaults = all idle (cluster AWAKE with capacity 1.0, db COUNT 0, queues
    0/0, no tasks) so a test that calls this without overrides and asserts
    scaling is exercising the awake->query->idle happy path.
    `capacity`:
      - >0 (default) -> awake -> falls through to the `runs` query
      - 0           -> PAUSED -> report idle with NO query
      - None        -> metric has no datapoints -> falls through to the query
    `metric_points` overrides the Datapoints verbatim.
    """
    cloudwatch = mock.Mock()
    cloudwatch.get_metric_statistics.return_value = (
        {"Datapoints": metric_points}
        if metric_points is not None
        else {"Datapoints": [{"Minimum": capacity}] if capacity is not None else []}
    )
    guard.cloudwatch = cloudwatch

    rds_data = mock.Mock()
    # Data API returns rows as lists of field objects: records = [[field, ...], ...]
    rds_data.execute_statement.return_value = {"records": [[{"longValue": db_count}]]}
    guard.rds_data = rds_data

    sqs = mock.Mock()
    sqs.get_queue_attributes.return_value = {
        "Attributes": sqs_attr
        or {
            "ApproximateNumberOfMessages": "0",
            "ApproximateNumberOfMessagesNotVisible": "0",
        }
    }
    guard.sqs = sqs

    ecs = mock.Mock()
    ecs.list_tasks.return_value = {"taskArns": list(list_tasks)}
    guard.ecs = ecs

    autoscaling = mock.Mock()
    guard.autoscaling = autoscaling

    sns = mock.Mock()
    guard.sns = sns

    return {
        "cloudwatch": cloudwatch,
        "rds_data": rds_data,
        "sqs": sqs,
        "ecs": ecs,
        "autoscaling": autoscaling,
        "sns": sns,
    }


def _body(resp: Any) -> Any:
    return json.loads(resp["body"])


@mock.patch.dict(os.environ, _env(), clear=False)
def test_idle_scales_to_zero(guard: Any) -> None:
    clients = _patch_clients(guard)
    resp = guard.handler({}, None)
    assert resp["statusCode"] == 200
    assert _body(resp)["refused"] is False
    clients["ecs"].update_service.assert_called_once()


@mock.patch.dict(os.environ, _env(), clear=False)
def test_paused_cluster_scales_without_query(guard: Any) -> None:
    """S-2: capacity 0 => cluster PAUSED => idle, no Data API call (the bug)."""
    clients = _patch_clients(guard, capacity=0)
    resp = guard.handler({}, None)
    assert resp["statusCode"] == 200
    assert _body(resp)["refused"] is False
    clients["rds_data"].execute_statement.assert_not_called()
    clients["ecs"].update_service.assert_called_once()

    # Regression (found live 2026-08-16): the capacity metric must be requested
    # with the REAL RDS cluster id — the old "cluster/" split yielded "" on the
    # colon-form ARN, so this dimension was empty and the check always errored.
    call = clients["cloudwatch"].get_metric_statistics.call_args
    dims = call.kwargs.get("Dimensions") if call else None
    assert dims is not None
    assert dims[0]["Name"] == "DBClusterIdentifier"
    assert dims[0]["Value"] == "eval-dev-aurora-v2"


def test_cluster_id_from_real_rds_arn(guard: Any) -> None:
    """The colon-form RDS cluster ARN must resolve to the cluster identifier."""
    assert (
        guard._cluster_id_from_arn("arn:aws:rds:us-west-2:123456789012:cluster:eval-dev-aurora-v2")
        == "eval-dev-aurora-v2"
    )
    # The bug: a slash split returned "" for the same input.
    assert guard._cluster_id_from_arn("") == ""


@mock.patch.dict(os.environ, _env(), clear=False)
def test_active_postgres_run_refuses(guard: Any) -> None:
    clients = _patch_clients(guard, db_count=3)  # 3 runs pending/running (awake)
    resp = guard.handler({}, None)
    assert _body(resp)["refused"] is True
    assert any("Postgres" in r for r in _body(resp)["reasons"])
    clients["ecs"].update_service.assert_not_called()


@mock.patch.dict(os.environ, _env(), clear=False)
def test_inflight_sqs_message_refuses(guard: Any) -> None:
    """NotVisible>0 = a worker is processing right now (the trap in the review)."""
    clients = _patch_clients(
        guard,
        sqs_attr={
            "ApproximateNumberOfMessages": "0",
            "ApproximateNumberOfMessagesNotVisible": "1",
        },
    )
    resp = guard.handler({}, None)
    assert _body(resp)["refused"] is True
    assert any("queued" in r for r in _body(resp)["reasons"])
    clients["ecs"].update_service.assert_not_called()


@mock.patch.dict(os.environ, _env(), clear=False)
def test_running_ecs_task_refuses(guard: Any) -> None:
    clients = _patch_clients(guard, list_tasks=("arn:aws:ecs:us-west-2:1:task/abc",))
    resp = guard.handler({}, None)
    assert _body(resp)["refused"] is True
    assert any("running tasks" in r for r in _body(resp)["reasons"])
    clients["ecs"].update_service.assert_not_called()


@mock.patch.dict(os.environ, _env(), clear=False)
def test_metric_error_failsafe_refuses(guard: Any) -> None:
    """S-2: a CloudWatch metric read error => fail SAFE toward NOT tearing down."""
    clients = _patch_clients(guard)
    clients["cloudwatch"].get_metric_statistics.side_effect = Exception("CloudWatch down")
    resp = guard.handler({}, None)
    assert _body(resp)["refused"] is True
    assert any("ERRORS" in r for r in _body(resp)["reasons"])
    clients["ecs"].update_service.assert_not_called()


@mock.patch.dict(os.environ, _env(), clear=False)
def test_postgres_error_failsafe_refuses(guard: Any) -> None:
    """An awake cluster whose `runs` query errors => fail SAFE toward NOT tearing down."""
    clients = _patch_clients(guard)  # default capacity 1.0 => awake => runs query
    clients["rds_data"].execute_statement.side_effect = Exception("Data API down")
    resp = guard.handler({}, None)
    assert _body(resp)["refused"] is True
    assert any("ERRORS" in r for r in _body(resp)["reasons"])
    clients["ecs"].update_service.assert_not_called()


@mock.patch.dict(os.environ, _env(), clear=False)
def test_refusal_notifies(guard: Any) -> None:
    clients = _patch_clients(guard)
    clients["cloudwatch"].get_metric_statistics.side_effect = Exception("boom")
    guard.handler({}, None)
    clients["sns"].publish.assert_called_once()
    subject = clients["sns"].publish.call_args.kwargs["Subject"]
    assert "REFUSED" in subject.upper()


# --- Stage 0.1 (A-6): the guard can zero the eval host ASG -------------------
_ASG = "arn:aws:autoscaling:us-west-2:1:autoScalingGroup:1:autoScalingGroupName/eval-dev-eval-asg"


def _with_asg(**overrides: str) -> dict[str, str]:
    return _env(ASG_ARNS=f'["{_ASG}"]', **overrides)


@mock.patch.dict(os.environ, _with_asg(), clear=False)
def test_idle_zeroes_asg(guard: Any) -> None:
    """A-6 done-when: with an idle ASG (desired >= 1, no work) the guard sets it to 0."""
    clients = _patch_clients(guard)
    resp = guard.handler({}, None)
    assert resp["statusCode"] == 200
    assert _body(resp)["refused"] is False
    clients["autoscaling"].set_desired_capacity.assert_called_once_with(
        AutoScalingGroupName="eval-dev-eval-asg", DesiredCapacity=0
    )
    # services are still scaled too, not just the ASG.
    clients["ecs"].update_service.assert_called_once()


@mock.patch.dict(os.environ, _with_asg(), clear=False)
def test_running_service_task_refuses_and_leaves_asg(guard: Any) -> None:
    """The second half that matters: a task in flight => refuse, ASG untouched."""
    clients = _patch_clients(guard, list_tasks=("arn:aws:ecs:us-west-2:1:task/abc",))
    resp = guard.handler({}, None)
    assert _body(resp)["refused"] is True
    clients["autoscaling"].set_desired_capacity.assert_not_called()
    clients["ecs"].update_service.assert_not_called()


@mock.patch.dict(os.environ, _with_asg(), clear=False)
def test_warm_run_task_refuses_asg(guard: Any) -> None:
    """A run-task NOT tied to a service still blocks ASG zeroing.

    This is why the cluster-wide check exists: _idle_ecs is service-scoped and
    cannot see a warm-job run-task, yet zeroing the ASG under one strands it.
    Simulate that by making the service-scoped lookup idle while a run-task
    (no serviceName) is running.
    """
    clients = _patch_clients(guard)

    def fake_list_tasks(**kw: object) -> dict[str, object]:
        if "serviceName" in kw:
            return {"taskArns": []}  # no task on the eval service itself
        return {"taskArns": ["arn:aws:ecs:us-west-2:1:task/warm"]}  # run-task running

    clients["ecs"].list_tasks.side_effect = fake_list_tasks
    resp = guard.handler({}, None)
    assert _body(resp)["refused"] is True
    assert any("cluster tasks" in r for r in _body(resp)["reasons"])
    clients["autoscaling"].set_desired_capacity.assert_not_called()
    clients["ecs"].update_service.assert_not_called()


# --- 2026-08-22: the guard refused six nights running -------------------------
# Six consecutive invocations (2026-08-16..22) refused with ZERO Lambda errors.
# Three distinct defects, none of which any existing test could see, because
# _idle_ecs short-circuits on the first busy service and orchestrator-api was
# always busy — so the loop never reached the broken entry.


def _client_error(code: str) -> Any:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": code}}, "ListTasks")


@mock.patch.dict(
    os.environ,
    _env(
        ECS_SERVICES=json.dumps(
            [
                "arn:aws:ecs:us-west-2:1:service/eval-dev-cluster/orchestrator-api",
                "arn:aws:ecs:us-west-2:1:service/eval-dev-cluster/does-not-exist",
            ]
        )
    ),
    clear=False,
)
def test_absent_service_counts_as_idle_and_still_scales(guard: Any) -> None:
    """A service ARN naming a service that does not exist must NOT wedge the guard.

    This is the live bug: ECS_SERVICES carried `eval-dev-custom_minimal`, which
    has never existed. Every night refused before reaching it; the first
    genuinely idle night would have raised ServiceNotFoundException and refused
    too — permanently.
    """
    clients = _patch_clients(guard)

    def list_tasks(**kwargs: Any) -> Any:
        if kwargs.get("serviceName") == "does-not-exist":
            raise _client_error("ServiceNotFoundException")
        return {"taskArns": []}

    clients["ecs"].list_tasks.side_effect = list_tasks
    clients["ecs"].update_service.side_effect = lambda **kw: (
        (_ for _ in ()).throw(_client_error("ServiceNotFoundException"))
        if kw.get("service") == "does-not-exist"
        else None
    )

    body = _body(guard.handler({}, None))
    assert body["refused"] is False, body
    # The real service was scaled; the absent one reported absent, not failed.
    scaled = {r["service"]: r for r in body["scaled"] if "service" in r}
    assert scaled["orchestrator-api"]["ok"] is True
    assert scaled["does-not-exist"] == {"service": "does-not-exist", "absent": True, "ok": True}


@mock.patch.dict(os.environ, _env(), clear=False)
def test_access_denied_on_list_tasks_still_fails_safe(guard: Any) -> None:
    """Only ServiceNotFound is swallowed — AccessDenied must still REFUSE.

    The mutation that matters: if the new except clause caught ClientError
    broadly, a revoked IAM permission would read as "idle" and tear down a live
    run. Assert the fail-safe direction is intact.
    """
    clients = _patch_clients(guard)
    clients["ecs"].list_tasks.side_effect = _client_error("AccessDeniedException")

    body = _body(guard.handler({}, None))
    assert body["refused"] is True
    assert any("ECS check ERRORS" in r for r in body["reasons"]), body["reasons"]


@mock.patch.dict(os.environ, _env(), clear=False)
def test_postgres_refusal_reports_the_count_not_just_active(guard: Any) -> None:
    """The refusal must say HOW MANY runs are pending, not just "active".

    Six nights of logs read `"postgres": "active"` because the handler discarded
    the detail dict (`idle_db, _ =`). "3 runs are genuinely pending" and "the
    query returned no rows" are different problems with the same symptom.
    """
    _patch_clients(guard, db_count=3)

    body = _body(guard.handler({}, None))
    assert body["refused"] is True
    reason = next(r for r in body["reasons"] if "Postgres" in r)
    assert "postgres_active" in reason and "3" in reason, reason


@mock.patch.dict(os.environ, _env(), clear=False)
def test_postgres_empty_resultset_is_distinguishable_from_active_runs(guard: Any) -> None:
    """The other half: an empty result set must not masquerade as pending runs."""
    clients = _patch_clients(guard)
    clients["rds_data"].execute_statement.return_value = {"records": []}

    body = _body(guard.handler({}, None))
    assert body["refused"] is True
    reason = next(r for r in body["reasons"] if "Postgres" in r)
    assert "empty" in reason, reason


@mock.patch.dict(os.environ, _env(), clear=False)
def test_postgres_query_state_list_matches_the_python_constant(guard: Any) -> None:
    """N1 (BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md review): the guard's
    non-terminal state list is a DUPLICATED SQL literal — this Lambda cannot
    import ``results_writer._ALL_NON_TERMINAL_STATES`` (standalone deploy
    unit, json/os/datetime/boto3 only). Nothing at import time enforces they
    stay in sync; this test is what does. Mutation check: drop one state from
    either side and this goes red.
    """
    import re

    from swebench_eval.orchestrator.control_plane.results_writer import (
        _ALL_NON_TERMINAL_STATES,
    )

    clients = _patch_clients(guard)
    guard.handler({}, None)
    sql = clients["rds_data"].execute_statement.call_args.kwargs["sql"]
    states_in_sql = set(re.findall(r"'([A-Z_]+)'", sql.split("state IN")[1].split(")")[0]))
    assert states_in_sql == set(_ALL_NON_TERMINAL_STATES), (
        states_in_sql,
        _ALL_NON_TERMINAL_STATES,
    )


@mock.patch.dict(os.environ, _env(), clear=False)
def test_postgres_query_no_longer_treats_an_awaiting_close_run_as_active(guard: Any) -> None:
    """N2/§2.3 of the v2 design: a 'running' run with zero non-terminal
    instance_results rows must not block scale-to-zero forever now that
    close is a manual operator action. This only asserts the QUERY SHAPE (the
    mock can't evaluate the EXISTS subquery for us) — that 'running' is
    conditioned on the EXISTS clause and 'pending' is deliberately not.
    """
    clients = _patch_clients(guard)
    guard.handler({}, None)
    sql = clients["rds_data"].execute_statement.call_args.kwargs["sql"]
    assert "r.status = 'pending'" in sql
    assert "r.status = 'running' AND EXISTS" in sql


def test_schedule_is_not_during_the_working_evening() -> None:
    """The cron must not fire in PDT working hours.

    cron(0 2 * * ? *) is 02:00 UTC = 19:00 PDT — seven in the evening. The guard
    fired while the fleet was legitimately busy and correctly refused every
    night. EventBridge cron has no timezone field, so the UTC hour IS the
    setting; this asserts it lands overnight in both PDT (UTC-7) and PST (UTC-8).
    """
    import re

    tf = (
        pathlib.Path(__file__).resolve().parents[1]
        / "infra/terraform/modules/observability/main.tf"
    ).read_text()
    match = re.search(r'default\s*=\s*"cron\((\d+)\s+(\d+)\s', tf)
    assert match is not None, "nightly_scale_to_zero_cron default not found"
    utc_hour = int(match.group(2))
    for offset, label in ((7, "PDT"), (8, "PST")):
        local = (utc_hour - offset) % 24
        assert 0 <= local <= 5, f"fires at {local:02d}:00 {label} — not overnight"
