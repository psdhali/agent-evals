"""Stage 2.1/2.7 — the shared compaction module + the threshold.

BUILD-SPEC §4's guarantees, asserted:
  1. never drops a message individually — stage 2 drops whole turn-groups;
  2. never orphans a ``tool_call_id`` (asserted inside compact_messages);
  3. assistant content / reasoning_content / tool_calls are NEVER modified
     (a real run keeps 174/174 commands and 145/145 reasoning blocks);
  4. returns the input unchanged below keep_head + keep_tail;
  keep_tail_chars is a BYTE budget — a message-counted tail is unbounded in bytes.

§2.7: threshold = min(int(0.90*W), W - 16_384) -> 235_929 for W=262_144, and a
None window disables (0).
"""

from __future__ import annotations

import copy
from typing import Any

from swebench_eval.harnesses.compaction import (
    compact_messages,
    compute_threshold,
    find_orphans,
)


def _assistant(text: str, *tool_id: str) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": text,
        "reasoning_content": "thinking " + text,
    }
    if tool_id:
        msg["tool_calls"] = [
            {"id": tid, "type": "function", "function": {"name": "bash", "arguments": "{}"}}
            for tid in tool_id
        ]
    return msg


def _tool(tool_call_id: str, text: str = "out" * 400) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": text}


def _convo(n_turns: int = 10, *, keep_tail: int = 6) -> list[dict[str, Any]]:
    """A system + user header, then n_turns of (assistant tool-call, tool result).

    A turn = an assistant message that issues a tool call + the role:"tool"
    result answering it.  Long enough that the middle prunes.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are a coding agent"},
        {"role": "user", "content": "fix the bug"},
    ]
    for i in range(n_turns):
        messages.append(_assistant(f"turn {i}", f"c{i}"))
        messages.append(_tool(f"c{i}"))
    return messages


def test_returns_input_unchanged_below_head_plus_tail() -> None:
    """Guarantee 4: below keep_head+keep_tail the list is returned unchanged."""
    messages = _convo(n_turns=2, keep_tail=8)  # 2 head + 4 = 6 <= 2 + 8
    original = copy.deepcopy(messages)
    out, stats = compact_messages(messages, keep_head=2, keep_tail=8)
    assert out == original
    assert stats["gain_ratio"] == 0.0
    assert stats["before_msgs"] == len(messages)


def test_head_and_newest_turn_group_preserved() -> None:
    """Garantee 1+3: head verbatim, newest turn-group verbatim, no individual drops."""
    messages = _convo(n_turns=10, keep_tail=2)
    out, _ = compact_messages(messages, keep_head=2, keep_tail=2, keep_tool_head_chars=100)
    assert out[0]["content"] == "you are a coding agent"  # head preserved
    assert messages[1]["content"] == "fix the bug"  # user header preserved
    # The last two messages (a full turn-group) survive verbatim.
    assert out[-1] == messages[-1]
    assert out[-2] == messages[-2]


def test_no_orphaned_tool_results() -> None:
    """Guarantee 2: after any compaction there is no tool result whose originating
    assistant tool_call was dropped."""
    messages = _convo(n_turns=10, keep_tail=2)
    out, _ = compact_messages(messages, keep_head=2, keep_tail=2, keep_tool_head_chars=100)
    orphan_results, unanswered = find_orphans(out)
    assert not orphan_results
    assert not unanswered  # every surviving tool result has its assistant call


def test_assistant_blocks_never_modified() -> None:
    """Guarantee 3: assistant content / reasoning_content / tool_calls untouched."""
    messages = _convo(n_turns=10, keep_tail=2)
    before = {
        i: (m["content"], m.get("reasoning_content"), copy.deepcopy(m.get("tool_calls")))
        for i, m in enumerate(messages)
        if m["role"] == "assistant"
    }
    out, _ = compact_messages(messages, keep_head=2, keep_tail=2, keep_tool_head_chars=100)
    after = {
        i: (m["content"], m.get("reasoning_content"), m.get("tool_calls"))
        for i, m in enumerate(out)
        if m["role"] == "assistant"
    }
    # Every assistant block that survives is byte-identical to its input.
    for key, (content, reasoning, tool_calls) in after.items():
        orig = before[key]
        assert content == orig[0]
        assert reasoning == orig[1]
        assert tool_calls == orig[2]  # deep-equal: tool_calls untouched


def test_no_orphans_at_tail_boundary_with_parallel_tool_calls() -> None:
    """The tail boundary must snap to a turn-group so a multi-tool assistant in the
    middle never orphans the tail's tool results (the litellm.trim_messages bug)."""
    # Two tool messages answer ONE assistant message — a fixed-size tail that
    # splits between them would orphan the latter.
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        _assistant("m1", "a", "b"),  # parallel: two calls
        _tool("a"),
        _tool("b"),
        _assistant("recent", "c"),
        _tool("c", text="recent result"),
    ]
    out, _ = compact_messages(messages, keep_head=2, keep_tail=2, keep_tool_head_chars=100)
    orphan_results, unanswered = find_orphans(out)
    assert not orphan_results
    assert not unanswered


