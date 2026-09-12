"""Custom minimal agent harness.

A simple, direct tool-use loop that calls an OpenAI-compatible model API
(no gateway, no queue).  This is the reference harness — the first one built
and the simplest one to understand.  It proves the harness interface and the
grading-reuse assumption (F5) end-to-end before anything else depends on them.
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Any

from swebench_eval.database.state_machine import map_terminated_reason_to_error_category
from swebench_eval.gateway.pricing import cost_for
from swebench_eval.harnesses.base import (
    HarnessAdapter,
    HarnessInput,
    HarnessOutput,
    TerminatedReason,
    Usage,
)
from swebench_eval.harnesses.compaction import compact_messages, compute_threshold
from swebench_eval.harnesses.custom_minimal.tools import (
    TOOL_DEFINITIONS,
    execute_tool,
)
from swebench_eval.harnesses.custom_minimal.trajectory import (
    TrajectoryWriter,
    normalize_tool_call,
    normalize_tool_result,
)
from swebench_eval.harnesses.git_utils import git_diff_or_classify
from swebench_eval.harnesses.repo_prep import ensure_prepared_repo
from swebench_eval.harnesses.routing import gateway_api_key, gateway_base_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-model pricing (USD per 1M tokens) — single source of truth is the shared
# accounting to the gateway.
# ---------------------------------------------------------------------------

# Pricing resolved from the ONE shared module (review F-3) — see the import at
# the top of this file.  A second local table would price tokens differently
# from the shim, breaking cross-harness comparison.


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def _per_call_read_timeout(timeout_seconds: int) -> float:
    """Per-call read timeout for the model client (bug-findings B-1/B-2).

    Guaranteed SMALLER than the run's wall-clock ``timeout_seconds`` so the
    loop's top-of-turn deadline check can actually fire — this is what turns the
    wall-clock timeout from advisory into enforceable.  Bounded to [30, 120] so a
    stalled upstream call fails fast but a slow-but-healthy call isn't cut off.
    """
    read = min(120, max(30, timeout_seconds // 3))
    # Always strictly smaller than the budget so the deadline can fire even for a
    # degenerate tiny budget, where the 30 s floor would otherwise swallow it.
    if read >= timeout_seconds:
        read = max(1, timeout_seconds - 1)
    return read


def _maybe_compact(
    messages: list[dict[str, Any]],
    threshold: int,
    state: dict[str, Any],
    instance_id: str,
    at_model_call: int = 0,
) -> None:
    """Trigger + run one compaction pass when the estimated context nears the window.

    Trigger: ``last_prompt_tokens`` (the measured window occupancy from the last
    response) PLUS a ``chars/4`` estimate of everything appended since.  We NEVER
    use ``input - cached``: a cache-read token still occupies the window, and at
    the 74 % cache-read we have measured that trigger fires ~4× too early
    (BUILD-SPEC §4).

    Latches off after a pass that reclaims less than ``min_gain_ratio`` —
    without this, a real run compacted 14 times on 58 messages, most reclaiming
    nothing.  ``state`` carries the counters the harness records on its output.
    """
    if threshold <= 0 or state["exhausted"]:
        return
    applied = state["last_prompt_tokens"] + state["chars_since_measure"] // 4
    if applied < threshold:
        return
    if len(messages) <= 2 + 6:  # keep_head + keep_tail floor (BUILD-SPEC §4.1)
        return
    out, stats = compact_messages(
        messages,
        keep_head=2,
        keep_tail=6,
        keep_tool_head_chars=800,
    )
    messages[:] = out  # mutate the caller's list in place
    state["fired"] += 1
    state["tokens_before"] = stats["before_chars"]
    state["tokens_after"] = stats["after_chars"]
    # Compaction-point record (2026-09-02), same shape claude_code emits from
    # compact_boundary so the compaction_events.json artifact is uniform.  This
    # pruner measures CHARS, so the token figures are estimates (chars/4 — the
    # same estimator the trigger itself uses), flagged as such.
    state.setdefault("events", []).append(
        {
            "at_model_call": at_model_call,
            "trigger": "auto",
            "pre_tokens": applied,
            "post_tokens": stats["after_chars"] // 4,
            "estimated": True,
        }
    )
    if not stats["made_progress"]:
        state["exhausted"] = True  # nothing left to prune this run
    logger.warning(
        "COMPACT #%d fired on %s: applied~%d >= threshold %d | chars %d->%d",
        state["fired"],
        instance_id,
        applied,
        threshold,
        stats["before_chars"],
        stats["after_chars"],
    )


SYSTEM_PROMPT = """You are an expert software engineer. You are given a problem statement
describing an issue in a codebase. Your task is to fix the issue.

The repository is already checked out, and your `bash` commands run with the
repository root as their current working directory — `pwd` shows the exact
path. Do NOT guess or `cd` to a location such as `/repo`: it will not exist
here. File paths given to `str_replace_editor` are resolved relative to the
repository root, so a repo-relative path is fine.

