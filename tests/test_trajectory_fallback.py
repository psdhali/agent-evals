"""P4C-3: the unparsed trajectory fallback must actually be marked.

The comment in each adapter promised the one-line fallback was "marked as
unparsed", but no key was written — so a run with zero parseable events was
byte-identical to a short healthy run.  These tests pin the marker so the
fallback can't silently pass as healthy again (matters for the stuck detector,
Phase 8 thresholds, and Phase 7's instance view).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from swebench_eval.harnesses.claude_code.harness import _write_trajectory as claude_write
from swebench_eval.harnesses.codex.harness import _write_trajectory as codex_write
from swebench_eval.harnesses.opencode.harness import _write_trajectory as opencode_write

_TrajectoryWriter = Callable[[list[dict[str, Any]], str, str], bool]


@pytest.mark.parametrize("writer", [claude_write, codex_write, opencode_write])
def test_unparsed_fallback_is_marked(tmp_path: Path, writer: _TrajectoryWriter) -> None:
    """An empty event list writes a single marked-unparsed record."""
    path = tmp_path / "trajectory.jsonl"
    assert writer([], str(path), "problem statement") is True
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected one fallback line, got {len(lines)}"
    record = json.loads(lines[0])
    assert record.get("role") == "user"
    assert record.get("content") == "problem statement"
    assert record.get("unparsed") is True
