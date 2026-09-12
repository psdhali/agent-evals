"""State machine and error taxonomy.

Per architecture.md §9.3 (error taxonomy) and §9.4 (state machine).

The error taxonomy is defined here so the harness layer can import it
without creating a circular dependency.  The mapping from a harness-level
``TerminatedReason`` to an ``ErrorCategory`` is also here — it's the bridge
between the two layers.
"""

from __future__ import annotations

from typing import Literal

# ---------------------------------------------------------------------------
# Error taxonomy — architecture.md §9.3
# ---------------------------------------------------------------------------

ErrorCategory = Literal[
    "RESOLVED",
    "UNRESOLVED",
    "HARNESS_TIMEOUT",
    "HARNESS_STUCK",
    "HARNESS_BUDGET_EXCEEDED",
    "HARNESS_MAX_TURNS_EXCEEDED",
    "HARNESS_MAX_TOKENS_TRUNCATED",
    "HARNESS_MODEL_REFUSED",
    "HARNESS_CRASH",
    "MODEL_API_ERROR",
    "EMPTY_PATCH",
    "PATCH_APPLY_FAILED",
    "EVAL_INFRA_ERROR",
    "ORCHESTRATOR_INFRA_ERROR",
    "EVAL_GRADE_INVALID",
    # ADR-0034 M1.11: the gateway answered the operator-paused 503 marker.  NOT
    # MODEL_API_ERROR — the model did not fail; the operator paused, and
    # classifying it as an API failure pollutes the run with non-failures.
    "PAUSED_BY_OPERATOR",
    # R3-2 (round-3 review): the patch-extraction git add/diff timed out.  An
    # INFRA failure (the agent's work is present, we could not capture it) but
    # DELIBERATELY non-retryable — a slow working-tree stat on a large repo is
    # deterministic, so a retry repeats the model spend five times then lands in
    # the DLQ anyway.
    "HARNESS_PATCH_EXTRACT_TIMEOUT",
    # harness-05 F2 (2026-08-24): the model sent 3 consecutive malformed
    # tool-call arguments (truncated JSON).  Like HARNESS_MAX_TOKENS_TRUNCATED:
    # a provider-cut run, never a legitimate finish, never EMPTY_PATCH.
    "HARNESS_MALFORMED_TOOL_CALLS",
    # R5.3: the harness terminated without any model call reaching the shim.
    # Categorically an infrastructure failure (routing/gateway/launch), retryable
    # like the other infra failures — never a clean finish, never EMPTY_PATCH.
    "HARNESS_ZERO_MODEL_CALLS",
    # D5 (owner decision, option C): the model sent consecutive responses with
    # no content, no tool calls, no reasoning — provider-side stall/truncation.
    # Like HARNESS_MAX_TOKENS_TRUNCATED / HARNESS_MALFORMED_TOOL_CALLS: never a
    # legitimate finish (that was the bug — completed → EMPTY_PATCH), never
    # auto-graded, and RETRYABLE (it is provider-side, not an agent failure).
    "HARNESS_EMPTY_RESPONSE",
    # Compaction build: context window exhausted and compaction reclaimed
    # nothing more (like HARNESS_MAX_TURNS_EXCEEDED — in NEITHER _TERMINAL nor
    # _RETRYABLE: not a genuine finish to resolve, and re-running on a fresh
    # window may still not finish; keep it visible as a truncated run).
    "HARNESS_CONTEXT_EXHAUSTED",
    # EVAL-GRADE-RESOURCE-LIMITS (2026-09-01) §3.3: the kernel OOM-killed a
    # process inside the DinD grading container (docker ``oom`` event, or the
    # eval exec died mid-test-run).  NEVER a verdict — a 137 landing as
    # UNRESOLVED converts an infrastructure limit into a benchmark result, in
    # the one number the project exists to produce.  Infra-shaped and
    # retryable in nature (host-pressure kills are transient; a cgroup-limit
    # kill is deterministic and the remedy is an operator regrade or a bigger
    # host — nothing auto-retries this either way, per the no-automated-resume
    # owner decision).
    "EVAL_OOM_KILLED",
    # 2026-09-06: the test suite exceeded the grade timeout (SWE-bench raises
    # EvaluationError).  Owner decision (first 500-run, django-10097: the model's
    # regex backtracked catastrophically and the suite could never finish): a
    # timeout is the PATCH's doing, so it COUNTS AS A FAILED, GRADEABLE attempt —
    # the row lands as state UNRESOLVED / verdict "unresolved" with this category
    # kept on it for visibility.  Same convention as upstream SWE-bench, where a
    # timed-out instance is simply not resolved.  Terminal: the same patch will
    # hang again, never a retry.
    "EVAL_TIMEOUT",
]

