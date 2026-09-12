"""Malformed-event corpus test — STEP 5.3 (review 2026-08-26).

The three native-CLI harnesses (claude_code / codex / opencode) parse JSON event
streams from their subprocess stdout and normalise them into `trajectory.jsonl`.
STEP 5.2 hardened the ~16 sites where a wrong-shaped field (`or {}` does not
protect against a truthy non-dict) could raise mid-parse.

This corpus feeds, for every event field the writers read, each malformed shape:
a string where a dict is expected, a dict where a list is expected, ``null``, a
wrong-typed number, and a missing key.  It asserts the *contract* 5.3 exists to
lock down:

- the writer never raises on any of them;
- it still writes valid `trajectory.jsonl` (every line parses);
- records are PRESERVED, not silently dropped (coercion reads a bad field as
  empty rather than abandoning the whole record); and
- the string `tool_use_result` (the exact e3efd51 bug shape) still yields a
  role:tool record carrying the output — the patch path depends on the writer
  not corrupting/abandoning a turn.

Mutation-check each: revert the corresponding 5.2 coercion (or the claude
element-guard) and the matching test must FAIL — a test that cannot fail is how
G-1/G-7 and this e3efd51 string bug all survived.

NOTE on scope: the corpus targets the three event-parsing CLI writers, which are
exactly where malformed raw shapes land (the plan's 5.2 table: claude_code 7
sites, opencode 5, codex 4).  custom_minimal builds its trajectory from typed
OpenAI response objects (not raw JSON event streams), and mini_swe_agent's
normaliser already guards every read with ``isinstance`` — both have no raw
malformed-input surface, so they are out of this corpus.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from typing import Any

import pytest

from swebench_eval.harnesses.claude_code.harness import (
    _usage_from_result,
)
from swebench_eval.harnesses.claude_code.harness import (
    _write_trajectory as claude_write,
)
from swebench_eval.harnesses.codex.harness import (
    _usage_from_events as codex_usage,
)
from swebench_eval.harnesses.codex.harness import (
    _write_trajectory as codex_write,
)
from swebench_eval.harnesses.opencode.harness import (
    _usage_from_events as opencode_usage,
)
from swebench_eval.harnesses.opencode.harness import (
    _write_trajectory as opencode_write,
)

Writer = Callable[[list[dict[str, Any]], str, str], bool]

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_valid_json(
    writer: Writer, events: list[dict[str, Any]], tmp_path
) -> list[dict[str, Any]]:
    """Run a trajectory writer on `events` and return the parsed records.

    Asserts the two contract halves that never change: no exception, and every
    emitted line parses as JSON (a corrupted trajectory would not).
    """
    path = str(tmp_path / "traj.jsonl")
    writer(events, path, "fix the bug")
    with open(path) as fh:
        lines = [l for l in fh.read().splitlines() if l.strip()]
    assert lines, "writer produced no trajectory at all"
    return [_json.loads(l) for l in lines]


# ---------------------------------------------------------------------------
# claude_code — every field the writer reads, badly-typed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "events",
    [
        # message is a string, not a dict (as_dict -> empty)
        [{"type": "user", "message": "not a dict"}],
        # content is a dict, not a list (as_list -> empty)
        [{"type": "assistant", "message": {"content": {"a": 1}}}],
        # content list carries non-dict ELEMENTS — the claude element-guard hole
        [{"type": "assistant", "message": {"content": ["plain string", 3, None]}}],
        [{"type": "user", "message": {"content": ["plain string"]}}],
        # tool_use_result is a plain STRING (the e3efd51 bug shape), not a dict
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Bash"}]},
            },
            {"type": "user", "tool_use_result": "raw tool output string"},
        ],
        # tool_use_result is null
        [
            {
                "type": "user",
                "message": {"content": [{"type": "text", "text": "hi"}]},
                "tool_use_result": None,
            }
        ],
        # result usage is a string
        [{"type": "result", "usage": "oops", "is_error": "yes"}],
        # missing keys everywhere
        [{"type": "user"}, {"type": "assistant"}, {"type": "result"}],
    ],
)
def test_claude_writer_never_raises_and_writes_valid_json(events, tmp_path) -> None:
    """STEP 5.3: claude_code's trajectory writer must not raise on any field being
    the wrong type, null, or missing — and must still emit parseable records.

    Mutation: revert any 5.2 `as_dict`/`as_list` coercion, OR the
    element-guard filtering of non-dict content blocks; this fails (AttributeError
    on the string element / tool_use_result)."""
    _write_valid_json(claude_write, events, tmp_path)


def test_claude_string_tool_use_result_preserves_output(tmp_path) -> None:
    """The e3efd51 bug: `tool_use_result` arrives as a plain STRING (raw tool
    output), not `{stdout,stderr,...}`.  It must be treated as stdout and the
    role:tool record must still carry it — a preserved record, not a dropped
    one (the patch path depends on the whole turn surviving).

    Mutation: revert the `isinstance(tu_result, str)` + as_dict coercion (the
    pre-e3efd51 `msg = ev.get(...)` + `.get` chain); this raises AttributeError."""
    events = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Bash"}]},
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "src/"}]
            },
            "tool_use_result": "raw output",
        },
    ]
    recs = _write_valid_json(claude_write, events, tmp_path)
    tools = [r for r in recs if r["role"] == "tool"]
    assert tools, "string tool_use_result must still yield a role:tool record"
    assert tools[0]["output"] != "" or tools[0]["stdout"] != ""


def test_claude_usage_from_result_never_raises() -> None:
    """The claude COST path (read from the result event) must not raise on a
    wrong-typed usage/total_cost_usd — a malformed cost corrupts spend.

    Mutation: revert `as_int` coercion in `_usage_from_result`; this raises
    TypeError/ValueError on the null/string cost below."""
    # null tokens, non-numeric cost string, usage a string
    u = _usage_from_result({"usage": "str", "total_cost_usd": "abc"})
    assert u.input_tokens == 0
    assert u.cost_usd == 0.0


# ---------------------------------------------------------------------------
# codex — every field the writer reads, badly-typed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "events",
    [
        # item is a string, not a dict (as_dict -> empty)
        [{"type": "item.completed", "item": "oops"}],
        # item is a dict but its scalar fields are wrong-typed
        [{"type": "item.completed", "item": {"type": "command_execution", "command": ["ls"]}}],
        # usage is a string on turn.completed
        [{"type": "turn.completed", "usage": "oops"}],
        # usage tokens are wrong-typed / null / non-numeric (cost path)
        [{"type": "turn.completed", "usage": {"input_tokens": "abc", "output_tokens": None}}],
        # missing keys
        [{"type": "item.completed"}, {"type": "turn.completed"}],
    ],
)
def test_codex_writer_never_raises_and_writes_valid_json(events, tmp_path) -> None:
    """STEP 5.3: codex must not raise on wrong-typed / null / missing fields, and
    still emit parseable trajectory records.

    Mutation: revert the `as_dict`/`as_int` coercion; this fails (AttributeError
    on the string item/usage, TypeError on the non-numeric token in the cost path)."""
    _write_valid_json(codex_write, events, tmp_path)


def test_codex_usage_never_raises_on_non_numeric_tokens() -> None:
    """codex's COST path (`_usage_from_events`) sums input/output tokens into
    Usage; a non-numeric string must be read as 0, never raise TypeError.

    Mutation: revert `as_int` in `_usage_from_events`; this raises TypeError."""
    u = codex_usage(
        [{"type": "turn.completed", "usage": {"input_tokens": "abc", "output_tokens": None}}]
    )
    assert u.input_tokens == 0
    assert u.output_tokens == 0


# ---------------------------------------------------------------------------
# opencode — every field the writer reads, badly-typed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "events",
    [
        # part is a string, not a dict (as_dict -> empty)
        [{"type": "tool_use", "part": "oops"}],
        [{"type": "reasoning", "part": "oops"}],
        [{"type": "step_finish", "part": "oops"}],
        # nested state / input are wrong-typed
        [{"type": "tool_use", "part": {"state": "oops"}}],
        [{"type": "tool_use", "part": {"state": {"input": "oops"}}}],
        # command scalar is a list
        [{"type": "tool_use", "part": {"state": {"input": {"command": ["ls"]}}}}],
        # state.output is a list
        [{"type": "tool_use", "part": {"state": {"output": ["o1", "o2"]}}}],
        # event shape: type + event both missing, or event takes precedence
        [{"type": "tool_use"}],
        [{"event": "tool_use", "part": {"tool": "Bash"}}],
        # missing keys
        [{}],
    ],
)
def test_opencode_writer_never_raises_and_writes_valid_json(events, tmp_path) -> None:
    """STEP 5.3: opencode's writer must not raise on wrong-typed / null / missing
    fields, and still emit parseable records.

    Mutation: revert the `as_dict` coercion on `part`/`state`/`input`; this fails
    (AttributeError on the string part / nested state)."""
    _write_valid_json(opencode_write, events, tmp_path)


def test_opencode_usage_never_raises_on_bad_cost_or_tokens() -> None:
    """opencode's COST path reads cost/tokens from step_finish; a non-numeric
    cost string, a string tokens block, or a non-dict part must read as 0, never
    raise ValueError/AttributeError.

    Mutation: revert `_as_float`/`as_int`/`as_dict` in `_usage_from_events`; this
    raises ValueError on `cost: "abc"` / `tokens: "abc"` (and AttributeError on a
    string part)."""
    cases: list[list[dict[str, Any]]] = [
        [{"type": "step_finish", "part": {"cost": "abc"}}],
        [{"type": "step_finish", "part": {"tokens": "abc"}}],
        [{"type": "step_finish", "part": "oops"}],
        [{"type": "done", "totalCost": "abc"}],
        [{"type": "step_finish", "part": {"tokens": {"input": "1.5", "output": 2}}}],
    ]
    for events in cases:
        u = opencode_usage(events)  # must not raise
        assert u.input_tokens >= 0
        assert u.output_tokens >= 0
        assert u.cost_usd >= 0.0
