"""Test that every harness adapter resolves its base URL from the shared routing helper.

R5-1 / P4C-2: the adapter list is DERIVED from the one adapter map
(``swebench_eval.harnesses.registry.HARNESS_ADAPTERS``), never a hand-written
duplicate — that is what makes it impossible to omit an adapter.  Every adapter
reads its base URL from ONE shared helper (``routing.gateway_base_url``) rather
than a hardcoded literal, and the shim-routing membership is correct on both
ends: ``mini_swe_agent`` in, ``custom_minimal`` out.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from swebench_eval.harnesses.registry import HARNESS_ADAPTERS, SHIM_ROUTED_HARNESSES
from swebench_eval.harnesses.routing import gateway_base_url


def _all_adapters() -> list[Any]:
    """Instantiate every registered adapter with no base-URL override."""
    return [cls() for cls in HARNESS_ADAPTERS.values()]


def _shim_routed_adapters() -> list[Any]:
    """Instantiate every shim-routed (subprocess) adapter with no override."""
    return [HARNESS_ADAPTERS[name]() for name in SHIM_ROUTED_HARNESSES]


def test_shim_routed_membership() -> None:
    """The shim is the SOLE meter for every harness (ADR-0037 / M0 §1).

    custom_minimal is now shim-routed TOO — ADR-0037 revisits R5-2: that
    exclusion was about ENFORCEMENT only (custom_minimal keeps its in-loop
    budget trip), never about measurement.  One meter across all six harnesses
    is what makes a harness comparison a comparison of harnesses, not of their
    instrumentation.
    """
    from swebench_eval.harnesses.registry import HARNESS_ADAPTERS

    # Every registered harness is shim-metered — no subtraction hides one.
    assert SHIM_ROUTED_HARNESSES == frozenset(HARNESS_ADAPTERS)
    assert set(SHIM_ROUTED_HARNESSES) == {
        "mini_swe_agent",
        "aider",
        "claude_code",
        "codex",
        "opencode",
        "custom_minimal",
    }


def test_adapters_default_to_shared_gateway_base_url() -> None:
    """Every adapter's default base URL derives from the shared routing helper.

    With no override, the helper's env resolution is the adapter's address.  If an
    adapter hardcoded a different URL (or drifted), this fails.
    """
    expected = gateway_base_url().rstrip("/")
    expected_no_v1 = expected.removesuffix("/v1")
    for adapter in _all_adapters():
        base = adapter._api_base_url
        # Most adapters derive from the helper; Claude Code additionally strips
        # /v1 (Anthropic wire format).  The adapter's base must be prefix-equivalent.
        assert (
            base == expected or base == expected_no_v1
        ), f"{type(adapter).__name__} default {base!r} != helper {expected!r}"


def test_adapters_follow_env_override() -> None:
    """Setting LITELLM_BASE_URL moves every adapter's default with one change."""
    with mock.patch.dict("os.environ", {"LITELLM_BASE_URL": "http://shim:1234/v1"}):
        expected = gateway_base_url()
        assert expected == "http://shim:1234/v1"
        for adapter in _all_adapters():
            base = adapter._api_base_url
            assert base.startswith(
                "http://shim:1234"
            ), f"{type(adapter).__name__} did not follow env override: {base!r}"


def test_shim_routed_adapters_follow_env_override() -> None:
    """The shim-routed set (mini in, custom_minimal out) follows the shim env."""
    with mock.patch.dict("os.environ", {"LITELLM_BASE_URL": "http://shim:1234/v1"}):
        for adapter in _shim_routed_adapters():
            base = adapter._api_base_url
            assert base.startswith(
                "http://shim:1234"
            ), f"{type(adapter).__name__} did not follow shim env: {base!r}"


