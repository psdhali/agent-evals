"""Stuck-loop detector — reads the trajectory to spot a non-progressing agent.

Implements architecture §9.5's three signals, in §9.5's order (most to least
generic/trustworthy):

1. Repeated identical tool calls with no change in resulting `git diff`.
2. Repeated tool-call errors (same failing command/edit across several turns).
3. No diff progress relative to budget consumed — conservative (>50% of the
   token/cost budget spent AND no diff change in the last N turns), because
   legitimate deep exploration on hard instances shouldn't false-trigger.

Runs inside the harness worker's own wrapper, reading the same `trajectory.jsonl`
every adapter already writes (§9.5: no new instrumentation).  The detector is a
PURE function of the event list — it returns a verdict but does not itself kill.
The worker decides whether to log-only (Phase 4 detect-and-log) or act (Phase 8).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict


class _ToolRun(TypedDict):
    """A run of consecutive identical tool calls."""

    tool: str | None
    arg: str
    length: int
    start: int


# ── Thresholds (named constants for Phase 8 validation, not tuned to silence) ──
# How many consecutive identical-with-no-diff tool calls count as stuck (signal 1).
REPEAT_IDENTICAL_NO_DIFF = 5
# How many consecutive identical ERRORED tool calls count as stuck (signal 2).
REPEAT_ERRORED = 4
# Signal 3: fraction of budget that must be spent before the weak signal applies.
BUDGET_FRACTION_TRIGGER = 0.5
# Signal 3: no diff change within this many trailing turns.
TRAILING_TURNS_NO_DIFF = 15


class StuckState:
    """The detector's three verdict states (P4C-4).

    A trajectory that has too few turns / no tool calls cannot be evaluated —
    reporting it as ``not_stuck`` would be indistinguishable from a genuinely
    healthy short run, which is exactly how a broken detector passes the Phase 4
    "not spamming false positives" half of the DoD.  So the verdict is tri-state:
    ``stuck`` / ``not_stuck`` / ``insufficient_data``.
    """

    STUCK = "stuck"
    NOT_STUCK = "not_stuck"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass
class StuckVerdict:
    """The detector's decision — a pure observation, no side effects."""

    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    state: str = StuckState.NOT_STUCK

    @property
    def stuck(self) -> bool:
        """True only in the ``stuck`` state (kept for callers using the bool)."""
        return self.state == StuckState.STUCK


