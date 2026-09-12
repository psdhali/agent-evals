"""Nightly scale-to-zero Lambda for envs/dev services — GUARDED.

R-1b / DoD 10 (two-sided): this is the same-day cost control that does NOT
depend on billing data (AWS Budgets lags 8-12h, CloudWatch EstimatedCharges
~6h). A scheduled EventBridge rule invokes it nightly to scale dev ephemeral
ECS services to zero.

Why guarded (review item 2): a full eval run is ~6 hours and trivially spans
02:00. Scaling services to zero mid-run destroys sunk inference spend — the
same failure ADR-0021 rejected Fargate Spot to avoid. So the Lambda scales to
zero ONLY if every idle check passes, and REFUSES (leaving services running)
if any check reports activity OR errors out.

Idle checks (all three must report idle):
  1. Postgres idle — a PAUSED cluster (ServerlessDatabaseCapacity == 0, S-2)
     counts as idle without a query (nothing has connected for >=300s => no
     run); an AWAKE cluster must have zero `runs` pending/running.
  2. SQS harness-jobs, eval-jobs: ApproximateNumberOfMessages +
     ApproximateNumberOfMessagesNotVisible             -> both zero
     (NotVisible counts messages a worker is CURRENTLY IN FLIGHT — checking
     only the visible count reads "empty" mid-run)
  3. ECS list-tasks, desiredStatus=RUNNING, on the target services -> zero

Fail-safe toward NOT tearing down: any check that errors (metric read, IAM
denied, throttled, Data API down) means do not scale. A night idle is ~$12-15;
a destroyed six-hour run is inference, wall-clock and an evening.

Notify either way, more loudly on refusal: publishes to the SNS topic given
in env. The refusal message is explicit so three refusals in a row is how you
learn something is stuck holding a message.

Log the decision and its inputs: every run emits structured CloudWatch output
recording check results, so "found work" is distinguishable from "errored".
"""

import json
import os
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import ClientError

# A service (or cluster) that does not exist has no RUNNING tasks, so it is
# idle by definition.  These codes are the ONLY ones _idle_ecs swallows —
# AccessDenied, throttling and everything else still propagate and still
# fail-safe into a refusal.  Before this, a single stale/renamed service ARN in
# ECS_SERVICES made every night after a teardown refuse permanently.
_ABSENT_CODES = ("ServiceNotFoundException", "ClusterNotFoundException")

ecs = boto3.client("ecs")
sqs = boto3.client("sqs")
sns = boto3.client("sns")
cloudwatch = boto3.client("cloudwatch")
# A-6: closing the gap — the guard can now zero the eval host ASG, the one
# resource the 61-image build and the eval fleet run on.
autoscaling = boto3.client("autoscaling")

# RDS Data API is used for the Postgres check (no psycopg2 wheel, no VPC) —
# but ONLY when the cluster is awake. A paused cluster errors on the Data API,
# so check 1 short-circuits on the ServerlessDatabaseCapacity metric first
# (S-2 hybrid): capacity 0 => paused => idle, no query.
rds_data = boto3.client("rds-data")


def _env_json(name, default):
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _parse_service_arn(arn):
    """'arn:aws:ecs:region:acct:service/CLUSTER/SERVICE' -> (cluster, service)."""
    try:
        rest = arn.split("service/", 1)[1]
        cluster, service = rest.split("/", 1)
        return cluster, service
    except (IndexError, ValueError):
        return None, None


def _asg_name_from_arn(arn):
    """'arn:aws:autoscaling:...:autoScalingGroupName/NAME' -> 'NAME'."""
    try:
        return arn.rsplit("autoScalingGroupName/", 1)[1]
    except IndexError:
        return arn


def _idle_cluster_tasks(cluster_name):
    """True if NO task is RUNNING anywhere in the cluster (service or run-task).

    Gate used ONLY when an ASG is being zeroed. The service-scoped _idle_ecs
    cannot see a warm-job run-task (not tied to any service); zeroing the ASG
    under one strands it. A running task anywhere in the cluster => refuse.
    """
    if not cluster_name:
        return True, {}
    tasks = ecs.list_tasks(cluster=cluster_name, desiredStatus="RUNNING")["taskArns"]
    if tasks:
        return False, {"cluster_tasks": len(tasks)}
    return True, {}


