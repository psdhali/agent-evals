"""SQS and S3 client wrappers.

Pointed at the local ElasticMQ and MinIO services in Docker Compose.
Phase 5 swaps the endpoint URLs to real AWS — no code change anywhere else.

The swap is detected, not configured per-call: inside an ECS task or Lambda
container boto3 runs with the task role (AWS_CONTAINER_CREDENTIALS_RELATIVE_URI
set — no static creds, no localhost endpoint override, real SQS/S3). Outside a
container the local ElasticMQ/MinIO endpoints with dummy creds are used, exactly
as in Phases 1-4. The one thing a task must supply is ``AWS_DEFAULT_REGION``.
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass
from typing import Any

# ECS tasks and Lambda containers both run with these env vars set by the
# platform when the task/function carries an execution role. Their presence is
# the "real AWS" signal; local dev never sees them.
_AWS_CONTAINER_MARKERS = (
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_LAMBDA_RUNTIME_API",
)


def _running_in_aws() -> bool:
    """True inside an ECS task / Lambda, OR when the operator says so.

    Adoption Phase 2 finding 9: the laptop-side steps of `make images` (the cache manifest,
    the task families) read and write the REAL dataset bucket, but outside a container this
    module defaults to the compose stack's MinIO — the manifest writer connected to
    localhost:9000 and died. ``EVAL_REAL_AWS=1`` (set by scripts/adopt.py for everything it
    runs) selects the real endpoints with the ambient credential chain (the AWS profile).
    """
    if os.environ.get("EVAL_REAL_AWS", "").strip() in ("1", "true", "yes"):
        return True
    return any(os.environ.get(marker) for marker in _AWS_CONTAINER_MARKERS)


def _sqs_endpoint() -> str | None:
    return (
        None if _running_in_aws() else os.environ.get("SQS_ENDPOINT_URL", "http://localhost:9324")
    )


def _s3_endpoint() -> str | None:
    return None if _running_in_aws() else os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")


def _aws_region() -> str:
    return os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def _queue_prefix() -> str:
    """Prefix applied to bare queue names in AWS ('' locally).

    The code says ``harness-jobs`` / ``eval-jobs`` / ``results`` everywhere; the
    deployed queues are ``eval-dev-<name>``. The prefix is supplied by the task
    definition environment (``SQS_QUEUE_PREFIX``), so no call site changes when
    the piece moves from the local compose stack to AWS.
    """
    return os.environ.get("SQS_QUEUE_PREFIX", "")


# ── SQS ──────────────────────────────────────────────────────────────


def get_sqs_client() -> Any:
    """Return a boto3 SQS client.

    Inside an AWS container: default boto3 (real SQS endpoint, task-role
    credentials). Otherwise the local ElasticMQ endpoint with dummy creds.
    """
    import boto3

    if _running_in_aws():
        return boto3.client("sqs", region_name=_aws_region())
    return boto3.client(
        "sqs",
        endpoint_url=_sqs_endpoint(),
        region_name=_aws_region(),
        aws_access_key_id="dummy",
        aws_secret_access_key="dummy",
    )


def get_queue_url(queue_name: str) -> str:
    """Resolve a queue URL by name, applying SQS_QUEUE_PREFIX if set."""
    client = get_sqs_client()
    return str(client.get_queue_url(QueueName=_queue_prefix() + queue_name)["QueueUrl"])


def send_message(queue_name: str, body: dict[str, object]) -> str:
    """Enqueue a JSON message.  Returns the message ID."""
    client = get_sqs_client()
    url = get_queue_url(queue_name)
    resp = client.send_message(QueueUrl=url, MessageBody=json.dumps(body))
    return str(resp["MessageId"])


def receive_message(
    queue_name: str,
    wait_seconds: int = 20,
    visibility_timeout: int | None = None,
) -> dict[str, Any] | None:
    """Receive one message with long polling.  Returns None if the queue is empty."""
    client = get_sqs_client()
    url = get_queue_url(queue_name)
    kwargs: dict[str, Any] = {
        "QueueUrl": url,
        "MaxNumberOfMessages": 1,
        "WaitTimeSeconds": wait_seconds,
    }
    # ADR-0037 / M0 §4: queue_wait_s needs SentTimestamp →
    # ApproximateFirstReceiveTimestamp, both SQS system attributes delivered on
    # receive.  Local ElasticMQ omits them (the callers then write NULL — Trap 3).
    kwargs["MessageSystemAttributeNames"] = [
        "SentTimestamp",
        "ApproximateFirstReceiveTimestamp",
        # Abort 2026-09-04: the dispatcher's discard-on-abort tells a redelivered message
        # (a launched task that was stopped before reporting) from a never-received one.
        "ApproximateReceiveCount",
    ]
    if visibility_timeout is not None:
        kwargs["VisibilityTimeout"] = visibility_timeout
    resp = client.receive_message(**kwargs)
    messages = resp.get("Messages", [])
    if not messages:
        return None
    msg = messages[0]
    return {
        "message_id": msg["MessageId"],
        "receipt_handle": msg["ReceiptHandle"],
        "body": json.loads(msg["Body"]),
        "attributes": msg.get("Attributes", {}),
    }


def delete_message(queue_name: str, receipt_handle: str) -> None:
    """Acknowledge (delete) a received message."""
    client = get_sqs_client()
    url = get_queue_url(queue_name)
    client.delete_message(QueueUrl=url, ReceiptHandle=receipt_handle)


def change_message_visibility(queue_name: str, receipt_handle: str, timeout_seconds: int) -> None:
    """Extend the visibility timeout of an in-flight message (heartbeat)."""
    client = get_sqs_client()
    url = get_queue_url(queue_name)
    client.change_message_visibility(
        QueueUrl=url,
        ReceiptHandle=receipt_handle,
        VisibilityTimeout=timeout_seconds,
    )


# ── SQS depth readers (M2.1 — the incident view) ───────────────────────


@dataclass(frozen=True)
class QueueDepth:
    """Depth reading for one queue (M2.1).

    ``visible`` vs ``not_visible`` is the difference between "50 waiting"
    (capacity) and "50 stuck in flight" (an incident) — never collapse them into
    one number.  ``oldest_age_s`` comes from CloudWatch at 1-minute granularity,
    not from ``GetQueueAttributes``; ``None`` when unavailable rather than 0
    (a zero reads as "nothing is old", the opposite of "we don't know").
    """

    visible: int  # ApproximateNumberOfMessages
    not_visible: int  # ApproximateNumberOfMessagesNotVisible
    oldest_age_s: int | None  # CloudWatch ApproximateAgeOfOldestMessage


def get_cloudwatch_client() -> Any:
    """Return a boto3 CloudWatch client (or ``None`` when not running in AWS).

    Locally there is no CloudWatch emulator, so depth readers that source a
    metric from CloudWatch return ``None`` instead of poking at real AWS.
    """
    if not _running_in_aws():
        return None
    import boto3

    return boto3.client("cloudwatch", region_name=_aws_region())


def _oldest_age_s(queue_name: str, max_age_minutes: int = 5) -> int | None:
    """ApproximateAgeOfOldestMessage for *queue_name*, newest datapoint.

    CloudWatch publishes SQS age metrics on a 1-minute cadence.  A missing
    datapoint, a CloudWatch error, or not running in AWS all return ``None`` —
    never 0, so a caller cannot mistake "we don't know" for "nothing is old".
    """
    cw = get_cloudwatch_client()
    if cw is None:
        return None
    try:
        end = datetime.datetime.now(datetime.UTC)
        start = end - datetime.timedelta(minutes=max_age_minutes)
        resp = cw.get_metric_statistics(
            Namespace="AWS/SQS",
            MetricName="ApproximateAgeOfOldestMessage",
            Dimensions=[{"Name": "QueueName", "Value": _queue_prefix() + queue_name}],
            StartTime=start,
            EndTime=end,
            Period=60,
            Statistics=["Maximum"],
        )
    except Exception:  # noqa: BLE001 - an age we cannot know must read "unknown", not fail
        return None
    points = sorted(resp.get("Datapoints", []), key=lambda p: p["Timestamp"])
    if not points:
        return None
    value = points[-1].get("Maximum")
    return int(value) if value is not None else None


def get_queue_depth(queue_name: str) -> QueueDepth:
    """Depth + oldest-age for one queue: visible, in-flight, age (M2.1).

    The two ``ApproximateNumberOfMessages`` variants are read as separate
    numbers on purpose — see ``QueueDepth``.
    """
    client = get_sqs_client()
    url = get_queue_url(queue_name)
    attrs = client.get_queue_attributes(
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return QueueDepth(
        visible=int(attrs.get("ApproximateNumberOfMessages", 0)),
        not_visible=int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
        oldest_age_s=_oldest_age_s(queue_name),
    )


def get_dlq_depth(queue_name: str) -> int:
    """ApproximateNumberOfMessages for the queue's dead-letter queue.

    A DLQ depth above zero is an alarm, not a stat (M2.6 / ADR-0034 §3): a
    gated receive that increments receive counts can redrive the whole backlog
    to the DLQ within seconds, and the queue panel is how an operator would see
    that happening.
    """
    client = get_sqs_client()
    url = get_queue_url(queue_name + "-dlq")
    attrs = client.get_queue_attributes(
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages"],
    )["Attributes"]
    return int(attrs.get("ApproximateNumberOfMessages", 0))


# ── S3 ───────────────────────────────────────────────────────────────


def get_s3_client() -> Any:
    """Return a boto3 S3 client.

    Inside an AWS container: default boto3 (real S3, task-role credentials).
    Otherwise the local MinIO endpoint with minioadmin creds.
    """
    import boto3

    if _running_in_aws():
        return boto3.client("s3", region_name=_aws_region())
    return boto3.client(
        "s3",
        endpoint_url=_s3_endpoint(),
        region_name=_aws_region(),
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin",
    )


def upload_artifact(
    bucket: str, key: str, data: str | bytes, metadata: dict[str, str] | None = None
) -> str:
    """Upload an object to MinIO/S3.  Returns the object key.

    ``metadata`` becomes S3 object metadata (architecture §4 / ADR-0016): the
    forcefully-killed partial patch is tagged with its ``terminated_reason`` so
    Phase 7's dashboard reads it distinctly from a graded diff.
    """
    client = get_s3_client()
    if isinstance(data, str):
        data = data.encode("utf-8")
    kwargs: dict[str, object] = {"Bucket": bucket, "Key": key, "Body": data}
    if metadata:
        kwargs["Metadata"] = metadata
    client.put_object(**kwargs)
    return key


def get_artifact(bucket: str, key: str) -> bytes:
    """Download an object from MinIO/S3."""
    client = get_s3_client()
    resp = client.get_object(Bucket=bucket, Key=key)
    return bytes(resp["Body"].read())
