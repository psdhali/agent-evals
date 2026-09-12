"""Deterministic context pruner for mini-swe-agent (compaction build 2.2).

Standalone on purpose — imports ONLY stdlib + minisweagent, so it can be loaded
from ``PYTHONPATH`` into mini's isolated uv-tool venv without dragging
swebench_eval's dependency tree in (importing it from there would pull the whole
package ``__init__`` chain, which the uv-tool venv does not have).  Loaded via
``--agent-class mini_pruning_agent.PruningAgent``.

The `extra` dict on each mini message carries the provider-reported usage; this
is what makes the trigger exact — we use the measured ``prompt_tokens`` from the
previous response PLUS a ``chars/4`` estimate of what was appended since, NEVER
``input - cached`` (a cache-read token still occupies the window, and at the
74 % cache-read we have measured, that trigger fires ~4× too early).
"""

from __future__ import annotations

import logging

from minisweagent.agents.default import AgentConfig, DefaultAgent

logger = logging.getLogger("agent")


class PruningAgentConfig(AgentConfig):
    context_window: int = 0  # 0 = disabled (behave exactly like DefaultAgent)
    # Matches swebench_eval.harnesses.compaction.OUTPUT_RESERVE (16 384, 2026-09-02);
    # the harness passes compact_at_tokens explicitly, so this only binds standalone.
    output_reserve_tokens: int = 16384
    compact_at_tokens: int = 0  # 0 = derive: context_window - output_reserve_tokens
    keep_tail: int = 6
    keep_tool_head_chars: int = 800


class PruningAgent(DefaultAgent):
    """A DefaultAgent that prunes its own message list when it nears the window.

    Only the MIDDLE tool outputs are stubbed; asssistant reasoning, texts and
    tool_calls are kept verbatim, and a ``tool_call_id`` is never orphaned.  When
    a pass reclaims less than the progress threshold it latches off for the rest
    of the run (BUILD-SPEC §4 latch-off).
    """

    def __init__(self, model, env, **kwargs):
        super().__init__(model, env, config_class=PruningAgentConfig, **kwargs)
        self.n_compactions = 0
        self._exhausted = False

    # -- trigger ----------------------------------------------------------
    def _threshold(self) -> int:
        if self.config.compact_at_tokens:
            return self.config.compact_at_tokens
        if not self.config.context_window:
            return 0
        return max(0, self.config.context_window - self.config.output_reserve_tokens)

    def _measured_prompt_tokens(self) -> int:
        """Last provider-reported prompt_tokens — free, exact, no tokenizer."""
        for m in reversed(self.messages):
            u = ((m.get("extra") or {}).get("response") or {}).get("usage") or {}
            if u.get("prompt_tokens"):
                return int(u["prompt_tokens"])
        return 0

    def _estimated_since(self) -> int:
        """chars/4 for everything appended after that measurement (D2 fix).

        The measured prompt_tokens is from the LAST model response; everything
        the tool(s) returned since then is not in it.  A crude chars/4 estimate
        prices the tail without a tokenizer.
        """
        total, seen = 0, False
        for m in reversed(self.messages):
            u = ((m.get("extra") or {}).get("response") or {}).get("usage") or {}
            if u.get("prompt_tokens"):
                seen = True
                break
            total += len(str(m.get("content") or ""))
        return total // 4 if seen else 0

    def _compact_if_needed(self) -> None:
        threshold = self._threshold()
        if not threshold:
            return
        applied = self._measured_prompt_tokens() + self._estimated_since()
        if applied < threshold:
            return
        if len(self.messages) <= 2 + self.config.keep_tail:
            return
        if self._exhausted:
            return
        before = len(self.messages)
        head, tail = self.messages[:2], self.messages[-self.config.keep_tail :]
        middle = self.messages[2 : -self.config.keep_tail]
        pruned = []
        for m in middle:
            extra = m.get("extra") or {}
            # mini emits observations as role:"tool" (tool-call mode, what our
            # litellm config uses) or role:"user" (text mode).  Handle both, and
            # ALWAYS preserve tool_call_id — an assistant tool_call with no
            # matching tool result is a malformed conversation the API rejects.
            if "raw_output" in extra and m.get("role") in ("tool", "user"):
                body = str(m.get("content") or "")
                # Only stub when the stub is actually SMALLER.  Measured live:
                # on short outputs the boilerplate made the context GROW
                # (middle chars 390 -> 411).  A compaction that inflates is worse
                # than none.
                if len(body) <= self.config.keep_tool_head_chars + 200:
                    pruned.append(m)
                    continue
                head_txt = body[: self.config.keep_tool_head_chars]
                stub = {
                    "role": m.get("role"),
                    "content": (
                        f"{head_txt}\n[compacted: {len(body)} chars elided, "
                        f"returncode={extra.get('returncode')}. Full output is in the trajectory. "
                        f"Continue from your most recent conclusion.]"
                    ),
                    "extra": {"compacted": True, "returncode": extra.get("returncode")},
                }
                if "tool_call_id" in m:
                    stub["tool_call_id"] = m["tool_call_id"]
                pruned.append(stub)
            else:
                pruned.append(m)  # assistant reasoning + the task: verbatim
        self.messages = head + pruned + tail
        self.n_compactions += 1
        chars_before = sum(len(str(m.get("content") or "")) for m in middle)
        chars_after = sum(len(str(m.get("content") or "")) for m in pruned)
        # Progress guard (D3): if a pass reclaims <10 % there is nothing left to
        # prune -- stop trying, or we compact every turn for the rest of the run.
        if chars_before and chars_after > chars_before * 0.9:
            self._exhausted = True
            logger.warning(
                "COMPACT exhausted: reclaimed only %d of %d chars; disabling further passes",
                chars_before - chars_after,
                chars_before,
            )
        logger.warning(
            "COMPACT #%d fired: applied~%d >= threshold %d | msgs %d->%d | middle chars %d->%d",
            self.n_compactions,
            applied,
            threshold,
            before,
            len(self.messages),
            chars_before,
            chars_after,
        )

    def query(self) -> dict:
        self._compact_if_needed()
        return super().query()
