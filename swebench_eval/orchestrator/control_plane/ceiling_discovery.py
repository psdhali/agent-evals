"""Ceiling discovery orchestration — Part 1,
BUILDER4-AUTOSCALER-TPM-CEILING-DISCOVERY-DESIGN-2026-08-31.md.

Wires the pure probe logic (:mod:`swebench_eval.gateway.ceiling_discovery`) to the real gateway: a
dedicated, scoped discovery key (never a run's key), the model's real max-context window, and the
observation-log insert (§3's ``model_tpm_observations`` — this task is a one-shot writer, so it
inserts directly; the SQS path in §6.6 is specifically for the live dispatcher's hot-path writes,
not needed here).

**Where this runs is deliberately not decided by this module.** The design doc names an ECS task;
worth reconsidering per the same lesson this session already learned about the autoscaler itself
(§8: don't split into a separate process/deployment unless something concretely requires it) — the
orchestrator already has network access to the gateway (``gateway.admin`` calls it directly today)
and this work is I/O-bound, not compute-heavy. This module exposes the actual work as a plain async
function either path can call — an ECS task's entrypoint, or a FastAPI background task — without
committing to one now.

**Discovery is manual-only (design doc §5, owner's explicit requirement).** Nothing in this module
is self-triggering; every call here is the direct result of an operator action.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from swebench_eval.control import state as control_state
from swebench_eval.gateway import admin as gateway_admin
from swebench_eval.gateway.ceiling_discovery import (
    BatchResult,
    cached_prefix,
    cached_probe_content,
    probe_batch,
)
from swebench_eval.gateway.pacer import pacer_cfg_key
from swebench_eval.gateway.pricing import model_price
from swebench_eval.harnesses.compaction import OUTPUT_RESERVE
from swebench_eval.harnesses.routing import gateway_api_key, gateway_base_url

logger = logging.getLogger(__name__)

_DEFAULT_MAX_BUDGET = 30.0  # USD — a generous cap for the discovery key itself, not the expected
# cost (design doc: ~$20-25/model for a full ramp+bisect); the key's OWN budget is a backstop, not
# the primary cost control (that's the UI's confirm-gated cost preview, §5).
# 2026-09-05 (first gpt-5-mini probe): the ACCOUNT balance, not this cap, is what OpenRouter's
# 402 ("would exceed your available credits given your current in-flight requests") checks —
# it reserves credit per in-flight call against the balance; $7 in the account could not
# cover a 44-call step. Top the account up past the pool's preview before a probe.
_CONTEXT_SAFETY_MARGIN_TOKENS = 2_000  # headroom below the model's real max so the probe request
# itself (role/formatting overhead) never trips the window on its own.
_DISCOVERY_ALIAS_SUFFIX = "-ceiling-discovery"

# The supported pools (design doc §2: "never combine them, never assume one's headroom says
# anything about the other's" — each sits behind its own provider pool). Upstream slugs and
# windows match rotatable_models.py's families exactly — same backend, same real window; this
# is a SEPARATE, dedicated, non-rotated alias, never one of the per-run per-harness ones in
# that module (those get a fresh key every run and are revoked; a discovery alias is registered
# once and reused, key-only churn per invocation).
_UPSTREAM_MODELS: dict[str, str] = {
    "laguna-xs-2.1": "poolside/laguna-xs-2.1",
    "qwen3-coder-next": "qwen/qwen3-coder-next",
    "deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash-0731",
    # 2026-09-04 (owner): the two swebench.com-published pools — see rotatable_models.py
    # _GPT5_MINI / _MINIMAX for the provider, window and sampling decisions.
    "gpt-5-mini": "openai/gpt-5-mini",
    "minimax-m2.5": "minimax/minimax-m2.5",
}
# Exact-design §6: constants are per (model, PROVIDER) — discovery must pin the provider pool
# production rides (pin verified live through LiteLLM -> OpenRouter 2026-09-01). laguna needs no
# request-level pin: the OpenRouter ACCOUNT allowlist (privacy settings) already restricts it to
# Poolside. qwen fans out across 4 providers -> pinned to parasail (the pricing-table provider,
# enabled on the account 2026-09-01). deepseek fans out across ~29 -> pinned to OpenInference
# (owner decision 2026-09-04, the cheapest; enabled on the account the same day). The slug is
# the provider's OpenRouter TAG prefix ("open-inference/fp8" -> "open-inference"), not its
# display name — "openinference" 404'd every call of the first probe (routing funnel 30 -> 2
# after the allowlist -> 0 after the pin). Keep in sync with rotatable_models._DEEPSEEK.
_PROVIDER_PINS: dict[str, str | None] = {
    "laguna-xs-2.1": None,
    "qwen3-coder-next": "parasail",
    "deepseek-v4-flash-0731": "open-inference",
    # Tag prefixes read off /endpoints 2026-09-04. "openai/flex" = OpenAI's FLEX tier (owner
    # decision 2026-09-05, half price; a tier endpoint is only reachable by its suffixed slug —
    # the bare "openai" is the standard tier). "minimax" = the fp8 endpoint (the same slug's
    # "highspeed" tag is 2x).
    "gpt-5-mini": "openai/flex",
    "minimax-m2.5": "minimax",
}
_PROVIDER_LABELS: dict[str, str] = {  # what the observation rows record as `provider`
    "laguna-xs-2.1": "Poolside",
    "qwen3-coder-next": "Parasail",
    "deepseek-v4-flash-0731": "OpenInference",
    "gpt-5-mini": "OpenAI",
    "minimax-m2.5": "MiniMax",
}
# Per-pool window — the probe's "max-context" call and the seed floors are sized to what the
# harness will actually send through that pool (rotatable_models.py's max_input_tokens).
# deepseek: the owner chose to benchmark at the full 1,048,576 window (2026-09-04).
_DEFAULT_MAX_INPUT_TOKENS = 262_144
_MAX_INPUT_TOKENS_BY_POOL: dict[str, int] = {
    "laguna-xs-2.1": 262_144,
    "qwen3-coder-next": 262_144,
    "deepseek-v4-flash-0731": 1_048_576,
    # gpt-5-mini: the endpoint's max_prompt_tokens (400K total = 272K in + 128K out; a 400K
    # registration made every probe call a context-window rejection, 2026-09-05 — see the family)
    "gpt-5-mini": 272_000,
    "minimax-m2.5": 204_800,  # the provider's total context; the probe sizes to W - margin
}


def max_input_tokens_for(model_alias: str) -> int:
    """The pool's real window (what its rotatable family registers), default 262,144."""
    return _MAX_INPUT_TOKENS_BY_POOL.get(model_alias, _DEFAULT_MAX_INPUT_TOKENS)


