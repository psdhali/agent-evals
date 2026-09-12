"""(V8, switch-to-swebench-verified §10) No harness overrides the gateway's pin.

Builder 3 pinned per-model `temperature` / `top_p` / `top_k` / `reasoning_effort`
in `infra/docker/litellm_config.yaml` so every harness gives a model a uniform
request.  custom_minimal was the ONE harness that sent a sampling parameter
(`temperature=0.0` on every request) while the five CLI harnesses sent nothing —
so the control harness ran greedy at 0.0 and the other five at the gateway pin
(1.0).  V8 makes custom_minimal omit the key when None, so all six inherit the
pin.  These tests assert that state stays true.

Two-sided: the subprocess adapters are verified by SOURCE (they send nothing);
custom_minimal is verified behaviorally (its default-constructed request omits
temperature).  Proved by mutation: restoring the old `temperature=0.0` in
`harness_worker.py`, or sending temperature unconditionally in
custom_minimal, makes a side of this test fail.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from swebench_eval.harnesses.custom_minimal.harness import _call_with_retry

# The five subprocess adapters (incl. aider, still on disk though out of scope)
# plus custom_minimal = the six write path surfaces.
_SUBPROCESS_ADAPTERS = ("claude_code", "codex", "opencode", "mini_swe_agent", "aider")


def test_cli_adapters_send_no_temperature_or_sampling_param() -> None:
    """The five subprocess adapters must not put temperature/top_p/top_k in their
    request building — they send nothing and inherit the gateway pin."""
    repo = Path(__file__).parent.parent
    for name in _SUBPROCESS_ADAPTERS:
        src = (repo / "swebench_eval" / "harnesses" / name / "harness.py").read_text()
        assert "temperature" not in src, f"{name} sends temperature"
        assert "top_p" not in src, f"{name} sends top_p"
        assert "top_k" not in src, f"{name} sends top_k"


def test_model_config_default_temperature_is_none() -> None:
    """The framework default is None (inherit the pin), never 0.0."""
    from swebench_eval.harnesses.base import ModelConfig

    mc = ModelConfig(
        gateway_base_url="http://x/v1",
        gateway_api_key="k",
        model_name="laguna-xs-2.1",
    )
    assert mc.temperature is None


def test_custom_minimal_omits_temperature_when_none():
    """custom_minimal sends NO temperature key when ModelConfig.temperature is
    None — inherits the gateway pin.  Include the key only when non-None."""
    client = mock.Mock()
    client.chat.completions.create.return_value = mock.Mock(
        choices=[mock.Mock(message=mock.Mock(content="ok"))]
    )

    # Default (temperature=None): the request carries NO temperature key.
    _call_with_retry(
        client,
        deadline=60.0,
        max_retries=0,
        base_delay=0.0,
        max_delay=0.0,
        per_call_read=30.0,
        model="laguna-xs-2.1",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        tool_choice="auto",
        temperature=None,
        extra_body={},
    )
    _, kwargs = client.chat.completions.create.call_args
    assert "temperature" not in kwargs, (
        "custom_minimal must NOT send temperature when None — it would override "
        "the gateway pin and run the control harness greedy at 0.0"
    )
    # PART2 §1: max_tokens is gone ENTIRELY — no framework-imposed per-call cap
    # (the five CLI harnesses each set their own; custom_minimal sends none and
    # inherits the model's pin).  Mutation-proof: restoring the key (even as
    # None) makes this fail.
    assert "max_tokens" not in kwargs, (
        "custom_minimal must NOT send max_tokens — the per-call completion cap "
        "was removed; None is not omission (an explicit null goes on the wire)"
    )

    # Explicit temperature IS sent (a harness that configures one deliberately).
    client.reset_mock()
    _call_with_retry(
        client,
        deadline=60.0,
        max_retries=0,
        base_delay=0.0,
        max_delay=0.0,
        per_call_read=30.0,
        model="laguna-xs-2.1",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        tool_choice="auto",
        temperature=0.5,
        extra_body={},
    )
    _, kwargs = client.chat.completions.create.call_args
    assert kwargs.get("temperature") == 0.5


def test_custom_minimal_sends_no_strict_openrouter_provider_filter() -> None:
    """custom_minimal must NOT send `provider: {"require_parameters": True}`.

    Compaction-e2e (2026-08-28): that strict OpenRouter routing filter made
    OpenRouter return 404 "No endpoints found" for poolside/laguna-xs-2.1 — so
    custom_minimal failed MODEL_API_ERROR on its first call while mini_swe /
    opencode hit the same /v1/chat/completions with laguna and got 200 (they
    send no such filter).  The plan mandates custom_minimal on laguna, so the
    strict filter was removed from the harness.  Source-level assertion (same
    pattern as the CLI-adapter checks above): re-adding the literal string at
    the call site fails this test.  (A behavioral test cannot reach it — the
    filter was passed at the `run()` call site, and `_call_with_retry` is
    exercised here directly with a caller-supplied `extra_body`.)
    """
    repo = Path(__file__).parent.parent
    src = (repo / "swebench_eval" / "harnesses" / "custom_minimal" / "harness.py").read_text()
    assert "require_parameters" not in src, (
        "custom_minimal must NOT send the strict OpenRouter require_parameters "
        "filter — it 404s poolside/laguna-xs-2.1 (compaction-e2e 2026-08-28)"
    )
