"""ADR-0037 / M0 §4/§5 — phase-timing helpers (pure math/parsing, no ECS).

The ECS task-metadata endpoint and SQS system attributes are live-only, but the
parsing and the seconds-diff math are pure and unit-testable here.  Every
un-measurable path returns None (Trap 3): tests assert the NULL discipline too.
"""

from __future__ import annotations

from swebench_eval.workers import timing as tmod


def test_seconds_between_handles_epoch_ms_and_iso() -> None:
    # epoch-millis (SQS shape).
    assert tmod.seconds_between(1_700_000_000_000, 1_700_000_060_000) == 60.0
    # ISO strings with Z (ECS metadata shape).
    assert tmod.seconds_between("2026-08-19T12:00:00Z", "2026-08-19T12:01:00Z") == 60.0
    # None when a side is missing.
    assert tmod.seconds_between(None, 180) is None
    assert tmod.seconds_between(100, None) is None


def test_seconds_between_never_negative() -> None:
    # A clock/ordering anomaly → NULL, never a lie (a negative "elapsed" is
    # worse than none).
    assert tmod.seconds_between(200, 100) is None


def test_parse_sqs_attributes_epoch_millis() -> None:
    attrs = {
        "SentTimestamp": "1700000000000",
        "ApproximateFirstReceiveTimestamp": "1700000060000",
    }
    sent, received = tmod.parse_sqs_attributes(attrs)
    # epoch-millis → epoch-seconds.
    assert sent == 1_700_000_000.0
    assert received == 1_700_000_060.0


def test_parse_sqs_attributes_missing() -> None:
    assert tmod.parse_sqs_attributes(None) == (None, None)
    assert tmod.parse_sqs_attributes({}) == (None, None)


def test_parse_task_metadata_extracts_durations() -> None:
    raw = {
        "pullStartedAt": "2026-08-19T11:00:00Z",
        "pullStoppedAt": "2026-08-19T11:00:30Z",
        "containers": [{"createdAt": "2026-08-19T11:00:31Z", "startedAt": "2026-08-19T11:00:32Z"}],
    }
    meta = tmod.parse_task_metadata(raw)
    assert meta.image_pull_s == 30.0
    # A pull window present -> cold (Fargate pulls fresh every task).
    assert meta.image_pull_cold is True
    assert meta.container_created_at is not None
    assert meta.container_started_at is not None


def test_parse_task_metadata_absent_means_null_flags() -> None:
    meta = tmod.parse_task_metadata({})  # off-ECS / empty
    assert meta.image_pull_s is None
    assert meta.image_pull_cold is None


def test_fetch_task_metadata_returns_none_off_ecs(monkeypatch) -> None:
    # No ECS_CONTAINER_METADATA_URI_V4 -> None, without any network call.
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
    assert tmod.fetch_task_metadata() is None


def test_parse_accepts_str_metadata() -> None:
    raw = (
        '{"pullStartedAt":"2026-08-19T11:00:00Z",'
        '"pullStoppedAt":"2026-08-19T11:00:10Z",'
        '"containers":[{"createdAt":"2026-08-19T11:00:11Z","startedAt":"2026-08-19T11:00:12Z"}]}'
    )
    meta = tmod.parse_task_metadata(raw)
    assert meta.image_pull_s == 10.0
