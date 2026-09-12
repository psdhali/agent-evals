"""Tests for the shared pricing module (review F-2/F-3).

Two-tier resolution: API-reported cost wins, else local table, else a high
conservative default.  Also pins that the stale deepseek aliases are gone.
"""

from __future__ import annotations

import pytest

from swebench_eval.gateway.pricing import (
    _UNKNOWN_MODEL_PRICE,
    MODEL_CACHE_READ_RATIOS,
    MODEL_PRICING,
    cache_read_ratio,
    cost_for,
)


def test_gateway_cost_wins() -> None:
    """An API-reported cost beats local pricing."""
    assert cost_for(0.42, 1_000_000, 1_000_000, "cheap-oss-model") == 0.42


def test_local_pricing_fallback() -> None:
    """Without gateway cost, tokens are priced from the local table."""
    # B8: the alias carries deepseek-v4-flash-0731. 2026-09-04: the account allowlist moved
    # every unpinned deepseek alias onto OpenInference — $0.05 in / $0.16 out (was $0.06/$0.12
    # on the previous provider; before B8 it was the Qwen figure, 2× high).
    assert cost_for(None, 1_000_000, 1_000_000, "cheap-oss-model") == pytest.approx(0.21)


def test_unknown_model_uses_conservative_default() -> None:
    """An unknown model prices high so the budget cap trips, never silently zero."""
    price = cost_for(None, 1_000_000, 0, "some/unknown-model")
    assert price == _UNKNOWN_MODEL_PRICE[0]
    assert "deepseek" not in MODEL_PRICING  # stale legacy alias removed (F-3)


def test_claude_code_model_resolves_to_real_price_not_punitive_default() -> None:
    """B8/E5: claude-code-model was the TYPICAL victim — absent from MODEL_PRICING,
    it hit the $1/$5 unknown default (≈15× the backend's real rate). 2026-09-04: the rate is
    OpenInference's $0.05/$0.16 (allowlist change)."""
    assert "claude-code-model" in MODEL_PRICING
    assert MODEL_PRICING["claude-code-model"] == (0.05, 0.16)
    assert cost_for(None, 1_000_000, 1_000_000, "claude-code-model") == pytest.approx(0.21)


def test_deepseek_flash_priced_at_real_rate() -> None:
    """§1.2 (rebuild-and-smoke-handover.md): deepseek-flash was the ONLY gateway
    alias with no price — it hit the $1/$5 unknown default against a real
    backend (same deepseek-v4-flash-0731 as cheap-oss-model), overstating every
    flash cost ~15x and tripping the $5/instance ceiling. The alias key binds on
    the local fallback path too. Rate: OpenInference's (2026-09-04)."""
    assert MODEL_PRICING["deepseek-flash"] == (0.05, 0.16)
    assert cost_for(None, 1_000_000, 1_000_000, "deepseek-flash") == pytest.approx(0.21)


def test_every_deepseek_alias_prices_at_the_openinference_rate() -> None:
    """2026-09-04 (owner): DeepInfra left the OpenRouter account allowlist and OpenInference
    joined it, so EVERY alias routing to deepseek-v4-flash-0731 — the four unpinned yaml/judge
    aliases and the five pinned benchmark aliases — lands on the same provider and must carry
    its live rate ($0.05 in / $0.16 out / $0.013 cache-read per 1M -> ratio 0.26). A split
    table would price the same backend two ways depending on the alias (F-3)."""
    aliases = (
        "deepseek/deepseek-v4-flash-0731",
        "cheap-oss-model",
        "claude-code-model",
        "deepseek-flash",
        "judge-model",
        "deepseek-v4-flash-0731-mini",
        "deepseek-v4-flash-0731-codex",
        "deepseek-v4-flash-0731-opencode",
        "deepseek-v4-flash-0731-custom_minimal",
        "deepseek-v4-flash-0731-claude_code",
    )
    for alias in aliases:
        assert MODEL_PRICING[alias] == (0.05, 0.16), alias
        assert cache_read_ratio(alias) == pytest.approx(0.26), alias
        # 1M fully cached input = $0.05 x 0.26 = $0.013 — the live cache-read price.
        assert cost_for(None, 1_000_000, 0, alias, cached_input_tokens=1_000_000) == pytest.approx(
            0.013
        ), alias


def test_calibration_models_in_pricing() -> None:
    """V7a/DoD 10: the two calibration models price from the table, not the
    $1/$5 unknown default — otherwise the max-cost ceiling is silently over-/
    under-stated for the models this whole comparison is calibrated against.

    2026-08-28: laguna-xs-2.1 and qwen3-coder-next no longer have generic
    aliases (fully migrated to per-harness db-model aliases,
    rotatable_models.py) — the alias-keyed assertion below uses one of those
    (-custom_minimal) instead; the raw slugs are unaffected by that
    migration and still price directly."""
    assert MODEL_PRICING["poolside/laguna-xs-2.1"] == (0.06, 0.12)
    assert MODEL_PRICING["qwen/qwen3-coder-next"] == (0.12, 0.80)
    # Alias keys mirror the slug prices so the local (request-alias) fallback
    # binds too (local_proxy prices on the alias, not model_resolved).
    assert MODEL_PRICING["laguna-xs-2.1-custom_minimal"] == (0.06, 0.12)
    assert MODEL_PRICING["qwen3-coder-next-custom_minimal"] == (0.12, 0.80)
    # 1M in / 1M out prices to exactly the table rates.
    assert cost_for(None, 1_000_000, 1_000_000, "laguna-xs-2.1-custom_minimal") == pytest.approx(
        0.18
    )
    assert cost_for(None, 1_000_000, 1_000_000, "qwen3-coder-next-custom_minimal") == pytest.approx(
        0.92
    )