You have access to the following tools:
- `bash`: Run a shell command inside the repository. Use this to explore the
  codebase, run tests, and verify your changes.
- `str_replace_editor`: View, create, or edit files. Use the `view` command
  to see file contents, and `str_replace` to make precise edits.

Work step by step:
1. First, explore the repository to understand the codebase structure and find
   relevant files.
2. Identify the root cause of the issue.
3. Make the minimal fix needed to resolve the issue.
4. Verify your fix by running the relevant tests.

When you are done, output only the word "DONE" on its own line."""


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


# Retry parity with the CLI harnesses (OpenCode/Claude/Codex): those CLIs (and
# the Vercel AI SDK OpenCode uses) retry the transient model-call failures —
# HTTP 408/429 and all 5xx, plus network errors — and do NOT retry client-side
# 4xx (auth/validation). custom_minimal sets the SDK's max_retries=0 (retry
# policy lives at the gateway), so it must do its OWN in-loop retry or a single
# transient 504 kills the whole run — the asymmetry this closes.
_RETRIABLE_STATUS = {408, 429} | set(range(500, 600))

# B1 (round-review E11c): cap on the raw per-turn response dump. A single turn's
# response is ~1-5 KB (× 50 turns is fine), but a runaway field must not flood
# the artifact — truncate defensively, never drop the turn marker.  F6
# (2026-08-24, harness-05): raised 32k -> 128k — the p2 mid-word stop and the
# tool-call truncation are the THIRD appearance of the same cut, and the finish
# reason that would tell us the provider is cutting sits at the END of the dump,
# so the cap must not evict it.
_RAW_RESPONSE_CAP = 128_000


def _is_retriable(exc: Exception) -> bool:
    """True if a model-call failure is transient enough to retry.

    Retryable: HTTP 408/429/5xx, connection errors, timeouts/read stalls.
    Not retryable: auth/4xx (except 408/429), validation errors, and
    unexpected crashes — those fail immediately.
    """
    try:
        from openai import APIConnectionError, APIStatusError
    except ImportError:
        return False
    if isinstance(exc, APIConnectionError):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in _RETRIABLE_STATUS
    is_timeout = False
    try:
        import httpx as _httpx

        is_timeout = isinstance(exc, _httpx.TimeoutException)
    except ImportError:
        is_timeout = False
    if is_timeout:
        return True
    return isinstance(exc, TimeoutError)


def _call_with_retry(
    client: Any,
    *,
    deadline: float,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    per_call_read: float,
    on_retry: Any = None,
    # F4 (review): explicit named params, NOT **kwargs — a misspelled argument
    # at the call site is a compile/runtime error, not a silent **kwargs spill.
    model: Any = None,
    messages: Any = None,
    tools: Any = None,
    tool_choice: Any = None,
    temperature: Any = None,
    extra_body: Any = None,
) -> Any:
    """Call ``client.chat.completions.create(...)`` with jittered backoff.

    Retries only ``_is_retriable`` failures, up to ``max_retries``, and only
    while the remaining wall-clock budget (``deadline``) affords a backoff
    sleep plus one per-call read — a retrying call can never blow the run's
    timeout. Returns the response; raises the LAST exception once retries are
    exhausted or the deadline is reached.
    """
    attempt = 0
    # V8 (switch-to-swebench-verified §10): OMIT the temperature key when it is
    # None so the harness inherits the gateway's pinned value.  Sending
    # temperature=null (or 0.0, the old hardcode) would make custom_minimal the
    # only harness overriding the pin — the control runs greedy while the five
    # CLI harnesses run at the gateway's 1.0.  Include the key ONLY when a
    # non-None temperature is explicitly configured.
    #
    # PART2 §1 (2026-08-23): max_tokens is OMITTED ENTIRELY (not defaulted to
    # None).  The five CLI harnesses never see it (each sets its own), so it
    # made custom_minimal the only harness carrying a framework-imposed cap and
    # the only one that can be silently truncated mid-solve by a number we
    # chose.  `None` is NOT omission — the key with a null value still goes on
    # the wire and some providers 400 on it; DELETE the key, exactly like
    # temperature.  The per-INSTANCE budget (500k tokens / $5) is the real
    # spend bound and is untouched.
    call_kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "extra_body": extra_body,
    }
    if temperature is not None:
        call_kwargs["temperature"] = temperature
    while True:
        try:
            return client.chat.completions.create(**call_kwargs)
        except Exception as exc:
            if not _is_retriable(exc) or attempt >= max_retries:
                raise
            attempt += 1
            # F3 (review): cap the JITTERED delay, not the pre-jitter value — a
            # "max" that the jitter can exceed is not a max.
            delay = min(max_delay, (base_delay * (2 ** (attempt - 1))) * random.uniform(0.5, 1.5))
            remaining = deadline - time.monotonic()
            # F2 (review): the next CALL also has a read budget. Refuse a retry
            # when the backoff sleep PLUS one per-call read would overshoot the
            # deadline — otherwise a stalled upstream is retried until most of
            # the run budget is gone (B-1/B-2's original intent).
            if delay + per_call_read >= remaining:
                raise
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            time.sleep(delay)


class CustomMinimalHarness(HarnessAdapter):
    """A minimal tool-use harness that calls an OpenAI-compatible API directly.

    Parameters
    ----------
    api_base_url: Base URL for the OpenAI-compatible API (e.g.
        ``https://openrouter.ai/api/v1``).
    api_key: API key for the provider.
    model: Model name to use (e.g. ``qwen/qwen-2.5-coder-32b-instruct``).
    max_turns: Safety cap on the number of tool-use turns before the harness
        force-stops, regardless of timeout.  R7 (owner decision MADE): raised to
        500 so turns never decide an outcome — R3's shim is the only bound.
    """

    def __init__(
        self,
        api_base_url: str | None = None,
        api_key: str | None = None,
        model: str = "qwen/qwen3-coder-30b-a3b-instruct",
        max_turns: int = 500,
        max_retries: int = 10,
        retry_base_delay_s: float = 1.0,
        retry_max_delay_s: float = 12.0,
    ) -> None:
        # Routing comes from the shared helper (R5-1); an explicit base URL is
        # accepted as an override (e.g. Phase 1 `--skip-gateway` direct path).
        self._api_base_url = (api_base_url or gateway_base_url()).rstrip("/")
        self._api_key = api_key or gateway_api_key()
        self._model = model
        self._max_turns = max_turns
        self._max_retries = max_retries
        self._retry_base_delay_s = retry_base_delay_s
        self._retry_max_delay_s = retry_max_delay_s

    def run(self, input: HarnessInput) -> HarnessOutput:
        start_time = time.monotonic()
        exit_code = 0
        error_msg = ""
        terminated_reason: TerminatedReason = "completed"

        trajectory = TrajectoryWriter()
        usage = Usage()

        # Resolve model routing from HarnessInput.model_config, falling back to
        # constructor params (for backward compatibility and direct-call testing).
        # From Phase 2 onward, model_config is the primary source.
        api_base_url = input.model_config.gateway_base_url or self._api_base_url
        api_key = input.model_config.gateway_api_key or self._api_key
        model_name = input.model_config.model_name or self._model

        repo_dir = Path(input.repo_checkout_path)

        # --- Repo-prep assert (5b) -------------------------------------------
        # The worker prepares the repo via SWE-bench's install_repo_script
        # (repo_prep.py) BEFORE this adapter runs.  Assert rather than clone:
        # the script removed origin and pruned future history, so cloning would
        # fail (no origin) or silently re-expose the gold patch.
        try:
            ensure_prepared_repo(repo_dir)
        except Exception as exc:  # noqa: BLE001
            return HarnessOutput(
                patch=None,
                success=False,
                trajectory_path="",
                raw_log_path="",
                exit_code=1,
                error=f"repo not prepared: {exc}",
                terminated_reason="crash",
                error_category="HARNESS_CRASH",
                wall_clock_seconds=time.monotonic() - start_time,
            )

        # --- Tool-use loop ----------------------------------------------------
        messages: list[dict[str, object]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            # K-1: the prompt above says cwd IS the repo, but the model must also
            # know WHERE. The clone lands in a per-run temp dir (mkdtemp), and 5b
            # moves it to /testbed — an absolute path stated here beats any guess
            # (all three 0004-0006 runs opened with a failed `cd /repo`).
            {
                "role": "system",
                "content": (
                    "The repository for this task is checked out at "
                    f"{repo_dir.resolve()} — your shell's current working directory "
                    "is already that path. Use it directly; do not guess or `cd` "
                    "to a path like /repo."
                ),
            },
            {"role": "user", "content": input.problem_statement},
        ]

        # X3 (trajectory review §5): record the system prompts so the trajectory
        # is reproducible — they are part of what produced the result, and the
        # comparison across five harnesses needs them in the record. The repo
        # path K-1 note is dynamic (per-run temp dir), which is exactly why it
        # belongs here rather than only in the code.
        trajectory.add_system_message(SYSTEM_PROMPT)
        trajectory.add_system_message(str(messages[1]["content"]))
        trajectory.add_user_message(input.problem_statement)

        # Raw log buffer — collects model responses + tool results per turn.
        raw_log_lines: list[str] = []
        retries_observed = {"n": 0}  # F1: queryable retry count (not just a log blob)

        def _record_retry(n: int, d: float, e: object) -> None:
            retries_observed["n"] = n
            raw_log_lines.append(
                f"retried transient model error ({n}/{self._max_retries}) after {d:.1f}s: {e}"
            )

        cumulative_tokens = 0
        cumulative_cost = 0.0
        # F2 (2026-08-24, harness-05): consecutive malformed tool-call arguments.
        # The F1 guard makes the run stay alive; this bounds that recovery so a
        # provider that keeps truncating does not loop to max_turns burning budget
        # on nothing.  Terminate with ITS OWN reason after 3 consecutive — NOT a
        # clean "completed", NOT EMPTY_PATCH, NOT a crash.
        malformed_tool_calls = 0
        # D5 (owner decision, option C): consecutive responses with NO content
        # AND NO tool_calls.  A model that emits "nothing" is not cleanly
        # finished — the agent produced no text and made no tool call, which is
        # provider-side stall or truncation, not "the agent decided it was
        # done" (which used to be recorded completed → EMPTY_PATCH, blaming the
        # model).  Retry up to 2 times (3 total), then terminate with its own
        # `empty_response` reason.  Mirrors the malformed_tool_calls recovery
        # directly below — one bad turn must not lose the run's work.
        empty_responses = 0
        # B4 (E10a): did we capture ANY reasoning text? Used after the loop to
        # warn when the gateway reported reasoning_tokens but the reasoning never
        # arrived under either name — the silent-drop condition B4 exists to catch.
        captured_reasoning_any = False

        # Compaction build (Stage 2.3/2.4): deterministic middle-pruning when the
        # context nears the window.  `messages` is the OpenAI message list kept by
        # custom_minimal; `compact_messages` stubs middle tool RESULTS (never
        # assistant text/reasoning/tool_calls, never orphans a tool_call_id).
        # Trigger: the measured prompt_tokens from the PREVIOUS response PLUS a
        # chars/4 estimate of everything appended since — never `input - cached`
        # (a cache-read token still occupies the window; at 74 % cache-read that
        # fires ~4× too early, BUILD-SPEC §4).
        _compaction_state: dict[str, Any] = {
            "fired": 0,
            "exhausted": False,
            "last_prompt_tokens": 0,
            "chars_since_measure": 0,
            "tokens_before": None,
            "tokens_after": None,
        }
        _compaction_threshold = compute_threshold(input.context_window_tokens)

        try:
            import httpx
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "The `openai` package is required for the custom harness. "
                "Install it with: uv sync"
            )

        # Bound every call so the wall-clock check below can actually fire
        # (bug-findings B-1/B-2): the per-call read timeout must be SMALLER than
        # `timeout_seconds`, or a stalled call blocks for the SDK default (~600 s ×
        # retried attempts) while the loop's deadline sits unreachable.  Retry is
        # disabled here on purpose — retry policy belongs at the gateway (§4), so
        # the SDK doesn't multiply the gateway's own num_retries (B-1a).
        per_call_read = _per_call_read_timeout(input.timeout_seconds)
        client = OpenAI(
            base_url=api_base_url,
            api_key=api_key,
            timeout=httpx.Timeout(connect=5.0, read=per_call_read, write=30.0, pool=30.0),
            max_retries=0,
        )

        for turn in range(1, self._max_turns + 1):
            elapsed = time.monotonic() - start_time
            if elapsed > input.timeout_seconds:
                terminated_reason = "timeout"
                error_msg = f"wall-clock timeout ({input.timeout_seconds}s)"
                break

            # Budget check.
            if input.max_tokens_per_instance and cumulative_tokens >= input.max_tokens_per_instance:
                terminated_reason = "budget_exceeded"
                error_msg = f"token budget exceeded ({cumulative_tokens} >= {input.max_tokens_per_instance})"
                break
            if (
                input.max_cost_usd_per_instance
                and cumulative_cost >= input.max_cost_usd_per_instance
            ):
                terminated_reason = "budget_exceeded"
                error_msg = f"cost budget exceeded (${cumulative_cost:.4f} >= ${input.max_cost_usd_per_instance:.2f})"
                break

            # Compaction build (Stage 2.4): trigger + compact at the top of the
            # loop, BEFORE the model call (compaction must never run mid-call).
            _maybe_compact(
                messages,
                _compaction_threshold,
                _compaction_state,
                input.instance_id,
                at_model_call=turn,
            )

            # Call the model — transient errors are retried IN-LOOP with bounded,
            # deadline-aware jittered exponential backoff (parity with the CLI
            # harnesses; the SDK's own max_retries stays 0 so we don't multiply
            # the gateway's num_retries unbounded).
            try:
                response = _call_with_retry(
                    client,
                    deadline=start_time + input.timeout_seconds,
                    max_retries=self._max_retries,
                    base_delay=self._retry_base_delay_s,
                    max_delay=self._retry_max_delay_s,
                    per_call_read=per_call_read,
                    on_retry=_record_retry,
                    model=model_name,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="auto",
                    temperature=input.model_config.temperature,
                    # Omit the OpenRouter strict-routing provider filter
                    # (compaction-e2e, 2026-08-28): it made OpenRouter return 404
                    # "No endpoints found that can handle the requested
                    # parameters" for poolside/laguna-xs-2.1, so custom_minimal —
                    # whose run the plan mandates on laguna — failed with
                    # MODEL_API_ERROR on its very first call. The strict filter
                    # excluded every endpoint hosting laguna; mini_swe_agent /
                    # opencode hit the same /v1/chat/completions with laguna and
                    # got 200 because they send no such filter. Dropping it
                    # restores normal routing; `tools` already tells OpenRouter
                    # what the request needs. (Do not re-add a strict
                    # require-every-parameter routing knob here — see the
                    # source-level guard in tests/test_no_sampling_params.py.)
                )
            except Exception as exc:  # noqa: BLE001
                # A stalled upstream call (per-call read timeout) is a run TIMEOUT,
                # not a crash — it terminates in bounded time thanks to per_call_read.
                if isinstance(exc, (httpx.TimeoutException, TimeoutError)) or (
                    _is_api_error(exc) and "timeout" in str(exc).lower()
                ):
                    terminated_reason = "timeout"
                    error_msg = f"per-call timeout on turn {turn} after {per_call_read}s: {exc}"
                elif _is_api_error(exc):
                    # Distinguish gateway/provider errors from generic crashes.
                    terminated_reason = "model_api_error"
                    error_msg = f"model API error on turn {turn}: {exc}"
                else:
                    terminated_reason = "crash"
                    error_msg = f"model API call failed on turn {turn}: {exc}"
                exit_code = 2
                break

            choice = response.choices[0]
            msg = choice.message

            # B1 (round-review E11c): dump the RAW per-turn model response. The
            # rendered `assistant` line below loses exactly the fields that explain
            # a run: which reasoning field name arrived (E10a), finish_reason (Z2),
            # refusal (E10c), and prompt/completion token details (E10f). The SDK
            # response is pydantic, so one call captures the whole turn. The fields
            # are what make a failing run a grep instead of a design argument;
            # capped defensively. The dump alone does not prove REPLAY — that is
            # the reasoning test's job — it proves what ARRIVED.
            try:
                raw_dump = response.model_dump_json()
            except Exception:  # noqa: BLE001 — the dump is diagnostic, never fatal
                raw_dump = repr(response)
            if len(raw_dump) > _RAW_RESPONSE_CAP:
                raw_dump = raw_dump[:_RAW_RESPONSE_CAP] + "\n...[raw response truncated]"
            raw_log_lines.append(f"--- turn {turn} RAW RESPONSE ---\n{raw_dump}\n")

            # B2 / B3 / B4 / B5: classify THIS turn before deciding how the loop
            # proceeds — the difference between a clean finish and a truncated or
            # refused one is the whole point of the Stage-B instrument round.
            finish_reason = getattr(choice, "finish_reason", None)
            # B3/E10c: a refusal is the (non-empty) refusal STRING; guard on
            # isinstance rather than truthiness so a provider that omits the
            # field (None) never reads as a refusal.
            _refusal = getattr(msg, "refusal", None)
            refused = _refusal if isinstance(_refusal, str) and _refusal.strip() else None

            # B4 (E10a): accept `reasoning_content` *or* `reasoning`. The gateway
            # may normalise DeepSeek's native `reasoning` field to either name; a
            # one-name read with no fallback would silently drop reasoning if the
            # field arrives under the other name, and nothing would tell us.
            _reasoning = getattr(msg, "reasoning_content", None)
            if not isinstance(_reasoning, str):
                _reasoning = getattr(msg, "reasoning", None)
            reasoning_content = _reasoning if isinstance(_reasoning, str) else ""
            if reasoning_content:
                captured_reasoning_any = True

            # Log raw model response.
            raw_log_lines.append(
                f"--- turn {turn} assistant ---\n"
                f"content: {msg.content or '(none)'}\n"
                f"reasoning: {reasoning_content or '(none)'}\n"
                f"tool_calls: {json.dumps([tc.function.name for tc in (msg.tool_calls or [])])}\n"
                f"finish_reason: {finish_reason}\n"
                f"refusal: {refused or ''}\n"
            )

            # Track token usage and cost. Note the sub-category CUMULATIVES on the
            # shared `Usage` (B5 emits them per turn AND cumulative); turn_* are
            # captured for the trajectory record.
            turn_input = 0
            turn_output = 0
            turn_reasoning = 0
            turn_cached = 0
            turn_cache_write = 0
            if response.usage:
                turn_input = response.usage.prompt_tokens or 0
                turn_output = response.usage.completion_tokens or 0
                usage.input_tokens += turn_input
                usage.output_tokens += turn_output
                cumulative_tokens = usage.input_tokens + usage.output_tokens
                # Compaction build (Stage 2.4): the measured prompt_tokens IS the
                # window occupancy at this call (the gateway sized the whole
                # context); fresh measurement resets the chars-since counter.
                if turn_input:
                    _compaction_state["last_prompt_tokens"] = turn_input
                    _compaction_state["chars_since_measure"] = 0

                # B5 (E10f): the sub-category tokens were ACCUMULATED but never
                # emitted — the number that would have shown "reasoning generated
                # and discarded" was computed every run and thrown away, which is
                # how the reasoning bug survived until someone reasoned it out.
                # Track on the turn AND sum onto the shared Usage.
                if response.usage.prompt_tokens_details:
                    turn_cached = response.usage.prompt_tokens_details.cached_tokens or 0
                    turn_cache_write = response.usage.prompt_tokens_details.cache_write_tokens or 0
                    usage.cached_tokens += turn_cached
                    usage.cache_write_tokens += turn_cache_write
                if response.usage.completion_tokens_details:
                    turn_reasoning = response.usage.completion_tokens_details.reasoning_tokens or 0
                    usage.reasoning_tokens += turn_reasoning

                # Two-tier cost (F-2): prefer the API-reported cost, else price
                # locally from the shared pricing module.  P2-1: accumulate
                # per-turn cost (response.usage.cost is per-call, NOT a running
                # total — assigning would silently disable the budget cap).
                gateway_cost = (
                    response.usage.cost
                    if (response.usage.cost and response.usage.cost > 0)
                    else None
                )
                # B8: pass the cache-read slice so cached input prices at 20% of
                # the input rate, matching the shim (both use the shared module).
                cumulative_cost += cost_for(
                    gateway_cost,
                    turn_input,
                    turn_output,
                    model_name,
                    cached_input_tokens=turn_cached,
                )
                if gateway_cost:
                    usage.source = "gateway"
                usage.cost_usd = cumulative_cost
                usage.retry_count = retries_observed["n"]  # F1

            # Append the assistant message to the conversation (B2: ALWAYS, so the
            # final turn — including a truncation or refusal — is in the record,
            # never only in raw_log_lines).
            assistant_msg: dict[str, object] = {"role": "assistant"}
            if reasoning_content:
                # Replay the model's own reasoning back into the context — the
                # API contract for reasoning models requires it on every
                # subsequent call (the reasoning IS the chain of thought the
                # next turn builds on).
                assistant_msg["reasoning_content"] = reasoning_content
            if msg.content:
                assistant_msg["content"] = msg.content
            if msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]

            messages.append(assistant_msg)
            # Compaction build: the assistant message (text + reasoning) is new
            # content since the last measurement — count it for the chars/4
            # trigger estimate.
            _compaction_state["chars_since_measure"] += len(str(msg.content or "")) + len(
                str(reasoning_content or "")
            )

            # Record in trajectory.
            model_content = msg.content or ""
            tool_calls_normalized = (
                [normalize_tool_call(tc) for tc in msg.tool_calls] if msg.tool_calls else []
            )
            # X3 (trajectory review §5) + B5 (E10f): self-describing keys — sum
            # the turn_* keys for totals; LAST record's cumulative_* is the run
            # total. The sub-category cumulatives (reasoning/cached/cache-write)
            # are the fields that were tracked and then dropped before B5.
            trajectory.add_assistant_message(
                turn=turn,
                content=model_content,
                reasoning=reasoning_content,
                tool_calls=tool_calls_normalized,
                usage={
                    "turn_input_tokens": turn_input,
                    "turn_output_tokens": turn_output,
                    "turn_reasoning_tokens": turn_reasoning,
                    "turn_cached_tokens": turn_cached,
                    "turn_cache_write_tokens": turn_cache_write,
                    "cumulative_cost_usd": cumulative_cost,
                    "cumulative_reasoning_tokens": usage.reasoning_tokens,
                    "cumulative_cached_tokens": usage.cached_tokens,
                    "cumulative_cache_write_tokens": usage.cache_write_tokens,
                },
            )

            # B2/Z1: finish_reason "length" = the model was truncated at
            # max_tokens — a framework-imposed cut, given its OWN termination
            # reason and a non-zero exit (NOT a clean finish; before Z1 a
            # truncated turn was recorded "completed" → EMPTY_PATCH as if the
            # model had nothing to say). The truncated turn IS recorded above.
            if finish_reason == "length":
                terminated_reason = "max_tokens_truncated"
                error_msg = (
                    f"response truncated at max_tokens on turn {turn} "
                    f"(finish_reason=length, output tokens={turn_output})"
                )
                exit_code = 2
                break

            # B3/E10c: a model refusal (message.refusal) is its own termination —
            # NOT a clean finish and NOT an infra failure. Never EMPTY_PATCH.
            if refused:
                terminated_reason = "refused"
                error_msg = f"model refused on turn {turn}: {refused}"
                exit_code = 2
                break

            # D5 (owner decision, option C): an EMPTY response — no content AND
            # no tool calls — is NOT "the model is done".  A clean finish is
            # text with a conclusion and no further tool use (the common "the
            # fix is complete" summary turn).  An empty turn is the model sent
            # nothing: provider stall/truncation, indistinguishable from a clean
            # finish previously → completed → EMPTY_PATCH, blamed on the model.
            # Retry up to 2 times (3 total) by handing the turn back; on the
            # third consecutive empty response terminate with its own reason.
            if not msg.tool_calls and not (msg.content or "").strip() and not reasoning_content:
                empty_responses += 1
                if empty_responses >= 3:
                    terminated_reason = "empty_response"
                    error_msg = (
                        f"model sent {empty_responses} consecutive empty "
                        f"responses (no content, no tool calls) on turn {turn}"
                    )
                    exit_code = 2
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was empty (no text, no tool "
                            "call). Continue working on the issue and make a "
                            "productive next step — either edit a file or report "
                            "your final result in text."
                        ),
                    }
                )
                continue

            # A real (non-empty) turn resets the empty-response streak.
            empty_responses = 0

            # No tool calls but non-empty content — the model is done: a clean
            # finish (text response with a conclusion).
            if not msg.tool_calls:
                break

            # Execute tool calls.
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                raw_args = tc.function.arguments
                try:
                    tool_args = json.loads(raw_args)
                except json.JSONDecodeError as exc:
                    # harness-05 F1 (2026-08-24): the provider cut the response
                    # mid-argument (the run-4 crash — Unterminated string at
                    # column 88). This is truncation again, arriving under a
                    # tool call instead of a "stop". NEVER crash — one bad tool
                    # payload must not lose 22 turns of work. Recoverable: hand
                    # the model the error and let it re-issue the call. The
                    # trajectory entry below is still written so the failure is
                    # visible.
                    malformed_tool_calls += 1
                    # F2 (2026-08-24, harness-05): bound the recovery.  If the
                    # provider keeps truncating, this would otherwise loop to
                    # max_turns burning budget.  Terminate with its OWN reason —
                    # the run stopped because the MODEL kept sending broken tool
                    # calls, which is neither a clean finish nor EMPTY_PATCH.
                    if malformed_tool_calls >= 3:
                        terminated_reason = "malformed_tool_calls"
                        error_msg = (
                            f"model sent {malformed_tool_calls} consecutive "
                            f"malformed tool-call arguments (truncated JSON)"
                        )
                        exit_code = 2
                        break
                    result = (
                        f"ERROR: tool arguments were not valid JSON ({exc}). "
                        f"Re-issue this tool call with complete, valid JSON."
                    )
                    tool_args = {}
                else:
                    # F3 (2026-08-24, harness-05 D1): execute_tool is the OTHER
                    # unguarded raise in the loop — an exception here would lose
                    # every artifact (the crash run's hole).  Recoverable: hand
                    # the model the executor's error and let it re-issue, exactly
                    # like the malformed-JSON path.  Never crash a run on one bad
                    # tool execution.
                    try:
                        result = execute_tool(tool_name, tool_args, repo_dir)
                    except Exception as exc:  # noqa: BLE001
                        malformed_tool_calls += 1
                        if malformed_tool_calls >= 3:
                            terminated_reason = "malformed_tool_calls"
                            error_msg = (
                                f"tool execution failed {malformed_tool_calls} "
                                f"consecutive times (tool={tool_name}): {exc}"
                            )
                            exit_code = 2
                            break
                        result = (
                            f"ERROR: tool execution failed ({exc}). "
                            f"Re-issue this tool call or try a different command."
                        )
                        tool_args = {}

                raw_log_lines.append(
                    f"--- turn {turn} tool {tool_name} ---\n"
                    f"args: {json.dumps(tool_args)}\n"
                    f"output: {result[:8000]}\n"
                )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )
                # Compaction build: a tool result is new content since the last
                # measurement — count its chars for the chars/4 trigger estimate.
                _compaction_state["chars_since_measure"] += len(result)
                trajectory.add_tool_result(
                    turn=turn,
                    tool_call_id=tc.id,
                    name=tool_name,
                    output=result,
                    normalized=normalize_tool_result(tool_name, tool_args, result),
                )
        else:
            # K-3: the loop finished without any break — the agent used all
            # ``max_turns`` without signalling DONE. Previously it fell out with
            # the default ``"completed"``, so a run cut off at the cap was
            # recorded (and could auto-grade) like a run that finished on its
            # own. This is a framework-imposed cut: give it its own reason,
            # category and a non-zero exit code. ``while``/``for...else`` fires
            # only when no ``break`` executed, so a natural finish (no tool
            # calls on the final turn) is untouched.
            terminated_reason = "max_turns_exceeded"
            error_msg = f"max_turns exceeded after {self._max_turns} turns"
            exit_code = 2

        # B4 (E10a): the model reasoned per the token count but we never captured a
        # reasoning string under either name — a silent-drop condition that would
        # otherwise look exactly like a model that chose not to reason. Log one
        # WARNING for the run so the artifact names it.
        if usage.reasoning_tokens > 0 and not captured_reasoning_any:
            logger.warning(
                "input %s: gateway reported %d reasoning tokens yet no reasoning "
                "content was ever captured (neither `reasoning_content` nor "
                "`reasoning` on any message) — the reasoning may be dropped; see "
                "the RAW RESPONSE lines",
                input.instance_id,
                usage.reasoning_tokens,
            )

        # --- Extract patch ----------------------------------------------------
        # Write artifacts to the output directory.  The repo lives at the
        # install script's hardcoded /testbed (5b), so the worker passes a
        # per-job workdir; fall back to the checkout's parent for old callers.
        output_dir = Path(input.output_dir or repo_dir.parent)
        trajectory_path = str(output_dir / "trajectory.jsonl")
        trajectory.write(trajectory_path)

        raw_log_path_final = str(output_dir / "harness_stdout.log")
        elapsed = time.monotonic() - start_time
        Path(raw_log_path_final).write_text(
            json.dumps(
                {
                    "instance_id": input.instance_id,
                    "terminated_reason": terminated_reason,
                    "error": error_msg,
                    "exit_code": exit_code,
                    "wall_clock_seconds": elapsed,
                    "usage": {
                        "cumulative_input_tokens": usage.input_tokens,
                        "cumulative_output_tokens": usage.output_tokens,
                        "cumulative_cost_usd": usage.cost_usd,
                        # B5 (E10f): the sub-categories, cumulative — they were
                        # accumulated for every run and never written until now.
                        "cumulative_reasoning_tokens": usage.reasoning_tokens,
                        "cumulative_cached_tokens": usage.cached_tokens,
                        "cumulative_cache_write_tokens": usage.cache_write_tokens,
                    },
                },
                indent=2,
            )
            + "\n\n"
            + "\n".join(raw_log_lines)
        )

        # Stage all changes (including new files) then diff against HEAD.  Uses
        # the shared git_utils.git_diff_or_classify (R2-3/R3-2) — custom_minimal's
        # own byte-for-byte duplicate was deleted; the shared helper owns the
        # patch_extract_s timing and classifies a git timeout as a DISTINCT
        # terminated_reason (not a silent "no patch", and not an exception that
        # would destroy this attempt's artifacts + call rows).
        diff = git_diff_or_classify(repo_dir)
        patch = diff.patch
        success = patch is not None and len(patch) > 0
        if diff.timed_out:
            terminated_reason = "patch_extract_timeout"
            error_msg = "git diff timed out (30s) capturing the patch"

        # Write the patch to disk so it survives the run (architecture §9.2).
        patch_path = str(output_dir / "patch.diff")
        if patch:
            Path(patch_path).write_text(patch)

        # Compute error category from the terminated reason and patch presence.
        error_category = map_terminated_reason_to_error_category(terminated_reason, patch)

        return HarnessOutput(
            patch=patch,
            success=success,
            trajectory_path=trajectory_path,
            raw_log_path=raw_log_path_final,
            usage=usage,
            # M0 §1.3 (R2-1): custom_minimal parses usage — store it as the
            # cross-check against the shim meter (never added to it).
            adapter_reported_usage=usage,
            patch_extract_s=diff.patch_extract_s,
            wall_clock_seconds=time.monotonic() - start_time,
            exit_code=exit_code,
            error=error_msg,
            terminated_reason=terminated_reason,
            error_category=error_category or "",
            # Compaction build (Stage 1.5/2.4): per-instance measurement so "did
            # compaction change this result?" is answerable.  fired is 0 when no
            # pass ran; tokens_before/after are the LAST pass (None = none ran).
            compactions_fired=_compaction_state["fired"],
            compaction_tokens_before=_compaction_state["tokens_before"],
            compaction_tokens_after=_compaction_state["tokens_after"],
            compaction_events=_compaction_state.get("events") or None,
            context_window_tokens=input.context_window_tokens,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_api_error(exc: Exception) -> bool:
    """Return True if *exc* is a gateway/provider API error (auth, 5xx, rate limit).

    These should be classified as ``MODEL_API_ERROR`` rather than ``HARNESS_CRASH``
    so the error taxonomy distinguishes infrastructure failures from agent failures.
    """
    try:
        from openai import APIConnectionError, APIStatusError, AuthenticationError

        if isinstance(exc, (APIStatusError, AuthenticationError)):
            return True
        # Connection errors (DNS, refused) are infrastructure, not model errors.
        if isinstance(exc, APIConnectionError):
            return False
    except ImportError:
        pass
    return False
