"""B6/E11a — mini's NATIVE (pre-normalisation) trajectory is surfaced for upload.

mini_swe_agent is the one adapter whose native artifact is a separate file
(``mini_raw_trajectory.json``, written by the mini CLI). It used to be written,
normalised into trajectory.jsonl, and then die with the task — the raw doc never
reached S3. HarnessOutput gained ``native_trajectory_path`` and the harness must
surface it, so the worker can upload it beside the normalized one.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

from swebench_eval.harnesses.base import HarnessInput, ModelConfig
from swebench_eval.harnesses.mini_swe_agent.harness import MiniSweAgentHarness

_NATIVE = '{"trajectory_format":1,"messages":[{"role":"user","content":"p"}]}'


def _prepared_repo(tmp_path: Path) -> Path:
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


def test_mini_surfaces_native_trajectory_path(tmp_path: Path) -> None:
    """The harness returns native_trajectory_path pointing at the file the (mocked)
    CLI wrote, so the worker uploads it — the raw document no longer dies with the
    task."""
    repo = _prepared_repo(tmp_path)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    calls: list[list[str]] = []

    def _fake_run(
        cmd: list[str],
        *,
        cwd: object = None,
        env: object = None,
        timeout: int = 0,
        on_line: object = None,
    ) -> object:
        calls.append(cmd)
        # R8.1: the adapter now streams via run_streaming (Popen), so the mock
        # is on the adapter module's run_streaming seam, not subprocess.run.
        # Only the mini CLI invocation carries --output; write the native doc
        # there (the tail thread + post-hoc normalization both read it).
        if "--output" in cmd:
            Path(cmd[cmd.index("--output") + 1]).write_text(_NATIVE)
        return type(
            "StreamResult", (), {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}
        )()

    input_ = HarnessInput(
        instance_id="mini-b6",
        repo_url=f"file://{repo}",
        base_commit="HEAD",
        problem_statement="p",
        attempt_number=1,
        repo_checkout_path=str(repo),
        output_dir=str(output_dir),
        model_config=ModelConfig(gateway_base_url="http://t", gateway_api_key="k", model_name="m"),
        timeout_seconds=60,
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=5.0,
    )
    harness = MiniSweAgentHarness(bin_path="mini-swe-agent", api_base_url="http://t", api_key="k")
    with mock.patch(
        "swebench_eval.harnesses.mini_swe_agent.harness.run_streaming", side_effect=_fake_run
    ):
        output = harness.run(input_)

    assert calls, "the mini CLI should have been invoked"
    expected = str(output_dir / "mini_raw_trajectory.json")
    assert output.native_trajectory_path == expected
    assert Path(output.native_trajectory_path).exists()
    assert Path(output.native_trajectory_path).read_text() == _NATIVE
    # the normalized trajectory still points at the same suffix path structure
    assert output.trajectory_path.endswith("trajectory.jsonl")


def test_mini_adds_pruner_agent_class_when_window_set(tmp_path: Path) -> None:
    """Stage 2.2: when the run resolves a context window, the adapter passes
    --agent-class mini_pruning_agent.PruningAgent with the window + threshold and
    PYTHONPATH=/app — the knob that must ship for mini's compaction to exist."""
    repo = _prepared_repo(tmp_path)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    captured: dict[str, Any] = {}

    def _fake_run(
        cmd: list[str],
        *,
        cwd: object = None,
        env: object = None,
        timeout: int = 0,
        on_line: object = None,
    ) -> object:
        captured["cmd"] = cmd
        captured["env"] = env
        if "--output" in cmd:
            Path(cmd[cmd.index("--output") + 1]).write_text(_NATIVE)
        return type(
            "StreamResult", (), {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}
        )()

    input_ = HarnessInput(
        instance_id="mini-cmp",
        repo_url=f"file://{repo}",
        base_commit="HEAD",
        problem_statement="p",
        attempt_number=1,
        repo_checkout_path=str(repo),
        output_dir=str(output_dir),
        model_config=ModelConfig(gateway_base_url="http://t", gateway_api_key="k", model_name="m"),
        timeout_seconds=60,
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=5.0,
        # Compaction build: W = 262_144 -> threshold = W - 32_768.
        context_window_tokens=262_144,
    )
    harness = MiniSweAgentHarness(bin_path="mini-swe-agent", api_base_url="http://t", api_key="k")
    with mock.patch(
        "swebench_eval.harnesses.mini_swe_agent.harness.run_streaming", side_effect=_fake_run
    ):
        harness.run(input_)

    cmd = captured["cmd"]
    assert "--agent-class" in cmd
    assert cmd[cmd.index("--agent-class") + 1] == "mini_pruning_agent.PruningAgent"
    # mini.yaml is required whenever ANY -c is passed (else the default config is
    # silently dropped).
    assert "-c" in cmd
    assert cmd[cmd.index("-c") + 1] == "mini.yaml"
    assert "agent.context_window=262144" in cmd
    assert "agent.compact_at_tokens=235929" in cmd
    assert captured["env"]["PYTHONPATH"] == "/app"


