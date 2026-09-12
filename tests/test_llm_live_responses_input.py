"""A Responses-API request body (codex) carries its conversation as `input` items, not
`messages`; the live-call detail normalises them so the whole history renders
(2026-09-07, owner: the panel showed one step per call)."""

from __future__ import annotations

from swebench_eval.orchestrator.api.llm_live import responses_input_to_messages


def test_input_items_become_chat_messages_in_order() -> None:
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Fix it"}]},
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "Look at the file"}],
            "encrypted_content": "…",
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "exec_command",
            "arguments": '{"cmd": "ls"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "a.py\nb.py"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Two files."}],
        },
        {"type": "mystery_item"},
    ]
    msgs = responses_input_to_messages(items)
    assert [m["role"] for m in msgs] == [
        "user",
        "assistant",
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    assert msgs[0]["content"] == "Fix it"
    assert msgs[1]["reasoning"] == "Look at the file"
    assert msgs[2]["tool_calls"][0]["function"] == {
        "name": "exec_command",
        "arguments": '{"cmd": "ls"}',
    }
    assert msgs[2]["tool_calls"][0]["id"] == "call_1"
    assert msgs[3] == {"role": "tool", "content": "a.py\nb.py", "tool_call_id": "call_1"}
    assert msgs[4]["content"] == "Two files."
    assert msgs[5]["content"] == "[mystery_item]"  # kept, labelled — never silently dropped


def test_bare_string_and_odd_inputs() -> None:
    assert responses_input_to_messages("hello") == [{"role": "user", "content": "hello"}]
    assert responses_input_to_messages(None) == []
    assert responses_input_to_messages(["plain", 42, {"role": "user", "content": "x"}]) == [
        {"role": "user", "content": "plain"},
        {"role": "user", "content": "x"},
    ]


def test_reasoning_without_text_is_marked_not_invented() -> None:
    msgs = responses_input_to_messages([{"type": "reasoning", "encrypted_content": "zz"}])
    assert msgs[0]["reasoning"] == "(encrypted / not returned)"
