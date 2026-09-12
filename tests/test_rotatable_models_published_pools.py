"""The two swebench.com-published benchmark families (owner decision 2026-09-04).

gpt-5-mini — 59.8 % at $0.04/instance on swebench.com's mini-SWE-agent board (reasoning medium);
minimax-m2.5 — 75.8 % at $0.07/instance on the same board ("high" = reasoning effort high),
sampling temperature 1.0 / top_p 0.95 / top_k 40 (owner-set).
Pins what was decided: provider, window, sampling (or the absence of it), reasoning effort,
a price for every alias at the PINNED provider's rate, and discovery knowing both pools.
"""

from __future__ import annotations

from typing import Any, cast

from swebench_eval.gateway.pricing import MODEL_CACHE_READ_RATIOS, MODEL_PRICING
from swebench_eval.gateway.rotatable_models import (
    _PENDING_ROTATION_KEY,
    ROTATABLE_MODELS,
    pool_alias_for,
)
from swebench_eval.harnesses.compaction import OUTPUT_RESERVE

_OPENAI_SHAPED = ("mini", "codex", "opencode", "custom_minimal")


def _aliases(pool: str) -> tuple[tuple[str, ...], str]:
    return tuple(f"{pool}-{h}" for h in _OPENAI_SHAPED), f"{pool}-claude_code"


# ── gpt-5-mini ──────────────────────────────────────────────────────────────────────────────


def test_gpt5_mini_family_has_five_aliases_on_the_same_backend() -> None:
    aliases, claude = _aliases("gpt-5-mini")
    for alias in (*aliases, claude):
        spec = ROTATABLE_MODELS[alias]
        assert spec.model_name == alias
        assert str(spec.litellm_params["model"]).endswith("openai/gpt-5-mini")
        assert spec.litellm_params["api_key"] == _PENDING_ROTATION_KEY
        assert pool_alias_for(alias) == "gpt-5-mini"


def test_gpt5_mini_sends_no_sampling_params_and_runs_medium_reasoning() -> None:
    """OpenAI's reasoning models reject temperature/top_p/top_k; the spec must never send
    them (OpenRouter would drop them for this provider, but a spec that depends on that is
    one allowlist change from a 400 on every call). "medium" is the published row's effort."""
    aliases, _ = _aliases("gpt-5-mini")
    for alias in aliases:
        params = ROTATABLE_MODELS[alias].litellm_params
        assert "temperature" not in params, alias
        assert "top_p" not in params, alias
        assert "top_k" not in params, alias
        assert params["reasoning_effort"] == "medium", alias
        assert params["max_tokens"] == OUTPUT_RESERVE, alias


def test_gpt5_mini_pins_the_flex_tier_at_its_real_272k_input_cap() -> None:
    aliases, claude = _aliases("gpt-5-mini")
    for alias in (*aliases, claude):
        spec = ROTATABLE_MODELS[alias]
        extra_body = cast("dict[str, Any]", spec.litellm_params["extra_body"])
        assert extra_body["provider"] == {"order": ["openai/flex"], "allow_fallbacks": False}
        # The endpoint's real INPUT cap (max_prompt_tokens 272,000; the advertised 400K is
        # input + output) — comparable with the published row, and a 400K registration made
        # every probe call a context-window rejection (2026-09-05).
        assert spec.model_info["max_input_tokens"] == 272_000, alias
        assert spec.model_info["max_output_tokens"] == OUTPUT_RESERVE


def test_gpt5_mini_is_priced_at_the_flex_rate_with_the_10pct_cache_ratio() -> None:
    aliases, claude = _aliases("gpt-5-mini")
    for alias in (*aliases, claude, "openai/gpt-5-mini"):
        assert MODEL_PRICING[alias] == (0.125, 1.00), alias
        assert MODEL_CACHE_READ_RATIOS[alias] == 0.1, alias


# ── minimax-m2.5 ────────────────────────────────────────────────────────────────────────────


def test_minimax_family_has_five_aliases_on_the_same_backend() -> None:
    aliases, claude = _aliases("minimax-m2.5")
    for alias in (*aliases, claude):
        spec = ROTATABLE_MODELS[alias]
        assert spec.model_name == alias
        assert str(spec.litellm_params["model"]).endswith("minimax/minimax-m2.5")
        assert spec.litellm_params["api_key"] == _PENDING_ROTATION_KEY
        assert pool_alias_for(alias) == "minimax-m2.5"