def _cluster_id_from_arn(cluster_arn):
    """'arn:aws:rds:region:acct:cluster:NAME' -> 'NAME' (CloudWatch dimension).

    RDS cluster ARNs use a COLON after "cluster", not a slash — the old split on
    "cluster/" always raised, returned "", and made the CloudWatch capacity call
    fail param-validation ("Dimensions[0].Value, value: 0"), so the S-2 hybrid's
    PAUSED-short-circuit could never run and the guard fail-safed into permanent
    refusal. Found live on 2026-08-16 (DoD 5 two-sided test); unit tests missed
    it because the mocked cloudwatch client accepts an empty dimension.
    """
    try:
        return cluster_arn.split("cluster:")[1]
    except IndexError:
        return ""


def _idle_postgres(cluster_arn, cluster_id, secret_arn, db_name):
    """(idle, detail) — True if no runs are pending, or running with real
    work outstanding (a 'running' run with zero non-terminal instance rows —
    awaiting a manual /close — does not count, see the query below). Raises
    on error.

    S-2 hybrid: first read the ServerlessDatabaseCapacity CloudWatch metric
    (minimum over a recent window). min_capacity=0 + SecondsUntilAutoPause
    means an idle cluster sits at 0 ACU for >=300s, so a minimum of 0 over the
    window means it is PAUSED. A paused cluster has had no connection for that
    long => no active run — and the Data API cannot answer it anyway (it would
    error, garbaging the fail-safe into a permanent refuse). So paused => idle
    with no query, never waking the cluster.

    If the cluster is AWAKE (capacity > 0, or the metric has no datapoints yet
    — e.g. freshly deployed), fall through to the `runs` table query to catch a
    run registered as pending but not yet enqueued to SQS. The fail-safe
    direction is unchanged: an error reading the metric OR running the query
    propagates so the caller refuses.
    """
    # Step 1: capacity metric.
    now = datetime.now(UTC)
    metric = cloudwatch.get_metric_statistics(
        Namespace="AWS/RDS",
        MetricName="ServerlessDatabaseCapacity",
        Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
        StartTime=now - timedelta(minutes=15),
        EndTime=now,
        Period=60,
        Statistics=["Minimum"],
    )
    points = metric.get("Datapoints", [])
    if points and min(p["Minimum"] for p in points) == 0:
        # Paused: nothing has connected for >= SecondsUntilAutoPause.
        return True, {"postgres": "paused (capacity 0)"}

    # Step 2: cluster awake (or capacity unknown) -> query the runs table.
    #
    # BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md N1/N2: finalising a run
    # is now a deliberate operator action (POST /runs/{id}/close), so a
    # run can sit at status='running' indefinitely with genuinely nothing
    # left to do, awaiting review — that must not block scale-to-zero
    # forever. 'running' now also requires a real non-terminal
    # instance_results row to count as active; 'pending' (registered but not
    # yet dispatched — no instance_results rows exist yet at all) is left on
    # the old, always-active rule on purpose, so an about-to-launch run is
    # never mistaken for idle just because it hasn't seeded any rows yet.
    #
    # N1: this state list is a DUPLICATED literal, not an import — this
    # Lambda is a standalone deploy unit (json/os/datetime/boto3 only) and
    # cannot import swebench_eval.database.state_machine or
    # results_writer._ALL_NON_TERMINAL_STATES. Pinned against drift by
    # tests/test_scale_to_zero_guard.py, not by the type system.
    #
    # N2: this clause only ever runs in THIS branch — the paused fast path
    # above returns before any query at all, so a genuinely-parked cluster
    # (no connections for >=300s, which a restart's own write would itself
    # break) is never checked against it. That's correct, not a gap: nothing
    # could have called /restart against a paused cluster in that window
    # either. But it means this is one of two idle-detection paths, not "the"
    # mechanism — the paused branch above is the other, and reaches its
    # answer without ever running this query.
    resp = rds_data.execute_statement(
        resourceArn=cluster_arn,
        secretArn=secret_arn,
        database=db_name,
        sql=(
            "SELECT COUNT(*) FROM runs r WHERE "
            "(r.status = 'pending') OR "
            "(r.status = 'running' AND EXISTS ("
            "  SELECT 1 FROM instance_results "
            "  WHERE run_id = r.run_id "
            "    AND state IN ('PENDING','DISPATCHED','HARNESS_RUNNING','EVAL_RUNNING')"
            "))"
        ),
        includeResultMetadata=False,
    )
    records = resp.get("records", [])
    if not records or not records[0]:
        # Empty result set: conservative => not idle (refuse). Normally COUNT(*)
        # always returns a row (0), so this branch is the unexpected one.
        return False, {"postgres": "empty"}
    first = records[0][0]
    # Data API returns COUNT(*) as a longValue (or stringValue on some engines).
    if "longValue" in first:
        count = first["longValue"]
    elif "doubleValue" in first:
        count = int(first["doubleValue"])
    else:
        count = int(first.get("stringValue", "0") or 0)
    return (count == 0), {"postgres_active": count}


