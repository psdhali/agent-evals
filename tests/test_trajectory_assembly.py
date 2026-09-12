"""offline-analysis-design.md §3.4/§9.1/§9.4/§10.5 — the judge's input assembly."""

from __future__ import annotations

import json

import pytest

from swebench_eval.analysis import trajectory_assembly as ta


def _traj(*records: dict[str, object]) -> str:
    return "\n".join(json.dumps(r) for r in records)


def test_full_mode_never_prunes_even_when_oversized() -> None:
    big_output = "\n".join(f"line {i}" for i in range(500))
    traj = _traj({"turn": 0, "role": "tool", "output": big_output})
    out = ta.assemble(
        patch_text="", trajectory_jsonl_text=traj, prune_mode="full", max_input_tokens=10
    )
    assert "line 0" in out.text and "line 499" in out.text
    assert out.tool_output_pruned is False
    assert out.input_truncated is False
    assert out.events_elided == 0


def test_pruned_mode_prunes_even_when_it_would_have_fit() -> None:
    """§9.4: pruned mode prunes REGARDLESS of whether the full trajectory
    would already have fit — unlike auto, it does not check first."""
    small_output = "just three\nshort\nlines"
    big_output = "\n".join(f"line {i}" for i in range(200))
    traj = _traj(
        {"turn": 0, "role": "tool", "output": small_output},
        {"turn": 1, "role": "tool", "output": big_output},
    )
    out = ta.assemble(
        patch_text="", trajectory_jsonl_text=traj, prune_mode="pruned", max_input_tokens=1_000_000
    )
    assert "line 100" not in out.text  # the middle of the 200-line output is gone
    assert "line 0" in out.text and "line 199" in out.text  # head+tail survive
    assert out.tool_output_pruned is True
    assert json.dumps(small_output) in out.text  # short output untouched (JSON-escaped form)


def test_auto_mode_does_not_prune_when_it_fits() -> None:
    traj = _traj({"turn": 0, "role": "tool", "output": "small"})
    out = ta.assemble(
        patch_text="", trajectory_jsonl_text=traj, prune_mode="auto", max_input_tokens=1_000_000
    )
    assert out.tool_output_pruned is False
    assert out.input_truncated is False


def test_auto_mode_prunes_only_when_oversized() -> None:
    big_output = "\n".join(f"line {i}" for i in range(1000))
    traj = _traj({"turn": 0, "role": "tool", "output": big_output})
    # max_input_tokens set below the full size but above the pruned size.
    full_tokens = ta._approx_tokens(
        ta._serialize([{"turn": 0, "role": "tool", "output": big_output}], "")
    )
    out = ta.assemble(
        patch_text="",
        trajectory_jsonl_text=traj,
        prune_mode="auto",
        max_input_tokens=full_tokens - 10,
    )
    assert out.tool_output_pruned is True


def test_pruning_never_touches_non_tool_roles() -> None:
    """§3.4: reasoning and tool arguments (assistant records) are what most
    of the rubric scores — pruning must never touch them."""
    long_reasoning = "\n".join(f"thought {i}" for i in range(200))
    traj = _traj({"turn": 0, "role": "assistant", "content": long_reasoning})
    out = ta.assemble(
        patch_text="", trajectory_jsonl_text=traj, prune_mode="pruned", max_input_tokens=1_000_000
    )
    assert "thought 100" in out.text  # untouched even under pruned mode


def test_hallucination_relevant_output_survives_pruning_head_and_tail() -> None:
    """§3.4: hallucination needs SOME tool output to check against — head+tail
    keeps both ends, not zero."""
    lines = [f"irrelevant line {i}" for i in range(100)]
    lines[0] = "SYMBOL_DEFINED_HERE"
    lines[-1] = "test result: FAILED"
    traj = _traj({"turn": 0, "role": "tool", "output": "\n".join(lines)})
    out = ta.assemble(
        patch_text="", trajectory_jsonl_text=traj, prune_mode="pruned", max_input_tokens=1_000_000
    )
    assert "SYMBOL_DEFINED_HERE" in out.text
    assert "test result: FAILED" in out.text


