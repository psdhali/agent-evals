"""custom_minimal in-loop retry (parity with the CLI harnesses).

The CLI harnesses (OpenCode/Claude/Codex) retry transient model-call failures
inside their clients; custom_minimal disables the SDK retries (max_retries=0)
so it landed one transient 504 as a terminal FAILED_HARNESS. This closes that
asymmetry: transient errors (408/429/5xx, connection errors, timeouts) are
retried in-loop with bounded, deadline-aware backoff; auth/4xx fail fast.
"""

from __future__ import annotations

import time
from unittest import mock

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from swebench_eval.harnesses.custom_minimal.harness import (
    _call_with_retry,
    _is_retriable,
)


def _status_error(code: int) -> APIStatusError:
    req = httpx.Request("POST", "http://gateway/v1")
    resp = httpx.Response(code, request=req)
    return APIStatusError("boom", response=resp, body=None)


def _conn_error() -> Exception:
    req = httpx.Request("POST", "http://gateway/v1")
    return APIConnectionError(message="conn ref", request=req)


class _FakeCompletions:
    def __init__(self, errors: list[Exception]) -> None:
        self.errors = list(errors)
        self.calls = 0

    def create(self, **kwargs: object) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "response-ok"


class _FakeClient:
    def __init__(self, errors: list[Exception]) -> None:
        self.chat = mock.Mock()
        self.chat.completions = _FakeCompletions(errors)


# --- _is_retriable ----------------------------------------------------------
def test_retriable_statuses() -> None:
    for code in (408, 429, 500, 503, 504):
        assert _is_retriable(_status_error(code)), f"{code} should be retriable"


def test_non_retriable_4xx() -> None:
    for code in (400, 401, 403, 404, 409, 422):
        assert not _is_retriable(_status_error(code)), f"{code} should NOT retry"


def test_connection_error_retriable() -> None:
    assert _is_retriable(_conn_error())


def test_timeout_and_generic_are_retriable() -> None:
    assert _is_retriable(httpx.ReadTimeout("stalled"))
    assert _is_retriable(TimeoutError("stalled"))
    assert _is_retriable(httpx.ConnectTimeout("conn"))


# --- _call_with_retry -------------------------------------------------------
def test_retries_transient_then_succeeds() -> None:
    client = _FakeClient([_status_error(503), _status_error(429), _conn_error()])
    retries: list[int] = []
    with mock.patch("time.sleep"):
        result = _call_with_retry(
            client,
            deadline=time.monotonic() + 3600,
            max_retries=5,
            base_delay=1.0,
            max_delay=12.0,
            per_call_read=60.0,
            on_retry=lambda n, d, e: retries.append(n),
            messages=[],
        )
    assert result == "response-ok"
    assert client.chat.completions.calls == 4  # 3 failures + 1 success
    assert retries == [1, 2, 3]  # one backoff per failure


def test_non_retriable_fails_immediately() -> None:
    client = _FakeClient([_status_error(401)])
    retries: list[int] = []
    with mock.patch("time.sleep"), pytest.raises(APIStatusError):
        _call_with_retry(
            client,
            deadline=time.monotonic() + 60,
            max_retries=5,
            base_delay=1.0,
            max_delay=12.0,
            per_call_read=60.0,
            on_retry=lambda n, d, e: retries.append(n),
            messages=[],
        )
    assert client.chat.completions.calls == 1  # no retry after a 401
    assert retries == []


def test_retries_bounded_by_max() -> None:
    client = _FakeClient([_status_error(503)] * 20)  # never succeeds
    retries: list[int] = []
    with mock.patch("time.sleep"), pytest.raises(APIStatusError):
        _call_with_retry(
            client,
            deadline=time.monotonic() + 3600,
            max_retries=3,
            base_delay=1.0,
            max_delay=12.0,
            per_call_read=60.0,
            on_retry=lambda n, d, e: retries.append(n),
            messages=[],
        )
    assert client.chat.completions.calls == 4  # initial + 3 retries
    assert retries == [1, 2, 3]


def test_deadline_prevents_backoff_overshoot() -> None:
    """If a backoff sleep would blow the remaining budget, give up at once."""
    client = _FakeClient([_status_error(503), _status_error(503)])
    retries: list[int] = []
    # deadline has already passed -> no delay can be afforded -> fail fast.
    with mock.patch("time.sleep") as sleep, pytest.raises(APIStatusError):
        _call_with_retry(
            client,
            deadline=time.monotonic() - 1,
            max_retries=5,
            base_delay=1.0,
            max_delay=12.0,
            per_call_read=60.0,
            on_retry=lambda n, d, e: retries.append(n),
            messages=[],
        )
    sleep.assert_not_called()
    assert client.chat.completions.calls == 1  # first failure, then gave up