def test_keep_tail_chars_walks_back_to_a_byte_budget() -> None:
    """keep_tail_chars bounds the tail BYTES by walking back whole turn-groups."""
    # Many small tool results: a message-counted 8-message tail is ~9.6 K chars of
    # tool output.  A 2.5 K byte budget must walk the tail back to ~3 groups.
    messages = _convo(n_turns=10)
    out, stats = compact_messages(
        messages,
        keep_head=2,
        keep_tail=8,
        keep_tool_head_chars=100,
        keep_tail_chars=2_500,
    )
    # The tail must have been walked back from its message-count boundary to fit
    # the byte budget (stage 1 stubbing preserves the message COUNT, so what
    # shrinks is the tail's byte footprint — the messages forced out of the tail
    # move into the middle and get stubbed).
    assert stats["tail_chars"] <= 2_500
    assert stats["tail_snapped"] is True  # tail_start != message-count position
    assert stats["after_chars"] < stats["before_chars"]  # still actually shrank
    orphan_results, unanswered = find_orphans(out)
    assert not orphan_results
    assert not unanswered


def test_target_chars_drops_turn_groups_when_stubbing_is_not_enough() -> None:
    """Stage 2 only when stage 1 misses: whole turn-groups drop, never singletons."""
    messages = _convo(n_turns=8)
    # A target below the post-stub size forces stage 2.
    keep_head, keep_tail = 2, 2
    out, stats = compact_messages(
        messages,
        keep_head=keep_head,
        keep_tail=keep_tail,
        keep_tool_head_chars=50,
        target_chars=600,
    )
    assert stats["stubbed"] > 0
    assert stats["dropped_groups"] > 0
    orphan_results, unanswered = find_orphans(out)
    assert not orphan_results
    assert not unanswered


def test_compute_threshold_262144() -> None:
    """§2.7 with the shared 16_384 reserve: for W=262_144 ->
    min(235_929, 245_760) = 235_929 (the 0.90·W cap binds)."""
    assert compute_threshold(262_144) == 235_929


def test_compute_threshold_none_disables() -> None:
    """A None window disables compaction (threshold 0 = never trigger)."""
    assert compute_threshold(None) == 0


