"""Run configuration schema.

The single home for run-level configuration (architecture §11), including the
per-instance and per-run budget ceilings (ADR-0019).  This is the top of the
ceiling-plumbing chain:

    run config → dispatcher → job message → worker → shim

The default for ``max_cost_usd_per_instance`` lives here and nowhere else — it
used to be repeated in four files (base.py, schemas.py, harness_worker.py,
dispatcher.py), which was four places to change and three to forget (ADR-0019).
"""

from __future__ import annotations

from dataclasses import dataclass

# Single source of the default per-instance ceilings (ADR-0019).  The shim reads
# its ceiling from the job it was started for and holds no default of its own.
#
# METERING-COMPLETENESS (2026-08-28): the owner decision is that we NEVER abort
# on total token count.  Instances are bounded by per-instance COST and by the
# turn cap (500), never by cumulative tokens.  Default is None = "explicitly
# unlimited"; both enforcement sites are `is not None`-guarded (local_proxy.py
# token_breached, harness_worker budget check) so an absent ceiling makes them
# inert without touching the enforcement code.
#
# V5 history: was raised 200k -> 500k, but that ceiling was never a
# comparability axis — the turn cap is.  The duplicated `input + output`
# expression at both guard sites stays exactly as-is (NOT refactored into a
# shared billable_tokens() helper); per-provider normalisation is intentionally
# NOT done here (see BUILDER1-METERING-COMPLETENESS §1 — it carries a real
# double-counting risk: OpenAI cached is a subset of input, Anthropic's
# cache_read is additive).
#
# NOTE: the API launch path still re-imposes 500k until Builder 4's half of
# this two-file change lands (RunLaunchRequest.max_tokens_per_instance in
# api/schemas.py).  See the response doc.
DEFAULT_MAX_TOKENS_PER_INSTANCE: int | None = None  # None = explicitly unlimited (owner decision)
DEFAULT_MAX_COST_USD_PER_INSTANCE: float = 5.0
# master-handover 3.12 (2026-08-24): a 500-turn cap enforced at the shim, so
# ALL five harnesses get the same limit (three have no native cap and two of
# them cannot be given one).  Matches published SWE-bench runs.  500 is the
# DEFAULT, not a constant — settable per run.  `None` = deliberately unlimited
# (same convention as max_tokens_per_instance).
DEFAULT_MAX_TURNS_PER_INSTANCE: int | None = 500

# Context-compaction build (BUILDER1-SEQUENCED-WORK §0, BUILD-SPEC rev 2 §0): the
# resolved per-model context window.  This constant is the STEP-4 FLOOR ONLY —
# the dispatcher resolves the real window per model (run-config override → live
# gateway /model/info → baked litellm_config.yaml → this constant with a WARNING),
# because ONE constant cannot express the per-model table (qwen/laguna-xs 262 144,
# deepseek-v4-flash 1 048 576 — using the constant as the primary source would
# understate the e2e window 4× and compact far too early with nothing reporting it).
# `None` = "resolve it", NOT "disabled".
DEFAULT_CONTEXT_WINDOW_TOKENS: int | None = 262_144
# Fixed output reserve (BUILD-SPEC §0): NOT the model's max_completion_tokens
# (qwen's is 235 929, which would leave a 26 K threshold).  Used to derive the
# compaction threshold: min(int(0.90 × W), W − this).  ONE number, owned by
# harnesses/compaction.py OUTPUT_RESERVE (16 384 since 2026-09-02) — this was a
# stale 32 768 copy until F1 (2026-09-04).  A literal rather than an import
# because harnesses/base.py imports this module (DEFAULT_TIMEOUT_SECONDS), so an
# import here would be circular; tests/test_shim_max_tokens_injection.py pins
# the two equal.
DEFAULT_OUTPUT_RESERVE_TOKENS: int = 16_384

# 2026-08-29 owner decision: raised from 600s (10 min) to 5400s (1h30m) — some
# instances genuinely take over an hour to solve, and the old default put
# the reaper's rule 2 deadline (timeout_seconds + _REAP_MARGIN_S, now in
# run_supervisor.py per CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md)
# within reach of a real in-progress task, not just a dead one. Same
# four-places-to-forget shape as the other DEFAULT_* constants above — was
# independently hardcoded as 600 in base.py, queue/schemas.py, s3_dispatch.py,
# and api/schemas.py; those now import this instead.
DEFAULT_TIMEOUT_SECONDS: int = 5400


@dataclass
class RunConfig:
    """The resolved run configuration, carried end-to-end.

    Fields named here flow into the HarnessJob message and then into the
    per-worker ADR-0019 shim's ceiling, which has no default of its own.
    """

    attempts_per_instance: int = 1
    harness: str = "custom_minimal"
    model_alias: str = "cheap-oss-model"
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_tokens_per_instance: int | None = DEFAULT_MAX_TOKENS_PER_INSTANCE
    max_cost_usd_per_instance: float = DEFAULT_MAX_COST_USD_PER_INSTANCE
    max_turns_per_instance: int | None = DEFAULT_MAX_TURNS_PER_INSTANCE
    # Compaction build: the operator override for the per-model context window.
    # None = "resolve it" in the dispatcher (run-config override → live gateway →
    # baked yaml → DEFAULT_CONTEXT_WINDOW_TOKENS floor).  Threaded to every
    # harness so it can size its own compaction threshold — never looked up by an
    # adapter (one call per run, in the dispatcher).
    context_window_tokens: int | None = None
    # 2026-09-09 (efficiency prompt arm): operator text appended to every job's
    # problem_statement at dispatch under a fixed heading (orchestrator/harness_instructions).
    # Advice, not a limit — recorded in config_snapshot and echoed as a launch field so a run
    # with instructions is never read as a plain one. None = nothing appended.
    harness_instructions: str | None = None

    # ── Autoscaler per-run overrides (harness-autoscaler exact-design §8 / §6.7) ──
    # These land in config_snapshot via CLAIM's asdict AND are published to Redis at
    # launch (run_launch._publish_autoscaler_overrides) for the dispatcher, which is a
    # separate long-running service that never reads config_snapshot.
    #
    # The operator's per-run task ceiling — min-wins against the dispatcher's static
    # MAX_CONCURRENT_HARNESS_TASKS (which stays in force in every mode).
    max_parallel_harness_tasks: int = 150
    # Sets the pacer's starting triple directly (keys of pacer:cfg — c_burst / r_tok /
    # k_inflight, optionally c_req / r_qps), bypassing discovery's seed for THIS launch.
    # None = use whatever pacer:cfg holds (discovery's numbers, or the pacer defaults).
    initial_budget_override: dict[str, float] | None = None
    # +X% growth per clean predicted-peak reconciliation. 5 is also the HARD MAX
    # (owner-fixed "never more"); the dispatcher clamps, so a larger request is a no-op.
    ramp_step_pct: float = 5.0
    # §6.4 stabilization: how long admission stays frozen after an overload before
    # anything resumes.
    ramp_cooldown_seconds: int = 60
    # False disables L2's dynamic ceiling gate ONLY — the static ceiling and the L1
    # pacer stay in force (running a fleet with no arrival pacing is the one
    # configuration the evidence says must not exist).
    autoscaler_enabled: bool = True
