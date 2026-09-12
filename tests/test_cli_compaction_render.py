"""Stage 2.5/2.7 — the three CLIs that ALREADY compact are configured at runtime.

BUILD-SPEC §7's mutation check: for each of claude/codex/opencode, the produced
env / config.toml / opencode.json contains the computed threshold when a context
window is resolved, and does NOT when ``context_window_tokens is None``.  A knob
that is written and never read is this project's most repeated defect — these
tests prove the number REACHES the harness's own knob.

codex/opencode render the trigger through ``compute_threshold`` (opencode
derives ``reserved = W - threshold``).  claude exports the RAW window since
2026-09-02: the CLI treats CLAUDE_CODE_AUTO_COMPACT_WINDOW as a window and
subtracts its own output reserve, so exporting the pre-subtracted threshold
double-reserved and compacted ~33k early (proven live, pre_tokens=197,621).
"""

from __future__ import annotations

from pathlib import Path


def _run_harness(
    harness_cls,
    context_window: int | None,
    tmp_path: Path,
    stream_lines: list[str] | None = None,
):
    """Drive a subprocess-CLI harness with run_streaming stubbed and capture env.

    ``stream_lines`` (2026-09-02): when given, each line is fed through the
    harness's ``on_line`` callback and joined into the fake stdout — how the
    compact_boundary tests script a CLI event stream.  Returns ``(captured,
    output)`` so callers can assert on the HarnessOutput too.
    """
    from unittest import mock

    from swebench_eval.harnesses.base import HarnessInput, ModelConfig

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    import subprocess

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

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    captured: dict[str, object] = {}

    def _fake_run(
        cmd: list[str],
        *,
        cwd: object = None,
        env: object = None,
        timeout: int = 0,
        on_line: object = None,
    ) -> object:
        captured["env"] = env
        lines = stream_lines or []
        if callable(on_line):
            for ln in lines:
                on_line(ln)
        stdout = "\n".join(lines)
        return type(
            "R", (), {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}
        )()

    hi = HarnessInput(
        instance_id="cli-cmp",
        repo_url=f"file://{repo_dir}",
        base_commit="HEAD",
        problem_statement="p",
        attempt_number=1,
        repo_checkout_path=str(repo_dir),
        output_dir=str(output_dir),
        model_config=ModelConfig(gateway_base_url="http://t", gateway_api_key="k", model_name="m"),
        timeout_seconds=60,
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=5.0,
        context_window_tokens=context_window,
    )
    harness = harness_cls(api_base_url="http://t", api_key="k", model="m")
    with (
        mock.patch("swebench_eval.harnesses.proc.run_streaming", side_effect=_fake_run),
        mock.patch(f"{harness_cls.__module__}.run_streaming", side_effect=_fake_run),
    ):
        output = harness.run(hi)
    return captured, output


def test_claude_env_aligns_trigger_to_compute_threshold(tmp_path: Path, monkeypatch) -> None:
    """W=262_144 -> AUTO_COMPACT_WINDOW = compute_threshold(W) + 16_384 = 252_313,
    so the CLI (which subtracts its max_tokens reserve) lands the trigger on
    compute_threshold(W) = 235_929, IDENTICAL to every other harness.

    Mutation guard: exporting the raw window (262144) would trigger at 245_760
    — diverging from the other four and leaving zero output margin; exporting
    compute_threshold itself (235929) would double-subtract to 219_545.
    """
    from swebench_eval.harnesses.claude_code.harness import ClaudeCodeHarness

    monkeypatch.delenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", raising=False)
    captured, _ = _run_harness(ClaudeCodeHarness, 262_144, tmp_path)
    env = captured["env"]
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "262144"
    # 235_929 + 16_384 = 252_313; CLI subtracts 16_384 -> trigger 235_929.
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "252313"
    # The reserve the CLI subtracts is its requested max_tokens; it MUST equal
    # OUTPUT_RESERVE (16_384) for the identity above to hold.
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16384"


