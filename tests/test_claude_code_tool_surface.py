"""B7/E11b — claude_code's tool surface is native-minus-network, not an allowlist.

The reviewer proved on django-10924 that a tool ALLOWLIST is not a network
control (deny WebFetch, grant Bash, network still reached); A1/ADR-0033 then
removed the network at the route layer. This is the SEQUENCED follow-on: drop the
over-tight --allowedTools (which denied Task/Skill/Workflow — we were scoring a
crippled harness), keep ONLY WebFetch/WebSearch denied, and grant a permission
mode so the agent's own Bash/Edit/Write are auto-approved where the previous
allowlist granted them. The surface is declared once and reflected into the run's
config snapshot.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

from swebench_eval.harnesses.base import HarnessInput, ModelConfig
from swebench_eval.harnesses.claude_code.harness import (
    DISALLOWED_TOOLS,
    PERMISSION_MODE,
    ClaudeCodeHarness,
)


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, timeout=10, check=False)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=repo,
        capture_output=True,
        timeout=10,
        check=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=repo, capture_output=True, timeout=10, check=False
    )
    (repo / "src.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, timeout=10, check=False)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, capture_output=True, timeout=10, check=False
    )
    return repo


def _input(repo: Path) -> HarnessInput:
    return HarnessInput(
        instance_id="cc-tools",
        repo_url=f"file://{repo}",
        base_commit="HEAD",
        problem_statement="fix the bug",
        attempt_number=1,
        repo_checkout_path=str(repo),
        model_config=ModelConfig(gateway_base_url="http://t", gateway_api_key="k", model_name="m"),
        timeout_seconds=60,
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=5.0,
    )


def test_command_denies_only_web_tools_with_permission_mode(tmp_path: Path) -> None:
    """The CLI is invoked with --disallowedTools (WebFetch/WebSearch), a
    --permission-mode, and NO --allowedTools — Task/Skill/Workflow are native again."""
    captured: list[list[str]] = []

    def _run(
        cmd: list[str],
        *,
        cwd: object = None,
        env: object = None,
        timeout: int = 0,
        on_line: object = None,
    ) -> object:
        captured.append(cmd)
        return type(
            "StreamResult", (), {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}
        )()

    harness = ClaudeCodeHarness(claude_bin="claude", api_base_url="http://t", api_key="k")
    with (
        mock.patch("swebench_eval.harnesses.claude_code.harness.run_streaming", side_effect=_run),
        mock.patch(
            # the test is about the TOOL SURFACE, not repo-prep — let the harness
            # proceed to the claude invocation regardless of repo hardening
            "swebench_eval.harnesses.claude_code.harness.ensure_prepared_repo"
        ),
    ):
        harness.run(_input(_git_repo(tmp_path)))

    assert captured, "claude should have been invoked"
    claude_calls = [c for c in captured if c and c[0] == "claude"]
    assert claude_calls, f"no claude invocation captured: {captured}"
    cmd = claude_calls[0]
    # deny ONLY the two web tools
    assert "--disallowedTools" in cmd
    assert cmd[cmd.index("--disallowedTools") + 1] == DISALLOWED_TOOLS == "WebFetch WebSearch"
    # permission mode present (the auto-grant successor to the allowlist)
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == PERMISSION_MODE
    # the crippling allowlist is GONE — Task/Skill/Workflow are available again
    assert "--allowedTools" not in cmd
    assert "Read Read.Range Edit Write Bash" not in cmd


def test_tool_surface_is_declared_and_reflectable() -> None:
    """The B7 policy is one source of truth: the harness declares it, the config
    snapshot records it, and it is NOT duplicated in the dispatcher."""
    surface = ClaudeCodeHarness.tool_surface()
    assert surface["tools"] == "native minus network"
    assert surface["disallowed_tools"] == DISALLOWED_TOOLS
    assert surface["permission_mode"] == PERMISSION_MODE
