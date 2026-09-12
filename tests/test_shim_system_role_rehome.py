"""Shim: Claude Code's mid-list ``role: system`` notices become user text (2026-09-06).

Run 01788665399451336589-d2bf217e (claude_code x minimax on MiniMax's own
endpoint): the sphinx-10614 request carried 31 ``role: system`` messages inside
``messages`` (30 ``<total_tokens>`` countdowns + one "Available agent types"
notice), the cache breakpoint sat on the newest one, and LiteLLM's Anthropic
transform dropped them all — 7% cache hit, ~3.5x the cost of the OpenAI-shaped
harnesses.  ``_rehome_system_role_messages`` re-roles them as user text so the
content and the breakpoint reach the provider and the prefix stays cacheable.
"""

from __future__ import annotations

import json
from typing import Any

from swebench_eval.gateway.local_proxy import _rehome_system_role_messages


def _body(messages: list[dict[str, Any]], **extra: Any) -> bytes:
    return json.dumps(
        {
            "model": "minimax-m2.5-claude_code",
            "max_tokens": 4096,
            "stream": True,
            "system": [
                {
                    "type": "text",
                    "text": "You are a coding agent.",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": messages,
            **extra,
        }
    ).encode()


def test_string_content_system_notice_becomes_a_user_text_block_with_its_breakpoint() -> None:
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": "Fix the bug."},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
        {
            "role": "system",
            "content": "<total_tokens>14979966 tokens left</total_tokens>",
            "cache_control": {"type": "ephemeral"},
        },
    ]
    out, n = _rehome_system_role_messages("/v1/messages", _body(msgs))
    assert n == 1
    obj = json.loads(out)
    last = obj["messages"][-1]
    assert last["role"] == "user"
    assert last["content"] == [
        {
            "type": "text",
            "text": "<total_tokens>14979966 tokens left</total_tokens>",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert "cache_control" not in last  # moved onto the block, not left message-level
    # everything else is untouched, including the top-level system prompt
    assert obj["system"] == json.loads(_body(msgs))["system"]
    assert [m["role"] for m in obj["messages"]] == ["user", "assistant", "user", "user"]


def test_list_content_system_notice_keeps_its_blocks() -> None:
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": "hi"},
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": "Available agent types: ...",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        {"role": "assistant", "content": "ok"},
    ]
    out, n = _rehome_system_role_messages("/v1/messages", _body(msgs))
    assert n == 1
    obj = json.loads(out)
    assert obj["messages"][1]["role"] == "user"
    assert obj["messages"][1]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_many_notices_are_all_rehomed() -> None:
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "start"}]
    for i in range(30):
        msgs.append({"role": "assistant", "content": f"step {i}"})
        msgs.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x"}],
            }
        )
        msgs.append(
            {
                "role": "system",
                "content": f"<total_tokens>{15_000_000 - i * 1000} tokens left</total_tokens>",
            }
        )
    out, n = _rehome_system_role_messages("/v1/messages", _body(msgs))
    assert n == 30
    assert all(m["role"] != "system" for m in json.loads(out)["messages"])


def test_body_without_system_role_messages_is_returned_verbatim() -> None:
    body = _body([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}])
    out, n = _rehome_system_role_messages("/v1/messages", body)
    assert n == 0
    assert out is body  # same bytes object: no re-serialisation


def test_only_anthropic_messages_path_is_touched() -> None:
    body = json.dumps(
        {
            "model": "m",
            "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        }
    ).encode()
    for path in ("/v1/chat/completions", "/v1/responses", "/v1/messages/count_tokens"):
        out, n = _rehome_system_role_messages(path, body)
        assert n == 0 and out is body, path


def test_unparseable_body_is_left_alone() -> None:
    body = b"not json"
    out, n = _rehome_system_role_messages("/v1/messages", body)
    assert (out, n) == (body, 0)
