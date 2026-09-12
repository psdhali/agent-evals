"""swebench_eval.aws_names — the one source of region / prefix / derived names."""

from __future__ import annotations

import pytest

from swebench_eval import aws_names


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    aws_names.account_id.cache_clear()


def test_region_prefers_aws_region_then_default_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    assert aws_names.region() == "eu-central-1"
    monkeypatch.delenv("AWS_REGION")
    assert aws_names.region() == "us-east-2"


def test_region_falls_back_to_default_when_nothing_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    assert aws_names.region() == aws_names.DEFAULT_REGION


def test_prefix_and_derived_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_ENV_PREFIX", "acme-prod")
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    assert aws_names.name_prefix() == "acme-prod"
    assert aws_names.named("cluster") == "acme-prod-cluster"
    assert aws_names.dataset_bucket_default() == "acme-prod-dataset-123456789012-us-east-2"
    assert aws_names.artifacts_bucket_default() == "acme-prod-artifacts-123456789012-us-east-2"
    assert aws_names.ecr_registry() == "123456789012.dkr.ecr.us-east-2.amazonaws.com"
    assert (
        aws_names.ecr_repository("harness-worker")
        == "123456789012.dkr.ecr.us-east-2.amazonaws.com/acme-prod-harness-worker"
    )


def test_prefix_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_ENV_PREFIX", raising=False)
    assert aws_names.name_prefix() == "eval-dev"
    monkeypatch.setenv("EVAL_ENV_PREFIX", "   ")
    assert aws_names.name_prefix() == "eval-dev"