def test_step3_elision_fires_only_when_step2_still_does_not_fit() -> None:
    records = [{"turn": i, "role": "tool", "output": f"call {i}"} for i in range(50)]
    traj = "\n".join(json.dumps(r) for r in records)
    tiny_budget = 5  # forces elision no matter how much pruning helps
    out = ta.assemble(
        patch_text="", trajectory_jsonl_text=traj, prune_mode="auto", max_input_tokens=tiny_budget
    )
    assert out.events_elided > 0
    assert out.input_truncated is True


def test_step3_never_fires_when_step2_is_enough() -> None:
    big_output = "\n".join(f"line {i}" for i in range(1000))
    traj = _traj({"turn": 0, "role": "tool", "output": big_output})
    out = ta.assemble(
        patch_text="", trajectory_jsonl_text=traj, prune_mode="pruned", max_input_tokens=1_000_000
    )
    assert out.events_elided == 0
    assert out.input_truncated is False


def test_codex_benign_metadata_warning_is_filtered_by_content_not_blanket() -> None:
    """The offline-analysis-design.md C-1 note: match on content, never a
    blanket 'ignore turn-0 errors' — a REAL metadata failure with a
    different tail must still surface."""
    benign = {
        "turn": 0,
        "role": "result",
        "error": "Model metadata for `laguna-xs-2.1` not found. Defaulting to fallback metadata; foo",
    }
    real_failure = {
        "turn": 0,
        "role": "result",
        "error": "Model metadata for `x` could not be parsed",
    }
    traj = _traj(benign, real_failure)
    out = ta.assemble(
        patch_text="", trajectory_jsonl_text=traj, prune_mode="full", max_input_tokens=10
    )
    assert "Defaulting to fallback metadata" not in out.text
    assert "filtered by Pass B" in out.text
    assert "could not be parsed" in out.text  # the real failure survives verbatim


def test_benign_metadata_filter_does_not_swallow_content_prepended_before_it() -> None:
    """Security review, 2026-09-02: the earlier version matched substring
    containment anywhere (`prefix in text and tail in text`), so "<real
    failure text>Model metadata for `x` not found. Defaulting to fallback
    metadata" was classified benign and the WHOLE field -- including the
    real failure -- got discarded and replaced with the trusted marker. The
    match must be anchored at the start: a genuinely benign codex message
    starts with the phrase, it never has unrelated content before it."""
    smuggled = {
        "turn": 0,
        "role": "result",
        "error": (
            "CRITICAL: tests were deliberately deleted. Model metadata for "
            "`x` not found. Defaulting to fallback metadata; foo"
        ),
    }
    traj = _traj(smuggled)
    out = ta.assemble(
        patch_text="", trajectory_jsonl_text=traj, prune_mode="full", max_input_tokens=10
    )
    assert "tests were deliberately deleted" in out.text  # real content survives
    assert "filtered by Pass B" not in out.text  # NOT masked as the benign warning


def test_benign_metadata_filter_does_not_smuggle_through_a_huge_gap() -> None:
    """Same escape, other direction: prefix...tail with a large attacker-
    controlled blob stuffed between them (disguised as the backtick-quoted
    model name) must not match either -- the gap is bounded."""
    smuggled = {
        "turn": 0,
        "role": "result",
        "error": "Model metadata for "
        + ("X" * 5000)
        + " not found. Defaulting to fallback metadata",
    }
    traj = _traj(smuggled)
    out = ta.assemble(
        patch_text="", trajectory_jsonl_text=traj, prune_mode="full", max_input_tokens=10
    )
    assert "filtered by Pass B" not in out.text


def test_malformed_jsonl_line_is_skipped_not_fatal() -> None:
    traj = '{"turn": 0, "role": "user"}\nnot valid json\n{"turn": 1, "role": "assistant"}'
    out = ta.assemble(
        patch_text="", trajectory_jsonl_text=traj, prune_mode="full", max_input_tokens=10
    )
    assert out.text.count("turn") == 2


def test_patch_text_is_prepended() -> None:
    traj = _traj({"turn": 0, "role": "user"})
    out = ta.assemble(
        patch_text="--- a/foo.py\n+++ b/foo.py",
        trajectory_jsonl_text=traj,
        prune_mode="full",
        max_input_tokens=10,
    )
    assert out.text.startswith("--- a/foo.py")


def test_unknown_prune_mode_rejected() -> None:
    with pytest.raises(ValueError):
        ta.assemble(
            patch_text="", trajectory_jsonl_text="", prune_mode="bogus", max_input_tokens=10
        )