# Retryable on resume: infrastructure failures, not agent failures.
_RETRYABLE: set[ErrorCategory] = {
    "HARNESS_TIMEOUT",
    "HARNESS_STUCK",
    "HARNESS_BUDGET_EXCEEDED",
    "HARNESS_CRASH",
    "MODEL_API_ERROR",
    "EVAL_INFRA_ERROR",
    "ORCHESTRATOR_INFRA_ERROR",
    "HARNESS_ZERO_MODEL_CALLS",
    "HARNESS_EMPTY_RESPONSE",
    "EVAL_OOM_KILLED",
}
# NOTE (K-3): HARNESS_MAX_TURNS_EXCEEDED is deliberately in NEITHER set. It is
# not an infra failure to retry (the model simply never finished), and it is not
# a legitimate finish — it is a truncated run that must remain visible as
# truncated. Whether Phase 8's resume logic revisits turn-capped runs is an
# owner decision, not a taxonomy default.

# Terminal — no retry, these are legitimate final results.
_TERMINAL: set[ErrorCategory] = {
    "RESOLVED",
    "UNRESOLVED",
    "EMPTY_PATCH",
    "PATCH_APPLY_FAILED",
    "EVAL_GRADE_INVALID",
    # A patch that hangs the tests is a failed attempt (see the category note).
    "EVAL_TIMEOUT",
}


def is_retryable(category: ErrorCategory) -> bool:
    """Return True if this error category should be retried on resume."""
    return category in _RETRYABLE


def is_terminal(category: ErrorCategory) -> bool:
    """Return True if this error category is a legitimate final result."""
    return category in _TERMINAL


# ---------------------------------------------------------------------------
# Run-level status — BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md §5 (D1)
# ---------------------------------------------------------------------------
#
# Moved here from results_writer.py (a control-plane *consumer* module) so the
# API layer's restart/close handlers can check eligibility without importing
# in the wrong direction (API importing from a control-plane internal, per the
# design review). results_writer.py imports this too — one definition.
_ALREADY_FINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {"completed", "aborted", "aborting", "finalising"}
)


def is_run_closed(status: str) -> bool:
    """True once a run has reached (or is mid-reaching) a torn-down state.

    Restart and close are both refused once this is True: keys are revoked or
    about to be, and dispatching more work against a closed run's credentials
    would fail auth on the first call.
    """
    return status in _ALREADY_FINAL_RUN_STATUSES


# ---------------------------------------------------------------------------
# State machine — architecture.md §9.4
# ---------------------------------------------------------------------------

HarnessState = Literal[
    "PENDING",
    # run-launch ledger (BUILDER4-RUN-LAUNCH-ORCHESTRATOR §6.2 / D6): the
    # harness dispatcher's DISPATCHED notice, emitted after RunTask succeeds
    # and before any worker-side result exists.  Ranked between PENDING and
    # HARNESS_RUNNING by state_rank() (init.sql) / _STATE_RANK below.
    "DISPATCHED",
    "HARNESS_RUNNING",
    "PATCH_READY",
    "FAILED_HARNESS",
    "STUCK",
    "BUDGET_EXCEEDED",
    "EMPTY_PATCH",
    # ADR-0034 M1.8: abort outcomes.  Both terminal, and both excluded from the
    # resolve-rate numerator AND denominator (abort.py emits them).
    "ABORTED_IN_FLIGHT",
    "NEVER_DISPATCHED",
    # run-launch reaper §7 rule 1/2: an instance the reaper gave up waiting on
    # (dead-lettered or deadline-passed-with-no-sign-of-life).  Ranked BELOW
    # every real terminal state (§6.3) so a straggler result still wins —
    # a premature reap is self-correcting, never permanent.
    "ABANDONED",
]

EvalState = Literal[
    "EVAL_RUNNING",
    "RESOLVED",
    "UNRESOLVED",
    "FAILED_EVAL",
    "PATCH_APPLY_FAILED",
    # run-launch reaper §7: the eval-phase equivalent of a harness ABANDONED —
    # same rank, same self-correcting property.
    "ABANDONED",
]

# Combined state type for the instance_results.state column.
State = HarnessState | EvalState