def test_claude_max_output_tokens_stays_settable(tmp_path: Path, monkeypatch) -> None:
    """setdefault, same fairness rule as MAX_THINKING_TOKENS — task-def wins."""
    from swebench_eval.harnesses.claude_code.harness import ClaudeCodeHarness

    monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "32000")
    captured, _ = _run_harness(ClaudeCodeHarness, 262_144, tmp_path)
    assert captured["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "32000"


def _compact_boundary_line(pre: int, post: int, trigger: str = "auto") -> str:
    import json as _json

    return _json.dumps(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compact_metadata": {"trigger": trigger, "pre_tokens": pre, "post_tokens": post},
        }
    )


def _assistant_line(text: str = "ok") -> str:
    import json as _json

    return _json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def test_claude_records_compact_boundary_events(tmp_path: Path) -> None:
    """The stream's compact_boundary events are the authoritative record.

    Two model calls, then a compaction, then one more call: compactions_fired=1,
    tokens before/after from the event, and the full event list carries the
    model-call position (at_model_call=2).  The old marker substrings match
    nothing in this stream — exactly the live bug (1 real compaction, marker
    count 0) this replaces.
    """
    from swebench_eval.harnesses.claude_code.harness import ClaudeCodeHarness

    lines = [
        _assistant_line("a"),
        _assistant_line("b"),
        _compact_boundary_line(pre=197_621, post=9_934),
        _assistant_line("c"),
    ]
    _, out = _run_harness(ClaudeCodeHarness, 262_144, tmp_path, stream_lines=lines)
    assert out.compactions_fired == 1
    assert out.compaction_tokens_before == 197_621
    assert out.compaction_tokens_after == 9_934
    assert out.context_window_tokens == 262_144
    assert out.compaction_events == [
        {"at_model_call": 2, "trigger": "auto", "pre_tokens": 197_621, "post_tokens": 9_934}
    ]


def test_claude_no_compaction_records_zero_not_none(tmp_path: Path) -> None:
    """Non-empty stream with no boundary event -> honest 0 via the marker
    fallback (Trap 3: None stays reserved for 'nothing observed at all')."""
    from swebench_eval.harnesses.claude_code.harness import ClaudeCodeHarness

    _, out = _run_harness(ClaudeCodeHarness, 262_144, tmp_path, stream_lines=[_assistant_line("a")])
    assert out.compactions_fired == 0
    assert out.compaction_events is None
    assert out.compaction_tokens_before is None


def test_claude_env_omits_compaction_without_window(tmp_path: Path, monkeypatch) -> None:
    """None window -> neither env var is written (compaction disabled).

    ``agent_environment()`` copies the parent env, so clear the vars first — an
    inherited value (e.g. set by another test) would make the assertion vacuous.
    """

    from swebench_eval.harnesses.claude_code.harness import ClaudeCodeHarness

    for key in ("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_AUTO_COMPACT_WINDOW"):
        monkeypatch.delenv(key, raising=False)
    captured, _ = _run_harness(ClaudeCodeHarness, None, tmp_path)
    env = captured["env"]
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in env
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in env


def test_claude_thinking_stays_settable(tmp_path: Path) -> None:
    """F2 (Stage 2.6): MAX_THINKING_TOKENS uses setdefault — env-set values win."""
    # inject an env value (as a task definition could); setdefault must keep it.
    import os

    from swebench_eval.harnesses.claude_code.harness import ClaudeCodeHarness

    os.environ["MAX_THINKING_TOKENS"] = "2048"
    try:
        captured, _ = _run_harness(ClaudeCodeHarness, None, tmp_path)
    finally:
        del os.environ["MAX_THINKING_TOKENS"]
    env = captured["env"]
    assert env["MAX_THINKING_TOKENS"] == "2048", "task-def value must not be overwritten"


