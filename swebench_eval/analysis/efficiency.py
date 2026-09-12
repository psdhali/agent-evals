"""Deterministic per-attempt efficiency profile (2026-09-09).

Owner, after the Laguna opencode pilot ($0.20/attempt, 5.6M prompt tokens/attempt vs 1.2M
on MiniMax): "add a rubric to the judge to show why it took so many tokens / calls — what
could have made it more efficient, e.g. fixed reads vs full-file reads".

The judge sees a pruned trajectory and no numbers, so it cannot say where the tokens went.
This module computes the facts FIRST, without an LLM, from the normalised trajectory
(``trajectory.jsonl`` — role/name/normalized/output rows) and the attempt's ``llm_calls``
rows (prompt/cached/output tokens and cost per call, in call order):

- calls and prompt growth (first / median / p90 / max prompt tokens);
- file reads: how many were unbounded (no limit/offset/range), their bytes, repeats of the
  same path;
- duplicate tool calls (same tool, same arguments), test-suite runs and identical re-runs;
- the largest tool outputs, output bytes per tool;
- reasoning volume;
- spend after the last edit (calls the agent made once its patch was final);
- the share of cost carried by calls whose prompt exceeded 96k tokens.

The profile is stored on ``judge_results.efficiency_profile`` and rendered as a short
factual block in the judge's prompt, so the ``token_efficiency`` dimension (rubric v3)
classifies AVOIDABLE waste against measured numbers rather than impressions. ``hints`` are
the causes the numbers alone suggest — signals for the judge, never a verdict.

Pure functions; tolerant of missing calls (token fields become null, never 0) and of the
per-harness argument shapes (opencode wraps its JSON args in a "command" string; codex /
claude_code / mini-swe use their own keys) — an unrecognised shape counts as "bounds
unknown", which is reported, not assumed either way.
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Sequence
from typing import Any

JsonRecord = dict[str, Any]

EFFICIENCY_CAUSES: tuple[str, ...] = (
    "unbounded_file_reads",
    "repeated_reads",
    "redundant_test_runs",
    "unbounded_command_output",
    "probing_without_search",
    "verbose_reasoning",
    "looping",
    "other",
)

READ_TOOLS = frozenset({"read", "read_file", "view", "view_file", "open_file", "cat"})
EDIT_TOOLS = frozenset(
    {
        "edit",
        "write",
        "patch",
        "apply_patch",
        "multiedit",
        "str_replace_editor",
        "str_replace_based_edit_tool",
        "create",
        "write_file",
        "edit_file",
        "insert",
    }
)
SEARCH_TOOLS = frozenset({"grep", "glob", "search", "find", "rg", "ls", "list", "list_dir"})
SHELL_TOOLS = frozenset({"bash", "shell", "run", "exec", "command", "execute"})

_BOUND_KEYS = frozenset(
    {"limit", "offset", "start_line", "end_line", "view_range", "lines", "range", "start", "end"}
)
_PATH_KEYS = ("filePath", "file_path", "path", "file", "filename", "target_file")
_TEST_RE = re.compile(
    r"(?:\bpytest\b|\bpy\.test\b|runtests\.py|manage\.py\s+test\b|\btox\b|"
    r"python[0-9.]*\s+-m\s+(?:pytest|unittest)\b|\bnosetests\b)"
)
_READ_SHELL_RE = re.compile(r"^\s*(?:cat|sed\s+-n|head|tail|less|more)\s+")

# A single tool result above this is "large" for the profile; 20 kB is ~5k tokens — one
# whole-file read on the Laguna pilot was 58 kB and sat in every later prompt.
LARGE_OUTPUT_BYTES = 20_000
UNBOUNDED_READ_MIN_BYTES = 8_000
CONTEXT_HEAVY_PROMPT_TOKENS = 96_000
PROFILE_VERSION = 1


def _parse_normalized(rec: JsonRecord) -> dict[str, Any]:
    norm = rec.get("normalized")
    if not isinstance(norm, dict):
        return {}
    # opencode's adapter normalises every tool to {"command": <args>}, where <args> is the
    # tool's JSON object serialised as a string (read/edit/write) or a plain string (bash,
    # grep, glob). Unwrap the JSON case so the read/edit keys are visible.
    if set(norm.keys()) == {"command"} and isinstance(norm["command"], str):
        s = norm["command"].strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                inner = json.loads(s)
                if isinstance(inner, dict):
                    return inner
            except ValueError:
                pass
    return norm


def _command_of(norm: dict[str, Any]) -> str:
    for k in ("command", "cmd", "script", "code"):
        v = norm.get(k)
        if isinstance(v, str):
            return v
    return ""


def _path_of(norm: dict[str, Any]) -> str | None:
    for k in _PATH_KEYS:
        v = norm.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _has_bounds(norm: dict[str, Any]) -> bool | None:
    """True when the read carried a limit/offset/range, False when it clearly did not,
    None when the argument shape is not one this profiler recognises."""
    if not norm:
        return None
    if any(k in norm and norm[k] not in (None, "", 0) for k in _BOUND_KEYS):
        return True
    if _path_of(norm) is not None:
        return False
    return None


def _out_bytes(rec: JsonRecord) -> int:
    n = 0
    for key in ("output", "stdout", "stderr", "content"):
        v = rec.get(key)
        if isinstance(v, str):
            n += len(v.encode("utf-8", "replace"))
    return n


def _quantiles(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"first": None, "median": None, "p90": None, "max": None}
    s = sorted(values)
    return {
        "first": float(values[0]),
        "median": float(statistics.median(s)),
        "p90": float(s[min(len(s) - 1, int(len(s) * 0.9))]),
        "max": float(s[-1]),
    }


def _head(text: str, n: int = 80) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def build_efficiency_profile(
    records: list[JsonRecord], calls: list[JsonRecord] | None
) -> dict[str, Any]:
    """*records*: the parsed trajectory JSONL (role/name/normalized/output/reasoning rows,
    in order). *calls*: the attempt's llm_calls rows in call order, each with
    ``input_tokens``/``cached_tokens``/``output_tokens``/``cost_usd`` (any may be null);
    None when the ledger is unavailable — the token/cost fields are then null."""
    tools = [r for r in records if r.get("role") == "tool"]
    assistant = [r for r in records if r.get("role") == "assistant"]
    results = [r for r in records if r.get("role") == "result"]

    # ---- calls + tokens (from the ledger when present) ------------------------------------
    prompt: list[int] = []
    cost_by_call: list[float] = []
    cached_total = 0
    output_total = 0
    if calls:
        for c in calls:
            # a call with no usage (a probe, a failed call) is counted but does not shape
            # the prompt-size quantiles — "first 0 tokens" would be a fiction
            if c.get("input_tokens") is not None:
                prompt.append(int(c["input_tokens"]))
            cost_by_call.append(float(c.get("cost_usd") or 0.0))
            cached_total += int(c.get("cached_tokens") or 0)
            output_total += int(c.get("output_tokens") or 0)
    n_calls = len(calls) if calls else len(results)
    total_cost = sum(cost_by_call) if calls else None
    prompt_total = sum(prompt) if calls else None
    heavy_cost = (
        sum(
            float(c.get("cost_usd") or 0.0)
            for c in calls
            if (c.get("input_tokens") or 0) > CONTEXT_HEAVY_PROMPT_TOKENS
        )
        if calls
        else None
    )

    # ---- tool usage ----------------------------------------------------------------------
    by_tool: dict[str, int] = {}
    bytes_by_tool: dict[str, int] = {}
    signatures: dict[str, int] = {}
    reads_total = 0
    reads_unbounded = 0
    reads_bounds_unknown = 0
    unbounded_read_bytes = 0
    read_paths: dict[str, int] = {}
    shell_reads = 0
    test_runs = 0
    test_cmds: dict[str, int] = {}
    large_outputs = 0
    large_output_bytes = 0
    total_output_bytes = 0
    largest: list[tuple[int, str, str]] = []
    first_read_idx: int | None = None
    first_search_idx: int | None = None
    call_idx = 0
    last_edit_call: int | None = None

    for r in records:
        role = r.get("role")
        if role == "result":
            call_idx += 1
            continue
        if role != "tool":
            continue
        name = str(r.get("name") or "").lower()
        norm = _parse_normalized(r)
        by_tool[name] = by_tool.get(name, 0) + 1
        nbytes = _out_bytes(r)
        total_output_bytes += nbytes
        bytes_by_tool[name] = bytes_by_tool.get(name, 0) + nbytes
        sig = name + ":" + json.dumps(norm, sort_keys=True)[:400]
        signatures[sig] = signatures.get(sig, 0) + 1
        if nbytes > LARGE_OUTPUT_BYTES:
            large_outputs += 1
            large_output_bytes += nbytes
        label = _path_of(norm) or _command_of(norm)
        largest.append((nbytes, name, _head(label)))

        if name in READ_TOOLS:
            reads_total += 1
            if first_read_idx is None:
                first_read_idx = len(signatures)
            path = _path_of(norm)
            if path:
                read_paths[path] = read_paths.get(path, 0) + 1
            b = _has_bounds(norm)
            if b is None:
                reads_bounds_unknown += 1
            elif not b:
                reads_unbounded += 1
                if nbytes >= UNBOUNDED_READ_MIN_BYTES:
                    unbounded_read_bytes += nbytes
        elif name in SEARCH_TOOLS:
            if first_search_idx is None:
                first_search_idx = len(signatures)
        elif name in SHELL_TOOLS:
            cmd = _command_of(norm)
            if _READ_SHELL_RE.match(cmd):
                shell_reads += 1
            if _TEST_RE.search(cmd):
                test_runs += 1
                key = " ".join(cmd.split())[:300]
                test_cmds[key] = test_cmds.get(key, 0) + 1
        if name in EDIT_TOOLS:
            last_edit_call = call_idx

    duplicate_calls = sum(c - 1 for c in signatures.values() if c > 1)
    repeated_reads = sum(c - 1 for c in read_paths.values() if c > 1)
    test_reruns = sum(c - 1 for c in test_cmds.values() if c > 1)
    largest.sort(reverse=True)

    reasoning_chars = sum(len(str(r.get("reasoning") or "")) for r in assistant)
    content_chars = sum(len(str(r.get("content") or "")) for r in assistant)

    calls_after_last_edit = (n_calls - last_edit_call) if last_edit_call is not None else None
    cost_after_last_edit = (
        sum(cost_by_call[last_edit_call:])
        if calls and last_edit_call is not None and last_edit_call < len(cost_by_call)
        else None
    )

    # ---- hints: what the numbers alone suggest (signals for the judge, not verdicts) ------
    hints: list[str] = []
    if reads_unbounded >= 3 or (
        total_output_bytes and unbounded_read_bytes / total_output_bytes >= 0.25
    ):
        hints.append("unbounded_file_reads")
    if repeated_reads >= 3:
        hints.append("repeated_reads")
    if test_reruns >= 3:
        hints.append("redundant_test_runs")
    if large_outputs >= 3 and any(t in SHELL_TOOLS for t in by_tool):
        hints.append("unbounded_command_output")
    if reads_total >= 5 and first_search_idx is not None and first_read_idx is not None:
        if first_read_idx < first_search_idx:
            hints.append("probing_without_search")
    elif reads_total >= 5 and first_search_idx is None:
        hints.append("probing_without_search")
    if assistant and reasoning_chars / max(len(assistant), 1) > 600:
        hints.append("verbose_reasoning")
    if duplicate_calls >= 10:
        hints.append("looping")

    return {
        "profile_version": PROFILE_VERSION,
        "calls": n_calls,
        "tool_calls": len(tools),
        "assistant_turns": len(assistant),
        "prompt_tokens": _quantiles(prompt) if calls else _quantiles([]),
        "prompt_tokens_total": prompt_total,
        "cached_tokens_total": cached_total if calls else None,
        "cached_share": (cached_total / prompt_total) if calls and prompt_total else None,
        "output_tokens_total": output_total if calls else None,
        "cost_usd": total_cost,
        "context_heavy_cost_share": (
            (heavy_cost / total_cost) if calls and total_cost and heavy_cost is not None else None
        ),
        "tools": dict(sorted(by_tool.items(), key=lambda kv: -kv[1])),
        "output_bytes_total": total_output_bytes,
        "output_bytes_by_tool": dict(sorted(bytes_by_tool.items(), key=lambda kv: -kv[1])),
        "largest_outputs": [
            {"bytes": b, "tool": t, "target": label} for b, t, label in largest[:5]
        ],
        "large_outputs": large_outputs,
        "large_output_bytes": large_output_bytes,
        "reads": reads_total,
        "reads_unbounded": reads_unbounded,
        "reads_bounds_unknown": reads_bounds_unknown,
        "unbounded_read_bytes": unbounded_read_bytes,
        "repeated_reads": repeated_reads,
        "shell_reads": shell_reads,
        "duplicate_calls": duplicate_calls,
        "test_runs": test_runs,
        "test_reruns": test_reruns,
        "reasoning_chars": reasoning_chars,
        "content_chars": content_chars,
        "last_edit_call": last_edit_call,
        "calls_after_last_edit": calls_after_last_edit,
        "cost_after_last_edit_usd": cost_after_last_edit,
        "hints": hints,
    }


def _fmt_tokens(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{int(v):,}"


def _fmt_usd(v: float | None) -> str:
    return "n/a" if v is None else f"${v:.4f}"


def _fmt_share(v: float | None) -> str:
    return "n/a" if v is None else f"{100 * v:.0f}%"


def render_profile_for_judge(p: dict[str, Any]) -> str:
    """The factual block placed in the judge's user prompt. Short, numbers only, and
    explicit about what was not measured."""
    q = p.get("prompt_tokens") or {}
    tools = p.get("tools") or {}
    tools_line = ", ".join(f"{k}={v}" for k, v in list(tools.items())[:8]) or "none"
    largest = p.get("largest_outputs") or []
    largest_lines = [
        f"  - {o['bytes']:,} bytes from {o['tool']}: {o['target']}" for o in largest[:4]
    ]
    last_edit = p.get("last_edit_call")
    after = p.get("calls_after_last_edit")
    lines = [
        "EFFICIENCY PROFILE (computed from the call ledger and the trajectory, not an opinion):",
        (
            f"- LLM calls: {p.get('calls')}; tool calls: {p.get('tool_calls')}; "
            f"assistant turns: {p.get('assistant_turns')}"
        ),
        (
            f"- prompt tokens per call: first {_fmt_tokens(q.get('first'))}, median "
            f"{_fmt_tokens(q.get('median'))}, p90 {_fmt_tokens(q.get('p90'))}, max "
            f"{_fmt_tokens(q.get('max'))}; total prompt tokens "
            f"{_fmt_tokens(p.get('prompt_tokens_total'))} "
            f"({_fmt_share(p.get('cached_share'))} served from cache); output tokens "
            f"{_fmt_tokens(p.get('output_tokens_total'))}; cost {_fmt_usd(p.get('cost_usd'))}"
        ),
        (
            f"- share of cost carried by calls with a prompt above "
            f"{CONTEXT_HEAVY_PROMPT_TOKENS // 1000}k tokens: "
            f"{_fmt_share(p.get('context_heavy_cost_share'))}"
        ),
        f"- tool calls by tool: {tools_line}",
        (
            f"- file reads: {p.get('reads')} (unbounded — no limit/offset: "
            f"{p.get('reads_unbounded')}, bounds unknown: {p.get('reads_bounds_unknown')}); "
            f"bytes returned by unbounded reads of {UNBOUNDED_READ_MIN_BYTES // 1000}kB+: "
            f"{p.get('unbounded_read_bytes'):,}; re-reads of an already-read path: "
            f"{p.get('repeated_reads')}; shell reads (cat/sed/head): {p.get('shell_reads')}"
        ),
        (
            f"- duplicate tool calls (same tool, same arguments): {p.get('duplicate_calls')}; "
            f"test-suite runs: {p.get('test_runs')} (identical re-runs: {p.get('test_reruns')})"
        ),
        (
            f"- tool output fed back to the model: {p.get('output_bytes_total'):,} bytes total; "
            f"outputs above {LARGE_OUTPUT_BYTES // 1000}kB: {p.get('large_outputs')} "
            f"({p.get('large_output_bytes'):,} bytes)"
        ),
        (
            f"- reasoning text: {p.get('reasoning_chars'):,} chars over "
            f"{p.get('assistant_turns')} turns"
        ),
        (
            f"- last edit at call {last_edit if last_edit is not None else 'n/a'}; "
            f"calls after it: {after if after is not None else 'n/a'}; "
            f"cost after it: {_fmt_usd(p.get('cost_after_last_edit_usd'))}"
        ),
    ]
    if largest_lines:
        lines.append("- largest tool outputs:")
        lines.extend(largest_lines)
    hints = p.get("hints") or []
    lines.append(
        "- causes the numbers alone suggest (verify against the trajectory; not a verdict): "
        + (", ".join(hints) if hints else "none")
    )
    return "\n".join(lines)


def parse_trajectory_jsonl(text: str) -> list[JsonRecord]:
    """Same tolerant JSONL parse the judge's assembly uses (malformed lines skipped)."""
    out: list[JsonRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