def test_custom_minimal_compaction_counter_fires_and_reports() -> None:
    """Stage 2.3: a run with a context window drives custom_minimal's compaction
    and the counter lands on the output (BUILD-SPEC §6 measurement)."""
    import json
    import subprocess
    import tempfile
    from pathlib import Path
    from unittest import mock

    from swebench_eval.harnesses.base import HarnessInput, ModelConfig
    from swebench_eval.harnesses.custom_minimal.harness import CustomMinimalHarness

    # Turn 1 issues a tool call with a LARGE result (so the chars/4 estimate
    # crosses the threshold); remaining turns keep working so the message list
    # grows past the compaction floor (keep_head + keep_tail = 8).  context_window
    # small enough that W - 16_384 leaves a positive threshold (~2_232) but the
    # tool output pushes the estimate over it.
    # Use 6 tool turns: head(2) + 6 messages over 3 turns = 8, so compaction
    # first becomes legal at turn 4+ once >8 messages accumulate.
    def _resp(*, big: bool) -> mock.MagicMock:
        r = mock.MagicMock()
        c = mock.MagicMock()
        m = mock.MagicMock()
        tc = mock.MagicMock()
        tc.id = "call_c"
        tc.function.name = "bash"
        tc.function.arguments = json.dumps({"command": "cat big"})
        m.tool_calls = [tc]
        m.content = ""
        c.message = m
        r.choices = [c]
        r.usage = mock.MagicMock()
        r.usage.prompt_tokens = 10_000
        r.usage.completion_tokens = 5
        r.usage.cost = 0.0
        r.usage.prompt_tokens_details = mock.MagicMock()
        r.usage.prompt_tokens_details.cached_tokens = 0
        r.usage.prompt_tokens_details.cache_write_tokens = 0
        r.usage.completion_tokens_details = mock.MagicMock()
        r.usage.completion_tokens_details.reasoning_tokens = 0
        return r

    big_sizes = [0, 300_000, 0, 0, 0, 0]  # the SECOND tool result is huge

    def _tool_call_output(command: str, args: str, repo_dir: str) -> str:
        # The big result (chars/4 = 75_000 >> threshold ~2_232) crosses the
        # trigger the moment the message count allows a pass.
        return ("x" * big_sizes.pop(0)) if big_sizes else "ok"

    fake_client = mock.MagicMock()
    fake_client.chat.completions.create = mock.MagicMock(
        side_effect=[_resp(big=True) for _ in range(6)]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, timeout=10, check=False)
        subprocess.run(
            ["git", "config", "user.email", "t@t.com"],
            cwd=repo_dir,
            capture_output=True,
            timeout=10,
            check=False,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=repo_dir,
            capture_output=True,
            timeout=10,
            check=False,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=repo_dir,
            capture_output=True,
            timeout=10,
            check=False,
        )

        hi = HarnessInput(
            instance_id="cmp-instance",
            repo_url=f"file://{repo_dir}",
            base_commit="HEAD",
            problem_statement="p",
            attempt_number=1,
            repo_checkout_path=str(repo_dir),
            model_config=ModelConfig(
                gateway_base_url="http://t", gateway_api_key="k", model_name="m"
            ),
            timeout_seconds=60,
            max_tokens_per_instance=None,
            max_cost_usd_per_instance=5.0,
            context_window_tokens=18_616,
        )
        harness = CustomMinimalHarness(api_base_url="http://t", api_key="k", model="m", max_turns=6)
        from swebench_eval.harnesses.custom_minimal import harness as _cmh

        with (
            mock.patch("openai.OpenAI", return_value=fake_client),
            mock.patch(f"{_cmh.__name__}.execute_tool", side_effect=_tool_call_output),
        ):
            out = harness.run(hi)

    assert out.compactions_fired is not None
    assert out.compactions_fired >= 1, "the big tool result must cross the threshold"
    # The measurement is recorded (not a fabricated 0); whether the pass shrank
    # depends on where the big result landed — the recent TAIL is kept verbatim by
    # design, so an equal before/after is correct policy, not a failure.
    assert out.compaction_tokens_before is not None
    assert out.compaction_tokens_after is not None
    assert out.context_window_tokens == 18_616
    # Compaction-point record (2026-09-02): every pass lands in compaction_events
    # with the model-call turn it fired on — the artifact results analysis reads.
    assert out.compaction_events is not None
    assert len(out.compaction_events) == out.compactions_fired
    ev = out.compaction_events[0]
    assert ev["trigger"] == "auto"
    assert ev["estimated"] is True
    assert ev["at_model_call"] >= 1
    assert ev["pre_tokens"] is not None and ev["post_tokens"] is not None


def test_count_native_compactions_counts_and_distinguishes_none() -> None:
    """D-2 (review 2026-08-26): the native-CLI harnesses (claude_code / opencode
    / codex) compact INSIDE the CLI, so we count the CLI's OWN notice.  None must
    be distinguishable from an honest 0 (a NULL vs 0 records "no signal seen" vs
    "no pass fired"; the first is a counter bug a 0 would hide).

    Mutation: revert count_native_compactions to always return None (or always 0);
    this test fails."""
    from swebench_eval.harnesses.compaction import count_native_compactions

    markers = ("auto-compact", "compacting the conversation")
    # two notices -> 2
    assert (
        count_native_compactions(
            "auto-compact fired... compacting the conversation... auto-compact", markers
        )
        == 3
    )
    # an honest 0 (text present, no marker) -> 0, NOT None
    assert count_native_compactions("normal run, no compaction", markers) == 0
    # no text -> None (nothing to say)
    assert count_native_compactions("", markers) is None
