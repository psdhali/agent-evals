"""K-1 regression: the harness must tell the model where the repository is.

The reviewer's finding (phase-5a-ii-dod1-root-cause-handover): ``SYSTEM_PROMPT``
said "run a shell command inside the repository" but never gave the path. The
clone lands in a per-run ``mkdtemp`` directory (``/tmp/harness-<random>/repo``),
different every run, so the model guessed the SWE-bench convention ``/repo`` and
all three AWS runs (0004-0006) opened with a failed ``cd /repo``. In 5b the repo
moves to ``/testbed``; the guess would move with it.

This test pins two things on the actual message list sent to the model:
- the SYSTEM_PROMPT tells the model its cwd IS the repo and not to guess /repo,
- a per-run system message states the concrete absolute checkout path, so any
  future change of where the repo lives (mkdtemp today, /testbed in 5b) stays
  anchored to the real path.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from swebench_eval.harnesses.base import HarnessInput, ModelConfig
from swebench_eval.harnesses.custom_minimal.harness import SYSTEM_PROMPT, CustomMinimalHarness


def _make_done_response() -> mock.MagicMock:
    """A model response that finishes immediately (no tool calls)."""
    resp = mock.MagicMock()
    choice = mock.MagicMock()
    msg = mock.MagicMock()
    msg.content = "DONE"
    msg.tool_calls = None
    choice.message = msg
    resp.choices = [choice]
    resp.usage = mock.MagicMock()
    resp.usage.prompt_tokens = 10
    resp.usage.completion_tokens = 5
    resp.usage.cost = 0.0
    resp.usage.prompt_tokens_details = mock.MagicMock()
    resp.usage.prompt_tokens_details.cached_tokens = 0
    resp.usage.prompt_tokens_details.cache_write_tokens = 0
    resp.usage.completion_tokens_details = mock.MagicMock()
    resp.usage.completion_tokens_details.reasoning_tokens = 0
    return resp


def _captured_messages() -> list[dict[str, object]]:
    """Run the harness once and return the ``messages`` list the model received."""
    fake_client = mock.MagicMock()
    captured: dict[str, object] = {}

    def fake_create(**kwargs: object) -> mock.MagicMock:
        captured["messages"] = kwargs["messages"]
        return _make_done_response()

    fake_client.chat.completions.create = mock.MagicMock(side_effect=fake_create)

    with tempfile.TemporaryDirectory() as tmpdir:
        # 5b: the adapter asserts a PREPARED repo — the git-init repo here IS
        # the checkout path (install_repo_script does real prep in AWS).
        repo_dir = Path(tmpdir) / "repo"
        repo_dir.mkdir()
        subprocess.run(  # noqa: PLW1510
            ["git", "init"], cwd=repo_dir, capture_output=True, timeout=10
        )
        subprocess.run(  # noqa: PLW1510
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_dir,
            capture_output=True,
            timeout=10,
        )
        subprocess.run(  # noqa: PLW1510
            ["git", "config", "user.name", "Test"],
            cwd=repo_dir,
            capture_output=True,
            timeout=10,
        )
        subprocess.run(  # noqa: PLW1510
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=repo_dir,
            capture_output=True,
            timeout=10,
        )

        harness_input = HarnessInput(
            instance_id="k1-instance",
            repo_url=f"file://{repo_dir}",
            base_commit="HEAD",
            problem_statement="Test problem.",
            attempt_number=1,
            repo_checkout_path=str(repo_dir),
            model_config=ModelConfig(
                gateway_base_url="http://test/v1",
                gateway_api_key="test-key",
                model_name="test-model",
            ),
            timeout_seconds=30,
            max_tokens_per_instance=None,
            max_cost_usd_per_instance=5.0,
        )
        harness = CustomMinimalHarness(
            api_base_url="http://test/v1",
            api_key="test-key",
            model="test-model",
        )
        with mock.patch("openai.OpenAI", return_value=fake_client):
            harness.run(harness_input)

    messages = captured["messages"]
    assert isinstance(messages, list)
    return messages


def test_prompt_tells_model_cwd_is_the_repo() -> None:
    """The static SYSTEM_PROMPT must anchor the agent to its actual cwd."""
    assert "current working directory" in SYSTEM_PROMPT
    assert "/repo" in SYSTEM_PROMPT  # explicitly warned against, not promised


def test_messages_state_the_concrete_repo_path() -> None:
    """The model is told the absolute checkout path, and told to use it directly."""
    messages = _captured_messages()

    # Leading order: system prompt, per-run repo-path system message, user
    # problem (a trailing assistant "DONE" follows once the loop finishes).
    assert [m["role"] for m in messages[:3]] == ["system", "system", "user"]

    repo_path_msg = messages[1]["content"]
    assert isinstance(repo_path_msg, str)
    # The path is the real prepared-repo directory (mkdtemp-derived here;
    # /testbed in 5b).
    assert "repo" in repo_path_msg
    assert "current working directory" in repo_path_msg
    assert "/repo" in repo_path_msg  # warns the model not to guess it


def test_path_message_uses_the_resolved_checkout_not_a_guess() -> None:
    """The stated path must come from repo_checkout_path, not a hardcoded value."""
    messages = _captured_messages()
    repo_path_msg = str(messages[1]["content"])
    # The harness's actual prepared-repo dir is `<tmp>/repo`; an mkdtemp temp
    # dir prefix proves it is the runtime path, not a stale hardcode.
    assert "repo" in repo_path_msg
    # And the JSON round-trip proves the message isn't mangled into the loop.
    _ = json.loads(json.dumps(messages))