def test_mini_no_pruner_without_window(tmp_path: Path) -> None:
    """Stage 2.2: with context_window_tokens=None, mini runs stock — no agent-class,
    no -c, no PYTHONPATH override (compaction disabled = DefaultAgent behaviour)."""
    repo = _prepared_repo(tmp_path)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    captured: dict[str, Any] = {}

    def _fake_run(
        cmd: list[str],
        *,
        cwd: object = None,
        env: object = None,
        timeout: int = 0,
        on_line: object = None,
    ) -> object:
        captured["cmd"] = cmd
        captured["env"] = env
        if "--output" in cmd:
            Path(cmd[cmd.index("--output") + 1]).write_text(_NATIVE)
        return type(
            "StreamResult", (), {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}
        )()

    input_ = HarnessInput(
        instance_id="mini-noprune",
        repo_url=f"file://{repo}",
        base_commit="HEAD",
        problem_statement="p",
        attempt_number=1,
        repo_checkout_path=str(repo),
        output_dir=str(output_dir),
        model_config=ModelConfig(gateway_base_url="http://t", gateway_api_key="k", model_name="m"),
        timeout_seconds=60,
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=5.0,
        context_window_tokens=None,
    )
    harness = MiniSweAgentHarness(bin_path="mini-swe-agent", api_base_url="http://t", api_key="k")
    with mock.patch(
        "swebench_eval.harnesses.mini_swe_agent.harness.run_streaming", side_effect=_fake_run
    ):
        harness.run(input_)

    cmd = captured["cmd"]
    assert "--agent-class" not in cmd
    assert "-c" not in cmd
    assert "PYTHONPATH" not in (captured["env"] or {})


def test_other_harnesses_default_to_empty_native_path() -> None:
    """The new field must be optional — the five adapters without a separate native
    file (stdout IS their native stream) default to empty, so the worker's upload
    guard skips them."""
    from swebench_eval.harnesses.custom_minimal.harness import CustomMinimalHarness

    assert CustomMinimalHarness.__mro__  # importable
    from swebench_eval.harnesses.base import HarnessOutput

    out = HarnessOutput(patch=None, success=True, trajectory_path="t", raw_log_path="r")
    assert out.native_trajectory_path == ""


def test_mini_task_carries_repo_location_and_trajectory_records_it(tmp_path: Path) -> None:
    """2026-09-08 (task_framing.located): mini's own instance_template never names
    the repo directory — sampled run-1 instances opened with `find /workspace`.
    The --task text must start with the location line, and the normalized
    trajectory's turn 0 must be that SAME text (X3: the record is what the
    model saw), not the bare problem statement."""
    from swebench_eval.harnesses.task_framing import REPO_LOCATION, located

    repo = _prepared_repo(tmp_path)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    captured: dict[str, Any] = {}

    def _fake_run(
        cmd: list[str],
        *,
        cwd: object = None,
        env: object = None,
        timeout: int = 0,
        on_line: object = None,
    ) -> object:
        captured["cmd"] = cmd
        if "--output" in cmd:
            Path(cmd[cmd.index("--output") + 1]).write_text(_NATIVE)
        return type(
            "StreamResult", (), {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}
        )()

    input_ = HarnessInput(
        instance_id="mini-loc",
        repo_url=f"file://{repo}",
        base_commit="HEAD",
        problem_statement="Fix the widget.",
        attempt_number=1,
        repo_checkout_path=str(repo),
        output_dir=str(output_dir),
        model_config=ModelConfig(gateway_base_url="http://t", gateway_api_key="k", model_name="m"),
        timeout_seconds=60,
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=5.0,
    )
    harness = MiniSweAgentHarness(bin_path="mini-swe-agent", api_base_url="http://t", api_key="k")
    with mock.patch(
        "swebench_eval.harnesses.mini_swe_agent.harness.run_streaming", side_effect=_fake_run
    ):
        output = harness.run(input_)

    cmd = captured["cmd"]
    task = cmd[cmd.index("--task") + 1]
    assert task == located("Fix the widget.")
    assert task.startswith(REPO_LOCATION)
    assert "/testbed" in task and task.endswith("Fix the widget.")
    first = json.loads(Path(output.trajectory_path).read_text().splitlines()[0])
    assert first["role"] == "user" and first["turn"] == 0
    assert first["content"] == task
