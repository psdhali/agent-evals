"""Every rotatable alias carries explicit per-token prices from pricing.py (2026-09-06).

LiteLLM v1.99.1 priced ``openrouter/minimax/minimax-m2.5`` cache reads at $0.15/M
(real: $0.03/M), so its spend counter ran ~3.5x the OpenRouter bill and a $10 key
cap fired at $2.90 of real spend (codex run 01788653487361028986-1de1c022).  The
cap is gone; these params make LiteLLM's counter agree with the provider instead.
"""

from __future__ import annotations

import pytest

from swebench_eval.gateway import pricing
from swebench_eval.gateway.rotatable_models import ROTATABLE_MODELS, _price_params

_PRICE_KEYS = ("input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost")


def _price(params: dict[str, object], key: str) -> float:
    """One price param as the float it must be (litellm_params is typed dict[str, object])."""
    value = params[key]
    assert isinstance(value, float), (key, value)
    return value


@pytest.mark.parametrize("alias", sorted(ROTATABLE_MODELS))
def test_every_alias_carries_prices_matching_pricing_py(alias: str) -> None:
    params = ROTATABLE_MODELS[alias].litellm_params
    in_per_m, out_per_m = pricing.MODEL_PRICING[alias]  # KeyError = the alias is unpriced
    assert params["input_cost_per_token"] == pytest.approx(in_per_m / 1e6)
    assert params["output_cost_per_token"] == pytest.approx(out_per_m / 1e6)
    assert params["cache_read_input_token_cost"] == pytest.approx(
        in_per_m * pricing.cache_read_ratio(alias) / 1e6
    )
    for key in _PRICE_KEYS:
        assert _price(params, key) > 0


def test_minimax_prices_are_the_provider_rates_not_litellms_table() -> None:
    # $0.30 in / $1.20 out / $0.03 cache-read per 1M on MiniMax's fp8 endpoint.
    for harness in ("mini", "codex", "opencode", "custom_minimal", "claude_code"):
        params = ROTATABLE_MODELS[f"minimax-m2.5-{harness}"].litellm_params
        assert params["input_cost_per_token"] == pytest.approx(0.30e-6)
        assert params["output_cost_per_token"] == pytest.approx(1.20e-6)
        assert params["cache_read_input_token_cost"] == pytest.approx(0.03e-6)


def test_litellm_local_and_gateway_pricing_agree_on_a_cached_call() -> None:
    """The three params price a call exactly like pricing.cost_for's local path."""
    alias = "minimax-m2.5-codex"
    params = ROTATABLE_MODELS[alias].litellm_params
    prompt, cached, completion = 120_000, 110_000, 2_000
    via_params = (
        (prompt - cached) * _price(params, "input_cost_per_token")
        + cached * _price(params, "cache_read_input_token_cost")
        + completion * _price(params, "output_cost_per_token")
    )
    via_pricing = pricing.cost_for(None, prompt, completion, alias, cached_input_tokens=cached)
    assert via_params == pytest.approx(via_pricing)


def test_unpriced_alias_is_refused_loudly() -> None:
    with pytest.raises(ValueError, match="no MODEL_PRICING entry"):
        _price_params("some-new-model-mini")
