"""Shared deterministic context compaction for the two harnesses we own.

``mini_swe_agent`` and ``custom_minimal`` both keep an OpenAI-shaped message list:

    {"role": "system"|"user",  "content": str}
    {"role": "assistant", "content": str, "tool_calls": [{"id", ...}], ...}
    {"role": "tool", "tool_call_id": str, "content": str}

so ONE function serves both.  mini messages additionally carry an ``extra`` dict;
it is preserved untouched on everything we keep and marked on anything we stub.

Policy: keep the first X verbatim, keep the last Y verbatim, shrink the middle.

Stage 1 stubs tool RESULTS in the middle (the bulk; already in trajectory.jsonl).
Stage 2, only if stage 1 is not enough, drops the oldest middle turn-GROUPS —
an assistant message together with the tool results answering its tool_calls.
Dropping in groups is what makes orphaned tool_call_ids structurally impossible;
that is the bug that makes litellm.utils.trim_messages unusable here (verified:
it emits a role:"tool" whose originating assistant tool_calls was dropped).

Nothing here calls a model.  Assistant text, reasoning_content and tool_calls are
kept verbatim.

Guarantees (BUILD-SPEC rev 2 §4) the caller relies on:
  1. never drops a message individually — stage 2 drops whole turn-groups;
  2. never orphans a ``tool_call_id`` — asserted before returning (an orphan is a
     400 that kills the instance);
  3. assistant ``content`` / ``reasoning_content`` / ``tool_calls`` are NEVER
     modified (a real run keeps 174/174 commands and 145/145 reasoning blocks);
  4. returns the input unchanged when ``len(messages) <= keep_head + keep_tail``.
"""

from __future__ import annotations

from typing import Any

STUB_SUFFIX = (
    "\n[compacted: {elided} chars elided. The full output is preserved in the run's "
    "trajectory artifact. Continue from your most recent conclusion.]"
)


def _content_len(m: dict[str, Any]) -> int:
    return len(str(m.get("content") or ""))


def messages_chars(messages: list[dict[str, Any]]) -> int:
    """Cheap size proxy: total content chars (+ tool_call arguments)."""
    total = 0
    for m in messages:
        total += _content_len(m)
        total += len(str(m.get("reasoning_content") or ""))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or tc
            total += len(str(fn.get("arguments") or ""))
    return total


