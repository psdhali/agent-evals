"""analysis/efficiency.py — the computed per-attempt efficiency profile (rubric v3)."""

from __future__ import annotations

import json
from typing import Any

from swebench_eval.analysis import efficiency


def _tool(name: str, normalized: dict[str, Any], output: str) -> dict[str, Any]:
    return {"role": "tool", "name": name, "normalized": normalized, "output": output}


def _result() -> dict[str, Any]:
    return {"role": "result", "usage": {"cost": 0}}


def _assistant(reasoning: str = "", content: str = "") -> dict[str, Any]:
    return {"role": "assistant", "reasoning": reasoning, "content": content}


def _opencode_read(path: str, **bounds: int) -> dict[str, Any]:
    # opencode's adapter wraps the tool's JSON args in a "command" string
    args = {"filePath": path, **bounds}
    return {"command": json.dumps(args)}


def test_profile_counts_unbounded_reads_repeats_duplicates_and_test_reruns() -> None:
    big = "x" * 30_000
    records = [
        {"role": "user", "content": "task"},
        _assistant("think"),
        _tool("read", _opencode_read("/testbed/a.py"), big),  # unbounded, large
        _result(),
        _assistant(),
        _tool("read", _opencode_read("/testbed/a.py"), big),  # repeat of the same path
        _result(),
        _assistant(),
        _tool("read", _opencode_read("/testbed/b.py", offset=10, limit=50), "ok"),  # bounded
        _result(),
        _assistant(),
        _tool("bash", {"command": "cd /testbed && pytest tests/test_a.py -x"}, "1 failed"),
        _result(),
        _assistant(),
        _tool("bash", {"command": "cd /testbed && pytest tests/test_a.py -x"}, "1 failed"),
        _result(),
        _assistant(),
        _tool("edit", _opencode_read("/testbed/a.py"), "ok"),
        _result(),
        _assistant(),
        _tool("bash", {"command": "cd /testbed && pytest tests/test_a.py -x"}, "1 passed"),
        _result(),
        _assistant("", "done"),
        _result(),
    ]
    calls = [
        {
            "call_index": i + 1,
            "input_tokens": 10_000 * (i + 1),
            "cached_tokens": 9_000 * i,
            "output_tokens": 100,
            "cost_usd": 0.01 * (i + 1),
        }
        for i in range(8)
    ]
    p = efficiency.build_efficiency_profile(records, calls)

    assert p["calls"] == 8 and p["tool_calls"] == 7
    assert p["reads"] == 3 and p["reads_unbounded"] == 2 and p["reads_bounds_unknown"] == 0
    assert p["repeated_reads"] == 1
    assert p["unbounded_read_bytes"] == 60_000
    assert p["large_outputs"] == 2
    assert p["test_runs"] == 3 and p["test_reruns"] == 2  # the same command three times
    assert p["duplicate_calls"] == 1 + 2  # the repeated read + two identical pytest calls
    assert p["last_edit_call"] == 5 and p["calls_after_last_edit"] == 3
    assert p["cost_after_last_edit_usd"] == sum(0.01 * (i + 1) for i in range(5, 8))
    assert p["prompt_tokens"]["max"] == 80_000 and p["prompt_tokens_total"] == 360_000
    assert 0 < p["cached_share"] < 1
    assert p["largest_outputs"][0]["bytes"] == 30_000
    assert p["largest_outputs"][0]["target"] == "/testbed/a.py"
    assert "redundant_test_runs" not in p["hints"]  # only 2 re-runs, below the hint bar


def test_profile_without_the_ledger_leaves_token_fields_null_never_zero() -> None:
    records = [_assistant("r"), _tool("bash", {"command": "ls"}, "a\nb"), _result()]
    p = efficiency.build_efficiency_profile(records, None)
    assert p["calls"] == 1
    assert p["prompt_tokens_total"] is None
    assert p["cost_usd"] is None
    assert p["cached_share"] is None
    assert p["context_heavy_cost_share"] is None
    text = efficiency.render_profile_for_judge(p)
    assert "n/a" in text and "EFFICIENCY PROFILE" in text


def test_context_heavy_cost_share_counts_calls_above_96k_prompt_tokens() -> None:
    records = [_assistant(), _result(), _assistant(), _result()]
    calls = [
        {"call_index": 1, "input_tokens": 50_000, "cost_usd": 0.01},
        {"call_index": 2, "input_tokens": 150_000, "cost_usd": 0.03},
    ]
    p = efficiency.build_efficiency_profile(records, calls)
    assert p["context_heavy_cost_share"] == 0.75


def test_unrecognised_read_arguments_are_bounds_unknown_not_unbounded() -> None:
    records = [_tool("read", {"weird": 1}, "x" * 100), _result()]
    p = efficiency.build_efficiency_profile(records, None)
    assert p["reads"] == 1 and p["reads_unbounded"] == 0 and p["reads_bounds_unknown"] == 1


def test_hints_fire_on_many_unbounded_reads_and_looping() -> None:
    records = []
    for i in range(4):
        records += [_tool("read", _opencode_read(f"/testbed/f{i}.py"), "y" * 9_000), _result()]
    for _ in range(12):
        records += [_tool("bash", {"command": "python probe.py"}, "same"), _result()]
    p = efficiency.build_efficiency_profile(records, None)
    assert "unbounded_file_reads" in p["hints"]
    assert "looping" in p["hints"]
    # reads came before any search tool — the probing hint needs >= 5 reads, so not here
    assert "probing_without_search" not in p["hints"]


def test_render_is_plain_text_with_the_hints_line_last() -> None:
    p = efficiency.build_efficiency_profile([_assistant(), _result()], None)
    text = efficiency.render_profile_for_judge(p)
    assert text.splitlines()[-1].startswith("- causes the numbers alone suggest")


def test_parse_trajectory_jsonl_skips_malformed_lines() -> None:
    text = '{"role": "assistant"}\nnot json\n\n{"role": "result"}\n'
    assert [r["role"] for r in efficiency.parse_trajectory_jsonl(text)] == ["assistant", "result"]


def test_calls_without_usage_are_counted_but_do_not_shape_the_prompt_quantiles() -> None:
    records = [_assistant(), _result(), _assistant(), _result(), _assistant(), _result()]
    calls: list[dict[str, Any]] = [
        {"call_index": 1, "input_tokens": None, "cost_usd": None},  # a probe with no usage
        {"call_index": 2, "input_tokens": 20_000, "cost_usd": 0.01},
        {"call_index": 3, "input_tokens": 30_000, "cost_usd": 0.02},
    ]
    p = efficiency.build_efficiency_profile(records, calls)
    assert p["calls"] == 3
    assert p["prompt_tokens"]["first"] == 20_000 and p["prompt_tokens"]["max"] == 30_000
    assert p["prompt_tokens_total"] == 50_000