def evaluate_trajectory_file(
    trajectory_path: str,
    budget_fraction_spent: float = 1.0,
    max_tokens_per_instance: int | None = None,
) -> StuckVerdict:
    """Read a trajectory file and evaluate it for stuck behaviour.

    Parameters
    ----------
    trajectory_path:
        Path to the normalized `trajectory.jsonl`.
    budget_fraction_spent:
        Fraction (0..1) of the instance's token/cost budget already consumed —
        used only by the weak signal 3, which needs `>= BUDGET_FRACTION_TRIGGER`.
    max_tokens_per_instance:
        The token ceiling, if set — for the budget-relative wording of signal 3.
    """
    path = Path(trajectory_path)
    if not path.exists():
        return StuckVerdict(
            state=StuckState.INSUFFICIENT_DATA,
            reason="no-trajectory",
            evidence=["trajectory file does not exist — cannot evaluate"],
        )
    events: list[dict[str, object]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return evaluate(events, budget_fraction_spent, None, max_tokens_per_instance)


def evaluate(
    events: list[dict[str, object]],
    budget_fraction_spent: float = 1.0,
    diff_fingerprints: list[str] | None = None,
    max_tokens_per_instance: int | None = None,
) -> StuckVerdict:
    """Evaluate a list of trajectory events for stuck behaviour.

    Pure function — no I/O, no side effects.  Called with the events one slice
    at a time as the worker tails the file.

    ``diff_fingerprints`` is an optional list (one fingerprint per tool turn) the
    worker computes from ``git diff`` — it is what lets signal 1 know the diff did
    not change and signal 3 know there is no diff progress.  If absent, signal 1
    still fires on the strong repeated-tool-call pattern and signal 3 is skipped.
    """
    # --- Insufficient data: nothing to evaluate (P4C-4 / review N-1) ---------
    # Every §9.5 signal needs tool calls across multiple turns.  A trajectory
    # with no tool events — the one-line fallback, an empty file — cannot be
    # judged either way, and MUST NOT read as a healthy "not_stuck".  Tool calls
    # count whether they arrive as role:"tool" events or assistant.tool_calls.
    if not any(_event_tool_calls(ev) for ev in events):
        return StuckVerdict(
            state=StuckState.INSUFFICIENT_DATA,
            reason="no-tool-calls",
            evidence=["trajectory has no tool calls to evaluate"],
        )

    # --- Signal 1: repeated identical tool calls, no diff change ------------
    identical_run = _longest_identical_tool_run(events, errored_only=False)
    if identical_run and identical_run["length"] >= REPEAT_IDENTICAL_NO_DIFF:
        if diff_fingerprints:
            # Confirm the diff actually did not change over that run.
            if _diff_unchanged_over_run(diff_fingerprints, identical_run):
                return StuckVerdict(
                    state=StuckState.STUCK,
                    reason="signal1-repeated-identical-tool-call-no-diff",
                    evidence=[
                        (
                            f"{identical_run['length']}x {identical_run['tool']} "
                            f"{identical_run['arg']!r} with no diff change"
                        )
                    ],
                )

        else:
            # No fingerprints: rely on the strong repeated-tool pattern alone.
            return StuckVerdict(
                state=StuckState.STUCK,
                reason="signal1-repeated-identical-tool-call",
                evidence=[
                    f"{identical_run['length']}x {identical_run['tool']} {identical_run['arg']!r}"
                ],
            )

    # --- Signal 2: repeated identical tool-call ERRORS -----------------------
    err_run = _longest_identical_tool_run(events, errored_only=True)
    if err_run and err_run["length"] >= REPEAT_ERRORED:
        return StuckVerdict(
            state=StuckState.STUCK,
            reason="signal2-repeated-tool-error",
            evidence=[
                (
                    f"{err_run['length']}x same failing {err_run['tool']} {err_run['arg']!r} "
                    f"across {err_run['length']} turns"
                )
            ],
        )

    # --- Signal 3: no diff progress relative to budget spent (weak/conservative) ---
    # Needs real diff fingerprints to be meaningful; skip if none provided.
    if (
        diff_fingerprints
        and budget_fraction_spent >= BUDGET_FRACTION_TRIGGER
        and len(diff_fingerprints) >= TRAILING_TURNS_NO_DIFF
        and len(set(diff_fingerprints[-TRAILING_TURNS_NO_DIFF:])) == 1
    ):
        return StuckVerdict(
            state=StuckState.STUCK,
            reason="signal3-no-diff-progress-late-budget",
            evidence=[
                (
                    f"{budget_fraction_spent:.0%} of budget spent, no diff change "
                    f"in the last {TRAILING_TURNS_NO_DIFF} turns"
                )
            ],
        )

    return StuckVerdict(state=StuckState.NOT_STUCK, reason="ok")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_identity(ev: dict[str, object]) -> tuple[str | None, str]:
    """Return (tool_name, normalized_args_key) for a tool event, or (None, '')."""
    norm = ev.get("normalized") or {}
    if not isinstance(norm, dict):
        norm = {}
    tool = ev.get("name") or norm.get("tool")
    command = str(norm.get("command") or "")
    path = str(norm.get("path") or "")
    editor = str(norm.get("editor_command") or "")
    arg = f"{editor} {path} {command}".strip()
    return str(tool) if tool else None, arg


def _event_tool_calls(ev: dict[str, object]) -> list[tuple[str, str]]:
    """Return (tool, arg) for every tool call an event carries (review N-1).

    The normalized contract lets a tool call appear either as a dedicated
    ``role: "tool"`` event (custom_minimal) or as a ``tool_calls`` array on an
    assistant event (claude_code).  Both must be visible to the detector — an
    ``insufficient_data`` verdict because the detector only reads one shape is
    reporting the detector's state, not the data's.
    """
    if ev.get("role") == "tool":
        tool, arg = _tool_identity(ev)
        return [(tool, arg)] if tool else []
    out: list[tuple[str, str]] = []
    raw_tc = ev.get("tool_calls")
    if isinstance(raw_tc, list):
        for tc in raw_tc:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name") or tc.get("tool")
            inp = tc.get("input")
            if isinstance(inp, dict):
                arg = json.dumps(inp, sort_keys=True)
            else:
                arg = str(inp or "")
            if name:
                out.append((str(name), arg))
    return out


def _is_error(ev: dict[str, object]) -> bool:
    """A best-effort error heuristic on the tool output."""
    out = str(ev.get("output", ""))
    low = out.lower()
    return (
        "error" in low
        or "traceback" in low
        or "exception" in low
        or "command not found" in low
        or "no such file" in low
    )


def _longest_identical_tool_run(
    events: list[dict[str, object]], errored_only: bool
) -> _ToolRun | None:
    """Find the longest run of identical tool calls (optionally errored only).

    Tracks the start index (in tool-event order) so a caller with aligned diff
    fingerprints can check whether the diff changed across the run.
    """
    best: _ToolRun | None = None
    cur_tool: str | None = None
    cur_arg = ""
    cur_count = 0
    cur_start = 0
    tool_idx = 0  # index into the tool-event subsequence
    for ev in events:
        calls = _event_tool_calls(ev)
        if not calls:
            continue
        for tool, arg in calls:
            if errored_only and not _is_error(ev):
                cur_count = 0
                cur_tool = None
                tool_idx += 1
                continue
            if tool == cur_tool and arg == cur_arg:
                cur_count += 1
            else:
                cur_tool, cur_arg, cur_count, cur_start = tool, arg, 1, tool_idx
            if best is None or cur_count > best["length"]:
                best = {
                    "tool": tool,
                    "arg": arg,
                    "length": cur_count,
                    "start": cur_start,
                }
        tool_idx += 1
    return best


def _diff_unchanged_over_run(diff_fingerprints: list[str], run: _ToolRun) -> bool:
    """Whether the diff fingerprints are identical across a tool-call run.

    ``diff_fingerprints`` must be aligned with the tool-event subsequence (one
    fingerprint per tool event, in order).  The run's ``start`` and ``length``
    index into it.
    """
    start = run["start"]
    length = run["length"]
    if start + length > len(diff_fingerprints):
        return False
    window = diff_fingerprints[start : start + length]
    return len(window) >= 2 and len(set(window)) == 1
