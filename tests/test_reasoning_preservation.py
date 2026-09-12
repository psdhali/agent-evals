"""Reasoning-preservation regression tests (builder 3, Workstream A).

Guards the fixes documented in dev/fix-trajectory-reasoning-export.md.  Each
test feeds an adapter's ``_write_trajectory`` (or normaliser) a NATIVE event
that carries reasoning, then asserts the reasoning lands in the normalised
``trajectory.jsonl`` under the assistant record's ``reasoning`` field.

Before the fixes all of these FAIL (demonstrated against the live stack):
claude_code/codex/opencode dropped thinking/reasoning at the normaliser; mini
dropped assistant text+reasoning when the turn also had a tool call.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

from swebench_eval.harnesses.claude_code.harness import _write_trajectory as cc_write
from swebench_eval.harnesses.codex.harness import _write_config as cx_write_config
from swebench_eval.harnesses.codex.harness import _write_trajectory as cx_write
from swebench_eval.harnesses.mini_swe_agent.harness import _normalize_mini_trajectory
from swebench_eval.harnesses.opencode.harness import _write_trajectory as oc_write


def _assistant_records(path: Path) -> list[dict[str, object]]:
    return [
        _json.loads(ln)
        for ln in path.read_text().splitlines()
        if ln.strip() and _json.loads(ln)["role"] == "assistant"
    ]


def test_claude_code_preserves_thinking_block(tmp_path: Path) -> None:
    """claude_code: a `type:"thinking"` content block must reach trajectory.jsonl."""
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "I should grep for the caller first."},
                    {"type": "text", "text": "Let me look at the code."},
                    {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}},
                ]
            },
        }
    ]
    path = tmp_path / "cc.jsonl"
    assert cc_write(events, str(path), "problem")
    recs = _assistant_records(path)
    assert recs, "claude_code produced no assistant record"
    assert any(
        r.get("reasoning") == "I should grep for the caller first." for r in recs
    ), f"thinking text lost: {recs}"


def test_claude_code_stream_stdout_thinking_delta(tmp_path: Path) -> None:
    """claude_code stream-json: thinking arrives as a content block, not a delta."""
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "Check the diff first."},
                    {"type": "text", "text": "ok"},
                ]
            },
        }
    ]
    path = tmp_path / "cc2.jsonl"
    assert cc_write(events, str(path), "problem")
    recs = _assistant_records(path)
    assert any(r.get("reasoning") == "Check the diff first." for r in recs)


def test_codex_preserves_reasoning_item(tmp_path: Path) -> None:
    """codex: a `reasoning` item (Responses API) must reach trajectory.jsonl."""
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "reasoning",
                "content": [
                    {"type": "reasoning_text", "text": "The bug is the separability matrix."}
                ],
                "status": "completed",
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "I found it."}},
    ]
    path = tmp_path / "cx.jsonl"
    assert cx_write(events, str(path), "problem")
    recs = _assistant_records(path)
    assert recs, "codex produced no assistant record"
    assert any(
        "separability matrix" in str(r.get("reasoning", "")) for r in recs
    ), f"reasoning lost: {recs}"


def test_opencode_preserves_reasoning_event(tmp_path: Path) -> None:
    """opencode: a `type:"reasoning"` event (stdout --format json) must reach trajectory.jsonl."""
    events = [
        {
            "type": "reasoning",
            "part": {"type": "reasoning", "text": "The user asks a math question. 17*23 = 391."},
        },
        {"type": "text", "text": "391", "part": {"type": "text"}},
    ]
    path = tmp_path / "oc.jsonl"
    assert oc_write(events, str(path), "problem")
    recs = _assistant_records(path)
    assert recs, "opencode produced no assistant record"
    assert any(
        "17*23 = 391" in str(r.get("reasoning", "")) for r in recs
    ), f"reasoning lost: {recs}"


def test_mini_swe_agent_preserves_reasoning_when_turn_has_tool_call() -> None:
    """mini: assistant content+reasoning must survive even when the turn has tool_calls.

    This is the exact bug found on a live run: every mini assistant turn also
    carries a tool call, and the old guard `if content and not tcs` skipped the
    whole assistant record — reasoning (and text) silently vanished.
    """
    raw = {
        "trajectory_format": 1,
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "problem"},
            {
                "role": "assistant",
                "content": "Let me check the file.",
                "reasoning_content": "Let me start by analyzing the current state.",
                "tool_calls": [
                    {
                        "id": "id1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "cat t.txt"}'},
                    }
                ],
            },
        ],
    }
    events = _normalize_mini_trajectory(_json.dumps(raw), "problem")
    assistants = [e for e in events if e["role"] == "assistant"]
    assert assistants, "mini produced no assistant record for an assistant turn with a tool call"
    assert any(
        str(a.get("reasoning", "")) is not None
        and "analyzing the current state" in str(a.get("reasoning", ""))
        for a in assistants
    ), f"mini reasoning lost: {events}"
    # text content must not be conflated into reasoning
    assert any(a.get("content") == "Let me check the file." for a in assistants)


def test_mini_reasoning_only_turn_still_emits_assistant(tmp_path: Path) -> None:
    """mini: a reasoning-only assistant turn (no content, no tools) still emits."""
    raw = {"messages": [{"role": "assistant", "reasoning_content": "thinking only"}]}
    events = _normalize_mini_trajectory(_json.dumps(raw), "problem")
    assert any(e["role"] == "assistant" and e.get("reasoning") == "thinking only" for e in events)


def test_codex_config_requests_high_reasoning_effort(tmp_path: Path) -> None:
    """codex: the per-run config.toml must set model_reasoning_effort = high.

    Without it codex's wire request carries `reasoning:{summary:"auto"}` with
    NO effort, which suppresses reasoning at the provider (0 tokens, measured
    live).  This guards the config-level fix so it cannot silently regress.
    """
    cx_write_config(tmp_path, "http://gateway:4000", "sk-x", "deepseek-flash", None)
    text = (tmp_path / "config.toml").read_text()
    assert 'model_reasoning_effort = "high"' in text, text


def test_codex_config_interpolates_the_run_model(tmp_path: Path) -> None:
    """codex C-2: the config.toml `model` must be the run's model, not a hardcode.

    `_write_config` used to hardcode `claude-code-model` while the CLI's `-m`
    flag carried the real model.  Not a live bug (the flag shadows the config),
    but a single-model leftover one flag-deletion away from silently routing the
    whole codex arm to deepseek — a failure that would look like a model result
    rather than an error.  The config must interpolate the run's model.
    """
    cx_write_config(tmp_path, "http://gateway:4000", "sk-x", "qwen3-coder-next", None)
    text = (tmp_path / "config.toml").read_text()
    assert 'model = "qwen3-coder-next"' in text, text
    assert "claude-code-model" not in text, "hardcoded model leaked into config.toml"
