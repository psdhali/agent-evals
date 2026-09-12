"""Harness adapter protocol.

Every agent harness (custom, Aider, Claude Code, Codex, OpenCode) must satisfy
this protocol so the orchestrator can treat them interchangeably (F1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from swebench_eval.orchestrator.run_config import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_MAX_COST_USD_PER_INSTANCE,
    DEFAULT_MAX_TOKENS_PER_INSTANCE,
    DEFAULT_MAX_TURNS_PER_INSTANCE,
    DEFAULT_TIMEOUT_SECONDS,
)


@dataclass
class ModelConfig:
    """Model-routing configuration passed to a harness.

    In Phase 1 (direct provider call) these fields point at the raw provider.
    From Phase 2 onward ``gateway_base_url`` / ``gateway_api_key`` point at the
    LiteLLM gateway and ``model_name`` is a gateway-side alias.
    """

    gateway_base_url: str
    gateway_api_key: str
    model_name: str
    # Passthrough fields — harness-specific, not interpreted by the framework.
    # PART2 §1 (2026-08-23): `max_tokens` was REMOVED from ModelConfig.  The
    # old default (4096) is the exact value that truncated django-10924, and it
    # made custom_minimal the only harness with a framework-imposed per-call cap
    # (the five CLI harnesses set their own).  The per-INSTANCE budget
    # (max_tokens_per_instance / max_cost_usd_per_instance) is the real spend
    # bound and is untouched.
    # V8 (switch-to-swebench-verified §10): temperature is None by default so a
    # harness sends NO sampling parameter and INHERITS the gateway's pinned
    # value (litellm_config.yaml).  custom_minimal was the only harness sending
    # one (temperature=0.0 on every request) — the control harness ran greedy at
    # 0.0 while the five CLI harnesses ran at the gateway pin (1.0).  Now all
    # six inherit the same pin.
    temperature: float | None = None


@dataclass
class HarnessInput:
    """Everything a harness needs to run a single instance/attempt.

    Mirrors ``architecture.md`` §3's ``HarnessInput``.
    """

    instance_id: str
    repo_url: str  # e.g. "https://github.com/org/repo" — clone is retired in 5b
    base_commit: str
    problem_statement: str
    attempt_number: int
    repo_checkout_path: str  # prepared by the worker (5b: /testbed); read-only to the adapter
    model_config: ModelConfig
    # 5b: where the harness writes trajectory/logs.  The repo now lives at the
    # install script's hardcoded /testbed, so ``repo_dir.parent`` ("/") is not a
    # usable output dir — the worker passes a per-job workdir instead.
    output_dir: str = ""
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_tokens_per_instance: int | None = DEFAULT_MAX_TOKENS_PER_INSTANCE
    max_cost_usd_per_instance: float = DEFAULT_MAX_COST_USD_PER_INSTANCE
    max_turns_per_instance: int | None = DEFAULT_MAX_TURNS_PER_INSTANCE  # 3.12; drives native flags
    # Compaction build (Stage 1.2): the resolved per-model context window, so
    # the harness sizes its OWN compaction threshold.  None = compaction
    # disabled (deliberate opt-out).  Resolved once per run in the dispatcher;
    # never looked up by an adapter.
    context_window_tokens: int | None = DEFAULT_CONTEXT_WINDOW_TOKENS
    # master-handover 3.3: the SAME Usage object the shim accumulates into, so an
    # adapter can read LIVE cumulative totals (cum_in/cum_out/cum_cached/cum_cost)
    # for each TRAJ line without its own accounting.  None only in tests / when
    # custom_minimal runs without the shim (custom_minimal accounts itself).
    live_usage: Usage | None = None


@dataclass
class Usage:
    """Token / cost accounting for a single harness run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0  # prompt_tokens_details.cached_tokens
    cache_write_tokens: int = 0  # prompt_tokens_details.cache_write_tokens
    reasoning_tokens: int = 0  # completion_tokens_details.reasoning_tokens
    cost_usd: float = 0.0
    source: str = "harness"  # "harness" | "gateway" — "gateway" when API-reported cost is used
    retry_count: int = 0  # transient model-call failures retried in-loop (F1)
    # METERING-COMPLETENESS (2026-08-28): instance-level count of calls whose
    # usage could not be parsed (a 2xx/3xx with a body but no parseable usage
    # block).  Exists per call in llm_calls.jsonl as `usage_parse_failed`, but
    # had no rollup — so an instance where 20% of calls had unparseable usage
    # (exactly the codex streaming-tail bug) showed a clean-looking token total
    # with nothing marking it short.  This is a completeness marker, never a
    # subtraction: a shorted total recorded as if complete is worse than one
    # flagged as incomplete.
    usage_parse_failed_calls: int = 0
    # BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5: the L1 pacer's cumulative footprint
    # on this instance, folded in by the shim at every admission / hold-cap timeout / §8b
    # retry, so a slow-or-dying instance is explainable LIVE (Redis progress) and after the
    # fact (instance_results). paced_wait_ms_total counts hold-cap timeouts at the full cap.
    paced_wait_ms_total: int = 0
    paced_calls: int = 0  # calls that were denied at least once before admitting
    pacer_timeouts: int = 0  # calls that hit the hold cap (each surfaced a 429 to the CLI)
    overload_retries_total: int = 0  # §8b provider-429 retries the shim absorbed
    pacer_last_deny_axis: str | None = None  # 'tok' | 'req' | 'inflight' — the last deny
    pacer_last_queue_len: int = 0  # wait-queue depth seen at the last admission


