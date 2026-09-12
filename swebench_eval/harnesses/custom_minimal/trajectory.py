"""Trajectory normalization.

Writes the harness-agnostic ``trajectory.jsonl`` format defined in
``architecture.md`` §9.2.  Every harness adapter must produce this same schema
so a future LLM-judge can reason over any harness's output in one format (F13).

X3 (trajectory-review-astropy-12907 §5, 2026-08-18): the per-turn ``usage`` dict
must use SELF-DESCRIBING keys. The old keys mixed per-turn token counts with a
cumulative cost under sibling keys — summing ``cost_usd`` overstated a run 13×
while ``input_tokens`` meant per-turn in one record and cumulative in another.
A record's usage keys are therefore ``turn_input_tokens`` / ``turn_output_tokens``
/ ``cumulative_cost_usd`` (sum the turn keys; read the last record's cumulative
cost). New harness adapters must follow the same convention.

Assistant records also carry the model's ``reasoning`` (chain-of-thought) when
the provider emits it — both a diagnostic record AND the value the harness
replays into the next call (a reasoning model builds on its own prior
reasoning; dropping it breaks multi-turn context).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _trunc(text: str, limit: int = 200) -> str:
    """Truncate a field to ~200 chars for a readable TRAJ log line (R8.1 §2.1).

    Not for cost (the whole 500×5 run is ~$0.13) — for readability: a 30 KB
    tool output in the log stream makes the stream useless.
    """
    if text is None:
        return ""
    text = str(text).strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def cum_usage(usage: Any) -> dict[str, Any]:
    """master-handover 3.3: the cumulative token/cost fields for a TRAJ line.

    Returns ``{"cum_in", "cum_out", "cum_cached", "cum_cost"}`` from a Usage
    (the shim's live-accumulated totals; its fields never go backwards).  A
    separate ``cum_cached`` keeps cache-read volume visible — 736k of 992k input
    tokens were cache hits (74%), which one combined number hides and makes a
    run look ~4x costlier.

    ``usage`` may be None (custom_minimal without the shim); returns an empty
    dict then, so callers render nothing rather than "None".
    """
    if usage is None:
        return {}
    return {
        "cum_in": usage.input_tokens,
        "cum_out": usage.output_tokens,
        "cum_cached": usage.cached_tokens,
        "cum_cost": round(usage.cost_usd, 4),
    }


def cmd_hash(command: str) -> str:
    """master-handover 3.2: a short stable hash of the tool command, the
    stuck/loop signal.

    ``... | grep -o 'cmd_hash=[a-f0-9]*' | uniq -c`` — a repeating hash across
    turns is a loop.  Serves the log reader now and the stuck-detector's
    calibration data (harness_worker.py:911) later.
    """
    import hashlib

    if not command:
        return ""
    return hashlib.sha1(command.strip().encode("utf-8", "replace")).hexdigest()[:12]


def traj_log(
    role: str,
    turn: int,
    tool: str = "NONE",
    content: str = "",
    output: str = "",
    **extra: Any,
) -> str:
    """Build one compact, always-on TRAJ log line (R8.1).

    Shared across custom_minimal and the four subprocess adapters so the live
    CloudWatch stream reads identically regardless of harness:

        TRAJ turn=6 role=assistant tool=bash content_chars=123
        TRAJ turn=6 role=tool tool=bash bytes=2481 head="=== 12 passed, 3 failed ==="
        TRAJ turn=8 role=assistant tool=NONE content_chars=0   # the empty-turn case

    ``tool`` defaults to NONE and is ALWAYS printed — the empty-turn case must
    be explicit (a 0-Write/Edit run used to read as a clean finish until an S3
    dig).  ``output`` is tailed via :func:`_trunc`.  ``extra`` carries optional
    keys (reasoning_chars, finish, cmdh, cum_in, cum_out, cum_cached, cum_cost, …)
    so per-adapter detail doesn't fork the format.

    ``cmd`` (3.1: WHAT the tool did) is truncated like output.  ``cmdh`` (3.2:
    its hash, the stuck/loop signal) and the ``cum_*`` counters (3.3) render as
    given — they are already compact.
    """
    parts = [f"turn={turn}", f"role={role}", f"tool={tool or 'NONE'}"]
    parts.append(f"content_chars={len(content or '')}")  # ALWAYS — empty-turn explicit
    if output:
        parts.append(f'bytes={len(output)} head="{_trunc(output)}"')
    cmd = extra.pop("cmd", None)
    if cmd:
        parts.append(f'cmd="{_trunc(cmd)}"')
    parts.extend(f"{k}={v}" for k, v in extra.items())
    return " ".join(parts)


class TrajectoryWriter:
    """Collects turn-by-turn events and writes them as JSONL."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def _traj(self, line: str) -> None:
        """R8.1: one compact per-turn line to CloudWatch (always-on, ~$0.13).

        The empty-turn case is logged EXPLICITLY — ``tool=NONE content_chars=0``
        made the custom_minimal mid-word stop and claude's 0-Write/Edit obvious
        in seconds instead of an S3 dig (PART 3 §2).
        """
        logger.info("TRAJ %s", line)

    def add_system_message(self, content: str) -> None:
        """A system prompt record — part of what produced the result (X3).

        The trajectory must be reproducible: the system prompt is as much a
        factor as the agent's turns, so it is recorded like any other message.
        """
        self._events.append(
            {
                "turn": 0,
                "role": "system",
                "content": content,
                "ts": _now_iso(),
            }
        )
        self._traj(f"turn=0 role=system content_chars={len(content)}")

    def add_user_message(self, content: str) -> None:
        self._events.append(
            {
                "turn": 0,
                "role": "user",
                "content": content,
                "ts": _now_iso(),
            }
        )
        self._traj(f"turn=0 role=user content_chars={len(content)}")

    def add_assistant_message(
        self,
        turn: int,
        content: str,
        tool_calls: list[dict[str, Any]],
        usage: dict[str, Any],
        reasoning: str = "",
    ) -> None:
        self._events.append(
            {
                "turn": turn,
                "role": "assistant",
                "content": content,
                "reasoning": reasoning,  # model chain-of-thought (DeepSeek/OpenRouter)
                "tool_calls": tool_calls,
                "usage": usage,
                "ts": _now_iso(),
            }
        )
        # R8.1: the empty-turn case logged EXPLICITLY — tool=NONE content_chars=0
        # (the custom_minimal mid-word stop read as a "clean finish").
        tool_name = tool_calls[0].get("name", "NONE") if tool_calls else "NONE"
        self._traj(
            f"turn={turn} role=assistant tool={tool_name} "
            f"reasoning_chars={len(reasoning)} content_chars={len(content)}"
        )

    def add_tool_result(
        self,
        turn: int,
        tool_call_id: str,
        name: str,
        output: str,
        normalized: dict[str, Any] | None = None,
    ) -> None:
        self._events.append(
            {
                "turn": turn,
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": name,
                "output": output,
                "normalized": normalized or {},
                "ts": _now_iso(),
            }
        )
        self._traj(
            f"turn={turn} role=tool tool={name} bytes={len(output)} " f'head="{_trunc(output)}"'
        )

    def write(self, path: str) -> None:
        with open(path, "w") as f:
            f.writelines(json.dumps(event, ensure_ascii=False) + "\n" for event in self._events)


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def normalize_tool_call(tc: Any) -> dict[str, Any]:
    """Convert an OpenAI tool-call object into the normalized schema."""
    return {
        "id": tc.id,
        "name": tc.function.name,
        "arguments": tc.function.arguments,
    }


def normalize_tool_result(name: str, args: dict[str, Any], output: str) -> dict[str, Any]:
    """Build a normalized tool-result entry, tool-type-aware.

    The normalized form makes it easy for a future LLM judge to see *what*
    happened without parsing the raw output string.
    """
    base: dict[str, Any] = {"tool": name}

    if name == "bash":
        base["command"] = args.get("command", "")
    elif name == "str_replace_editor":
        base["editor_command"] = args.get("command", "")
        base["path"] = args.get("path", "")

    return base