def test_minimax_carries_the_owner_sampling_and_high_reasoning() -> None:
    """Owner-set 2026-09-04: temperature 1.0 / top_p 0.95 / top_k 40, reasoning high."""
    aliases, _ = _aliases("minimax-m2.5")
    for alias in aliases:
        params = ROTATABLE_MODELS[alias].litellm_params
        assert params["temperature"] == 1.0, alias
        assert params["top_p"] == 0.95, alias
        assert params["top_k"] == 40, alias
        assert params["reasoning_effort"] == "high", alias
        assert params["max_tokens"] == OUTPUT_RESERVE, alias


def test_minimax_pins_its_home_provider_at_the_providers_total_context() -> None:
    aliases, claude = _aliases("minimax-m2.5")
    for alias in (*aliases, claude):
        spec = ROTATABLE_MODELS[alias]
        extra_body = cast("dict[str, Any]", spec.litellm_params["extra_body"])
        assert extra_body["provider"] == {"order": ["minimax"], "allow_fallbacks": False}
        assert spec.model_info["max_input_tokens"] == 204_800, alias
        # The compaction threshold keeps a prompt under W - OUTPUT_RESERVE, so a max-context
        # turn plus its output still fits the provider's 204,800 total.
        assert 204_800 - OUTPUT_RESERVE + OUTPUT_RESERVE <= 204_800


def test_minimax_is_priced_at_the_fp8_endpoint_rate() -> None:
    aliases, claude = _aliases("minimax-m2.5")
    for alias in (*aliases, claude, "minimax/minimax-m2.5"):
        assert MODEL_PRICING[alias] == (0.30, 1.20), alias
        assert MODEL_CACHE_READ_RATIOS[alias] == 0.1, alias


# ── shared invariants ───────────────────────────────────────────────────────────────────────


def test_claude_code_aliases_stay_anthropic_shaped_without_sampling() -> None:
    for pool in ("gpt-5-mini", "minimax-m2.5"):
        params = ROTATABLE_MODELS[f"{pool}-claude_code"].litellm_params
        assert params["custom_llm_provider"] == "anthropic"
        assert "temperature" not in params
        assert "reasoning_effort" not in params
        assert params["max_tokens"] == OUTPUT_RESERVE


def test_the_existing_families_are_untouched() -> None:
    """Making temperature/top_p optional must not have changed what laguna/qwen/deepseek send."""
    q = ROTATABLE_MODELS["qwen3-coder-next-mini"].litellm_params
    assert (q["temperature"], q["top_p"], q["top_k"], q["reasoning_effort"]) == (
        1.0,
        0.95,
        40,
        "high",
    )
    d = ROTATABLE_MODELS["deepseek-v4-flash-0731-mini"].litellm_params
    assert (d["temperature"], d["top_p"], d["reasoning_effort"]) == (1.0, 0.95, "high")
    assert "top_k" not in d
    lg = ROTATABLE_MODELS["laguna-xs-2.1-mini"].litellm_params
    assert (lg["temperature"], lg["top_p"], lg["top_k"]) == (1.0, 1.0, 20)


def test_discovery_knows_both_pools_with_pin_label_and_window() -> None:
    from swebench_eval.orchestrator.control_plane import ceiling_discovery as cd

    assert cd.upstream_model_for("gpt-5-mini") == "openai/gpt-5-mini"
    assert cd._PROVIDER_PINS["gpt-5-mini"] == "openai/flex"
    assert cd._PROVIDER_LABELS["gpt-5-mini"] == "OpenAI"
    assert cd.max_input_tokens_for("gpt-5-mini") == 272_000
    assert cd.resolve_target_tokens("gpt-5-mini") == 272_000 - 2_000
    assert cd.upstream_model_for("minimax-m2.5") == "minimax/minimax-m2.5"
    assert cd._PROVIDER_PINS["minimax-m2.5"] == "minimax"
    assert cd._PROVIDER_LABELS["minimax-m2.5"] == "MiniMax"
    assert cd.max_input_tokens_for("minimax-m2.5") == 204_800
    assert cd.resolve_target_tokens("minimax-m2.5") == 204_800 - 2_000


def test_ceilings_api_lists_both_pools() -> None:
    from swebench_eval.orchestrator.api.main import _KNOWN_MODEL_ALIASES

    assert "gpt-5-mini" in _KNOWN_MODEL_ALIASES
    assert "minimax-m2.5" in _KNOWN_MODEL_ALIASES
