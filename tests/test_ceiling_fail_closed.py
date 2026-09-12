"""Test the ADR-0019 fail-closed ceiling rule (R4-4).

A job message that omits a ceiling must error, not silently resolve to
"unlimited" — the natural shape of this bug is silence, so an assertion that
something FAILS is the only evidence that counts.  The worker parses with
strict `body[...]` (not `body.get`), so a dropped field raises KeyError.
"""

from __future__ import annotations

import pytest

from swebench_eval.orchestrator.run_config import (
    DEFAULT_MAX_TOKENS_PER_INSTANCE,
    DEFAULT_MAX_TURNS_PER_INSTANCE,
)
from swebench_eval.workers.harness_worker import _parse_harness_job


def _job_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "run_id": "r1",
        "instance_id": "i1",
        "repo_url": "https://github.com/x/y",
        "base_commit": "abc123",
        "problem_statement": "fix it",
        "attempt_number": 1,
        "harness_name": "custom_minimal",
        "model_alias": "cheap-oss-model",
        "timeout_seconds": 300,
        "max_tokens_per_instance": DEFAULT_MAX_TOKENS_PER_INSTANCE,
        "max_cost_usd_per_instance": 5.0,
        "max_turns_per_instance": DEFAULT_MAX_TURNS_PER_INSTANCE,
    }
    body.update(overrides)
    return body


def test_parse_requires_token_ceiling() -> None:
    """A job missing max_tokens_per_instance must raise, not run unmetered."""
    body = _job_body()
    del body["max_tokens_per_instance"]
    with pytest.raises(KeyError):
        _parse_harness_job(body)


def test_parse_requires_cost_ceiling() -> None:
    """A job missing max_cost_usd_per_instance must raise."""
    body = _job_body()
    del body["max_cost_usd_per_instance"]
    with pytest.raises(KeyError):
        _parse_harness_job(body)


def test_explicit_none_token_means_unlimited() -> None:
    """None is allowed only as an explicit operator choice = deliberately unlimited."""
    job = _parse_harness_job(_job_body(max_tokens_per_instance=None))
    assert job.max_tokens_per_instance is None


def test_full_body_parses() -> None:
    """A well-formed job message parses cleanly."""
    job = _parse_harness_job(_job_body())
    assert job.max_tokens_per_instance == DEFAULT_MAX_TOKENS_PER_INSTANCE
    assert job.max_cost_usd_per_instance == 5.0
