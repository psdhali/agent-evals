"""codex per-run home files (2026-09-06, run 01788653487361028986-1de1c022).

Two codex 0.147.0 behaviours seen live against a non-OpenAI model:

* it registers NO apply_patch tool for an unknown model name, yet its built-in
  base prompt tells the model to use one -> "unsupported call: apply_patch" in
  10 of 41 sessions.  The adapter now ships its own copy of that prompt with
  the apply_patch lines replaced and points ``model_instructions_file`` at it.
* its dangerous-command heuristic refuses ``rm -f``/``rm -rf`` outright under
  ``--dangerously-bypass-approvals-and-sandbox``; a matching prefix rule skips
  the heuristic, so the adapter writes ``rules/default.rules`` allowing ``rm``.
"""

from __future__ import annotations

from pathlib import Path

from swebench_eval.harnesses.codex import harness as codex


def test_packaged_base_instructions_never_ask_for_apply_patch() -> None:
    from importlib import resources

    text = (
        resources.files("swebench_eval.harnesses.codex")
        .joinpath("base_instructions.md")
        .read_text(encoding="utf-8")
    )
    # the shipped prompt is codex's own (same opening line) ...
    assert text.startswith("You are a coding agent running in the Codex CLI")
    # ... minus every instruction to USE apply_patch; the only mentions left
    # are the explicit "there is no such tool" sentence.
    for line in text.splitlines():
        if "apply_patch" in line:
            assert "There is NO `apply_patch` tool" in line, line
    assert "Use the `apply_patch` tool" not in text
    assert "after calling `apply_patch`" not in text
    assert "modified files using `apply_patch`" not in text


def test_write_codex_home_files_writes_instructions_and_rules(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    instructions = codex._write_codex_home_files(home)

    assert instructions == home / "base_instructions.md"
    assert "There is NO `apply_patch` tool" in instructions.read_text()

    rules = home / "rules" / "default.rules"
    assert rules.exists()
    body = rules.read_text()
    assert 'pattern = ["rm"]' in body
    assert 'decision = "allow"' in body


def test_write_config_points_codex_at_the_instructions_file(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    codex._write_config(home, "http://127.0.0.1:4000/v1", "k", "minimax-m2.5-codex", 204_800)

    toml = (home / "config.toml").read_text()
    expected = str(home / "base_instructions.md")
    assert f'model_instructions_file = "{expected}"' in toml
    # the pointed-at file and the rules file both exist once config.toml is written
    assert (home / "base_instructions.md").exists()
    assert (home / "rules" / "default.rules").exists()
    # unchanged essentials
    assert 'model_provider = "litellm"' in toml
    assert 'wire_api = "responses"' in toml


def test_write_config_disables_the_view_image_tool(tmp_path: Path) -> None:
    """2026-09-07 (run c10df654, matplotlib-24177/23412): codex attached a PNG the
    agent opened; the text-only model's provider answered 404 "No endpoints found
    that support image input" and codex exited 1.  The tool is off, and the TOML
    still parses with the provider table intact (the [tools] table must not
    swallow the provider keys)."""
    import tomllib

    home = tmp_path / "home"
    home.mkdir()
    codex._write_config(home, "http://127.0.0.1:4000/v1", "k", "minimax-m2.5-codex", 204_800)
    parsed = tomllib.loads((home / "config.toml").read_text())
    assert parsed["tools"]["view_image"] is False
    assert parsed["model_providers"]["litellm"]["wire_api"] == "responses"
    assert parsed["model_providers"]["litellm"]["base_url"] == "http://127.0.0.1:4000/v1"
    assert parsed["model"] == "minimax-m2.5-codex"