def test_agent_environment_denylist_strips_dangerous_and_keeps_the_rest() -> None:
    """E1 + E2 (agent-env-denylist-handover): the agent env must NOT inherit the
    dangerous names (AWS_*/ECS_* credential prefixes, LITELLM_* incl. the
    unmetered LITELLM_BASE_URL, Redis/SQS/DB config, ARTIFACTS/DATASET buckets)
    — AND everything else, especially the testbed-shaped vars the env images
    need (CONDA_PREFIX, LD_LIBRARY_PATH, PYTHONPATH), must SURVIVE.  The
    survive half is the point of the denylist inversion; a stripping-only test
    would pass on an allowlist too."""
    from swebench_eval.harnesses.routing import agent_environment

    full_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "USER": "root",
        "CONDA_PREFIX": "/opt/miniconda3/envs/testbed",
        "LD_LIBRARY_PATH": "/usr/lib",
        "PYTHONPATH": "/testbed",
        "LITELLM_BASE_URL": "http://gateway:4000/v1",
        "LITELLM_MASTER_KEY": "sk-real-master",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc",
        "AWS_ACCESS_KEY_ID": "AKIAFAKE",
        "ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/…",
        "DATASET_BUCKET": "eval-dev-dataset-…",
        "ARTIFACTS_BUCKET": "eval-dev-artifacts-…",
        "SQS_QUEUE_PREFIX": "eval-dev-",
        "DATABASE_URL": "postgres://…",
        "RESULTS_QUEUE_URL": "https://sqs…",
        "REDIS_URL": "redis://…",
    }
    with mock.patch.dict("os.environ", full_env, clear=True):
        env = agent_environment()

    # The dangerous names + AWS_*/ECS_* prefixes must be gone.
    for banned in (
        "LITELLM_BASE_URL",
        "LITELLM_MASTER_KEY",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_ACCESS_KEY_ID",
        "ECS_CONTAINER_METADATA_URI_V4",
        "DATASET_BUCKET",
        "ARTIFACTS_BUCKET",
        "SQS_QUEUE_PREFIX",
        "DATABASE_URL",
        "RESULTS_QUEUE_URL",
        "REDIS_URL",
    ):
        assert banned not in env, f"{banned} leaked into the agent env"
    # The safe + testbed-shaped vars survive (the point of the inversion).
    for kept in ("PATH", "HOME", "LANG", "USER", "CONDA_PREFIX", "LD_LIBRARY_PATH", "PYTHONPATH"):
        assert env[kept] == full_env[kept], f"{kept} was dropped by the denylist"


def test_is_operator_block_response_matches_the_real_litellm_body() -> None:
    """BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §1/§9.2: pin the
    EXACT body captured live against ``litellm:main-stable`` 2026-08-31
    (mint -> block -> call), not an invented shape."""
    from swebench_eval.harnesses.routing import is_operator_block_response

    real_body = (
        b'{"error":{"message":"Authentication Error, Key is blocked. Update via '
        b'`/key/unblock` if you\'re an admin.","type":"auth_error","param":"None","code":"401"}}'
    )
    assert is_operator_block_response(401, real_body) is True


def test_is_operator_block_response_does_not_misclassify_a_genuinely_bad_key() -> None:
    """§9.2 negative case: an unrelated/expired key's 401 (``token_not_found_
    in_db``, no "blocked" wording) must NOT read as an operator pause — the
    same data corruption M1.11's is_framework_paused_response test guards
    against, run backwards for this second marker."""
    from swebench_eval.harnesses.routing import is_operator_block_response

    bad_key_body = (
        b'{"error":{"message":"Authentication Error, token_not_found_in_db","type":"auth_error"}}'
    )
    assert is_operator_block_response(401, bad_key_body) is False


def test_is_operator_block_response_requires_401() -> None:
    from swebench_eval.harnesses.routing import is_operator_block_response

    # Same body, wrong status — must not match on body content alone.
    body = b'{"error":{"message":"...Key is blocked...","type":"auth_error"}}'
    assert is_operator_block_response(200, body) is False
    assert is_operator_block_response(503, body) is False


def test_is_real_provider_overload_true_when_no_retry_after_header() -> None:
    """BUILDER4-AUTOSCALER-FULL-2026-08-29.md §2.1: a real upstream overload
    (OpenRouter's engine_overloaded) carries no retry-after header — measured
    live, 0 of 2,651 real overload calls had one."""
    from swebench_eval.harnesses.routing import is_real_provider_overload

    assert is_real_provider_overload(429, None) is True


def test_is_real_provider_overload_false_when_retry_after_present() -> None:
    """§2.1's actual discriminator: LiteLLM's own self-imposed 429 ALWAYS
    carries retry-after — that's a gateway throttle on us, not a real
    provider overload, and must not be counted as one."""
    from swebench_eval.harnesses.routing import is_real_provider_overload

    assert is_real_provider_overload(429, 60.0) is False


def test_is_real_provider_overload_requires_429() -> None:
    from swebench_eval.harnesses.routing import is_real_provider_overload

    assert is_real_provider_overload(200, None) is False
    assert is_real_provider_overload(500, None) is False


def test_subprocess_adapters_use_agent_environment_not_os_environ_copy() -> None:
    """B1: none of the five subprocess adapters may still hand the agent the whole
    worker env (os.environ.copy()).  Grep the adapters for the pattern."""
    from pathlib import Path

    for name in ("claude_code", "codex", "opencode", "aider", "mini_swe_agent"):
        src = (
            Path(__file__).parent.parent / "swebench_eval" / "harnesses" / name / "harness.py"
        ).read_text()
        assert "os.environ.copy()" not in src, f"{name} still copies the worker env"
        assert "agent_environment()" in src, f"{name} does not use the allow-listed env"
