"""The set of gateway aliases whose upstream OpenRouter key is rotated per run.

BUILDER4-RUN-LAUNCH-ORCHESTRATOR-2026-08-26 §5.1/§5.2 (ADR-0035 decisions 1-2):
each run mints its own OpenRouter key and rotates it onto the model_alias's
deployment.  Verified locally 2026-08-26 against a real
``ghcr.io/berriai/litellm:main-stable`` container (D3): LiteLLM's
``/model/update`` REFUSES to edit a model that came from ``litellm_config.yaml``
— "Model in config. Store model in db via `/model/new` to edit." — even with
``store_model_in_db: true``.  A rotatable alias must therefore exist ONLY as a
db-model (created via ``/model/new``), never also in the yaml's ``model_list``:
declaring it in both places makes LiteLLM register two deployments under the
same ``model_name``, and the router load-balances every request across them —
some fraction of a run's calls would silently go out on the shared, un-rotated
key instead of the run's own.

This module is the git-tracked SPEC for those aliases (mirrors D3's "the YAML
remains the seed" principle, just for a different persistence path) —
:mod:`swebench_eval.gateway.admin` reconciles it into the gateway's DB at
first use, one ``/model/new`` per alias, idempotent.

**Why per-harness, not one shared alias per model (2026-08-28):** the launch
mutex (``run_launch._claim``) keys on ``f"{harness}:{model_alias}"`` — two
runs on DIFFERENT harnesses using the SAME ``model_alias`` are NOT blocked by
it and can run concurrently. But key rotation (``/model/update``) identifies
its target purely by ``model_alias`` — one db-model row holds exactly one
current OpenRouter key. Two concurrent runs on different harnesses sharing one
alias would each rotate their own key onto that single row; the second
``/model/update`` silently overwrites the first, and one run ends up making
calls on the other run's key — exactly the isolation failure ADR-0035 exists
to prevent, just triggered by cross-harness concurrency instead of
same-alias redelivery. Giving every concurrently-launchable (harness, model)
pair its own db-model row makes the collision structurally impossible rather
than merely unlikely.

**2026-08-28: `aider` is explicitly OUT OF SCOPE.** Nothing has been tested
against it yet. It gets no alias here for either model family — a run
launched with harness=aider and model_alias=one of these will simply find no
matching db-model, by design, not by oversight.

**2026-08-28: laguna and qwen are now FULLY migrated — no yaml fallback of
any kind remains for either model.** Every alias below is the ONLY way to
reach either backend; ``litellm_config.yaml`` declares neither
``laguna-xs-2.1``/``-claude`` nor ``qwen3-coder-next``/``-claude`` any more.
This is deliberate, not an oversight to fix later: it's the only way to be
certain every run against either model is using a rotatable, per-run-keyed
alias — a lingering generic yaml entry would be a silent escape hatch back to
an un-rotated, shared key.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from swebench_eval.gateway.pricing import MODEL_PRICING, cache_read_ratio, model_price
from swebench_eval.harnesses.compaction import OUTPUT_RESERVE

# Placeholder at registration time — control_plane/run_launch.py immediately
# rotates this to the run's own OpenRouter key via /model/update before the
# run's first job is dispatched. Never left pointing at a real shared
# credential. Deliberately obviously-fake (test_rotatable_model_spec_never_
# carries_a_real_looking_shared_key asserts this substring is present).
_PENDING_ROTATION_KEY = "sk-or-unset-pending-rotation"

# rpm/tpm: 2026-08-28 owner decision (interim). The earlier attempt to derive
# a "real" OpenRouter ceiling from historical x-ratelimit-remaining-requests
# in llm_calls was wrong — that header comes from LiteLLM's OWN
# parallel_request_limiter (key_remaining_rpm_limit, litellm/proxy/hooks/
# parallel_request_limiter.py), echoing back our OWN configured rpm, not a
# genuine upstream OpenRouter measurement. No real evidence of OpenRouter's
# actual ceiling exists yet. Set generously for now (200 rpm, 1,000,000 tpm —
# well above a single max-context call) rather than under-provisioning on a
# guess; to be properly tuned against real data once the autoscaler work
# measures actual throughput/throttling.
# 2026-09-01 (exact-design §10 step 0): raised from the 200/1M interim. With the router_settings
# fix these limits become REAL pre-call checks — and the fleet's measured demand (laguna proven
# sustained 4.9M tok/min; 150-task ambitions need many x that) would BIND at 1M tpm, converting
# capacity pressure into per-call gateway 429s (spec §1.3: an unlaunched task costs nothing, a
# throttled launched one burns Fargate + wall clock). Set to never fire: counters as telemetry,
# the L1 pacer (gateway/pacer.py) is the actual admission control.
_RPM = 20_000
_TPM = 100_000_000

# The five harnesses that get a per-harness alias for each model family.
# `aider` is deliberately excluded (see module docstring). `claude_code` is
# handled separately below — it needs the Anthropic-adapter shape, not the
# OpenAI-shaped one the other four share.
_OPENAI_SHAPED_HARNESSES: tuple[str, ...] = ("mini", "codex", "opencode", "custom_minimal")

# The window laguna and qwen serve (both 262,144). A family whose backend serves more passes
# its own — the registered max_input_tokens is what the launch screen offers and what the
# dispatcher resolves the run's context window from (gateway/model_info.py).
_DEFAULT_MAX_INPUT_TOKENS = 262_144


def _price_params(alias: str) -> dict[str, float]:
    """Explicit per-token prices for *alias*, from :mod:`pricing` (owner decision 2026-09-06).

    LiteLLM prices a deployment from its OWN cost table unless the deployment's
    ``litellm_params`` carry ``input_cost_per_token`` / ``output_cost_per_token`` /
    ``cache_read_input_token_cost`` (v1.99.1 ``Router._deployment_model_cost_payload``
    folds every ``CustomPricingLiteLLMParams`` field into the cost-map entry keyed by
    the deployment id, on ``/model/new`` and again on every ``/model/update``).  Its
    table had ``openrouter/minimax/minimax-m2.5`` cache reads at $0.15/M against the
    real $0.03/M, so its spend counter ran ~3.5x the provider's bill — a $10 key cap
    fired at $2.90 of real spend (codex run 01788653487361028986-1de1c022).  The cap
    is gone (``generate_key(max_budget=None)``); this makes the counter — the spend
    log, the dashboard, ``/key/info`` — agree with the OpenRouter bill instead.

    Units are USD per TOKEN (LiteLLM's convention); ``pricing.py`` holds USD per 1M.
    The cache-read price is ``input × cache_read_ratio``, the same product
    :func:`pricing.cost_for` charges locally, so the two accounting paths agree.
    The alias MUST be in ``MODEL_PRICING`` — an unknown alias would inherit the $1/$5
    conservative default and overstate every call ~15x in LiteLLM's counter too.
    """
    if alias not in MODEL_PRICING:
        raise ValueError(
            f"{alias!r} has no MODEL_PRICING entry — every rotatable alias needs one "
            "in the same change that adds it (pricing.py)"
        )
    in_per_m, out_per_m = model_price(alias)
    return {
        "input_cost_per_token": in_per_m / 1_000_000,
        "output_cost_per_token": out_per_m / 1_000_000,
        "cache_read_input_token_cost": in_per_m * cache_read_ratio(alias) / 1_000_000,
    }


@dataclass(frozen=True)
class RotatableModelSpec:
    """The db-model spec :func:`gateway.admin.ensure_model_registered` creates
    on first use.  ``litellm_params``/``model_info`` mirror the shape
    ``litellm_config.yaml`` entries used to use — same backend/window as the
    aliases' former yaml-declared versions, just persisted as a db-model
    instead."""

    model_name: str
    litellm_params: dict[str, object]
    model_info: dict[str, object] = field(default_factory=dict)


def _openai_shaped_spec(
    alias: str,
    *,
    upstream_model: str,
    temperature: float | None,
    top_p: float | None,
    top_k: int | None,
    provider_pin: str | None = None,
    output_cap: int | None = None,
    max_input_tokens: int = _DEFAULT_MAX_INPUT_TOKENS,
    reasoning_effort: str = "high",
) -> RotatableModelSpec:
    """The OpenAI-shaped (openrouter/ provider) variant — mini/codex/opencode/
    custom_minimal. Generation-param tuning matches the model's former plain
    yaml entry exactly (owner-set values, unchanged by this migration).
    ``top_k=None`` sends no top_k at all (the deepseek family: owner set
    temperature + top_p + reasoning only, 2026-09-04). ``temperature=None`` /
    ``top_p=None`` likewise send nothing (the gpt-5-mini family, 2026-09-04:
    OpenAI's reasoning models reject any sampling parameter but the default —
    OpenRouter drops what the pinned provider does not list, but a spec that
    never sends it cannot depend on that).

    ``reasoning_effort`` is per family: "high" for every family the owner tuned
    that way; gpt-5-mini runs "medium" to match the swebench.com mini-SWE-agent
    row it is compared against (59.8 % at $0.04/instance, 2025-08-07).

    ``output_cap`` (F1, 2026-09-04): a deployment-level ``max_tokens`` default —
    LiteLLM's router merges ``litellm_params`` UNDER the request's own kwargs, so
    it applies only when the caller sent no cap. Belt-and-braces behind the
    shim's own injection (local_proxy._inject_max_tokens): without any cap
    Parasail reserves 131,072 output tokens and rejects every prompt past 131K.
    """
    return RotatableModelSpec(
        model_name=alias,
        litellm_params={
            "model": f"openrouter/{upstream_model}",
            "api_base": "https://openrouter.ai/api/v1",
            "api_key": _PENDING_ROTATION_KEY,
            **({"temperature": temperature} if temperature is not None else {}),
            **({"top_p": top_p} if top_p is not None else {}),
            **({"top_k": top_k} if top_k is not None else {}),
            "reasoning_effort": reasoning_effort,
            "allowed_openai_params": ["reasoning_effort", "thinking", "top_k"],
            "rpm": _RPM,
            "tpm": _TPM,
            **_price_params(alias),
            **({"max_tokens": output_cap} if output_cap is not None else {}),
            # F4 (exact-design review, 2026-09-01): provider pin via extra_body — VERIFIED live
            # through a db-model (a pin to a non-allowlisted provider 404s; without the pin the
            # same call 200s). The account allowlist (poolside/parasail/openinference since
            # 2026-09-04; deepinfra removed then) is the PRIMARY enforcement — qwen's other
            # three providers cannot serve this account at all — this pin is defense-in-depth
            # for the day the allowlist changes, so the measured per-provider constants and
            # production traffic can never silently diverge.
            **(
                {"extra_body": {"provider": {"order": [provider_pin], "allow_fallbacks": False}}}
                if provider_pin
                else {}
            ),
        },
        # max_output_tokens is informational to LiteLLM; it tracks the shared compaction
        # reserve (the cap every harness's output is held to), not the provider's own maximum.
        model_info={"max_input_tokens": max_input_tokens, "max_output_tokens": OUTPUT_RESERVE},
    )


def _anthropic_shaped_spec(
    alias: str,
    *,
    upstream_model: str,
    provider_pin: str | None = None,
    output_cap: int | None = None,
    max_input_tokens: int = _DEFAULT_MAX_INPUT_TOKENS,
) -> RotatableModelSpec:
    """The Anthropic-adapter variant — claude_code only. Matches every
    existing Anthropic twin in the (former) yaml exactly: no generation-param
    tuning (temperature/top_p/top_k/reasoning_effort/allowed_openai_params) —
    that pattern is consistent across all three prior examples
    (cheap-oss-model/claude-code-model, qwen3-coder-next/-claude,
    laguna-xs-2.1/-claude), never partial."""
    return RotatableModelSpec(
        model_name=alias,
        litellm_params={
            "model": f"anthropic/{upstream_model}",
            "custom_llm_provider": "anthropic",
            "api_base": "https://openrouter.ai/api",
            "api_key": _PENDING_ROTATION_KEY,
            "rpm": _RPM,
            "tpm": _TPM,
            **_price_params(alias),
            **({"max_tokens": output_cap} if output_cap is not None else {}),
            # F4: on the Anthropic-shaped path this pin is measured INERT (a pin to a blocked
            # provider still 200s — OpenRouter's anthropic-compat endpoint ignores the field,
            # verified live 2026-09-01, and does NOT reject it). Kept for consistency and for
            # any future honoring; the account allowlist is what actually pins this shape.
            **(
                {"extra_body": {"provider": {"order": [provider_pin], "allow_fallbacks": False}}}
                if provider_pin
                else {}
            ),
        },
        model_info={"max_input_tokens": max_input_tokens, "max_output_tokens": OUTPUT_RESERVE},
    )


def _family(
    *,
    prefix: str,
    upstream_model: str,
    temperature: float | None,
    top_p: float | None,
    top_k: int | None,
    provider_pin: str | None = None,
    max_input_tokens: int = _DEFAULT_MAX_INPUT_TOKENS,
    reasoning_effort: str = "high",
) -> dict[str, RotatableModelSpec]:
    """One model family's full per-harness alias set: four OpenAI-shaped +
    one Anthropic-shaped (claude_code). Every harness alias carries the shared
    OUTPUT_RESERVE as its deployment-level max_tokens default (F1).

    *prefix* MUST equal the last segment of *upstream_model* — that segment is
    the POOL alias (:func:`pool_alias_for`), and the planner / pricing / launch
    code build ``f"{pool}-{harness}"`` from it. Asserted, not assumed."""
    if prefix != upstream_model.rsplit("/", 1)[-1]:
        raise ValueError(
            f"family prefix {prefix!r} must equal the upstream slug's last segment "
            f"({upstream_model.rsplit('/', 1)[-1]!r}) — the pool alias is derived from it"
        )
    specs: dict[str, RotatableModelSpec] = {}
    for harness in _OPENAI_SHAPED_HARNESSES:
        alias = f"{prefix}-{harness}"
        specs[alias] = _openai_shaped_spec(
            alias,
            upstream_model=upstream_model,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            provider_pin=provider_pin,
            output_cap=OUTPUT_RESERVE,
            max_input_tokens=max_input_tokens,
            reasoning_effort=reasoning_effort,
        )
    claude_alias = f"{prefix}-claude_code"
    specs[claude_alias] = _anthropic_shaped_spec(
        claude_alias,
        upstream_model=upstream_model,
        provider_pin=provider_pin,
        output_cap=OUTPUT_RESERVE,
        max_input_tokens=max_input_tokens,
    )
    return specs


# laguna-xs-2.1: temperature 1.0 / top_p 1.0 / top_k 20 (owner-set, from the
# former plain yaml entry).
_LAGUNA: dict[str, RotatableModelSpec] = _family(
    prefix="laguna-xs-2.1",
    upstream_model="poolside/laguna-xs-2.1",
    temperature=1.0,
    top_p=1.0,
    top_k=20,
)

# qwen3-coder-next: temperature 1.0 / top_p 0.95 / top_k 40 (owner-set, from
# the former plain yaml entry).
_QWEN: dict[str, RotatableModelSpec] = _family(
    prefix="qwen3-coder-next",
    upstream_model="qwen/qwen3-coder-next",
    temperature=1.0,
    top_p=0.95,
    top_k=40,
    # F4: parasail — the provider the discovery constants were measured against (E15/Q1), the
    # pricing-table provider, and the only qwen provider on the account allowlist.
    provider_pin="parasail",
)

# deepseek-v4-flash-0731 (owner decision 2026-09-04): the third benchmark family — qwen is
# expensive ($0.12/$0.80), this backend is $0.05/$0.16 on OpenInference. Owner-set:
# temperature 1.0 / top_p 0.95 / reasoning, NO top_k (the family helper omits it). The REAL
# window (1,048,576 — the yaml deepseek-flash entry and the judge alias carry the same figure),
# not laguna/qwen's 262,144; the owner chose to benchmark at the full window.
#
# Pinned to OpenInference (F4 discipline: constants are per (model, PROVIDER)): the cheapest
# provider on OpenRouter's endpoint list for this model as of 2026-09-04 ($0.05 in / $0.16 out /
# $0.013 cache-read per 1M, 1M context, 393K max output, fp8), enabled on the account
# allowlist the same day (DeepInfra removed). 29 providers serve this model — an unpinned
# alias would measure one pool and run on another.
#
# The pin SLUG is "open-inference" — the provider's OpenRouter tag is "open-inference/fp8"
# (parasail's is "parasail/fp8", hence the bare "parasail" pin works). Found live 2026-09-04:
# the first probe pinned "openinference" and every call 404'd with a routing funnel of
# 30 endpoints -> 2 after the account allowlist -> 0 after the pin. The slug is NOT the
# display name; read it off /api/v1/models/<slug>/endpoints `tag` before pinning any provider.
_DEEPSEEK: dict[str, RotatableModelSpec] = _family(
    prefix="deepseek-v4-flash-0731",
    upstream_model="deepseek/deepseek-v4-flash-0731",
    temperature=1.0,
    top_p=0.95,
    top_k=None,
    provider_pin="open-inference",
    max_input_tokens=1_048_576,
)

# gpt-5-mini (owner decision 2026-09-04): the cheapest model with a PUBLISHED SWE-bench Verified
# number under one of our own harnesses — swebench.com's mini-SWE-agent board: 59.8 % at
# $0.04/instance (reasoning medium, 2025-08-07; 56.2 % at $0.05 on the 2026-02-17 v2.0.0 row),
# same $3 budget / 250 steps for every row. That makes it the calibration point for the
# framework's own cost accounting, not just a resolve rate.
#
# Sampling: NONE. OpenAI's reasoning models accept no temperature/top_p/top_k (the endpoint's
# supported_parameters list on OpenRouter carries only reasoning/max_tokens/tools/...); the
# family helper omits them entirely. reasoning_effort "medium" = the published row.
#
# Pinned to "openai/flex" (owner decision 2026-09-05, cost): OpenAI's FLEX tier via OpenRouter —
# $0.125 in / $1.00 out / $0.0125 cache-read per 1M, half the standard tier, same 400K context.
# Flex trades price for latency and availability: OpenAI answers 429 "Resource Unavailable"
# (unbilled) when flex capacity is short, and OpenRouter never falls back from a flex pin to
# the default tier (docs/guides/features/service-tiers). The shim treats that 429 as an
# overload (backoff + re-admission); the probe classifies it as strain, so a flex probe seeds
# for the flex pool's real availability, which is the point. Tier-suffixed slugs are the
# documented way to target a tier ("base slugs never match tier endpoints"); the response's
# service_tier field, recorded per call in llm_calls, proves which tier served each call.
# The standard tier is the bare "openai" pin at $0.25 / $2.00 if this is ever flipped back.
#
# Window: the model's REAL INPUT cap, 272,000 (owner decision 2026-09-05: the published SWE-bench
# Verified figures ran at the model's own window, so a 262K cap would make our number
# incomparable). GPT-5's advertised 400K is input + output: OpenRouter's /endpoints lists
# max_prompt_tokens 272,000 and max_completion_tokens 128,000 for both the "openai" and
# "openai/flex" endpoints, and a 400K-registered pool sent ~326K real tokens at probe time —
# every call came back "Your input exceeds the context window of this model" (probe #3,
# 2026-09-05 00:53Z). 272K IS the window a published run hit, so comparability holds.
# Compaction keeps a prompt under min(0.9 W, W - 16,384) = 244,800; the probe's max-context
# call is 270K estimated (~221K real, about $0.03 on flex).
_GPT5_MINI: dict[str, RotatableModelSpec] = _family(
    prefix="gpt-5-mini",
    upstream_model="openai/gpt-5-mini",
    temperature=None,
    top_p=None,
    top_k=None,
    provider_pin="openai/flex",
    reasoning_effort="medium",
    max_input_tokens=272_000,
)

# minimax-m2.5 (owner decision 2026-09-04): the value outlier on the same board — 75.8 % at
# $0.07/instance ("MiniMax M2.5 high" = reasoning effort high, 2026-02-17). Sampling (owner-set
# 2026-09-04): temperature 1.0 / top_p 0.95 / top_k 40. NOTE: MiniMax's own endpoint does not
# list top_k among its supported parameters, so OpenRouter drops it on the pinned pool; Novita,
# SiliconFlow and AtlasCloud honour it at the same list price if the pin is ever moved.
# reasoning_effort "high" = the published row.
#
# Pinned to "minimax" — the model's home provider ($0.30 in / $1.20 out / $0.03 cache-read
# per 1M on the fp8 endpoint, 204,800 context, 131K max output). The same provider slug also
# owns a "minimax/highspeed" endpoint at 2x the price; OpenRouter's default price-sort picks
# fp8 within the pin, and the probe's observation rows record the served provider so a
# mismatch shows up in the first batch, not after the run. StreamLake serves it cheaper
# ($0.27/$1.08) but is not the pool the published number came from.
#
# Window: 204,800 is the provider's TOTAL context (prompt + completion). The compaction
# threshold is min(0.9 W, W - OUTPUT_RESERVE), so a prompt never exceeds W - 16,384 and the
# call fits; the discovery probe sizes its max-context call to W - 2,000.
_MINIMAX: dict[str, RotatableModelSpec] = _family(
    prefix="minimax-m2.5",
    upstream_model="minimax/minimax-m2.5",
    temperature=1.0,
    top_p=0.95,
    top_k=40,
    provider_pin="minimax",
    max_input_tokens=204_800,
)

# Pass B — LLM judge (offline-analysis-design.md §9.2/§10.2, 2026-09-01). A SINGLE alias, not a
# per-harness family: nothing concurrently claims judge-model the way the harness mutex
# (f"{harness}:{model_alias}") protects the two models above — judge passes are serialized
# instead, by their own mutex (§10.2 point 4), because judging is an infrequent, operator-
# triggered, one-shot analysis pass, not something dispatched per-instance like a harness run.
#
# deepseek-v4-flash-0731 (§9.2): NOT one of the two models under comparison (§3.8's
# no-self-preference rule), and it already has a live-verified OpenRouter price in pricing.py
# ($0.06/$0.12 per 1M — re-verified 2026-08-26, not an estimate).
#
# model_info is deepseek-v4-flash-0731's REAL window (1048576/32768, matching cheap-oss-model's
# entry in litellm_config.yaml) — NOT the 262144/32768 the harness families above use (that's
# qwen/laguna's window). judge.py's prune_mode='auto' escalation (§3.4/§9.4) checks the
# assembled trajectory against this number; understating it would trigger pruning the model
# doesn't actually need.
#
# temperature=0.0 (§3.8: reproducibility — accepted as not fully deterministic, hence
# judged_at being part of judge_results' PK rather than an overwrite). top_p/top_k left at
# permissive defaults since temperature already pins the distribution; no provider_pin — the
# account allowlist does not currently restrict deepseek the way it restricts qwen (F4).
_JUDGE_MODEL: dict[str, RotatableModelSpec] = {
    "judge-model": replace(
        _openai_shaped_spec(
            "judge-model",
            upstream_model="deepseek/deepseek-v4-flash-0731",
            temperature=0.0,
            top_p=1.0,
            top_k=0,
        ),
        model_info={"max_input_tokens": 1048576, "max_output_tokens": 32768},
    ),
}

ROTATABLE_MODELS: dict[str, RotatableModelSpec] = {
    **_LAGUNA,
    **_QWEN,
    **_DEEPSEEK,
    **_GPT5_MINI,
    **_MINIMAX,
    **_JUDGE_MODEL,
}


def pool_alias_for(alias: str) -> str | None:
    """The shared-pool alias behind a per-harness rotatable alias.

    Ceiling discovery runs (and seeds ``pacer:cfg:{...}``) under the POOL name —
    the last segment of the upstream slug (``openrouter/poolside/laguna-xs-2.1``
    -> ``laguna-xs-2.1``) — because admission constants are a property of the
    provider pool, not of any one per-harness alias (exact-design §6).  Returns
    None for an alias this registry does not know.
    """
    spec = ROTATABLE_MODELS.get(alias)
    if spec is None:
        return None
    model = str(spec.litellm_params.get("model", ""))
    tail = model.rsplit("/", 1)[-1]
    return tail or None