def _idle_sqs(queue_urls):
    """True if every queue is idle (visible + not-visible both zero)."""
    for url in queue_urls:
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        visible = int(attrs.get("ApproximateNumberOfMessages", "0"))
        not_visible = int(attrs.get("ApproximateNumberOfMessagesNotVisible", "0"))
        if visible or not_visible:
            return False, {"queue": url, "visible": visible, "notVisible": not_visible}
    return True, {}


def _idle_ecs(cluster_name, service_arns):
    """(idle, detail) — True if no target service has any RUNNING task.

    A service named in ECS_SERVICES that does NOT exist counts as idle: it has
    no tasks, and after a `terraform destroy` of the eval/ui tiers none of them
    do. Only ServiceNotFoundException/ClusterNotFoundException are swallowed;
    AccessDenied and throttling still raise, so the fail-safe refusal is intact.

    Why this matters (found live 2026-08-22): ECS_SERVICES carried
    `eval-dev-custom_minimal`, a service that has never existed. The loop
    short-circuits on the FIRST service with running tasks, and orchestrator-api
    sorts ahead of it, so every night refused before reaching the bad ARN and
    nothing surfaced it. The moment the fleet was genuinely idle — the only
    night the guard could ever have succeeded — it would have raised
    ServiceNotFoundException and refused instead. A check whose success path had
    never once executed.
    """
    absent = []
    for arn in service_arns:
        cluster, name = _parse_service_arn(arn)
        cluster = cluster or cluster_name
        if not name:
            continue
        try:
            tasks = ecs.list_tasks(
                cluster=cluster,
                serviceName=name,
                desiredStatus="RUNNING",
            )["taskArns"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in _ABSENT_CODES:
                raise
            absent.append(name)
            continue
        if tasks:
            return False, {"cluster": cluster, "service": name, "running": len(tasks)}
    return True, ({"absent_services": absent} if absent else {})


def _notify(topic_arn, subject, message):
    if not topic_arn:
        return
    try:
        sns.publish(TopicArn=topic_arn, Subject=subject, Message=message)
    except Exception as exc:  # noqa: BLE001 - notification must never break the run
        print(f"NOTIFY FAILED: {exc}")


def handler(event, _context):
    cluster_arn = os.environ.get("DB_CLUSTER_ARN", "")
    cluster_id = _cluster_id_from_arn(cluster_arn)
    secret_arn = os.environ.get("DB_SECRET_ARN", "")
    db_name = os.environ.get("DB_NAME", "app_control_plane")
    queue_urls = _env_json("QUEUE_URLS", [])
    cluster_name = os.environ.get("ECS_CLUSTER", "")
    services = _env_json("ECS_SERVICES", [])  # list of service ARN strings
    asg_arns = _env_json("ASG_ARNS", [])  # eval host ASGs to zero behind the same gate
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")

    refuse_reasons = []
    check_log = {}

    # 1. Postgres (S-2 hybrid: paused => idle; awake => query runs). Fail-safe:
    #    any error -> refuse.
    try:
        idle_db, db_detail = _idle_postgres(cluster_arn, cluster_id, secret_arn, db_name)
        # Log the DETAIL, not just "active". The old code discarded it
        # (``idle_db, _ =``), so six consecutive refusals all read
        # `"postgres": "active"` and could not distinguish "3 runs really are
        # pending" from "the query returned an empty result set" — two very
        # different problems with the same one-word symptom.
        check_log["postgres"] = db_detail if db_detail else ("idle" if idle_db else "active")
        if not idle_db:
            refuse_reasons.append(f"active runs in Postgres (pending/running) {db_detail}")
    except Exception as exc:  # noqa: BLE001
        check_log["postgres"] = f"error: {exc}"
        refuse_reasons.append(f"Postgres check ERRORS (fail-safe refuse): {exc}")

    # 2. SQS depth (visible + not-visible).
    try:
        idle_q, q_detail = _idle_sqs(queue_urls)
        check_log["sqs"] = q_detail if q_detail else "idle"
        if not idle_q:
            refuse_reasons.append(f"queued/in-flight work {q_detail}")
    except Exception as exc:  # noqa: BLE001
        check_log["sqs"] = f"error: {exc}"
        refuse_reasons.append(f"SQS check ERRORS (fail-safe refuse): {exc}")

    # 3. ECS running tasks.
    try:
        idle_e, e_detail = _idle_ecs(cluster_name, services)
        check_log["ecs"] = e_detail if e_detail else "idle"
        if not idle_e:
            refuse_reasons.append(f"running tasks {e_detail}")
    except Exception as exc:  # noqa: BLE001
        check_log["ecs"] = f"error: {exc}"
        refuse_reasons.append(f"ECS check ERRORS (fail-safe refuse): {exc}")

    # 3b. Cluster-wide running tasks — ONLY when we are about to zero the ASG
    #     (the host layer). A warm-job run-task is not tied to a service, so the
    #     service-scoped check above cannot see it; zeroing the ASG under one
    #     strands it. A running task anywhere in the cluster => refuse.
    if asg_arns:
        try:
            idle_c, c_detail = _idle_cluster_tasks(cluster_name)
            check_log["cluster_tasks"] = c_detail if c_detail else "idle"
            if not idle_c:
                refuse_reasons.append(f"running cluster tasks {c_detail}")
        except Exception as exc:  # noqa: BLE001
            check_log["cluster_tasks"] = f"error: {exc}"
            refuse_reasons.append(f"cluster-task check ERRORS (fail-safe refuse): {exc}")

    print(
        json.dumps(
            {
                "checks": check_log,
                "services": services,
                "refuse_reasons": refuse_reasons,
            },
            default=str,
        )
    )

    if refuse_reasons:
        msg = (
            "REQUIRED: nightly scale-to-zero REFUSED — work detected or a guard "
            f"errored, services left running.\nReasons: {refuse_reasons}"
        )
        print(msg)
        _notify(topic_arn, "[eval-dev] scale-to-zero REFUSED", msg)
        return {
            "statusCode": 200,
            "body": json.dumps({"refused": True, "reasons": refuse_reasons}, default=str),
        }

    # All idle: scale to zero. Services first, then the host layer (ASGs) —
    # zeroing the ASG under running tasks is exactly the refusal path above.
    results = []
    for arn in services:
        cluster, name = _parse_service_arn(arn)
        cluster = cluster or cluster_name
        try:
            ecs.update_service(
                cluster=cluster, service=name, desiredCount=0, forceNewDeployment=False
            )
            results.append({"service": name, "desiredCount": 0, "ok": True})
        except ClientError as exc:
            # Absent service: already at zero in every sense that matters. Not a
            # partial failure — reporting it as one would page on a healthy
            # torn-down environment.
            if exc.response.get("Error", {}).get("Code") in _ABSENT_CODES:
                results.append({"service": name, "absent": True, "ok": True})
            else:
                results.append({"service": name, "desiredCount": 0, "ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surface per-service failure
            results.append({"service": name, "desiredCount": 0, "ok": False, "error": str(exc)})

    # The host layer (A-6): the eval ASG. Only reached after EVERY idle check
    # passed — including the cluster-wide task check above.
    for arn in asg_arns:
        name = _asg_name_from_arn(arn)
        try:
            autoscaling.set_desired_capacity(AutoScalingGroupName=name, DesiredCapacity=0)
            results.append({"asg": name, "desired": 0, "ok": True})
        except Exception as exc:  # noqa: BLE001 - surface per-ASG failure
            results.append({"asg": name, "desired": 0, "ok": False, "error": str(exc)})

    ok = all(r["ok"] for r in results) if results else True
    status = "ok" if ok else "partial"
    msg = f"Nightly scale-to-zero ran; all idle checks passed. Results: {results}"
    print(json.dumps({"scaled": results}))
    topic = (
        f"[eval-dev] scale-to-zero: {status}"
        if results
        else "[eval-dev] scale-to-zero: no services configured (idle confirmed)"
    )
    _notify(topic_arn, topic, msg)
    return {
        "statusCode": 200,
        "body": json.dumps({"refused": False, "scaled": results}, default=str),
    }