# Phase B ramp — BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03.md §2.1. The old single
# "paced confirmation" batch proved a LOWER BOUND and was then seeded as if it were the
# ceiling (x0.9 x ~0.63 window dilution x0.6 margin ≈ 0.34 of a serial-queue assumption that
# was itself ~4.6x wrong for a parallel pool — laguna seeded at 11,989 tok/s against a proven
# 105K). Now: start from the owner's model (the burst edge is one LATENCY of service, not one
# minute), step x1.5 until strain, measure the offered rate honestly, seed by what the ramp
# actually found. The starting assumption affects cost only — the ramp stops at strain either
# way, which is the point of ramping.
_RAMP_START_FRACTION = 0.5  # rate_0 = 0.5 x b_edge / L_A
_RAMP_STEP_FACTOR = 1.5
_RAMP_MAX_STEPS = 5  # ~$2.5 on laguna at ~$0.03 per max-context call
_RAMP_BATCH_N = 15  # the FLOOR — see ramp_batch_size
# 2026-09-05 (owner, after the $5 gpt-5-mini probe): TARGET-FIRST ramp mode. The bottom-up ramp
# always bills two or three steps before it finds strain, far below the rate that matters. The
# design's own argument (§2, top-down) is that we never need the true ceiling, only proof that
# the rate the fleet will actually draw is safe. So: step 0 offers the rate that
# `target_tasks` tasks need — tasks x real tokens per turn / (L_A + non-LLM turn time) — and a
# clean step ends the ramp (r_tok = the proven rate, "ceiling unknown, proven at target");
# strain steps DOWN by _TARGET_STEP_DOWN until a step runs clean. Soft strain is judged against
# the burst phase's latency (the lightest paced load the pool sees) since there is no gentle
# step-0 baseline. Never clean within _TARGET_MAX_STEPS -> 0.5 x the last offered rate,
# flagged, like hard_at_start. The bottom-up mode stays selectable per probe.
_DEFAULT_RAMP_MODE = "target_first"
_DEFAULT_TARGET_TASKS = 60  # twice the borrowed-curve cap (30); the most one 60-call step offers
_TARGET_STEP_DOWN = 0.75
_TARGET_MAX_STEPS = 4
# Non-LLM time per agent turn (tool execution, patch/diff, the shim), added to the call latency
# to form the turn period a task's tokens are spread over. Borrowed from the planner's measured
# laguna|mini period (6.0 s total, ~1.4 s of it latency, 2026-09-04); deliberately generous.
_NON_LLM_TURN_S = 4.0
_TARGET_PREVIEW_STEPS = 2  # the preview prices a clean target step plus one step down
# 2026-09-04 (deepseek probe, 1M window): a ramp step of 15 calls cannot offer more than
# ~15 x T / L tok/s whatever its stagger — the offering span always includes the last call's
# latency. At 1M tokens and ~45 s that is ~375K tok/s, and the probe ran "clean to the top" at
# an offered 245K while the pool had absorbed 24 simultaneous 1M calls without a 429: the seed
# was the probe's own ceiling, not the pool's. The batch is now sized so that it keeps
# _RAMP_OVERLAP_LATENCIES x (target_rate x latency / tokens_per_call) calls overlapping — with
# k overlaps the offered rate reaches ~k/(k+1) of the target (k=3 -> 75%) — floored at 15 and
# capped at _RAMP_BATCH_MAX (cost: at 1M tokens x $0.05 a 60-call step is ~$3; also the bound
# the burst phase already proved safe is 24 x 1M, so 60 is a deliberate step past it).
_RAMP_BATCH_MAX = 60
_RAMP_OVERLAP_LATENCIES = 3.0
_SOFT_STRAIN_LATENCY_FACTOR = 1.2  # median latency >= 1.2x the step-0 baseline, zero 429s
_SOFT_STRAIN_MARGIN = 0.85  # the step ran clean; back off the creep, do not push further
_HARD_AT_START_FRACTION = 0.5  # the one case with no clean rate on record — loud, conservative
# Phase D — the cache-hit axis (F3, BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04). Every agent
# harness resends its whole context each turn: 98% of the qwen x mini fleet's prompt tokens were
# cache hits, the probe's r_tok is an UNCACHED number, and the pacer charged every token at full
# price — throttling the fleet to r_tok while Parasail sat ~40x under its real limit. Phase D
# measures how much faster the pool takes a mostly-cached stream: one fixed prefix, warmed once,
# then 15-call steps of prefix + a unique 200-token suffix at 1x, 2x, 4x, 8x Phase B's clean
# rate (total tokens). Same strain rule as Phase B. A step whose responses do not PROVE the
# hits (cached_tokens >= 0.9 x the prefix on >= 90% of its calls) is INVALID — never clean.
# Seed: cached_weight = clamp(r_tok / R_cached, 0.05, 1.0); 8x clean is reported as an upper
# bound on the weight (the pool may be faster still). An empty multiplier tuple skips the phase.
_CACHED_PREFIX_TOKENS = 200_000
_CACHED_PREFIX_HEADROOM = 5_000  # the prefix must sit under the target with room for the suffix
_CACHED_SUFFIX_TOKENS = 200
_CACHED_MULTIPLIERS: tuple[int, ...] = (1, 2, 4, 8)
_CACHED_BATCH_N = 15
_CACHED_HIT_FRACTION = 0.9  # a call proves its hit when cached_tokens >= this x the prefix
_CACHED_VALID_HIT_SHARE = 0.9  # ... on at least this share of the step's successes
_CACHED_WEIGHT_MIN = 0.05
_FALLBACK_MAX_CONTEXT_LATENCY_S = 13.0  # E17 median at 213K real tokens; used only if Phase A
# somehow admitted nothing measurable (it cannot, past the inconclusive guard, but never divide
# by an unmeasured zero)


class CeilingDiscoveryError(RuntimeError):
    """A discovery run could not be carried out or completed."""


def ramp_batch_size(
    target_rate_tok_s: float,
    latency_s: float,
    tokens_per_call: float,
    *,
    floor: int = _RAMP_BATCH_N,
) -> int:
    """How many calls a paced step needs to actually OFFER *target_rate_tok_s*.

    A batch's offering span is ``(n-1) x stagger + latency``; with ``stagger = T / R`` the
    offered rate is ``R x n / (n - 1 + c)`` where ``c = R x L / T`` is the number of calls in
    flight at rate R. ``n = k x c`` gives ~``k/(k+1)`` of R. Floored at *floor* (the pre-2026-09-04
    constant, still right for short calls) and capped at ``_RAMP_BATCH_MAX``."""
    if target_rate_tok_s <= 0 or latency_s <= 0 or tokens_per_call <= 0:
        return floor
    in_flight_at_rate = target_rate_tok_s * latency_s / tokens_per_call
    need = math.ceil(_RAMP_OVERLAP_LATENCIES * in_flight_at_rate)
    return max(floor, min(_RAMP_BATCH_MAX, need))


def _db() -> Any:
    from swebench_eval.database.connection import get_connection

    return get_connection()


def _discovery_alias(model_alias: str) -> str:
    return f"{model_alias}{_DISCOVERY_ALIAS_SUFFIX}"


def upstream_model_for(model_alias: str) -> str:
    """The raw upstream slug for *model_alias* — also the ``pricing.py`` lookup key, kept
    separate from whatever gateway alias actually routes the call (§ below)."""
    upstream = _UPSTREAM_MODELS.get(model_alias)
    if upstream is None:
        raise CeilingDiscoveryError(
            f"{model_alias!r} is not a supported discovery target "
            f"(only {sorted(_UPSTREAM_MODELS)})"
        )
    return upstream


@dataclass(frozen=True)
class DiscoveryUpstreamKey:
    """What one probe holds: the alias to route through and the OpenRouter key hash it was
    rotated onto — the hash is the handle ``disable_discovery_upstream_key`` needs at the end."""

    alias: str
    openrouter_key_hash: str