# ---------------------------------------------------------------------------
# TerminatedReason → ErrorCategory mapping
# ---------------------------------------------------------------------------


def map_terminated_reason_to_error_category(
    terminated_reason: str,
    patch: str | None,
) -> ErrorCategory | None:
    """Map a harness-level ``TerminatedReason`` to an ``ErrorCategory``.

    Parameters
    ----------
    terminated_reason:
        The ``terminated_reason`` string from ``HarnessOutput``.
    patch:
        The harness patch (``None`` or empty string means no patch).

    Returns
    -------
    ErrorCategory or None
        ``None`` when the reason is ``"completed"`` with a non-empty patch —
        the final category depends on the eval verdict, not the harness alone.
    """
    if terminated_reason == "completed":
        if patch:
            return None  # Depends on eval verdict — RESOLVED or UNRESOLVED
        return "EMPTY_PATCH"

    if terminated_reason == "timeout":
        return "HARNESS_TIMEOUT"
    if terminated_reason == "stuck":
        return "HARNESS_STUCK"
    if terminated_reason == "budget_exceeded":
        return "HARNESS_BUDGET_EXCEEDED"
    if terminated_reason == "max_turns_exceeded":
        # K-3: not "completed" (the run was cut off mid-work) and not "timeout"
        # (no deadline was hit — the agent simply never said DONE). Its own
        # category so the truncation stays visible in the data.
        return "HARNESS_MAX_TURNS_EXCEEDED"
    if terminated_reason == "max_tokens_truncated":
        # B2/Z1: the model's response was cut at max_tokens (finish_reason
        # "length"). Same family as max_turns_exceeded — a framework-imposed cut,
        # NOT the model failing and NOT a timeout; its own category so it stays
        # visible (before this, a truncated turn was recorded "completed" → the
        # model "failed" with EMPTY_PATCH).
        return "HARNESS_MAX_TOKENS_TRUNCATED"
    if terminated_reason == "malformed_tool_calls":
        # harness-05 F2: 3 consecutive malformed tool-call arguments.  Same
        # family — a provider-cut run, never a legitimate finish, never
        # EMPTY_PATCH.  Own category so it stays visible and never auto-grades.
        return "HARNESS_MALFORMED_TOOL_CALLS"
    if terminated_reason == "zero_model_calls":
        # R5.3: never reached the model — categorically infra, retryable.
        return "HARNESS_ZERO_MODEL_CALLS"
    if terminated_reason == "context_exhausted":
        # Compaction build: window exhausted + nothing reclaimed — same family
        # as HARNESS_MAX_TURNS_EXCEEDED (in neither retryable nor terminal).
        return "HARNESS_CONTEXT_EXHAUSTED"
    if terminated_reason == "empty_response":
        # D5: provider-side stall/truncation — never a clean finish, never
        # EMPTY_PATCH, retryable.
        return "HARNESS_EMPTY_RESPONSE"
    if terminated_reason == "refused":
        # B3/E10c: the model refused (message.refusal) — a model-level non-
        # completion, its own category, never EMPTY_PATCH.
        return "HARNESS_MODEL_REFUSED"
    if terminated_reason == "crash":
        return "HARNESS_CRASH"
    if terminated_reason == "model_api_error":
        return "MODEL_API_ERROR"
    if terminated_reason == "patch_extract_timeout":
        # R3-2: infra, but see _TERMINAL/_RETRYABLE — deliberately in NEITHER
        # (like HARNESS_MAX_TURNS_EXCEEDED): not a legitimate finish to resolve,
        # and not a transient failure worth re-spending on.
        return "HARNESS_PATCH_EXTRACT_TIMEOUT"

    return "HARNESS_CRASH"  # Fallback for unknown reasons


