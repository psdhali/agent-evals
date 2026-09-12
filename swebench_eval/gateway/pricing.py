"""Shared token/cost pricing resolution (mid-phase review F-2/F-3).

One pricing module for the whole system, next to ``local_proxy.py``.  Every
harness and the shim resolve cost here rather than maintaining a separate table —
architecture §4 reuses a single pricing map "rather than maintaining a separate
pricing table", and F-3 flags that two independent pricing paths would make the
same run cost different amounts depending on which harness ran it.

Resolution is two-tier (F-2): prefer the API-reported ``cost`` from the gateway,
else price the tokens locally from the pinned table, else a conservative default
that deliberately trips the per-instance budget cap for unknown models (the
correct failure direction — never silently unbounded).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# (input_price_per_1M, output_price_per_1M), in USD per 1M tokens.
# B8 (round-review E5): the two gateway aliases route to the SAME backend
# (openrouter/deepseek/deepseek-v4-flash-0731) and must carry that model's real
# price — verified on OpenRouter 2026-08-19. Reviewer-2 2026-08-26
# (PRICING-FIX-BEFORE-REBUILD §1) re-verified the live rate as $0.06 in / $0.12
# out / $0.012 cache-read per 1M and corrected cheap-oss-model accordingly
# (it was $0.07/$0.14, ~17% high). Before B8, cheap-oss-model priced OUTPUT at
# the Qwen figure (2× high) and claude-code-model fell off the table to the
# $1/$5 unknown default — ~15× overstated (the two absent lines the review named).
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "qwen/qwen3-coder-30b-a3b-instruct": (0.07, 0.28),  # Qwen3 Coder 30B
    # 2026-09-04 (owner): the OpenRouter account allowlist changed — DeepInfra OUT, OpenInference
    # IN — so every UNPINNED deepseek-v4-flash-0731 alias (cheap-oss-model, claude-code-model,
    # deepseek-flash, judge-model) now lands on OpenInference, the only allowlisted provider
    # serving it. Its live rate (OpenRouter /api/v1/models/.../endpoints, 2026-09-04):
    # $0.05 in / $0.16 out / $0.013 cache-read per 1M. The earlier $0.06/$0.12 and $0.07/$0.14
    # figures were other providers' rates and are gone.
    "deepseek/deepseek-v4-flash-0731": (0.05, 0.16),
    "cheap-oss-model": (0.05, 0.16),  # openrouter/deepseek/deepseek-v4-flash-0731
    "claude-code-model": (0.05, 0.16),  # same backend, second harness alias
    # V7a (switch-to-swebench-verified §9): the two calibration models were
    # missing, so they fell to the conservative $1/$5 unknown default and cost
    # tracking over/under-stated real spend.  Keys are the exact OpenRouter
    # slugs (a.k.a. llm_calls.model_resolved); prices are $/1M in / per 1M out
    # from OpenRouter 2026-08-20.
    # The raw OpenRouter slugs stay — they're the backend model identity, not
    # a gateway alias, and remain valid regardless of which alias currently
    # routes to them.  The LOCAL fallback (local_proxy._extract_model) prices
    # on the REQUEST model string — the gateway ALIAS the harness sends, not
    # necessarily the slug — so alias-keyed entries are ALSO needed below;
    # without them the local path falls to the unknown default.
    "poolside/laguna-xs-2.1": (0.06, 0.12),
    "qwen/qwen3-coder-next": (0.12, 0.80),
    # 2026-08-28: laguna-xs-2.1 and qwen3-coder-next are now fully migrated
    # off litellm_config.yaml to per-harness db-model aliases
    # (swebench_eval/gateway/rotatable_models.py) — no generic
    # "laguna-xs-2.1"/"qwen3-coder-next"/"-claude" alias exists anywhere any
    # more, so those keys are gone from this table too (a stale entry for an
    # alias that no longer exists is worse than a missing one — it reads as
    # "still valid"). Every new alias needs its pricing entry in THE SAME
    # CHANGE that adds it to rotatable_models.py — a missing one falls to the
    # $1/$5 unknown default (~15x overstated, has tripped the $5/instance
    # ceiling before). `aider` is out of scope (untested) and has no alias
    # for either model, so no pricing entry either.
    "laguna-xs-2.1-mini": (0.06, 0.12),
    "laguna-xs-2.1-codex": (0.06, 0.12),
    "laguna-xs-2.1-opencode": (0.06, 0.12),
    "laguna-xs-2.1-custom_minimal": (0.06, 0.12),
    "laguna-xs-2.1-claude_code": (0.06, 0.12),
    "qwen3-coder-next-mini": (0.12, 0.80),
    "qwen3-coder-next-codex": (0.12, 0.80),
    "qwen3-coder-next-opencode": (0.12, 0.80),
    "qwen3-coder-next-custom_minimal": (0.12, 0.80),
    "qwen3-coder-next-claude_code": (0.12, 0.80),
    # deepseek-v4-flash-0731 benchmark family (rotatable_models.py _DEEPSEEK, 2026-09-04):
    # pinned to openinference, priced at that provider's live rate (see the slug entry above).
    "deepseek-v4-flash-0731-mini": (0.05, 0.16),
    "deepseek-v4-flash-0731-codex": (0.05, 0.16),
    "deepseek-v4-flash-0731-opencode": (0.05, 0.16),
    "deepseek-v4-flash-0731-custom_minimal": (0.05, 0.16),
    "deepseek-v4-flash-0731-claude_code": (0.05, 0.16),
    # gpt-5-mini benchmark family (rotatable_models.py _GPT5_MINI): pinned to OpenAI's FLEX
    # tier (owner decision 2026-09-05, tag openai/flex), priced at its live rate (OpenRouter
    # /endpoints 2026-09-04: $0.125 in / $1.00 out / $0.0125 cache-read per 1M — half the
    # standard tier). Reasoning tokens bill as output.
    "openai/gpt-5-mini": (0.125, 1.00),
    "gpt-5-mini-mini": (0.125, 1.00),
    "gpt-5-mini-codex": (0.125, 1.00),
    "gpt-5-mini-opencode": (0.125, 1.00),
    "gpt-5-mini-custom_minimal": (0.125, 1.00),
    "gpt-5-mini-claude_code": (0.125, 1.00),
    # minimax-m2.5 benchmark family (rotatable_models.py _MINIMAX, 2026-09-04): pinned to
    # MiniMax's own fp8 endpoint, priced at its live rate (OpenRouter /endpoints 2026-09-04:
    # $0.30 in / $1.20 out / $0.03 cache-read per 1M).
    "minimax/minimax-m2.5": (0.30, 1.20),
    "minimax-m2.5-mini": (0.30, 1.20),
    "minimax-m2.5-codex": (0.30, 1.20),
    "minimax-m2.5-opencode": (0.30, 1.20),
    "minimax-m2.5-custom_minimal": (0.30, 1.20),
    "minimax-m2.5-claude_code": (0.30, 1.20),
    # §1.2 (rebuild-and-smoke-handover.md): deepseek-flash is the only gateway
    # alias with no price — it fell to the $1/$5 unknown default against a real
    # backend (same deepseek-v4-flash-0731 as cheap-oss-model / claude-code-model),
    # overstating every flash cost ~15x and tripping the $5/instance ceiling on a
    # deceit. Rate: OpenInference's (allowlist change 2026-09-04, above).
    "deepseek-flash": (0.05, 0.16),
    # Pass B — LLM judge (offline-analysis-design.md §9.2/§10.2, rotatable_models.py
    # _JUDGE_MODEL, 2026-09-01). Same backend as cheap-oss-model — an unpriced
    # judge-model alias would fall to the $1/$5 unknown default and overstate every
    # judge pass's cost ~15x against its own budget ceiling (§3.10/§9.3).
    "judge-model": (0.05, 0.16),
}

# B8 "decide the cache-token dimension": cached INPUT tokens price at a fraction of
# the input rate, not the full input price.  Reviewer-2 (2026-08-26,
# PRICING-FIX-BEFORE-REBUILD §1): the ratio is NOT model-agnostic — OpenRouter's real
# cache-read price varies per model, verified live against /api/v1/models 2026-08-26:
#   deepseek-v4-flash-0731 (cheap-oss-model): $0.012 read / $0.06 in = 0.20
#   poolside/laguna-xs-2.1:                   $0.03  read / $0.06 in = 0.50
#   qwen/qwen3-coder-next:                    $0.07  read / $0.12 in = 0.583
# Keyed the same way MODEL_PRICING is (request alias / model string); anything not
# listed falls back to the global default.  This was a single constant before, which
# understated laguna's cost by ~2x and qwen's by ~2.2x (cache reads are 90%+ of input
# tokens on these runs) — and pricing.py is copied whole into the -hw image, so it had
# to be fixed before Phase 2's rebuild, not after.
MODEL_CACHE_READ_RATIO = 0.2  # fallback for any model not in the per-model table
MODEL_CACHE_READ_RATIOS: dict[str, float] = {
    # deepseek family (all aliases route to openrouter/deepseek/deepseek-v4-flash-0731). Since
    # the 2026-09-04 allowlist change every one of them lands on OpenInference: $0.013 read /
    # $0.05 in = 0.26 (was 0.20 on the previous provider).
    "deepseek/deepseek-v4-flash-0731": 0.26,
    "cheap-oss-model": 0.26,
    "claude-code-model": 0.26,
    "deepseek-flash": 0.26,
    "judge-model": 0.26,  # same deepseek-v4-flash-0731 backend as the above three
    "deepseek-v4-flash-0731-mini": 0.26,
    "deepseek-v4-flash-0731-codex": 0.26,
    "deepseek-v4-flash-0731-opencode": 0.26,
    "deepseek-v4-flash-0731-custom_minimal": 0.26,
    "deepseek-v4-flash-0731-claude_code": 0.26,
    # laguna — raw slug + every per-harness alias (2026-08-28 migration; see
    # MODEL_PRICING above for why the generic "laguna-xs-2.1"/"-claude" keys
    # are gone).
    "poolside/laguna-xs-2.1": 0.5,
    "laguna-xs-2.1-mini": 0.5,
    "laguna-xs-2.1-codex": 0.5,
    "laguna-xs-2.1-opencode": 0.5,
    "laguna-xs-2.1-custom_minimal": 0.5,
    "laguna-xs-2.1-claude_code": 0.5,
    # qwen ($0.07 read / $0.12 in) — encoded as the exact fraction (7/12) so
    # $0.12 × ratio reproduces the $0.07 cache-read price precisely, not the
    # rounded 0.583 (which yields $0.06996).
    "qwen/qwen3-coder-next": 0.5833333333333334,
    "qwen3-coder-next-mini": 0.5833333333333334,
    "qwen3-coder-next-codex": 0.5833333333333334,
    "qwen3-coder-next-opencode": 0.5833333333333334,
    "qwen3-coder-next-custom_minimal": 0.5833333333333334,
    "qwen3-coder-next-claude_code": 0.5833333333333334,
    # gpt-5-mini on OpenAI's flex endpoint: $0.0125 read / $0.125 in = 0.10 (2026-09-05). This ratio is
    # why its measured per-instance cost undercuts qwen despite the higher list price — an
    # agent loop resends its whole context every turn, so the cache-read rate dominates.
    "openai/gpt-5-mini": 0.1,
    "gpt-5-mini-mini": 0.1,
    "gpt-5-mini-codex": 0.1,
    "gpt-5-mini-opencode": 0.1,
    "gpt-5-mini-custom_minimal": 0.1,
    "gpt-5-mini-claude_code": 0.1,
    # minimax-m2.5 on MiniMax's fp8 endpoint: $0.03 read / $0.30 in = 0.10 (2026-09-04).
    "minimax/minimax-m2.5": 0.1,
    "minimax-m2.5-mini": 0.1,
    "minimax-m2.5-codex": 0.1,
    "minimax-m2.5-opencode": 0.1,
    "minimax-m2.5-custom_minimal": 0.1,
    "minimax-m2.5-claude_code": 0.1,
}

# Deliberately high so the per-instance budget cap trips for unknown models
# rather than silently never enforcing the limit.
_UNKNOWN_MODEL_PRICE: tuple[float, float] = (1.00, 5.00)  # $1/M in, $5/M out

# PART2 §1 (2026-08-23): MODEL_MAX_TOKENS / _DEFAULT_MAX_TOKENS /
# max_tokens_for were REMOVED — the per-call completion cap is gone entirely
# (the five CLI harnesses set their own; custom_minimal now sends none and
# inherits the model's pin). MODEL_PRICING below is a DIFFERENT mechanism
# (cost-per-token for budget accounting) and is untouched.


def model_price(model: str) -> tuple[float, float]:
    """Return (in_per_1M, out_per_1M) for ``model``, warning + conservative default
    when unknown (so the budget cap still trips instead of pricing at zero)."""
    price = MODEL_PRICING.get(model)
    if price is not None:
        return price
    logger.warning(
        "Model %r not in MODEL_PRICING — using conservative default %r. "
        "Add it to swebench_eval/gateway/pricing.py for accurate cost tracking.",
        model,
        _UNKNOWN_MODEL_PRICE,
    )
    return _UNKNOWN_MODEL_PRICE


def cost_for(
    gateway_cost: float | None,
    input_tokens: int,
    output_tokens: int,
    model: str,
    cached_input_tokens: int = 0,
) -> float:
    """Two-tier resolution (F-2): API-reported cost wins; else price locally.

    ``gateway_cost`` is the cost the gateway/API reported for a single call.  When
    it is absent or zero (e.g. the Anthropic/Claude-Code path, which can never
    carry cost — F-1), price this call's tokens from the shared table.

    B8 cache dimension: ``cached_input_tokens`` (``prompt_tokens_details.
    cached_tokens``) prices at the model's cache-read ratio of the input rate
    (``MODEL_CACHE_READ_RATIOS``, default ``MODEL_CACHE_READ_RATIO``).
    ``input_tokens`` is the full prompt figure and INCLUDEs the cached portion
    (OpenRouter/OpenAI shape), so the cached slice is charged at the discount and
    the rest at full.

    Finding 1 (2026-08-27): Anthropic-shaped usage is DIFFERENT. Anthropic's
    ``input_tokens`` reports only the NEW tokens for the turn, and
    ``cache_read_input_tokens`` is a separate, ADDITIVE count of the tokens re-read
    from cache — NOT a subset of ``input_tokens`` the way OpenAI's
    ``prompt_tokens_details.cached_tokens`` is. So for claude, ``cached`` can
    legitimately EXCEED ``input``. ``max(0, input - cached)`` still yields 0 for the
    full-slice (correct — those turn's new tokens are already counted in input), and
    the cached slice is capped at input so we never charge MORE cached tokens than
    the call actually carried. Without the cap, ``cached=5,000`` with ``input=2,000``
    would price 5,000 cached tokens (2,500% of the true new-input count) and
    overstate the call ~2.5x. At worst this is an under-count of a few KB of cache
    reads, never a fabrication.
    """
    if gateway_cost:
        return float(gateway_cost)
    in_price, out_price = model_price(model)
    cached = max(0, cached_input_tokens)
    full_input = max(0, input_tokens - min(cached, input_tokens))
    cached_chargeable = min(cached, input_tokens)
    return (
        (full_input / 1_000_000) * in_price
        + (cached_chargeable / 1_000_000) * (in_price * cache_read_ratio(model))
        + (output_tokens / 1_000_000) * out_price
    )


def cache_read_ratio(model: str) -> float:
    """Per-model cache-read ratio (fraction of the input price a cached INPUT token costs).

    Reviewer-2 (2026-08-26, PRICING-FIX-BEFORE-REBUILD §1): the ratio varies by model —
    deepseek 0.20, laguna 0.50, qwen 0.583.  Falls back to the global 0.2 for any model
    not in :data:`MODEL_CACHE_READ_RATIOS`.
    """
    return MODEL_CACHE_READ_RATIOS.get(model, MODEL_CACHE_READ_RATIO)