def test_codex_config_carries_window_and_threshold(tmp_path: Path) -> None:
    """W=262_144 -> model_context_window=262144 + auto_compact=235929."""
    from swebench_eval.harnesses.codex.harness import CodexHarness

    captured, _ = _run_harness(CodexHarness, 262_144, tmp_path)
    # codex writes config.toml under $CODEX_HOME (the output dir).
    codex_home = Path(captured["env"]["CODEX_HOME"])
    text = (codex_home / "config.toml").read_text()
    assert "model_context_window = 262144" in text
    assert "model_auto_compact_token_limit = 235929" in text


def test_codex_config_omits_compaction_without_window(tmp_path: Path) -> None:
    from swebench_eval.harnesses.codex.harness import CodexHarness

    captured, _ = _run_harness(CodexHarness, None, tmp_path)
    codex_home = Path(captured["env"]["CODEX_HOME"])
    text = (codex_home / "config.toml").read_text()
    # assert on the RENDERED assignment (the comment block legitimately contains
    # the phrase "model_context_window"); the key must not be written.
    assert "model_context_window =" not in text
    assert "model_auto_compact_token_limit =" not in text


def test_opencode_config_carries_limit_and_compaction(tmp_path: Path) -> None:
    """W=262_144 -> model limit.context=262144, compaction.reserved = W - 235929."""
    import json

    from swebench_eval.harnesses.opencode.harness import _write_opencode_json

    cfg_root = tmp_path / "cfgroot"
    _write_opencode_json(cfg_root, "http://gw", "k", "litellm/m", 262_144)
    cfg = json.loads((cfg_root / "opencode" / "opencode.json").read_text())
    m = cfg["provider"]["litellm"]["models"]["litellm/m"]
    assert m["limit"]["context"] == 262_144
    assert m["limit"]["input"] == 262_144
    assert m["limit"]["output"] == 16_384
    # reserved = W - threshold = 262_144 - 235_929 = 26_215
    assert cfg["compaction"]["auto"] is True
    assert cfg["compaction"]["reserved"] == 26_215


def test_opencode_config_omits_compaction_without_window(tmp_path: Path) -> None:
    import json

    from swebench_eval.harnesses.opencode.harness import _write_opencode_json

    cfg_root = tmp_path / "cfgroot"
    _write_opencode_json(cfg_root, "http://gw", "k", "litellm/m", None)
    cfg = json.loads((cfg_root / "opencode" / "opencode.json").read_text())
    # provider model entry has no limit; no compaction block at all.
    m = cfg["provider"]["litellm"]["models"]["litellm/m"]
    assert "limit" not in m
    assert "compaction" not in cfg


def test_codex_default_model_is_not_a_claude_alias(tmp_path: Path) -> None:
    """STEP 2.1 (review 2026-08-26): codex's DEFAULT model used to be
    "claude-code-model" (a copy-paste from the claude adapter).  Codex speaks the
    OpenAI RESPONSES API; that alias is now an Anthropic-ADAPTER alias (STEP 2),
    so if the default were ever used codex would hit the wrong protocol.  The
    run config normally overrides the model, but the default must never be a
    *-claude alias.

    Mutation: revert codex/harness.py's default model to "claude-code-model";
    this test fails."""
    from swebench_eval.harnesses.codex.harness import CodexHarness

    h = CodexHarness()
    assert not h._model.endswith("-claude"), h._model
    assert h._model != "claude-code-model", h._model

    # And the config.toml codex resolves is written with the same model, never a
    # claude alias — drive the harness with an explicit OpenAI-shaped alias and
    # assert writer default/passed model stays non-claude.
    import tempfile
    from pathlib import Path as _P

    from swebench_eval.harnesses.codex.harness import _write_config

    with tempfile.TemporaryDirectory() as d:
        _write_config(_P(d), "http://t", "k", "cheap-oss-model", None)
        text = (_P(d) / "config.toml").read_text()
        assert "claude-code-model" not in text
        assert "cheap-oss-model" in text
