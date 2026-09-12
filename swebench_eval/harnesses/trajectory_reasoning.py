"""Shared canonicalisers for extracting reasoning from native wire shapes.

The backend is a reasoning model.  Its chain-of-thought arrives in a small,
closed set of native shapes (observed live against the real gateway+model —
see fix-trajectory-reasoning-export.md §5):

- OpenAI chat/completions message  -> ``reasoning_content`` / ``reasoning`` keys
- OpenAI chat/completions stream    -> per-chunk ``delta.reasoning_*``,
  fragmented (a single ``.get`` can never read it)
- Anthropic messages content block  -> ``{type: "thinking", thinking: "..."}``
- Responses API item               -> ``{type: "reasoning", content:[...]}``
- CLI stdout events (claude_code stream-json / opencode --format json) ->
  each wraps one of the above shapes.

Every adapter's ``_write_trajectory`` funnels the reasoning it finds through
this module so all five harnesses land the text under ONE normalised record
key: the assistant record's ``reasoning`` field (the field ``custom_minimal``
already writes; §9.2 leaves it to the adapter).
"""

from __future__ import annotations

from typing import Any


def message_reasoning(msg: Any) -> str:
    """Extract reasoning from an OpenAI chat-shaped object (message or delta).

    First-wins over the two names the backend/gateway may use.  A one-name
    read with no fallback silently drops reasoning when the field arrives
    under the other name (the custom_minimal B4/E10a lesson).
    """
    if not isinstance(msg, dict):
        return ""
    for key in ("reasoning_content", "reasoning"):
        v = msg.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def delta_reasoning_fragments(delta: Any) -> str:
    """Accumulate reasoning text from a single chat/completions stream chunk.

    Streaming delivers the chain-of-thought one fragment at a time across
    several ``delta`` chunks.  The gateway may carry it under either
    ``delta.reasoning_content`` or ``delta.reasoning``; join both if present
    and return the text this chunk added (empty when the chunk has none).
    The caller appends across chunks — a single chunk is never the full
    reasoning, which is why this is a fragment accumulator, not a getter.
    """
    if not isinstance(delta, dict):
        return ""
    parts: list[str] = []
    for key in ("reasoning_content", "reasoning"):
        v = delta.get(key)
        if isinstance(v, str) and v:
            parts.append(v)
    return "".join(parts)


def reasoning_item_text(item: Any) -> str:
    """Extract the reasoning text from a Responses-API ``reasoning`` item.

    The full chain rides in ``content`` (``[{type: "reasoning_text", text}]``);
    ``summary`` (also a list) is the pruned version — read ``content`` first
    because ``summary`` is empty even when ``content`` is full (measured live).
    """
    if not isinstance(item, dict) or item.get("type") != "reasoning":
        return ""
    for key in ("content", "summary"):
        blocks = item.get(key)
        if isinstance(blocks, list):
            parts: list[str] = []
            for b in blocks:
                if isinstance(b, dict):
                    t = b.get("text")
                    if isinstance(t, str) and t:
                        parts.append(t)
            text = "".join(parts)
            if text:
                return text
        elif isinstance(blocks, str) and blocks:
            return blocks
    return ""


def thinking_block_text(block: Any) -> str:
    """Extract the reasoning from an Anthropic-style ``thinking`` content block.

    Claude Code surfaces the backend's chain-of-thought as a
    ``{type: "thinking", thinking: "..."}`` content block on the assistant
    message (both in its ``stream-json`` stdout and in its session file).
    """
    if not isinstance(block, dict) or block.get("type") != "thinking":
        return ""
    t = block.get("thinking")
    return t if isinstance(t, str) and t else ""