def ensure_discovery_alias_registered(model_alias: str) -> DiscoveryUpstreamKey:
    """Idempotently register a DEDICATED db-model alias for *model_alias*'s upstream — separate
    from the per-run/per-harness rotatable aliases in ``rotatable_models.py`` — and rotate it
    onto a FRESH upstream OpenRouter key for this probe.

    The caller-facing key minted per invocation (``discover_ceiling``,
    ``gateway_admin.generate_key``) only authenticates the caller to LiteLLM; it is NOT what
    LiteLLM uses to reach OpenRouter — that's this alias's own ``litellm_params.api_key``, which
    the placeholder used before this fix left permanently fake, so every real call through it
    would 401 upstream (found while wiring this up, not assumed).

    Owner decision 2026-09-03 (found live at the bring-up): the key used to be minted ONCE, at
    first creation, and trusted forever — the alias then outlived its key across a teardown
    (the db-model persists in Aurora; the OpenRouter key had been disabled outside the
    framework) and the qwen probe's first batch was twelve upstream ``401 "User not found"``.
    Now every probe mints its own key and ``discover_ceiling`` disables it in its ``finally``,
    exactly the per-run / per-judge-pass lifecycle: a discovery key never outlives its probe.

    Returns the alias plus the OpenRouter key hash to disable afterwards.
    """
    from swebench_eval.gateway import openrouter_admin
    from swebench_eval.orchestrator.control_plane.run_launch import (
        _fetch_openrouter_provisioning_key,
    )

    alias = _discovery_alias(model_alias)
    upstream = upstream_model_for(model_alias)
    base_url, master_key = gateway_base_url(), gateway_api_key()
    litellm_model = f"openrouter/{upstream}"

    model_id = gateway_admin.ensure_model_registered(
        base_url,
        master_key,
        alias,
        litellm_params={
            "model": litellm_model,
            "api_base": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-unset-pending-rotation",  # replaced below, every probe
        },
        model_info={
            "max_input_tokens": max_input_tokens_for(model_alias),
            "max_output_tokens": OUTPUT_RESERVE,
        },
    )

    provisioning_key = _fetch_openrouter_provisioning_key()  # fail closed first, same as run_launch
    # The name is a dashboard label, not an identifier (OpenRouter's identity is the hash and
    # the API enforces no uniqueness); the suffix keeps successive probes' keys — the disabled
    # ones included — tellable apart, matching the LiteLLM key alias below.
    or_raw, or_hash = openrouter_admin.mint_key(
        provisioning_key,
        name=f"ceiling-discovery-{model_alias}-{uuid.uuid4().hex[:8]}",
        limit_usd=_DEFAULT_MAX_BUDGET,
    )
    gateway_admin.rotate_model_key(
        base_url,
        master_key,
        alias,
        model_id,
        or_raw,
        upstream_model=litellm_model,
        litellm_params={
            "model": litellm_model,
            "api_base": "https://openrouter.ai/api/v1",
        },
    )
    logger.info("ceiling discovery alias %s: rotated onto a fresh upstream key", alias)
    _wait_for_alias_ready(base_url, master_key, alias)

    return DiscoveryUpstreamKey(alias=alias, openrouter_key_hash=or_hash)


def disable_discovery_upstream_key(lease: DiscoveryUpstreamKey) -> None:
    """Disable the probe's OpenRouter key (the third kill switch, ADR-0035) — always called
    from ``discover_ceiling``'s ``finally``. Never raises: a failure here must not mask the
    probe's own outcome, but it is an ERROR naming the hash, because the key is then live
    with budget on it and needs manual cleanup."""
    from swebench_eval.gateway import openrouter_admin
    from swebench_eval.orchestrator.control_plane.run_launch import (
        NoProvisioningKeyError,
        _fetch_openrouter_provisioning_key,
    )

    try:
        provisioning_key = _fetch_openrouter_provisioning_key()
        openrouter_admin.disable_key(provisioning_key, lease.openrouter_key_hash)
    except NoProvisioningKeyError:
        logger.error(
            "ceiling discovery %s: cannot disable OpenRouter key (no provisioning key) — "
            "MANUAL CLEANUP NEEDED for hash=%s",
            lease.alias,
            lease.openrouter_key_hash,
        )
    except Exception:
        logger.exception(
            "ceiling discovery %s: disabling OpenRouter key hash=%s FAILED — MANUAL CLEANUP NEEDED",
            lease.alias,
            lease.openrouter_key_hash,
        )


def _wait_for_alias_ready(
    base_url: str, master_key: str, alias: str, *, timeout_s: float = 120.0
) -> None:
    """Found by actually running this end to end, not assumed: LiteLLM's proxy does not apply a
    ``/model/update`` rotation to its in-memory router instantly — a real probe fired immediately
    after ``rotate_model_key`` returns can still 401 upstream against the OLD key for a short
    window, and with two gateway replicas behind the ALB a single 200 proves only one of them.
    Bring-up 2026-09-03 (found live): the old wait returned on the first 200; the stale
    replica's 401 had put its deployment into LiteLLM's 5 s cooldown, and the probe's first
    burst — fired 1.6 s later — read eight synthetic 429s as Parasail overload. Now
    ``gateway_admin.await_alias_served``: a streak of 200s across both replicas that outlasts
    the cooldown, or a loud failure at *timeout_s*.
    """
    try:
        gateway_admin.await_alias_served(
            base_url, master_key, alias, timeout_s=timeout_s, what="rotated discovery key"
        )
    except RuntimeError as exc:
        raise CeilingDiscoveryError(
            f"{exc} — refusing to start the probe against a not-yet-ready alias"
        ) from exc


def resolve_target_tokens(model_alias: str) -> int:
    """The near-max-context size to probe with — the model's real window, minus a safety margin.
    A known constant here (§ above, matching the models' registered ``model_info``), not a live
    lookup — there is no chicken-and-egg risk of reading back data this same module just wrote."""
    upstream_model_for(model_alias)  # raises on an unsupported model, same discipline either way
    return max(1, max_input_tokens_for(model_alias) - _CONTEXT_SAFETY_MARGIN_TOKENS)


def _refuse_if_error_dominated(model_alias: str, phase: str, batch: Any) -> None:
    """2026-09-05 (the first gpt-5-mini probe): three ramp steps with 2/44, 31/60 and 1/60
    successes were classified "clean" — the strain rule looked only at 429s and latency, and
    the 380 non-429 rejections (OpenRouter 402 "would exceed your available credits given
    your current in-flight requests") were invisible until the fourth step had zero successes
    and tripped the inconclusive guard. A step the provider mostly REJECTED measures nothing
    about its capacity; stop there, say what it answered, and bill no more steps."""
    if getattr(batch, "error_dominated", False):
        hist = batch.error_status_histogram() if hasattr(batch, "error_status_histogram") else ""
        hint = ""
        if 402 in getattr(batch, "error_statuses", ()):
            hint = (
                " — 402 = OpenRouter refused for CREDIT: the account balance cannot cover the "
                "credit it reserves for this many in-flight calls; top up past the pool's cost "
                "preview and re-run (the probe key's own limit was not the constraint)"
            )
        raise CeilingDiscoveryError(
            f"discovery {model_alias} {phase}: {batch.error_count} of {batch.concurrency} calls "
            f"errored ({hist or 'no status recorded'}) against {batch.success_count} successes "
            f"and {batch.overload_count} overloads — refusing to classify a step the provider "
            f"mostly rejected{hint}"
        )


def target_rate_tok_s(target_tasks: int, latency_s: float, tokens_per_call: float) -> float:
    """The rate *target_tasks* agent tasks draw at steady state: each task sends one call of
    ``tokens_per_call`` real tokens per turn, one turn per (latency + non-LLM time)."""
    period = max(0.5, latency_s + _NON_LLM_TURN_S)
    return max(1.0, target_tasks * tokens_per_call / period)


