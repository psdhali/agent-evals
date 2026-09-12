"""K-3 regression: exhausting ``max_turns`` is a truncated run, not ``completed``.

The reviewer's finding (phase-5a-ii-dod1-root-cause-handover): ``CustomMinimalHarness``
initialised ``terminated_reason = "completed"`` and the turn loop fell out with that
value when ``max_turns`` was exhausted — every other exit assigns an explicit reason.
A run cut off at the cap was therefore recorded (and, with a patch, auto-graded) as if
the model had finished on its own. The same defect had already fired five times under
Phase 4 on AWS/local runs.

This test asserts:
- the cap fires an explicit ``max_turns_exceeded`` reason + its own error category,
- a natural finish *on the last permitted turn* still records ``completed`` (the
  ``for...else`` must not misfire), and
- the state machine maps the new reason to ``FAILED_HARNESS`` (never ``PATCH_READY``,
  so the truncated run's partial patch is preserved but not auto-graded — ADR-0016).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from swebench_eval.database.state_machine import (
    map_terminated_reason_to_error_category,
    map_terminated_reason_to_state,
)
from swebench_eval.harnesses.base import HarnessInput, HarnessOutput, ModelConfig
from swebench_eval.harnesses.custom_minimal.harness import CustomMinimalHarness


def _make_response(has_tool_calls: bool, content: str = "ok") -> mock.MagicMock:
    """A canned model response, mirroring test_cost_tracking's fake client shape.

    ``cost=0.0`` rather than a MagicMock so the two-tier cost path takes the local
    pricing branch instead of treating a mock as gateway-reported cost.
    """
    resp = mock.MagicMock()
    choice = mock.MagicMock()
    msg = mock.MagicMock()
    msg.content = content
    if has_tool_calls:
        tc = mock.MagicMock()
        tc.id = "call_k3"
        tc.function.name = "bash"
        tc.function.arguments = json.dumps({"command": "echo hello"})
        msg.tool_calls = [tc]
    else:
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


def _run_harness(responses: list[mock.MagicMock], max_turns: int) -> HarnessOutput:
    """Drive ``CustomMinimalHarness.run`` with a stubbed OpenAI client."""
    fake_client = mock.MagicMock()
    fake_client.chat.completions.create = mock.MagicMock(side_effect=responses)

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"
        repo_dir.mkdir()
        # 5b: the adapter asserts a PREPARED repo; the test's git-init repo is
        # the prepared checkout (install_repo_script's job, not the adapter's).
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
            instance_id="k3-instance",
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
            max_turns=max_turns,
        )

        with mock.patch("openai.OpenAI", return_value=fake_client):
            return harness.run(harness_input)


def test_turn_cap_exhaustion_is_recorded_not_completed() -> None:
    """Every turn returns a tool call, never a DONE — the cap must fire explicitly."""
    # max_turns=2; BOTH responses keep working (tool calls), so the loop exhausts.
    output = _run_harness(
        [
            _make_response(has_tool_calls=True),
            _make_response(has_tool_calls=True),
        ],
        max_turns=2,
    )

    assert output.terminated_reason == "max_turns_exceeded", (
        f"Expected max_turns_exceeded, got {output.terminated_reason!r} — "
        "a run cut off at the cap must not fall out as 'completed'."
    )
    assert output.error_category == "HARNESS_MAX_TURNS_EXCEEDED"
    assert output.exit_code == 2
    assert "turns" in output.error
    assert output.success is False  # nothing was edited → no patch


def test_natural_done_on_final_permitted_turn_is_still_completed() -> None:
    """The ``for...else`` must not fire when the model finishes on the last turn."""
    # Turn 1 keeps working; turn 2 (the final permitted turn) emits DONE with no
    # tool calls — the ``if not msg.tool_calls: break`` must take the completed path.
    output = _run_harness(
        [
            _make_response(has_tool_calls=True),
            _make_response(has_tool_calls=False, content="DONE"),
        ],
        max_turns=2,
    )

    assert output.terminated_reason == "completed"
    assert output.error == ""


def test_state_machine_maps_max_turns_to_failed_harness() -> None:
    """Even WITH a patch, the cap maps to FAILED_HARNESS, not PATCH_READY (ADR-0016)."""
    # Contrast with ``completed`` + patch, which maps to PATCH_READY — the state
    # results_writer checks before auto-enqueuing an eval job.
    assert map_terminated_reason_to_state("completed", "a real diff") == "PATCH_READY"
    assert map_terminated_reason_to_state("max_turns_exceeded", "a real diff") == "FAILED_HARNESS"

    # And ``completed`` with no patch = EMPTY_PATCH; the cap is categorically
    # different from an empty (completed) run.
    assert map_terminated_reason_to_error_category("completed", None) == "EMPTY_PATCH"
    category = map_terminated_reason_to_error_category("max_turns_exceeded", "a real diff")
    assert category == "HARNESS_MAX_TURNS_EXCEEDED"
