"""Unit tests for the stuck-loop detector (§9.5).

The detector is a pure function of trajectory events.  These tests pin signal 1
(repeated identical tool calls), signal 2 (repeated errors), and signal 3 (no
diff progress late-budget) — including the guard that a dropped field / wrong
shape doesn't silently pass.
"""

from __future__ import annotations

from pathlib import Path

from swebench_eval.harnesses.stuck_detector import (
    REPEAT_ERRORED,
    REPEAT_IDENTICAL_NO_DIFF,
    StuckState,
    evaluate,
    evaluate_trajectory_file,
)


def _tool(tool: str, arg: str, out: str = "") -> dict[str, object]:
    return {"role": "tool", "name": tool, "normalized": {"command": arg}, "output": out}


def test_signal1_repeated_identical_tool_calls() -> None:
    """5+ identical calls with no diff change → stuck."""
    events = [_tool("bash", "ls -la") for _ in range(REPEAT_IDENTICAL_NO_DIFF)]
    verdict = evaluate(events, diff_fingerprints=["fp"] * REPEAT_IDENTICAL_NO_DIFF)
    assert verdict.stuck
    assert verdict.reason.startswith("signal1")


def test_signal2_repeated_errors() -> None:
    """4+ identical ERRORED calls → stuck."""
    events = [_tool("bash", "python nope.py", out="Traceback error") for _ in range(REPEAT_ERRORED)]
    verdict = evaluate(events)
    assert verdict.stuck
    assert verdict.reason.startswith("signal2")


def test_signal3_no_diff_progress_late_budget() -> None:
    """>50% budget spent and no diff change in trailing turns → stuck.

    Uses DISTINCT tool calls (so signal 1's repeated-identical case doesn't fire
    first) with an unchanged diff fingerprint — which is what signal 3 keys on.
    """
    from swebench_eval.harnesses.stuck_detector import TRAILING_TURNS_NO_DIFF

    events = [_tool("bash", f"grep pattern{i}.py") for i in range(TRAILING_TURNS_NO_DIFF)]
    verdict = evaluate(events, budget_fraction_spent=0.9, diff_fingerprints=["same"] * 20)
    assert verdict.stuck
    assert verdict.reason.startswith("signal3")


def test_healthy_agent_not_stuck() -> None:
    """A mix of distinct tool calls is not stuck."""
    events = [
        _tool("bash", "grep foo file.py"),
        _tool("str_replace_editor", "view", "diff --git"),
        _tool("bash", "python -m pytest -q"),
        _tool("bash", "git add -A"),
    ]
    verdict = evaluate(events, diff_fingerprints=["a", "b", "c", "d"])
    assert not verdict.stuck


def test_identical_calls_stuck_even_without_fingerprints() -> None:
    """5+ identical tool calls are stuck even without diff-fingerprint data."""
    events = [_tool("bash", "echo x")] * REPEAT_IDENTICAL_NO_DIFF
    verdict = evaluate(events, budget_fraction_spent=0.0)
    assert verdict.stuck
    assert verdict.reason.startswith("signal1")


# --- P4C-4: three-state verdict (stuck / not_stuck / insufficient_data) -----


def test_stuck_verdict_state_is_stuck() -> None:
    """A signal-1 verdict also reports state='stuck' (not just stuck=True)."""
    events = [_tool("bash", "ls -la") for _ in range(REPEAT_IDENTICAL_NO_DIFF)]
    verdict = evaluate(events, diff_fingerprints=["fp"] * REPEAT_IDENTICAL_NO_DIFF)
    assert verdict.state == StuckState.STUCK
    assert verdict.stuck


def test_healthy_verdict_state_is_not_stuck() -> None:
    """A mixed healthy run reports state='not_stuck' (NOT insufficient_data)."""
    events = [
        _tool("bash", "grep foo file.py"),
        _tool("str_replace_editor", "view", "diff --git"),
        _tool("bash", "python -m pytest -q"),
        _tool("bash", "git add -A"),
    ]
    verdict = evaluate(events, diff_fingerprints=["a", "b", "c", "d"])
    assert verdict.state == StuckState.NOT_STUCK
    assert not verdict.stuck


def test_no_tool_calls_is_insufficient_data() -> None:
    """A trajectory with no tool calls (the one-line fallback) is insufficient_data,
    NOT a healthy not_stuck — a broken detector must not pass on such input."""
    # The one-line fallback: a single user event, no tool calls.
    events: list[dict[str, object]] = [{"turn": 0, "role": "user", "content": "problem", "ts": "t"}]
    verdict = evaluate(events, budget_fraction_spent=1.0)
    assert verdict.state == StuckState.INSUFFICIENT_DATA
    assert not verdict.stuck
    assert verdict.reason == "no-tool-calls"


def test_missing_file_is_insufficient_data(tmp_path: Path) -> None:
    """A nonexistent trajectory file is insufficient_data, not silence."""
    missing = str(tmp_path / "does-not-exist.jsonl")
    verdict = evaluate_trajectory_file(missing)
    assert verdict.state == StuckState.INSUFFICIENT_DATA
    assert not verdict.stuck
    assert verdict.reason == "no-trajectory"