def map_terminated_reason_to_state(
    terminated_reason: str,
    patch: str | None,
) -> HarnessState:
    """Map a harness-level ``TerminatedReason`` to a ``HarnessState``.

    Derives the ``state`` column from the terminated reason first, falling
    back to patch presence only for ``"completed"``.  This is the retrofit
    the Phase 2 description explicitly warned about — *"cheaper now than
    retrofitting after four more harnesses exist."*

    Parameters
    ----------
    terminated_reason:
        The ``terminated_reason`` string from ``HarnessOutput``.
    patch:
        The harness patch (``None`` or empty string means no patch).

    Returns
    -------
    HarnessState
        The harness-phase state for the ``instance_results.state`` column.
    """
    if terminated_reason == "completed":
        return "PATCH_READY" if patch else "EMPTY_PATCH"

    if terminated_reason == "timeout":
        return "FAILED_HARNESS"
    if terminated_reason == "stuck":
        return "STUCK"
    if terminated_reason == "budget_exceeded":
        return "BUDGET_EXCEEDED"
    if terminated_reason == "max_turns_exceeded":
        # K-3: a framework cut, like timeout/crash. FAILED_HARNESS (not
        # PATCH_READY) means results_writer will NOT auto-enqueue an eval job —
        # ADR-0016: a truncated run's partial patch is inspectable, never graded.
        return "FAILED_HARNESS"
    if terminated_reason == "max_tokens_truncated":
        # B2/Z1: a framework-imposed cut — never grade the partial patch.
        return "FAILED_HARNESS"
    if terminated_reason == "malformed_tool_calls":
        # harness-05 F2: model kept sending broken tool JSON — never grade.
        return "FAILED_HARNESS"
    if terminated_reason == "zero_model_calls":
        # R5.3: infra failure, never a clean finish — FAILED_HARNESS, never
        # EMPTY_PATCH (the run never reached the model, so there is no "agent
        # finished and produced nothing").
        return "FAILED_HARNESS"
    if terminated_reason == "context_exhausted":
        # Compaction build: framework-imposed cut (window exhausted + nothing
        # reclaimed) — FAILED_HARNESS, never PATCH_READY (partial patch
        # inspectable but never graded, ADR-0016).
        return "FAILED_HARNESS"
    if terminated_reason == "empty_response":
        # D5: empty-response run stopped because the model stalled — never a
        # clean finish, never EMPTY_PATCH (no patch was made because nothing
        # was produced, which is a provider-side failure not a "clean empty").
        return "FAILED_HARNESS"
    if terminated_reason == "refused":
        # B3/E10c: the model refused — never grade.
        return "FAILED_HARNESS"
    if terminated_reason == "crash":
        return "FAILED_HARNESS"
    if terminated_reason == "model_api_error":
        return "FAILED_HARNESS"
    if terminated_reason == "patch_extract_timeout":
        # R3-2: captured as a FAILED_HARNESS row (never PATCH_READY -> the
        # partial patch is inspectable, never auto-graded, per ADR-0016).
        return "FAILED_HARNESS"

    return "FAILED_HARNESS"  # Fallback for unknown reasons


def map_eval_outcome_to_error_category(
    resolved: bool,
    patch_apply_ok: bool = True,
    grade_invalid: bool = False,
    oom_killed: bool = False,
    infra_failure: bool = False,
    timed_out: bool = False,
) -> ErrorCategory:
    """Map an eval verdict to an ``ErrorCategory``.

    ``infra_failure`` (SWE-bench 5.x, ADR-0043): the official harness itself
    flagged the grade's test output as a likely environment fault (#586) or
    refused a log that claimed success while the test command exited non-zero
    (#620).  Ranked with ``oom_killed`` — infrastructure voids the grade before
    any log-based verdict — and mapped to the existing retryable
    ``EVAL_INFRA_ERROR`` category.

    Parameters
    ----------
    resolved:
        Whether the FAIL_TO_PASS tests passed after applying the patch.
    patch_apply_ok:
        Whether the patch applied cleanly (default True — callers set False
        when the official harness reports a patch-apply failure).
    grade_invalid:
        Whether the gold tests could NOT be established (A2/E1, 2026-08-19) —
        the official harness ran to "completion" even though its test_patch
        failed to apply, so the tests that ran were not the gold tests and
        no verdict exists.  Terminal and non-retryable: re-running changes
        nothing, the inputs are the same.
    oom_killed:
        Whether the kernel killed a process inside the grading container
        (EVAL-GRADE-RESOURCE-LIMITS §3.3).  Checked FIRST — a killed grade's
        logs are not evidence of anything, so it outranks even
        ``grade_invalid``: infra voids the grade before log-based
        classification gets a say.
    """
    if oom_killed:
        return "EVAL_OOM_KILLED"
    if infra_failure:
        return "EVAL_INFRA_ERROR"
    if timed_out:
        return "EVAL_TIMEOUT"
    if grade_invalid:
        return "EVAL_GRADE_INVALID"
    if not patch_apply_ok:
        return "PATCH_APPLY_FAILED"
    return "RESOLVED" if resolved else "UNRESOLVED"
