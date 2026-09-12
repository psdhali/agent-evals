"""opencode's `external_directory` permission (found live 2026-09-07): the generated
opencode.json must allow tool calls outside /testbed, or `opencode run` auto-rejects them and
ends the session (3 empty patches + 5 truncated attempts in the first 291 of run 632ee018)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from swebench_eval.harnesses.opencode import harness as oc


def _cfg(tmp_path: Path, window: int | None) -> dict[str, Any]:
    oc._write_opencode_json(
        tmp_path, "http://127.0.0.1:1/v1", "sk-x", "minimax-m2.5-opencode", window
    )
    cfg: dict[str, Any] = json.loads((tmp_path / "opencode" / "opencode.json").read_text())
    return cfg


def test_external_directory_and_the_core_tools_are_allowed(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, 204_800)
    perm = cfg["permission"]
    assert perm["external_directory"] == "allow"
    for kind in ("edit", "bash", "read", "grep", "glob"):
        assert perm[kind] == "allow", kind
    # nothing is set to ask — in `opencode run` an ask is an auto-reject that ends the session
    assert "ask" not in perm.values()
    assert "webfetch" not in perm  # left at opencode's default on purpose


def test_permission_block_is_present_with_compaction_disabled_too(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, None)
    assert cfg["permission"]["external_directory"] == "allow"
    assert "compaction" not in cfg  # the no-window shape is otherwise unchanged
    assert cfg["provider"]["litellm"]["options"]["baseURL"] == "http://127.0.0.1:1/v1"
