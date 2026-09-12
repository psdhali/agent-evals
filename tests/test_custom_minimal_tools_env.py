"""E2 (agent-env-denylist-handover.md §3): custom_minimal's in-process bash tool
must pass the FILTERED environment to its shell subprocess.

custom_minimal is the in-process harness — it has no adapter layer on top of
agent_environment() — so its bash tool is the one place the worker's whole
environment used to reach the agent.  A test asserting the adapter uses
agent_environment() cannot cover it (that test iterates the five subprocess
adapters and correctly excludes custom_minimal).  This one drives _run_bash
directly and asserts the env= it passes omits the dangerous names and keeps the
harmless ones.
"""

from __future__ import annotations

from unittest import mock

from swebench_eval.harnesses.custom_minimal import tools


def test_run_bash_passes_filtered_env(tmp_path) -> None:
    """The bash tool's subprocess.run must receive env=agent_environment() —
    so the shell does NOT inherit AWS_CONTAINER_CREDENTIALS_RELATIVE_URI or
    RESULTS_QUEUE_URL, but DOES keep a testbed-shaped var.

    Fails (E2, DoD #9) if the `env=` argument is deleted from _run_bash — the
    recorded call then has no env= / the banned vars would be inherited.
    """
    captured: dict[str, object] = {}

    class _FakeResult:
        returncode = 0
        stdout = "ok"
        stderr = ""

    base_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/root",
        "CONDA_PREFIX": "/opt/miniconda3/envs/testbed",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc",
        "RESULTS_QUEUE_URL": "https://sqs…",
    }

    def _fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeResult()

    with (
        mock.patch.dict("os.environ", base_env, clear=True),
        mock.patch(
            "swebench_eval.harnesses.custom_minimal.tools.subprocess.run", side_effect=_fake_run
        ),
    ):
        tools._run_bash("echo hi", tmp_path)

    env = captured["env"]
    assert isinstance(
        env, dict
    ), "the bash tool passed no env= — it inherits the worker environment"
    assert "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" not in env
    assert "RESULTS_QUEUE_URL" not in env
    assert env["CONDA_PREFIX"] == "/opt/miniconda3/envs/testbed"  # harmless kept
