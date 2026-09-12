"""Assemble the judge's input from patch.diff + trajectory.jsonl
(offline-analysis-design.md §3.4, extended §9.1/§9.4/§10.5).

Real trajectories run ~111k tokens median (measured against S3, §9.1) — 4-5x
the design's original 10-30k assumption, ~77% of it tool-role output. Pruning
is therefore close to the DEFAULT case for real trajectories, not the rare
escalation the original design assumed, which is why it's now a configurable
``prune_mode`` (§9.4) rather than only an auto-escalation:

- ``full``   — never prune, whatever the size. Operator's deliberate choice
  (a small manual re-judge, or double-checking a pruning-affected finding);
  may exceed the judge model's context, and that's an accepted risk of
  choosing this mode.
- ``pruned`` — always apply step 2 (head+tail on tool output), regardless of
  whether the full trajectory would have fit. Expected common production
  mode given §9.1's measurement.
- ``auto``   — the original §3.4 behavior: escalate only if the full
  trajectory doesn't fit the judge model's context.

Escalation order, applied only as far as needed (§3.4):
  1. Full trajectory verbatim.
  2. Head+tail each tool-role output field (default 20/20 lines) — this is
     what actually moves the needle (§9.1: ~77% of bytes are tool output).
  3. Elide whole middle events (first/last window), only if step 2 alone
     still doesn't fit.

Every step is recorded, scoped to what it can affect (§3.4/Trap 2): a
pruned/elided trajectory does not silently produce a confident negative on
`loop`/`tool_efficiency`/`hallucination`/`environment_problem` — the caller
(judge.py) must read these flags and mark affected dimensions
``evidence_missing`` rather than a clean score, never inferred here.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

JsonRecord = dict[str, Any]

VALID_PRUNE_MODES = frozenset({"full", "pruned", "auto"})

_HEAD_TAIL_LINES = 20
# Rough chars-per-token estimate (§9.1 used this for the real-data sizing
# measurement) — good enough for a budget decision, not billed on; the
# gateway's own usage figures are what judge_results.input_tokens records.
_CHARS_PER_TOKEN = 4

# The benign codex "Model metadata ... not found" warning (builder3-judge-
# launch-and-pruning-2026-09-01.md / offline-analysis-design.md's C-1 note):
# codex writes this as a first-class {"role": "result", "error": ...} record
# for ANY model outside its own metadata table — which our gateway aliases
# always are — even on a fully successful run. A content-matched allowlist,
# never a blanket "ignore turn-0 errors": a REAL metadata failure (same
# prefix, different tail) must still surface.
#
# The replacement text below is deliberately harness-agnostic (BUILDER3-
# JUDGE-VERIFIED-AND-UI-BRIEF-2026-09-02.md §3.3): the detection strings
# above match codex's actual wording and stay codex-specific, but the marker
# ITSELF lands verbatim in the judge's user prompt, where it would otherwise
# be the one string that only ever appears for one of the five harnesses —
# a blinding leak "we" inserted, not something the agent wrote. Same text
# is asserted in judge_llm.py's system-prompt paragraph; keep them in sync.
#
# Anchored at the start, with a bounded gap (security review, 2026-09-02): an
# earlier version matched `prefix in text and tail in text` — plain substring
# containment, anywhere. That accepts "<anything>Model metadata for `x` not
# found. Defaulting to fallback metadata<anything>" as benign, which means a
# REAL failure (or any other content — including something aimed at the
# judge) prepended before the phrase gets silently discarded along with it,
# since the whole field is replaced with the trusted marker on a match. A
# genuinely benign codex message starts with the phrase; requiring that,
# plus bounding the `<backtick-quoted model name>` gap between prefix and
# tail, closes the "smuggle real content past the allowlist" escape while
# leaving the legitimate case (§3.4's own content-not-blanket test) matching
# exactly as before.
_CODEX_BENIGN_METADATA_PREFIX = "Model metadata for"
_CODEX_BENIGN_METADATA_TAIL = "not found. Defaulting to fallback metadata"
_CODEX_BENIGN_METADATA_PATTERN = re.compile(
    re.escape(_CODEX_BENIGN_METADATA_PREFIX)
    + r".{0,200}?"
    + re.escape(_CODEX_BENIGN_METADATA_TAIL),
    re.DOTALL,
)
_CODEX_BENIGN_MARKER = "[benign tooling warning — filtered by Pass B, not an environment failure]"


@dataclass(frozen=True)
class AssembledInput:
    text: str
    approx_input_tokens: int
    tool_output_pruned: bool  # step 2 fired
    input_truncated: bool  # step 3 fired (events_elided > 0)
    events_elided: int


def _parse_jsonl(text: str) -> list[JsonRecord]:
    records: list[JsonRecord] = []
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("trajectory_assembly: skipping malformed JSONL line %d", i)
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _is_benign_codex_metadata_warning(error_text: str) -> bool:
    return _CODEX_BENIGN_METADATA_PATTERN.match(error_text) is not None


def _filter_benign_codex_warnings(records: list[JsonRecord]) -> list[JsonRecord]:
    out = []
    for rec in records:
        error = rec.get("error")
        if (
            rec.get("role") == "result"
            and isinstance(error, str)
            and _is_benign_codex_metadata_warning(error)
        ):
            rec = dict(rec)
            rec["error"] = _CODEX_BENIGN_MARKER
        out.append(rec)
    return out


def _prune_tool_output(rec: JsonRecord, keep: int) -> JsonRecord:
    if rec.get("role") != "tool":
        return rec
    out = dict(rec)
    changed = False
    for key in ("output", "stdout", "stderr", "content"):
        value = out.get(key)
        if isinstance(value, str):
            lines = value.split("\n")
            if len(lines) > 2 * keep:
                elided = len(lines) - 2 * keep
                out[key] = "\n".join(
                    lines[:keep] + [f"... [elided {elided} lines] ..."] + lines[-keep:]
                )
                changed = True
    return out if changed else rec


def _serialize(records: list[JsonRecord], patch_text: str) -> str:
    parts = []
    if patch_text:
        parts.append(patch_text)
    parts.extend(json.dumps(r) for r in records)
    return "\n".join(parts)


def _approx_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def _elide_middle(
    records: list[JsonRecord], keep_first: int, keep_last: int
) -> tuple[list[JsonRecord], int]:
    n = len(records)
    if keep_first + keep_last >= n:
        return records, 0
    elided = n - keep_first - keep_last
    marker = {"role": "elided", "elided_events": elided}
    tail = records[n - keep_last :] if keep_last else []
    return records[:keep_first] + [marker] + tail, elided


def _shrink_to_fit(
    records: list[JsonRecord], patch_text: str, max_input_tokens: int
) -> tuple[str, int, int]:
    """Step 3: elide whole middle events until the assembled text fits, or
    until the keep-window hits a floor (best-effort — still returns
    something usable, honestly marked as elided either way)."""
    n = len(records)
    window = n
    text = _serialize(records, patch_text)
    tokens = _approx_tokens(text)
    events_elided = 0
    while tokens > max_input_tokens and window > 4:
        window = max(4, window // 2)
        keep_first = max(1, window // 2)
        keep_last = max(1, window - keep_first)
        candidate, events_elided = _elide_middle(records, keep_first, keep_last)
        text = _serialize(candidate, patch_text)
        tokens = _approx_tokens(text)
    return text, tokens, events_elided


def assemble(
    patch_text: str,
    trajectory_jsonl_text: str,
    *,
    prune_mode: str,
    max_input_tokens: int,
    head_tail_lines: int = _HEAD_TAIL_LINES,
) -> AssembledInput:
    if prune_mode not in VALID_PRUNE_MODES:
        raise ValueError(f"unknown prune_mode {prune_mode!r} (must be one of {VALID_PRUNE_MODES})")

    records = _filter_benign_codex_warnings(_parse_jsonl(trajectory_jsonl_text))

    full_text = _serialize(records, patch_text)
    full_tokens = _approx_tokens(full_text)

    if prune_mode == "full":
        return AssembledInput(
            text=full_text,
            approx_input_tokens=full_tokens,
            tool_output_pruned=False,
            input_truncated=False,
            events_elided=0,
        )

    needs_step2 = prune_mode == "pruned" or full_tokens > max_input_tokens
    if not needs_step2:
        return AssembledInput(
            text=full_text,
            approx_input_tokens=full_tokens,
            tool_output_pruned=False,
            input_truncated=False,
            events_elided=0,
        )

    pruned_records = [_prune_tool_output(r, head_tail_lines) for r in records]
    pruned_text = _serialize(pruned_records, patch_text)
    pruned_tokens = _approx_tokens(pruned_text)
    tool_output_pruned = pruned_text != full_text

    if pruned_tokens <= max_input_tokens:
        return AssembledInput(
            text=pruned_text,
            approx_input_tokens=pruned_tokens,
            tool_output_pruned=tool_output_pruned,
            input_truncated=False,
            events_elided=0,
        )

    final_text, final_tokens, events_elided = _shrink_to_fit(
        pruned_records, patch_text, max_input_tokens
    )
    return AssembledInput(
        text=final_text,
        approx_input_tokens=final_tokens,
        tool_output_pruned=tool_output_pruned,
        input_truncated=events_elided > 0,
        events_elided=events_elided,
    )