def test_cache_read_tokens_price_at_discount() -> None:
    """B8 cache dimension: cached input prices at the model's cache-read ratio of the
    input rate (per-model, Reviewer-2 2026-08-26). prompt tokens INCLUDE the cached slice,
    so all-cached input bills at the discount."""
    # cheap-oss-model (deepseek on OpenInference): in $0.05, ratio 0.26 → 1M fully cached =
    # $0.013.
    assert cost_for(
        None, 1_000_000, 0, "cheap-oss-model", cached_input_tokens=1_000_000
    ) == pytest.approx(0.013)
    # Mixed: 500k full + 500k cached → 0.5M×0.05 + 0.5M×0.013 = 0.025 + 0.0065.
    assert cost_for(
        None, 1_000_000, 0, "cheap-oss-model", cached_input_tokens=500_000
    ) == pytest.approx(0.0315)


def test_per_model_cache_read_ratios() -> None:
    """Reviewer-2 (2026-08-26): the cache-read ratio is model-specific, verified live
    against OpenRouter's /api/v1/models — deepseek 0.20, laguna 0.50, qwen 0.583.  A
    single global 0.2 understated laguna's cost by ~2x and qwen's by ~2.2x (cache reads
    are 90%+ of input tokens on these runs).

    2026-08-28: laguna-xs-2.1/qwen3-coder-next's generic and -claude aliases
    are gone (fully migrated to per-harness db-model aliases,
    rotatable_models.py) — asserted here via -custom_minimal (OpenAI-shaped)
    and -claude_code (Anthropic-shaped), one of each shape per model."""
    assert cache_read_ratio("cheap-oss-model") == 0.26  # OpenInference, 2026-09-04
    assert cache_read_ratio("claude-code-model") == 0.26
    assert cache_read_ratio("laguna-xs-2.1-custom_minimal") == 0.5
    assert cache_read_ratio("poolside/laguna-xs-2.1") == 0.5
    assert cache_read_ratio("laguna-xs-2.1-claude_code") == 0.5
    assert cache_read_ratio("qwen3-coder-next-custom_minimal") == pytest.approx(0.583, rel=1e-3)
    assert cache_read_ratio("qwen/qwen3-coder-next") == pytest.approx(0.583, rel=1e-3)
    assert cache_read_ratio("qwen3-coder-next-claude_code") == pytest.approx(0.583, rel=1e-3)
    # unknown -> global default 0.2
    assert cache_read_ratio("some/unknown-model") == 0.2
    # laguna: in $0.06 × 0.5 = $0.03 cache-read; qwen: $0.12 × 0.583 = $0.07.
    assert cost_for(
        None, 1_000_000, 0, "laguna-xs-2.1-custom_minimal", cached_input_tokens=1_000_000
    ) == pytest.approx(0.03)
    assert cost_for(
        None, 1_000_000, 0, "qwen3-coder-next-custom_minimal", cached_input_tokens=1_000_000
    ) == pytest.approx(0.07)
    # MODEL_CACHE_READ_RATIOS is keyed/aligned with MODEL_PRICING's entries.
    assert "cheap-oss-model" in MODEL_CACHE_READ_RATIOS


def test_anthropic_cached_exceeds_input_is_capped_not_negative() -> None:
    """Finding 1 (2026-08-27): Anthropic's `cache_read_input_tokens` is ADDITIVE, not a
    subset of `input_tokens` (input_tokens = only the NEW tokens; cache reads are a
    separate count). So `cached` can legitimately exceed `input`. The cost must NOT
    price more cached tokens than `input` carries, and the full-slice must not go
    negative.

    Cheap-oss-model (deepseek on OpenInference, 2026-09-04): in $0.05/M, cache-read ratio
    0.26 ($0.013/M cached).
    input=2,000,000, cached=5,000,000 (a legit Anthropic shape where cached >> input):
      - OLD `max(0, input - cached)` -> full_input 0, cached priced FULL at 5,000,000
        -> 5.0M * $0.013 = $0.065   (overstates the true ~$0.026)
      - NEW: cached_chargeable = min(5,000,000, 2,000,000) = 2,000,000
        -> 2.0M * $0.013 = $0.026; full_input = 0
    The true cost bounds at pricing `input` tokens (all cached), never more.

    Mutation-proof: restore the raw `(cached / 1_000_000) * ratio` without the
    `min(cached, input_tokens)` cap; this test fails.
    """
    from swebench_eval.gateway.pricing import cost_for

    got = cost_for(None, 2_000_000, 0, "cheap-oss-model", cached_input_tokens=5_000_000)
    # Capped at input (2M) at the cache-read ratio: 2.0M * (0.05 * 0.26)/1M = 0.026
    assert got == pytest.approx(0.026), got

    # Full-input never goes negative: input=1M, cached=2M -> full=0, cached_chargeable=1M.
    got2 = cost_for(None, 1_000_000, 0, "cheap-oss-model", cached_input_tokens=2_000_000)
    assert got2 == pytest.approx(0.013), got2

    # Normal OpenAI shape (cached <= input) is unchanged: 500k full + 500k cached.
    got3 = cost_for(None, 1_000_000, 0, "cheap-oss-model", cached_input_tokens=500_000)
    assert got3 == pytest.approx(0.0315), got3  # 0.5M*0.05 + 0.5M*0.013