TerminatedReason = Literal[
    "completed",
    "timeout",
    "stuck",
    "budget_exceeded",
    "crash",
    "model_api_error",
    # K-3: the run exhausted ``max_turns`` without the model signalling done.
    # Distinct from "timeout" (wall-clock / call deadline) and from "completed"
    # (the model ended the conversation on its own) — this is a framework-imposed
    # cut and must be recorded as one, not as a clean finish.
    "max_turns_exceeded",
    # B2/Z1 (round-review): the model's response was truncated at ``max_tokens``
    # (``finish_reason == "length"``) — the model wanted to say more. Like
    # max_turns_exceeded this is a framework-imposed cut, not a clean finish and
    # not a timeout.
    "max_tokens_truncated",
    # B3/E10c: the model refused the task (``message.refusal``). Not a clean
    # finish and not an infra failure — a model-level non-completion.
    "refused",
    # R3-2 (round-3 review): the patch-extraction git add/diff exceeded its
    # timeout.  A DISTINCT infra failure — the agent did the work but we could
    # not cheaply capture it; classified so the artifacts and call rows are kept
    # and the run is not retried five times against a $100/quarter budget.
    "patch_extract_timeout",
    # harness-05 F2 (2026-08-24): the model sent 3 consecutive malformed
    # tool-call arguments (truncated JSON) and the bounded recovery terminated
    # the run.  NOT a clean finish, NOT EMPTY_PATCH — the run stopped because the
    # MODEL kept sending broken tool calls.  Own category (like
    # max_tokens_truncated: a framework-imposed / provider-cut terminal, never
    # auto-graded).
    "malformed_tool_calls",
    # D5 (owner decision, option C, 2026-08-24): the model sent consecutive
    # responses with NO content AND NO tool calls (not even reasoning) — a
    # provider-side stall/truncation, NOT "the agent decided it was done".  The
    # harness retries up to 2 times (3 total); on the third consecutive empty it
    # terminates with this reason.  Never a clean finish (that was the bug: an
    # empty turn read as completed → EMPTY_PATCH, blaming the model), never
    # auto-graded, and RETRYABLE (provider-side, like model_api_error).
    "empty_response",
    # R5.3 (builder1-REMAINING-WORK-single-handover): the harness terminated
    # without the shim ever servicing a model call (opencode produced no
    # llm_calls.jsonl at all and was written up as a gateway error).  A run that
    # never called a model is categorically an infrastructure failure — never a
    # clean "completed with empty patch", never a gateway error.
    "zero_model_calls",
    # Compaction build (approved by the owner): the run's context window was
    # exhausted AND compaction has nothing left to reclaim — a framework-imposed
    # cut (same family as max_turns_exceeded), never graded, never a clean
    # finish, never EMPTY_PATCH.  mini maps litellm's ContextWindowExceededError
    # onto it; custom_minimal raises it when a compaction pass is exhausted.
    "context_exhausted",
]