def estimate_cost(
    model_alias: str,
    target_tokens: int,
    target_concurrency: int,
    *,
    output_tokens: int = 300,
    ramp_mode: str = "bottom_up",
) -> float:
    """A cost PREVIEW for the UI's confirm gate (design doc §5) — the single top-level probe's
    cost, the expected (success) case. A downward bisection would add more, but the point of a
    preview is the number an operator sees before confirming, not a worst-case bound.

    Prices the FULL multi-axis protocol (run_discovery): burst edge x2 (burst_n then up to 2x)
    + the Phase B ramp at its worst case (every step clean AND every step at the
    _RAMP_BATCH_MAX batch the latency-sized rule can reach — 2026-09-04; the real batch is
    usually smaller on a short-call pool) + the tiny-call request-axis phases (negligible) +
    Phase D at the same worst-case batch. Priced off the raw upstream slug (the ``pricing.py``
    key carrying real pricing); *target_concurrency* is the fleet_target (sizes only the tiny
    burst). Upper bound — rejected calls bill $0.
    """
    pricing_key = _UPSTREAM_MODELS.get(model_alias, model_alias)
    in_price, out_price = model_price(pricing_key)
    per_max_call = (target_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price
    # burst 1 + worst-case burst 2 + the ramp's worst case (all steps clean, max-size batches).
    # Target-first (2026-09-05): a clean target step plus one step down, both at the cap — the
    # expected shape, not five capped steps the mode was built to avoid.
    ramp_steps = _TARGET_PREVIEW_STEPS if ramp_mode == "target_first" else _RAMP_MAX_STEPS
    max_context_calls = 12 + 24 + _RAMP_BATCH_MAX * ramp_steps
    tiny_calls = int(target_concurrency * 1.5) + 900
    per_tiny_call = (200 / 1_000_000) * in_price + (5 / 1_000_000) * out_price
    # Phase D (F3): the warm call + every cached step clean, priced at the FULL input rate —
    # providers bill cache hits at a discount, so this stays an upper bound.
    cached_tokens = (
        min(_CACHED_PREFIX_TOKENS, max(0, target_tokens - _CACHED_PREFIX_HEADROOM))
        + _CACHED_SUFFIX_TOKENS
    )
    per_cached_call = (cached_tokens / 1_000_000) * in_price + (
        output_tokens / 1_000_000
    ) * out_price
    cached_calls = (1 + _RAMP_BATCH_MAX * len(_CACHED_MULTIPLIERS)) if _CACHED_MULTIPLIERS else 0
    return (
        per_max_call * max_context_calls
        + per_tiny_call * tiny_calls
        + per_cached_call * cached_calls
    )


async def run_discovery(
    model_alias: str,
    *,
    fleet_target: int = 150,
    triggered_by: str = "operator",
    burst_n: int = 12,
    inter_phase_gap_s: float = 90.0,
    max_ramp_steps: int = _RAMP_MAX_STEPS,
    ramp_mode: str | None = None,
    target_tasks: int = _DEFAULT_TARGET_TASKS,
    step_down: float = _TARGET_STEP_DOWN,
) -> dict[str, Any]:
    """The full multi-axis discovery protocol (exact-design §6, Phase B revised by
    BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03.md §2.1) — burst edge x2, a paced RAMP to
    strain, request axis — recording one observation row per measured value_kind and seeding
    ``pacer:cfg:{model_alias}``. Manual-only: the UI's Discover action (via the control plane)
    is the only trigger; nothing here schedules itself.

    Phases (each conclusive-or-refuse — the inconclusive guard applies everywhere):
      A. burst edge: *burst_n* x max-context simultaneous, twice, ``inter_phase_gap_s`` apart
         (measured recovery is ~60-90s). If the first burst admits everything, the second runs
         at 2x. B_edge = the smaller conclusive admitted volume (conservative). Its admitted
         calls' median latency L_A is the service-time measurement Phase B starts from.
      B. paced ramp: batches of 15 max-context calls at offered rates rate_0 x 1.5^k starting
         from rate_0 = 0.5 x B_edge / L_A (the burst edge is one LATENCY of service on a
         parallel pool — not one minute), until the first hard strain (a real 429), soft strain
         (median latency >= 1.2x the step-0 baseline with zero 429s — E17's early signal), or
         *max_ramp_steps*. Every step's offered rate is measured over its real offering span.
         Seed r_tok: hard -> the highest CLEAN rate (the x1.5 step is the margin); soft -> 0.85 x
         that step's rate; clean to the top -> 1.0 x the top rate; hard at step 0 -> 0.5 x
         step-0 rate, flagged. Never a flat 0.6 (design doc §2.1 records why that was a guess).
      C. request axis: tiny-call burst at 1.5x *fleet_target* (seeds C_req), then a 5/s 60s
         QPS ramp, escalating to 10/s only if clean (seeds R_qps). Token volume is trivial —
         this measures the REQUEST limiter the token phases cannot see (E12-E16).

    Also logs the consistency ratio r_tok / (k_inflight / L_A) (§2.3): below ~0.5 the arrival
    bucket binds before the in-flight cap — the exact starvation mode of run
    01788405363237319353 — legitimate only for a serial provider. A diagnostic, never forced.
    """
    if control_state.is_paused("gateway"):
        raise CeilingDiscoveryError(
            "gateway is globally paused — refusing to run a discovery probe "
            "(resume gateway via /control/resume first)"
        )
    ramp_mode = ramp_mode or _DEFAULT_RAMP_MODE
    if ramp_mode not in ("target_first", "bottom_up"):
        raise CeilingDiscoveryError(f"unknown ramp_mode {ramp_mode!r}")
    if target_tasks < 1:
        raise CeilingDiscoveryError("target_tasks must be >= 1")

    target_tokens = resolve_target_tokens(model_alias)
    pin = _PROVIDER_PINS.get(model_alias)
    provider_label = _PROVIDER_LABELS.get(model_alias)
    base_url, master_key = gateway_base_url(), gateway_api_key()
    lease = ensure_discovery_alias_registered(model_alias)
    discovery_alias = lease.alias

    key_alias = f"ceiling-discovery-{model_alias}-{uuid.uuid4().hex[:8]}"
    raw_key, _key_id = gateway_admin.generate_key(
        base_url,
        master_key,
        key_alias=key_alias,
        models=[discovery_alias],
        max_budget=_DEFAULT_MAX_BUDGET,
        metadata={"purpose": "ceiling_discovery", "triggered_by": triggered_by},
    )

    async def _batch(
        concurrency: int,
        tokens: int,
        stagger_s: float = 0.0,
        content_factory: Callable[[], str] | None = None,
    ) -> BatchResult:
        result = await probe_batch(
            base_url=base_url,
            api_key=raw_key,
            model=discovery_alias,
            concurrency=concurrency,
            target_tokens=tokens,
            provider_pin=pin,
            stagger_s=stagger_s,
            window_s=max(60.0, concurrency * stagger_s + 60.0),
            content_factory=content_factory,
        )
        if result.inconclusive:
            raise CeilingDiscoveryError(
                f"inconclusive batch at concurrency={concurrency} "
                f"({result.error_count} errors, 0 successes, 0 overloads) — refusing to "
                "derive any constant from a batch that never exercised the provider"
            )
        return result

    report: dict[str, Any] = {"model_alias": model_alias, "provider": provider_label}
    # Phase D state, read after the try (seeds are computed once every phase concluded).
    cached_steps: list[dict[str, Any]] = []
    cached_outcome = "skipped"  # skipped | invalid | strain | upper_bound
    cached_rate_tok_s = 0.0
    prefix_tokens = min(_CACHED_PREFIX_TOKENS, max(0, target_tokens - _CACHED_PREFIX_HEADROOM))
    try:
        # -- Phase A: burst edge x2 ---------------------------------------------------------
        b1 = await _batch(burst_n, target_tokens)
        n2 = burst_n * 2 if not b1.overloaded else burst_n
        await asyncio.sleep(inter_phase_gap_s)
        b2 = await _batch(n2, target_tokens)
        edges = [b.tokens_in_window for b in (b1, b2) if b.overloaded]
        if edges:
            b_edge = min(edges)  # conservative: the smaller measured edge
            edge_found = True
        else:
            b_edge = max(b1.tokens_in_window, b2.tokens_in_window)
            edge_found = False  # never overloaded — b_edge is a LOWER BOUND, flagged as such
        report.update(burst_edge_tokens=b_edge, edge_found=edge_found)

        # -- Phase B: paced RAMP to strain (design doc §2.1) --------------------------------
        await asyncio.sleep(inter_phase_gap_s)
        tok_real_est = int(target_tokens * 0.82)  # measured real/target ratio
        # L_A: the service time the burst edge is "one of" — the median latency of the calls
        # Phase A admitted. Previously measured and discarded; the old code assumed 60s here.
        latency_a = _phase_a_latency_s(b1, b2)
        rate_0 = max(1.0, _RAMP_START_FRACTION * b_edge / latency_a)
        ramp_steps: list[dict[str, Any]] = []
        ramp_outcome = "clean"  # hard | soft | clean | hard_at_start
        r_tok_basis = 0.0
        strain_rate_tok_s = 0.0
        last_clean_rate = 0.0
        baseline_latency: float | None = None
        last_latency: float | None = None
        rate_target = target_rate_tok_s(target_tasks, latency_a, tok_real_est)
        report.update(ramp_mode=ramp_mode, target_tasks=target_tasks)
        if ramp_mode == "target_first":
            # -- target-first: prove the rate the fleet needs, step DOWN only on strain -----
            report.update(target_rate_tok_s=round(rate_target, 1))
            rate_0 = rate_target
            ramp_outcome = "strained_to_floor"  # until a step runs clean
            for step in range(max(1, _TARGET_MAX_STEPS)):
                if step > 0:
                    await asyncio.sleep(inter_phase_gap_s / 3.0)
                target_rate = rate_target * (step_down**step)
                stagger = tok_real_est / target_rate
                n_calls = ramp_batch_size(target_rate, last_latency or latency_a, tok_real_est)
                batch = await _batch(n_calls, target_tokens, stagger_s=stagger)
                _refuse_if_error_dominated(model_alias, f"target step {step}", batch)
                offered = batch.offered_rate_tok_s(stagger)
                med = batch.median_latency_s
                if med is not None:
                    last_latency = med
                # No gentle baseline step exists here: the burst's median latency is the
                # lightest paced load the pool saw, so it is the soft-strain reference.
                soft = (
                    not batch.overloaded
                    and med is not None
                    and med >= _SOFT_STRAIN_LATENCY_FACTOR * latency_a
                )
                outcome = "hard" if batch.overloaded else ("soft" if soft else "clean")
                ramp_steps.append(
                    {
                        "step": step,
                        "n": n_calls,
                        "target_rate_tok_s": round(target_rate, 1),
                        "stagger_s": round(stagger, 3),
                        "offered_rate_tok_s": round(offered, 1),
                        "median_latency_s": round(med, 3) if med is not None else None,
                        "ok": batch.success_count,
                        "n429": batch.overload_count,
                        "errors": batch.error_count,
                        "outcome": outcome,
                    }
                )
                logger.info(
                    "discovery %s target step %d (n=%d, x%.2f of target): target %.0f tok/s "
                    "offered %.0f tok/s median latency %s ok=%d n429=%d -> %s",
                    model_alias,
                    step,
                    n_calls,
                    step_down**step,
                    target_rate,
                    offered,
                    f"{med:.2f}s" if med is not None else "n/a",
                    batch.success_count,
                    batch.overload_count,
                    outcome,
                )
                if outcome == "clean":
                    last_clean_rate = offered if offered > 0 else target_rate
                    r_tok_basis = last_clean_rate  # proven at this rate; ceiling unknown
                    ramp_outcome = "clean_at_target" if step == 0 else "clean_after_step_down"
                    break
                strain_rate_tok_s = offered if offered > 0 else target_rate
            if ramp_outcome == "strained_to_floor":
                r_tok_basis = _HARD_AT_START_FRACTION * strain_rate_tok_s
                logger.warning(
                    "discovery %s: no clean step within %d step-downs from the %d-task target "
                    "(%.0f tok/s) — seeding r_tok at %.0f (0.5x the last offered rate). Lower "
                    "the target or re-run once the pool is quiet.",
                    model_alias,
                    _TARGET_MAX_STEPS,
                    target_tasks,
                    rate_target,
                    r_tok_basis,
                )
        bottom_up_steps = range(max(1, max_ramp_steps)) if ramp_mode == "bottom_up" else range(0)
        for step in bottom_up_steps:
            if step > 0:
                # Let the previous step's tail complete so its in-flight calls do not inflate
                # the next step's arrival (a third of the between-phase gap; 0 in tests).
                await asyncio.sleep(inter_phase_gap_s / 3.0)
            target_rate = rate_0 * (_RAMP_STEP_FACTOR**step)
            stagger = tok_real_est / target_rate
            # Sized to the pool's latency (the previous step's median once there is one, else
            # L_A) so the step can actually offer its target — see ramp_batch_size.
            n_calls = ramp_batch_size(target_rate, last_latency or latency_a, tok_real_est)
            batch = await _batch(n_calls, target_tokens, stagger_s=stagger)
            _refuse_if_error_dominated(model_alias, f"ramp step {step}", batch)
            offered = batch.offered_rate_tok_s(stagger)
            med = batch.median_latency_s
            if med is not None:
                last_latency = med
            if baseline_latency is None and med is not None:
                baseline_latency = med
            soft = (
                not batch.overloaded
                and step > 0
                and med is not None
                and baseline_latency is not None
                and med >= _SOFT_STRAIN_LATENCY_FACTOR * baseline_latency
            )
            outcome = "hard" if batch.overloaded else ("soft" if soft else "clean")
            ramp_steps.append(
                {
                    "step": step,
                    "n": n_calls,
                    "target_rate_tok_s": round(target_rate, 1),
                    "stagger_s": round(stagger, 3),
                    "offered_rate_tok_s": round(offered, 1),
                    "median_latency_s": round(med, 3) if med is not None else None,
                    "ok": batch.success_count,
                    "n429": batch.overload_count,
                    "errors": batch.error_count,
                    "outcome": outcome,
                }
            )
            logger.info(
                "discovery %s ramp step %d (n=%d): target %.0f tok/s offered %.0f tok/s "
                "median latency %s ok=%d n429=%d -> %s",
                model_alias,
                step,
                n_calls,
                target_rate,
                offered,
                f"{med:.2f}s" if med is not None else "n/a",
                batch.success_count,
                batch.overload_count,
                outcome,
            )
            if batch.overloaded:
                strain_rate_tok_s = offered if offered > 0 else target_rate
                if last_clean_rate > 0:
                    ramp_outcome = "hard"
                    r_tok_basis = last_clean_rate  # the x1.5 step IS the margin
                else:
                    ramp_outcome = "hard_at_start"
                    r_tok_basis = _HARD_AT_START_FRACTION * strain_rate_tok_s
                    logger.warning(
                        "discovery %s: HARD strain at ramp step 0 (%.0f tok/s) — no clean "
                        "rate on record; seeding r_tok at %.0f (0.5x). Re-run once the pool "
                        "is quiet, or expect L2 growth to do the climbing.",
                        model_alias,
                        strain_rate_tok_s,
                        r_tok_basis,
                    )
                break
            if soft:
                ramp_outcome = "soft"
                strain_rate_tok_s = offered
                # Review F3 (2026-09-03): 0.85 × the STRAINED step's offered rate could exceed
                # the highest rate ever proven clean (×1.5 steps: 0.85 × 1.5 = 1.275, and 1.53×
                # demonstrated when latency inflation shrank the offered ratio less than the
                # step). The seed backs off the creep AND never exceeds what was proven clean.
                soft_basis = _SOFT_STRAIN_MARGIN * offered
                r_tok_basis = (
                    min(soft_basis, last_clean_rate) if last_clean_rate > 0 else soft_basis
                )
                break
            last_clean_rate = offered if offered > 0 else target_rate
            r_tok_basis = last_clean_rate  # clean to the top -> 1.0x, proven by definition
        report.update(
            latency_a_s=round(latency_a, 3),
            ramp_rate_0_tok_s=round(rate_0, 1),
            ramp_steps=ramp_steps,
            ramp_outcome=ramp_outcome,
            r_tok_basis_tok_s=round(r_tok_basis, 1),
            strain_rate_tok_s=round(strain_rate_tok_s, 1),
        )

        # -- Phase C: request axis ----------------------------------------------------------
        await asyncio.sleep(inter_phase_gap_s)
        tiny = await _batch(max(10, int(fleet_target * 1.5)), 200)
        c_req_seed = max(1, int(tiny.success_count * 0.8))
        ramp5 = await _batch(300, 200, stagger_s=0.2)  # 5/s for 60s
        clean_qps = 0.0
        if not ramp5.overloaded:
            clean_qps = 5.0
            ramp10 = await _batch(600, 200, stagger_s=0.1)  # 10/s for 60s
            if not ramp10.overloaded:
                clean_qps = 10.0
            else:
                clean_qps = max(1.0, ramp10.success_count / 60.0)
        else:
            clean_qps = max(0.5, ramp5.success_count / 60.0)
        r_qps_seed = round(0.8 * clean_qps, 2)
        report.update(req_burst_admitted=tiny.success_count, clean_qps=clean_qps)

        # -- Phase D: cache-hit axis (F3) ---------------------------------------------------
        if _CACHED_MULTIPLIERS and r_tok_basis > 0 and prefix_tokens > 0:
            await asyncio.sleep(inter_phase_gap_s)
            prefix = cached_prefix(prefix_tokens)

            def _cached_content() -> str:
                return cached_probe_content(prefix, _CACHED_SUFFIX_TOKENS)

            per_call_tokens = prefix_tokens + _CACHED_SUFFIX_TOKENS
            per_call_real = int(per_call_tokens * 0.82)  # the same measured real/target ratio
            min_cached = int(_CACHED_HIT_FRACTION * prefix_tokens * 0.82)
            # Warm the prefix once (its own cost is one max-context call; never a step).
            await _batch(1, per_call_tokens, content_factory=_cached_content)
            cached_outcome = "upper_bound"  # every step clean unless a step says otherwise
            cached_baseline: float | None = None
            cached_last_latency: float | None = None
            for step, mult in enumerate(_CACHED_MULTIPLIERS):
                if step > 0:
                    await asyncio.sleep(inter_phase_gap_s / 3.0)
                target_rate = mult * r_tok_basis
                stagger = per_call_real / target_rate
                # Same sizing rule as the ramp (cached calls are usually fast, so this mostly
                # stays at the floor — but a pool that serves cached prompts SLOWLY, as
                # OpenInference did, would otherwise be judged on an under-offered step).
                n_cached = ramp_batch_size(
                    target_rate,
                    cached_last_latency or latency_a,
                    per_call_real,
                    floor=_CACHED_BATCH_N,
                )
                batch = await _batch(
                    n_cached,
                    per_call_tokens,
                    stagger_s=stagger,
                    content_factory=_cached_content,
                )
                _refuse_if_error_dominated(model_alias, f"cached step {step}", batch)
                offered = batch.offered_rate_tok_s(stagger)
                med = batch.median_latency_s
                hit_share = batch.cache_hit_share(min_cached)
                if med is not None:
                    cached_last_latency = med
                if cached_baseline is None and med is not None:
                    cached_baseline = med
                valid = hit_share >= _CACHED_VALID_HIT_SHARE
                soft = (
                    not batch.overloaded
                    and step > 0
                    and med is not None
                    and cached_baseline is not None
                    and med >= _SOFT_STRAIN_LATENCY_FACTOR * cached_baseline
                )
                if not valid:
                    outcome = "invalid"
                elif batch.overloaded:
                    outcome = "hard"
                elif soft:
                    outcome = "soft"
                else:
                    outcome = "clean"
                cached_steps.append(
                    {
                        "step": step,
                        "n": n_cached,
                        "multiplier": mult,
                        "target_rate_tok_s": round(target_rate, 1),
                        "stagger_s": round(stagger, 3),
                        "offered_rate_tok_s": round(offered, 1),
                        "median_latency_s": round(med, 3) if med is not None else None,
                        "cache_hit_share": round(hit_share, 3),
                        "ok": batch.success_count,
                        "n429": batch.overload_count,
                        "errors": batch.error_count,
                        "outcome": outcome,
                    }
                )
                logger.info(
                    "discovery %s cached step %d (x%d): target %.0f tok/s offered %.0f tok/s "
                    "hit_share %.2f median latency %s ok=%d n429=%d -> %s",
                    model_alias,
                    step,
                    mult,
                    target_rate,
                    offered,
                    hit_share,
                    f"{med:.2f}s" if med is not None else "n/a",
                    batch.success_count,
                    batch.overload_count,
                    outcome,
                )
                if outcome == "invalid":
                    # The provider did not prove the hits: nothing here says anything about
                    # a cached stream. Weight stays 1.0 (full price) — the safe direction.
                    cached_outcome = "invalid"
                    cached_rate_tok_s = 0.0
                    break
                if outcome in ("hard", "soft"):
                    cached_outcome = "strain"  # the last CLEAN step is the cached rate
                    break
                cached_rate_tok_s = offered if offered > 0 else target_rate
    finally:
        # Always revoke, success or failure — a discovery key must never outlive its probe:
        # the LiteLLM caller key AND the alias's upstream OpenRouter key (owner decision
        # 2026-09-03; the upstream one used to be minted once and kept).
        try:
            gateway_admin.delete_key(base_url, master_key, key_alias)
        finally:
            disable_discovery_upstream_key(lease)

    # -- record + seed (only after every phase concluded) -----------------------------------
    k_inflight = int(0.95 * b_edge)
    r_tok = max(1, int(r_tok_basis))
    c_burst = int(0.5 * b_edge)
    # Review F1 (2026-09-03): a bucket smaller than ONE max-context call is a seed the pacer
    # can never satisfy for that call (the Lua now clamps, so the call admits alone — but a
    # burst bucket below one call means every max-context call drains it negative and the
    # alias serialises on refill). Floor both at the largest call the gateway admits and say
    # so in the report; a small Parasail burst edge must be a launch-time fact, not a silent
    # four-minute stall.
    seed_floored: list[str] = []
    window = max_input_tokens_for(model_alias)
    if c_burst < window:
        seed_floored.append(f"c_burst {c_burst} -> {window}")
        c_burst = window
    if k_inflight < window:
        seed_floored.append(f"k_inflight {k_inflight} -> {window}")
        k_inflight = window
    if seed_floored:
        logger.warning(
            "discovery %s: burst edge %d is below ONE max-context call (%d) — seeds floored: %s. "
            "The pool cannot take a full-context call in one burst; expect max-context calls "
            "to serialise on refill.",
            model_alias,
            b_edge,
            window,
            "; ".join(seed_floored),
        )
    # F3: the cached-token weight — what one cached prompt token costs the pool relative to an
    # uncached one. 1.0 (full price) unless Phase D proved a faster cached rate; a strain at
    # step 0 leaves cached_rate_tok_s at 0 and the weight at 1.0 (cached is no cheaper).
    cached_weight = 1.0
    if cached_outcome in ("strain", "upper_bound") and cached_rate_tok_s > 0:
        cached_weight = min(1.0, max(_CACHED_WEIGHT_MIN, r_tok / cached_rate_tok_s))
    cached_weight_is_bound = cached_outcome == "upper_bound" and cached_rate_tok_s > 0
    (logger.warning if cached_outcome in ("invalid", "skipped") else logger.info)(
        "discovery %s: cache-hit axis %s — cached rate %.0f tok/s vs r_tok %d -> "
        "cached_weight %.3f%s",
        model_alias,
        cached_outcome,
        cached_rate_tok_s,
        r_tok,
        cached_weight,
        " (UPPER BOUND: every step was clean)" if cached_weight_is_bound else "",
    )
    report.update(
        cached_prefix_tokens=prefix_tokens,
        cached_steps=cached_steps,
        cached_outcome=cached_outcome,
        cached_rate_tok_s=round(cached_rate_tok_s, 1),
        cached_weight=round(cached_weight, 4),
        cached_weight_is_bound=cached_weight_is_bound,
    )
    seeds = {
        # 0.5: the ONE margin with a measurement behind it (E6 hot sag to 0.49x the cold edge);
        # ~3s of refill once r_tok is sane, and loosening it risks a spurious 429 that L2 would
        # treat as a real overload and use to RESET r_tok (design doc §2.2).
        "c_burst": c_burst,
        # What the ramp found. No flat margin — the old 0.6 was a guess against a sustained-rate
        # sag never observed, applied on top of a lower bound (design doc §1, §2.1).
        "r_tok": r_tok,
        "k_inflight": k_inflight,
        "c_req": c_req_seed,
        "r_qps": r_qps_seed,
        # Review F2: the discovered seeds, kept beside the live values so the planner's growth
        # is bounded relative to the only rate ever measured clean (and its overload recovery
        # floored the same way). Growth rewrites r_tok/k_inflight/r_qps, never these.
        "r_tok_seed": r_tok,
        "k_inflight_seed": k_inflight,
        "r_qps_seed": r_qps_seed,
        # Review F4: the pool's OWN max-context call latency (Phase A median), so a planner
        # running on a borrowed latency curve has a measured floor from this pool.
        "latency_s_max_context": round(latency_a, 3),
        # F3: the pacer's weighted charge (shim + Lua) and the planner's arrival projection
        # read this; the seed copy is the measured value growth never touches.
        "cached_weight": round(cached_weight, 4),
        "cached_weight_seed": round(cached_weight, 4),
    }
    report["seed_floored"] = seed_floored
    # §2.3 consistency: the in-flight cap turns over k_inflight tokens every L_A seconds. If the
    # arrival bucket refills slower than that, it binds FIRST and is an artificial limit — the
    # starvation mode of run 01788405363237319353 (r_tok 12K vs ~156K turnover, ratio 0.08).
    # Legitimate only for a serial provider. A diagnostic, never a forced floor.
    inflight_turnover_tok_s = k_inflight / latency_a
    consistency_ratio = r_tok / inflight_turnover_tok_s if inflight_turnover_tok_s > 0 else 0.0
    (logger.warning if consistency_ratio < 0.5 else logger.info)(
        "discovery %s: r_tok=%d tok/s vs k_inflight/L_A=%.0f tok/s (ratio %.2f) — %s",
        model_alias,
        r_tok,
        inflight_turnover_tok_s,
        consistency_ratio,
        (
            "arrival bucket will bind BEFORE the in-flight cap (expected only for a serial "
            "provider; on a parallel pool this is the starvation mode)"
            if consistency_ratio < 0.5
            else "in-flight cap is the binding physical constraint, as intended"
        ),
    )
    report.update(
        inflight_turnover_tok_s=round(inflight_turnover_tok_s, 1),
        consistency_ratio=round(consistency_ratio, 3),
    )
    observations = [
        ("burst_admission_tokens", b_edge, burst_n),
        ("paced_rate_tok_per_s", int(r_tok_basis), _RAMP_BATCH_N),
        ("ramp_strain_rate_tok_per_s", int(strain_rate_tok_s), _RAMP_BATCH_N),  # 0 = none seen
        ("call_latency_ms_max_context", int(latency_a * 1000), burst_n),
        ("req_burst_admitted", tiny.success_count, int(fleet_target * 1.5)),
        ("req_qps_clean", int(clean_qps * 100), None),  # x100: the column is BIGINT
        # F3: the cache-hit axis (0 = not measured / invalid) and the weight x1000 (BIGINT).
        ("cached_rate_tok_per_s", int(cached_rate_tok_s), _CACHED_BATCH_N),
        ("cached_weight_x1000", round(cached_weight * 1000), None),
    ]
    _insert_observations(model_alias, provider_label, observations, triggered_by, report)
    # Owner decision 2026-09-04: the seeds outlive the Valkey cache (eval tier) as a row —
    # ONE stamp shared by the row and the live hash, so a rehydration restores the same clock.
    seeded_at = time.time()
    _persist_seeds(model_alias, seeds, seeded_at, provider_label, triggered_by, report)
    _seed_pacer_cfg(model_alias, {**seeds, "seeded_at": seeded_at})
    report["seeds"] = seeds
    logger.info("discovery complete for %s: %s", model_alias, report)
    return report


def _phase_a_latency_s(b1: BatchResult, b2: BatchResult) -> float:
    """L_A — the median per-call latency of the calls Phase A ADMITTED, taken from the burst
    with the better-populated sample. This is the measured service time the burst edge is
    "one of" (design doc §1: on a parallel pool the edge turns over every ~13s, not every
    60s). Falls back to E17's measured 13.0s only if neither burst produced a latency — which
    the inconclusive guard already makes impossible — never to an unmeasured zero."""
    best = max((b1, b2), key=lambda b: b.success_count)
    med = best.median_latency_s
    if med is None:
        other = b2 if best is b1 else b1
        med = other.median_latency_s
    if med is None or med <= 0:
        logger.warning(
            "discovery: Phase A yielded no per-call latency; using the E17 fallback %.1fs",
            _FALLBACK_MAX_CONTEXT_LATENCY_S,
        )
        return _FALLBACK_MAX_CONTEXT_LATENCY_S
    return med


def _insert_observations(
    model_alias: str,
    provider: str | None,
    rows: list[tuple[str, int, int | None]],
    triggered_by: str,
    report: dict[str, Any],
) -> None:
    """Direct insert — this is a one-shot task with no hot path to protect (the SQS path in the
    design's §6.6 is for the live dispatcher's tick loop, not for discovery)."""
    task_id = os.environ.get("ECS_TASK_ARN")
    conn = _db()
    try:
        with conn.cursor() as cur:
            for value_kind, value, at_concurrency in rows:
                cur.execute(
                    """INSERT INTO model_tpm_observations
                           (model_alias, run_id, event_type, value_kind, tpm_value,
                            at_concurrency, provider, task_id, notes)
                       VALUES (%s, NULL, 'discovery_initial', %s, %s, %s, %s, %s, %s)""",
                    (
                        model_alias,
                        value_kind,
                        value,
                        at_concurrency,
                        provider,
                        task_id,
                        (
                            f"triggered_by={triggered_by} edge_found={report.get('edge_found')} "
                            f"ramp_outcome={report.get('ramp_outcome')} "
                            f"consistency_ratio={report.get('consistency_ratio')}"
                        ),
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def _persist_seeds(
    model_alias: str,
    seeds: dict[str, Any],
    seeded_at: float,
    provider: str | None,
    triggered_by: str,
    report: dict[str, Any],
) -> None:
    """The durable copy (``pacer_cfg_seeds``; control_plane/pacer_seeds.py). A failure here is
    an ERROR, not a probe failure: the live seed below still lands, but the next bring-up will
    need another probe — say so."""
    from swebench_eval.orchestrator.control_plane import pacer_seeds

    try:
        pacer_seeds.persist_seeds(
            model_alias,
            seeds,
            seeded_at,
            provider=provider,
            triggered_by=triggered_by,
            report=report,
        )
    except Exception:
        logger.exception(
            "discovery %s: persisting the seeds to Aurora FAILED — the live pacer:cfg is still "
            "seeded, but it will not survive the next eval-tier destroy (re-probe then)",
            model_alias,
        )


def _seed_pacer_cfg(model_alias: str, seeds: dict[str, Any]) -> None:
    """Write the margined constants into the LIVE pacer config — this is the whole point of
    discovery: the L1 pacer and L2 planner start from a fresh measurement, not a default."""
    from swebench_eval.database.redis_client import _get_client

    # F3 (exact-design review): the planner's staleness policy keys on this stamp — past
    # PACER_CFG_MAX_AGE_S it halves its utilization margins until re-stamped. The caller may
    # pass the stamp (shared with the persisted row) — else it is now.
    seeded_at = float(seeds.get("seeded_at") or time.time())
    try:
        _get_client().hset(
            pacer_cfg_key(model_alias),
            mapping={
                **{k: repr(v) for k, v in seeds.items() if k != "seeded_at"},
                "seeded_at": repr(seeded_at),
            },
        )
    except Exception:
        # seed leaves the pacer on conservative defaults (the safe direction), logged loudly.
        logger.warning("discovery: pacer:cfg seed failed for %s", model_alias, exc_info=True)


def _max_age_days() -> int:
    """§4.1 of the design doc: TPM_CEILING_MAX_AGE_DAYS, default 15 — a sensible default is fine
    here (unlike MAX_CONCURRENT_HARNESS_TASKS's refuse-to-start-unset rule), since there is no
    unsafe direction to defaulting a staleness ceiling the way there is for a concurrency one."""
    raw = os.environ.get("TPM_CEILING_MAX_AGE_DAYS", "15")
    try:
        return int(raw)
    except ValueError:
        return 15


def list_ceilings(model_aliases: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    """Current `model_ceilings` view rows for *model_aliases* — always one entry per requested
    alias, even with no observation yet (§5's UI panel needs to show all known models, not only
    ones already discovered)."""
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT model_alias, value_kind, value, ceiling_source, discovered_at, provider
                   FROM model_ceilings WHERE model_alias = ANY(%s)""",
                (list(model_aliases),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    by_alias: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        by_alias.setdefault(row[0], []).append(row)

    max_age_days = _max_age_days()
    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for alias in model_aliases:
        alias_rows = by_alias.get(alias, [])
        values = {r[1]: int(r[2]) for r in alias_rows}
        # The headline value: the burst-admission edge (the primary discovered constant), with
        # a legacy fallback to old single-value 'tpm' rows so pre-migration data still renders.
        headline_row = next(
            (r for r in alias_rows if r[1] == "burst_admission_tokens"),
            next((r for r in alias_rows if r[1] == "tpm"), None),
        )
        if headline_row is None:
            out.append(
                {
                    "model_alias": alias,
                    "discovered_tpm": None,
                    "ceiling_source": None,
                    "discovered_at": None,
                    "provider": None,
                    "values": values or None,
                    "is_stale": False,
                }
            )
            continue
        _, _, value, source, discovered_at, provider = headline_row
        age_days = (now - discovered_at).total_seconds() / 86400.0
        out.append(
            {
                "model_alias": alias,
                "discovered_tpm": int(value),
                "ceiling_source": source,
                "discovered_at": discovered_at.isoformat(),
                "provider": provider,
                "values": values,
                "is_stale": age_days > max_age_days,
            }
        )
    return out


def record_manual_ceiling(
    model_alias: str,
    tpm_value: int,
    *,
    value_kind: str = "burst_admission_tokens",
    notes: str | None = None,
) -> None:
    """Operator manual entry (§5) — no ECS/network work at all, just a log row."""
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO model_tpm_observations
                       (model_alias, run_id, event_type, value_kind, tpm_value, notes)
                   VALUES (%s, NULL, 'manual', %s, %s, %s)""",
                (model_alias, value_kind, tpm_value, notes),
            )
        conn.commit()
    finally:
        conn.close()
    logger.info("manual ceiling entry for %s: %d tpm", model_alias, tpm_value)


def main() -> None:
    """One-shot ECS entrypoint (exact-design §6/§8): the RunTask-launched discovery probe.

    Inputs via env (RunTask containerOverrides): DISCOVERY_MODEL_ALIAS (required),
    DISCOVERY_FLEET_TARGET (default 150), DISCOVERY_TRIGGERED_BY. Exits non-zero on any
    failure so the task's stopped-reason carries the truth — never a silent success.
    """
    from swebench_eval.logging_bootstrap import configure_logging

    configure_logging()
    model_alias = os.environ.get("DISCOVERY_MODEL_ALIAS", "")
    if not model_alias:
        raise SystemExit("DISCOVERY_MODEL_ALIAS is required")
    fleet_target = int(os.environ.get("DISCOVERY_FLEET_TARGET", "150"))
    triggered_by = os.environ.get("DISCOVERY_TRIGGERED_BY", "operator")
    ramp_mode = os.environ.get("DISCOVERY_RAMP_MODE") or None
    target_tasks = int(os.environ.get("DISCOVERY_TARGET_TASKS", str(_DEFAULT_TARGET_TASKS)))
    report = asyncio.run(
        run_discovery(
            model_alias,
            fleet_target=fleet_target,
            triggered_by=triggered_by,
            ramp_mode=ramp_mode,
            target_tasks=target_tasks,
        )
    )
    logger.info("discovery task complete: %s", report)


if __name__ == "__main__":
    main()
