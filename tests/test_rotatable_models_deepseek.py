"""The deepseek-v4-flash-0731 benchmark family (owner decision 2026-09-04).

Pins what the owner set — OpenInference, the full 1,048,576 window, temperature 1.0 /
top_p 0.95 / reasoning with NO top_k — and the invariants every family must hold: prefix ==
pool alias, five per-harness aliases, a price for each, and discovery knowing the pool.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from swebench_eval.gateway.pricing import MODEL_CACHE_READ_RATIOS, MODEL_PRICING
from swebench_eval.gateway.rotatable_models import (
    _PENDING_ROTATION_KEY,
    ROTATABLE_MODELS,
    _family,
    pool_alias_for,
)
from swebench_eval.harnesses.compaction import OUTPUT_RESERVE

_POOL = "deepseek-v4-flash-0731"
_ALIASES = tuple(f"{_POOL}-{h}" for h in ("mini", "codex", "opencode", "custom_minimal"))
_CLAUDE = f"{_POOL}-claude_code"


def test_family_has_five_aliases_on_the_same_backend() -> None:
    for alias in (*_ALIASES, _CLAUDE):
        spec = ROTATABLE_MODELS[alias]
        assert spec.model_name == alias
        assert str(spec.litellm_params["model"]).endswith("deepseek/deepseek-v4-flash-0731")
        assert spec.litellm_params["api_key"] == _PENDING_ROTATION_KEY
        assert pool_alias_for(alias) == _POOL


def test_openai_shaped_aliases_carry_the_owner_sampling_params_and_no_top_k() -> None:
    """Owner: temperature 1.0, top_p 0.95, reasoning — and nothing else. A stray top_k would
    change the distribution the benchmark is calibrated on."""
    for alias in _ALIASES:
        params = ROTATABLE_MODELS[alias].litellm_params
        assert params["temperature"] == 1.0, alias
        assert params["top_p"] == 0.95, alias
        assert params["reasoning_effort"] == "high", alias
        assert "top_k" not in params, alias
        assert params["max_tokens"] == OUTPUT_RESERVE, alias


def test_claude_code_alias_is_anthropic_shaped_without_sampling_params() -> None:
    params = ROTATABLE_MODELS[_CLAUDE].litellm_params
    assert params["custom_llm_provider"] == "anthropic"
    assert params["api_base"] == "https://openrouter.ai/api"
    assert "temperature" not in params
    assert params["max_tokens"] == OUTPUT_RESERVE


def test_every_alias_pins_openinference_with_no_fallback() -> None:
    """29 providers serve this model on OpenRouter; discovery measures ONE pool, so production
    must ride the same one (exact-design §6). allow_fallbacks False: a pin that silently fell
    through would put measured constants against a different provider's limits."""
    for alias in (*_ALIASES, _CLAUDE):
        extra_body = cast("dict[str, Any]", ROTATABLE_MODELS[alias].litellm_params["extra_body"])
        assert extra_body["provider"] == {"order": ["open-inference"], "allow_fallbacks": False}


def test_window_is_the_full_backend_capacity_not_the_262k_default() -> None:
    """The owner chose to benchmark at deepseek's real window. The registered figure is what
    the launch screen offers and the dispatcher resolves the run's context window from."""
    for alias in (*_ALIASES, _CLAUDE):
        info = ROTATABLE_MODELS[alias].model_info
        assert info["max_input_tokens"] == 1_048_576, alias
        assert info["max_output_tokens"] == OUTPUT_RESERVE, alias
    # ...and the other two families are untouched.
    assert ROTATABLE_MODELS["qwen3-coder-next-mini"].model_info["max_input_tokens"] == 262_144
    assert ROTATABLE_MODELS["laguna-xs-2.1-mini"].model_info["max_input_tokens"] == 262_144


def test_every_alias_is_priced_at_openinference_rates() -> None:
    """An unpriced alias falls to the $1/$5 unknown default (~15x) and trips the per-instance
    ceiling; the rate must be the PINNED provider's, not the model's cheapest-anywhere."""
    for alias in (*_ALIASES, _CLAUDE):
        assert MODEL_PRICING[alias] == (0.05, 0.16), alias
        assert MODEL_CACHE_READ_RATIOS[alias] == 0.26, alias


def test_family_prefix_must_be_the_pool_alias() -> None:
    """pool_alias_for derives the pool from the upstream slug's last segment, and the planner /
    pricing / launch code build ``f"{pool}-{harness}"`` from it — a prefix that differs from
    the slug tail would register aliases nothing can find. The helper refuses."""
    with pytest.raises(ValueError, match="pool alias"):
        _family(
            prefix="deepseek-v4-flash",
            upstream_model="deepseek/deepseek-v4-flash-0731",
            temperature=1.0,
            top_p=0.95,
            top_k=None,
        )


def test_discovery_knows_the_pool_with_its_pin_label_and_window() -> None:
    from swebench_eval.orchestrator.control_plane import ceiling_discovery as cd

    assert cd.upstream_model_for(_POOL) == "deepseek/deepseek-v4-flash-0731"
    assert cd._PROVIDER_PINS[_POOL] == "open-inference"
    assert cd._PROVIDER_LABELS[_POOL] == "OpenInference"
    assert cd.max_input_tokens_for(_POOL) == 1_048_576
    assert cd.max_input_tokens_for("qwen3-coder-next") == 262_144
    # The probe's near-max-context call is sized to the POOL's window, not the 262K default.
    assert cd.resolve_target_tokens(_POOL) == 1_048_576 - 2_000
    assert cd.resolve_target_tokens("laguna-xs-2.1") == 262_144 - 2_000


def test_ceilings_api_lists_the_pool() -> None:
    from swebench_eval.orchestrator.api.main import _KNOWN_MODEL_ALIASES

    assert _POOL in _KNOWN_MODEL_ALIASES


def test_probe_timeout_grows_with_the_prompt_and_stays_under_the_alb() -> None:
    """A 1M-token probe cannot be held to the 262K window's 120 s: a timeout reads as a network
    failure and poisons the burst count. Scaled by a pessimistic prefill floor, capped under
    the gateway ALB's 600 s idle timeout."""
    from swebench_eval.gateway.ceiling_discovery import request_timeout_s

    assert request_timeout_s(260_144) == 120.0
    assert request_timeout_s(1_046_576) == pytest.approx(1_046_576 / 2_500.0)
    assert request_timeout_s(10_000_000) == 540.0
