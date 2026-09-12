"""M0 §4/§5 phase-timing helpers — measured, never invented.

This module holds the pure parsing/math for the phase-timing columns
(``queue_wait_s``/``provision_s``/... on ``instance_results``), separated from
the worker code so every boundary is unit-testable without ECS.  The rule from
M0 §5 applies everywhere: a boundary that could not be measured stays NULL
(Trap 3); an invented number is never written.

Two measurements come from the ECS **task metadata endpoint v4**
(``ECS_CONTAINER_METADATA_URI_V4/task``, link-local 169.254.170.2 — no IAM, no
VPC endpoint, works unchanged under A1 isolation, M0 §4.1):

- ``PullStartedAt`` / ``PullStoppedAt`` — the image-pull window.
- container ``CreatedAt`` / ``StartedAt`` — the provision window and when ECS
  started the container.

``queue_wait_s`` comes from SQS system attributes
(``SentTimestamp`` → ``ApproximateFirstReceiveTimestamp``).

Anything not derivable here — locally, off-ECS, or on failure — yields None.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def _epoch(value: Any) -> float | None:
    """One timestamp in (epoch-seconds | epoch-millis | ISO-8601) → epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        # Epoch millis (13 digits) are a real shape from introspec meters; the
        # metadata endpoint itself uses ISO strings, but never guess-wrong:
        # treat ≥ 1e12 as millis only when the value is obviously a timestamp.
        if f >= 1e12:
            f /= 1000.0
        return f
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            return _epoch(float(s))
        # ISO-8601: Python 3.11+ fromisoformat accepts a bare "Z" suffix.
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            logger.warning("timing: unparseable timestamp %r", value)
            return None
        return dt.timestamp()
    return None


def seconds_between(start: Any, end: Any) -> float | None:
    """Wall seconds from *start* to *end*; None unless both are known and end ≥ start."""
    a, b = _epoch(start), _epoch(end)
    if a is None or b is None:
        return None
    delta = b - a
    # A negative delta is a clock/ordering anomaly — better a NULL than a lie.
    return delta if delta >= 0 else None


def parse_sqs_attributes(attributes: dict[str, str] | None) -> tuple[float | None, float | None]:
    """(sent_epoch, first_received_epoch) from SQS system attributes.

    SQS returns both as epoch-millis strings (``SentTimestamp`` and
    ``ApproximateFirstReceiveTimestamp``); local ElasticMQ may omit them.
    """
    if not attributes:
        return None, None
    sent = _epoch(attributes.get("SentTimestamp"))
    received = _epoch(attributes.get("ApproximateFirstReceiveTimestamp"))
    return sent, received


@dataclass
class TaskTiming:
    """The parts of the ECS task-metadata endpoint the phase columns need.

    All fields are epoch-seconds floats; None when the endpoint did not carry
    them.  ``image_pull_cold`` is a pure flag: a pull window present means the
    image was pulled this task.
    """

    pull_started_at: float | None = None
    pull_stopped_at: float | None = None
    container_created_at: float | None = None
    container_started_at: float | None = None

    @property
    def image_pull_cold(self) -> bool | None:
        # A pull window present means the image was pulled this task (cold).
        # Absent is AMBIGUOUS (no metadata vs a warm layer reuse) — return None
        # (Trap 3: unknown, not a fabricated False).  N-2 (review): on the
        # harness tier this is True on every ECS row (and NULL off-ECS), which
        # carries no information per M0 §4.5 — Fargate is ALWAYS cold, so the
        # column's variance lives on the eval tier only (which writes
        # eval_image_pull_cold, NULL-by-design).  The comment documents what the
        # column does NOT convey so a later reader does not mistake a constant
        # True for a measurement.
        return True if self.pull_started_at is not None else None

    @property
    def image_pull_s(self) -> float | None:
        return seconds_between(self.pull_started_at, self.pull_stopped_at)


def parse_task_metadata(raw: str | dict[str, Any]) -> TaskTiming:
    """Parse the ``/task`` endpoint JSON into a :class:`TaskTiming`.

    The endpoint returns camelCase fields: ``pullStartedAt`` / ``pullStoppedAt``
    at the top level and per-container ``createdAt`` / ``startedAt`` in
    ``containers[]`` (the first container is this task's — a task-per-job
    worker has exactly one).  Accepts already-decoded dicts too, for tests.
    """
    obj = raw if isinstance(raw, dict) else _decode(raw)
    containers = obj.get("containers") or []
    container = containers[0] if containers else {}
    return TaskTiming(
        pull_started_at=_epoch(obj.get("pullStartedAt") or obj.get("PullStartedAt")),
        pull_stopped_at=_epoch(obj.get("pullStoppedAt") or obj.get("PullStoppedAt")),
        container_created_at=_epoch(container.get("createdAt") or container.get("CreatedAt")),
        container_started_at=_epoch(container.get("startedAt") or container.get("StartedAt")),
    )


def _decode(raw: str) -> dict[str, Any]:
    try:
        import json

        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def fetch_task_metadata() -> TaskTiming | None:
    """Fetch the ECS task metadata endpoint; None when not running under ECS.

    ``ECS_CONTAINER_METADATA_URI_V4`` is injected by the ECS agent into every
    task.  Its absence means local dev / tests → None (Trap 3 discipline: a
    missing measurement is NULL, never a fabricated number).  Failures log and
    return None — timing must never crash the worker.
    """
    import os

    uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if not uri:
        return None
    try:
        import httpx

        resp = httpx.get(f"{uri.rstrip('/')}/task", timeout=5.0)
        resp.raise_for_status()
        return parse_task_metadata(resp.text)
    except Exception:
        logger.warning("timing: ECS task metadata fetch failed", exc_info=True)
        return None


def now_epoch() -> float:
    return datetime.now(UTC).timestamp()