@dataclass
class HarnessOutput:
    """Everything a harness returns after running a single instance/attempt.

    Mirrors ``architecture.md`` §3's ``HarnessOutput``.
    """

    patch: str | None  # from ``git diff``, None if nothing changed
    success: bool  # harness's own belief, not the eval verdict
    trajectory_path: str  # normalized, harness-agnostic (see §9.2)
    raw_log_path: str  # native log, harness-specific
    # B6/E11a (round-review): the adapter's NATIVE trajectory (un-normalised),
    # where one exists (mini_swe_agent's own JSON doc). Default empty — most
    # harnesses have no separate native artifact (claude_code/codex/opencode
    # stream to stdout = raw_log_path; custom_minimal's raw responses are in the
    # stdout log). The worker uploads it beside the other artifacts under
    # `native_trajectory.json` when set and present.
    native_trajectory_path: str = ""
    trajectory_parsed: bool = True  # False = one-line unparsed fallback (P4C-3)
    usage: Usage = field(default_factory=Usage)
    # ADR-0037 / M0 §1.3: the adapter's OWN parsed usage (where it parses any),
    # carried ONLY as a cross-check against the shim's authoritative meter — it
    # is stored and compared, never added.  The worker uses the shim's number;
    # this lets the validation gate assert the two agree without a silent 2×.
    adapter_reported_usage: Usage | None = None
    # ADR-0037 / M0 §4.3 + R2-3 (review): the patch-extraction wall time, set by
    # the adapter from its git_diff call (git_utils.git_diff fills out[
    # "patch_extract_s"]).  NULL when not measured (Trap 3) — a real number only
    # from the shared helper.
    patch_extract_s: float | None = None
    wall_clock_seconds: float = 0.0
    exit_code: int = 0
    error: str = ""
    terminated_reason: TerminatedReason = "completed"
    error_category: str = ""  # from architecture §9.3 error taxonomy
    # Compaction build (BUILD-SPEC §6): per-instance compaction measurement the
    # worker persists to instance_results.  None = no pass ran / not measured
    # (Trap 3, never a fabricated 0).  compactions_fired is the counter;
    # tokens_before/after are the LAST pass; context_window_tokens is the window
    # the harness actually sized its threshold from.
    compactions_fired: int | None = None
    compaction_tokens_before: int | None = None
    compaction_tokens_after: int | None = None
    context_window_tokens: int | None = None
    # Full per-compaction detail for results analysis (owner request
    # 2026-09-02): a list of {"at_model_call", "trigger", "pre_tokens",
    # "post_tokens"} dicts, one per compaction, in order.  The DB columns above
    # keep only the count and the LAST pass; the worker ships this list as a
    # compaction_events.json artifact next to the trajectory, so analysis can
    # ask "did compacting at call N change the outcome" without a migration.
    # None = none observed / harness cannot observe them (never a fabricated []).
    compaction_events: list[dict[str, Any]] | None = None


def recognized_terminal_reason(text: str) -> TerminatedReason | None:
    """R5.1: map a CLI's terminal-self-report to a ``TerminatedReason``.

    A subprocess CLI that stops on its own (max turns, truncation…) usually
    says so in its last output before exiting non-zero.  The adapters map any
    non-zero exit to ``crash`` today, so a turn-capped run gets recorded as
    HARNESS_CRASH.  Scan the combined stdout+stderr for the markers the CLIs
    actually print; return the reason, or ``None`` so the caller keeps
    ``crash`` (an exit with NO recognised terminal self-report stays a crash —
    that is the honest fallback, not a new guess).
    """
    if not text:
        return None
    low = text.lower()
    markers: tuple[tuple[str, TerminatedReason], ...] = (
        # M1 (review): deliberately NO bare "max turns" substring.  A subprocess
        # CLI's stdout can *mention* the phrase ("I'll be efficient so we don't
        # burn max turns on exploration") without the run being turn-capped; a
        # bare substring match then silently reclassifies a clean, patch-
        # producing run into a max-turns failure that is never graded.  Only the
        # specific turn-cap phrasings the CLIs actually print are matched, and
        # callers must prefer the structured terminal subtype on a zero exit.
        # M1-b (round-2 review): error_max_turns — the SUBTYPE Claude Code's
        # result event actually emits (captured: harness-02b-claude-code
        # -ROOT-CAUSE.md — `"subtype":"error_max_turns"`).  It survives
        # .replace("-"," ") unchanged and matched none of the markers, so a
        # turn-capped exit-0 run was recorded completed→PATCH_READY and even the
        # exit-1 path only worked by accident (via the free-text fallback on the
        # prose "Reached maximum number of turns" inside errors).  The structured
        # signal must match on its own.
        ("error_max_turns", "max_turns_exceeded"),
        ("max turns exceeded", "max_turns_exceeded"),
        ("maximum number of turns", "max_turns_exceeded"),
        ("hit maximum turns", "max_turns_exceeded"),
        ("reached max turns", "max_turns_exceeded"),
        ("max_turns_exceeded", "max_turns_exceeded"),
    )
    for marker, reason in markers:
        if marker in low:
            return reason
    return None


class HarnessAdapter(Protocol):
    """Protocol that every harness wrapper must implement.

    A single-method protocol so adding a 6th harness means implementing one
    adapter — no changes to the orchestrator, gateway, evaluation, or reporting
    code (F1).
    """

    def run(self, input: HarnessInput) -> HarnessOutput:
        """Run the harness for a single instance/attempt and return the result."""
        ...