def find_orphans(messages: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """(tool results with no originating call, calls with no result)."""
    called: set[str] = set()
    for m in messages:
        for tc in m.get("tool_calls") or []:
            if tc.get("id"):
                called.add(str(tc["id"]))
    answered = {
        str(m["tool_call_id"])
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    return answered - called, called - answered


def _turn_groups(middle: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split the middle into groups that must live or die together.

    A group starts at an assistant message and absorbs the role:"tool" messages
    that answer it.  Anything before the first assistant message forms its own
    leading group.
    """
    groups: list[list[dict[str, Any]]] = []
    for m in middle:
        if m.get("role") == "assistant" or not groups:
            groups.append([m])
        else:
            groups[-1].append(m)
    return groups


def compact_messages(
    messages: list[dict[str, Any]],
    *,
    keep_head: int = 2,
    keep_tail: int = 6,
    keep_tail_chars: int | None = None,
    keep_tool_head_chars: int = 800,
    target_chars: int | None = None,
    tail_hard_cap_chars: int | None = None,
    min_gain_ratio: float = 0.10,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (compacted_messages, stats).  Never calls a model.

    ``target_chars`` (optional) enables stage 2: keep dropping the oldest middle
    turn-group until the whole list fits, or until only head+tail remain.
    """
    stats: dict[str, Any] = {
        "before_msgs": len(messages),
        "before_chars": messages_chars(messages),
        "stubbed": 0,
        "dropped_groups": 0,
        "stage2": False,
        "tail_capped": 0,
        "exhausted": False,
    }
    if len(messages) <= keep_head + keep_tail:
        stats.update(after_msgs=len(messages), after_chars=stats["before_chars"], gain_ratio=0.0)
        return list(messages), stats

    # The tail boundary MUST fall on a turn-group boundary.  A naive slice can
    # start the tail on a role:"tool" message whose assistant lives in the
    # middle; stage 2 then drops that assistant and orphans the tail's results.
    # Found by replaying a real custom_minimal run (parallel tool calls put >1
    # tool message after one assistant, so a fixed-size tail lands mid-group).
    tail_start = len(messages) - keep_tail
    while tail_start > keep_head and messages[tail_start].get("role") == "tool":
        tail_start -= 1
    # keep_tail counts MESSAGES, which is unbounded in size: measured on real
    # runs, single tool results reach 26,247 chars, so a 6-message tail can be
    # 45k chars and no amount of middle-pruning gets under the trigger.  When a
    # byte budget is given, walk the tail back only while it fits -- always
    # keeping at least one whole turn-group.
    if keep_tail_chars is not None:
        t = len(messages)
        acc = 0
        while t > keep_head:
            nxt = t - 1
            while nxt > keep_head and messages[nxt].get("role") == "tool":
                nxt -= 1
            grp = messages[nxt:t]
            grp_chars = messages_chars(grp)
            if acc and acc + grp_chars > keep_tail_chars:
                break
            acc += grp_chars
            t = nxt
        tail_start = min(tail_start, t) if acc == 0 else t
        stats["tail_chars"] = acc
    head = messages[:keep_head]
    tail = messages[tail_start:]
    middle = messages[keep_head:tail_start]
    stats["tail_snapped"] = tail_start != len(messages) - keep_tail

    # ---- stage 1: stub tool results in the middle -------------------------
    pruned: list[dict[str, Any]] = []
    for m in middle:
        if m.get("role") != "tool":
            pruned.append(m)
            continue
        body = str(m.get("content") or "")
        elided = len(body) - keep_tool_head_chars
        suffix = STUB_SUFFIX.format(elided=max(elided, 0))
        # Only stub when the stub is genuinely SMALLER -- measured live, the
        # boilerplate made short outputs GROW (390 -> 411 chars).
        if len(body) <= keep_tool_head_chars + len(suffix):
            pruned.append(m)
            continue
        stub = {k: v for k, v in m.items() if k not in ("content", "extra")}
        stub["content"] = body[:keep_tool_head_chars] + suffix
        if "extra" in m:
            stub["extra"] = {**(m["extra"] or {}), "compacted": True}
        pruned.append(stub)
        stats["stubbed"] += 1

    # ---- stage 2: drop oldest middle turn-groups, whole ------------------
    if target_chars is not None:
        groups = _turn_groups(pruned)
        while (
            messages_chars(head + [m for g in groups for m in g] + tail) > target_chars and groups
        ):
            groups.pop(0)
            stats["dropped_groups"] += 1
            stats["stage2"] = True
        pruned = [m for g in groups for m in g]
        if not groups:
            stats["exhausted"] = True

    # ---- stage 3 (last resort): cap oversized tool results in the TAIL ----
    # "keep the last Y verbatim" has a floor: one huge recent tool result can
    # sit in the tail and keep the context above the trigger no matter how much
    # middle you drop.  Measured on a real custom_minimal run: head+tail alone
    # were 34,209 chars against a 22,988-char target, so every later pass
    # reclaimed 0.0%.  Only engaged when a target is set and stage 1+2 missed it.
    if (
        target_chars is not None
        and tail_hard_cap_chars is not None
        and messages_chars(head + pruned + tail) > target_chars
    ):
        capped_tail = []
        for m in tail:
            body = str(m.get("content") or "")
            if m.get("role") == "tool" and len(body) > tail_hard_cap_chars:
                cm = {k: v for k, v in m.items() if k not in ("content", "extra")}
                cm["content"] = body[:tail_hard_cap_chars] + STUB_SUFFIX.format(
                    elided=len(body) - tail_hard_cap_chars
                )
                if "extra" in m:
                    cm["extra"] = {**(m["extra"] or {}), "compacted": True}
                capped_tail.append(cm)
                stats["tail_capped"] = stats.get("tail_capped", 0) + 1
            else:
                capped_tail.append(m)
        tail = capped_tail

    out = head + pruned + tail
    # Belt-and-braces: the policy above should make orphans impossible, but the
    # cost of being wrong is a 400 that kills the instance, so verify.
    orphan_results, _ = find_orphans(out)
    if orphan_results:  # pragma: no cover - defensive
        raise AssertionError(f"compaction orphaned tool results: {sorted(orphan_results)}")
    stats["after_msgs"] = len(out)
    stats["after_chars"] = messages_chars(out)
    stats["gain_ratio"] = (
        (stats["before_chars"] - stats["after_chars"]) / stats["before_chars"]
        if stats["before_chars"]
        else 0.0
    )
    stats["made_progress"] = stats["gain_ratio"] >= min_gain_ratio
    return out, stats


# The single source of truth for the compaction output reserve, shared by EVERY
# harness so all five compact at the SAME trigger (fair cross-harness comparison,
# 2026-09-02). Lowered 32_768 -> 16_384 after run-1/2 measured live outputs
# never exceeding ~2 K (32 K was ~16× the observed worst case). At W = 262 144
# the 0.90·W cap now binds — min(235 929, 245 760) = 235 929 — which is also
# SAFER than a pure reserve subtraction: it leaves 26 215 tokens of output
# headroom against provider token-count drift, versus the zero-margin 245 760 a
# bare W-16 384 would give. Every harness that requests output tokens must cap
# them at or below this value so a pre-compaction call cannot overflow W.
OUTPUT_RESERVE = 16_384


def compute_threshold(
    window: int | None,
    output_reserve: int = OUTPUT_RESERVE,
) -> int:
    """The compaction trigger token count for a context window (BUILD-SPEC §0/2.7).

    ``threshold = min(int(0.90 * W), W - OUTPUT_RESERVE)`` — for W = 262 144 and
    the shared 16 384 reserve that is ``min(235 929, 245 760) = 235 929`` (the
    0.90·W safety cap binds). The fixed output reserve is NOT the model's own
    ``max_completion_tokens`` (qwen's is 235 929, which would leave a 26 K
    threshold — reserving it looks correct and is wrong).

    Returns 0 when ``window`` is None (compaction disabled), so a harness treats
    a 0 threshold as "never trigger".
    """
    if window is None:
        return 0
    return min(int(0.90 * window), window - output_reserve)


# D-2 (review 2026-08-26): the native-CLI harnesses (claude_code / opencode /
# codex) compact INSIDE the CLI, so we cannot count our own pruner passes.  Each
# declares the CLI's own console/stream notice for an auto-compaction pass and we
# count occurrences.  The exact marker strings are the BEST AVAILABLE statement
# of each CLI's notice — validated/corrected against real S3 logs in Phase 3/4
# (a wrong marker shows 0 or None on a run that compacted, which Phase 4's
# "compactions_fired non-NULL" + ">=1 expected to fire" checks surfacing).
NATIVE_COMPACTION_MARKERS: dict[str, tuple[str, ...]] = {
    # claude_code (Anthropic /v1/messages): Claude Code emits a summary event /
    # "context compaction" notice in its stream-json + console when the auto
    # compact window is crossed.
    "claude_code": ("context compaction", "compacting the conversation", "auto-compact"),
    # opencode: the TUI/CLI logs the auto-compaction pass.
    "opencode": ("[compacting]", "compacting session", "auto-compact"),
    # codex: codex logs its auto-compaction notice to stderr.
    "codex": ("auto-compact", "compacting the conversation", "context compaction"),
}


def count_native_compactions(text: str, markers: tuple[str, ...]) -> int | None:
    """Count a native CLI's auto-compaction notices in its combined output.

    D-2 (review 2026-08-26): a real CLI run always emits output, and the plan's
    pass condition is "compactions_fired non-NULL on all five — a NULL is a
    recording bug even where the count is legitimately 0".  So: a non-empty
    output with no marker records a legitimate **0** (did not fire) — NOT None,
    which is what a broken counter would also look like.  Only an EMPTY output
    returns None (nothing observed at all).  The caller decides the markers per
    harness (see :data:`NATIVE_COMPACTION_MARKERS`).
    """
    if not text:
        return None
    low = text.lower()
    total = 0
    for m in markers:
        total += low.count(m.lower())
    return total
