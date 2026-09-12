"""EVAL_REAL_AWS=1 makes the queue/S3 clients target the real account from a laptop (finding 9)."""

from __future__ import annotations

import pytest

from swebench_eval.queue import client


def test_default_outside_a_container_is_the_compose_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("EVAL_REAL_AWS", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_LAMBDA_RUNTIME_API"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    assert client._running_in_aws() is False
    assert client._s3_endpoint() == "http://localhost:9000"


def test_eval_real_aws_selects_real_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_REAL_AWS", "1")
    assert client._running_in_aws() is True
    assert client._s3_endpoint() is None
    assert client._sqs_endpoint() is None
