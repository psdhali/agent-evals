"""Unit tests for cost tracking — P2-1 accumulation trap.

The harness must *accumulate* per-turn costs, not assign them.
``response.usage.cost`` is per-call; assigning would silently disable
the per-instance budget cap.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from swebench_eval.harnesses.base import HarnessInput, ModelConfig, Usage
from swebench_eval.harnesses.custom_minimal.harness import CustomMinimalHarness


def test_usage_accumulates_cost() -> None:
    """The running total must equal the sum of per-turn costs (not the last call)."""
    usage = Usage()
    assert usage.cost_usd == 0.0
    assert usage.source == "harness"

    # Simulate turn 1: gateway reports $0.001
    usage.cost_usd += 0.001
    assert usage.cost_usd == 0.001

    # Simulate turn 2: gateway reports $0.002
    usage.cost_usd += 0.002
    assert usage.cost_usd == 0.003  # Accumulated, not assigned

    # Simulate turn 3: gateway reports $0.0005
    usage.cost_usd += 0.0005
    assert usage.cost_usd == 0.0035  # Still accumulating


def test_usage_source_tracks_gateway() -> None:
    """The source field distinguishes gateway-reported from harness-computed cost."""
    usage = Usage()
    usage.source = "gateway"
    assert usage.source == "gateway"


def test_usage_token_subcategories() -> None:
    """Cached, cache-write, and reasoning tokens are tracked independently."""
    usage = Usage()
    usage.cached_tokens = 5000
    usage.cache_write_tokens = 1000
    usage.reasoning_tokens = 200
    assert usage.cached_tokens == 5000
    assert usage.cache_write_tokens == 1000
    assert usage.reasoning_tokens == 200


def test_usage_assignment_bug_would_understate_cost() -> None:
    """Verify that the correct pattern is accumulation, not assignment.

    If the harness used ``cumulative_cost = turn_cost`` (assignment) instead of
    ``cumulative_cost += turn_cost`` (accumulation), the budget cap would only
    see the last call's cost — always tiny — and never trip.
    """
    turns = [0.001, 0.002, 0.0005, 0.003, 0.0015]

    # Correct: accumulate
    correct = 0.0
    for cost in turns:
        correct += cost
    assert correct == 0.008

    # Bug: assign
    bug = 0.0
    for cost in turns:
        bug = cost  # Wrong — would only see the last turn's cost
    assert bug == 0.0015  # Only the last value, not the sum
    assert bug < correct  # The bug understates cost by a factor of ~5x


def test_harness_accumulates_cost_over_multiple_turns() -> None:
    """The harness accumulates per-turn API costs into the running total.

    Regression test for P2-1: response.usage.cost is per-call, so the harness
    must accumulate (cumulative_cost += turn_cost), not assign.  This test drives
    CustomMinimalHarness.run() with a stubbed OpenAI client that returns canned
    responses with known usage.cost values, then asserts the output's cost_usd
    equals the sum of per-turn costs.

    To verify the test guards the regression: mutate ``+=`` to ``=`` in
    harness.py and confirm this test fails.
    """
    # Per-turn costs the fake client will return.
    TURN_COSTS = [0.001, 0.002, 0.0005]

    # Build a sequence of fake responses.
    # Each response has a bash tool call (to keep the loop going) except the
    # last, which has no tool calls (model says "DONE").
    def _make_response(cost: float, has_tool_calls: bool) -> mock.MagicMock:
        resp = mock.MagicMock()
        choice = mock.MagicMock()
        msg = mock.MagicMock()
        msg.content = "DONE" if not has_tool_calls else "ok"
        if has_tool_calls:
            tc = mock.MagicMock()
            tc.id = f"call_{cost}"
            tc.function.name = "bash"
            tc.function.arguments = json.dumps({"command": "echo hello"})
            msg.tool_calls = [tc]
        else:
            msg.tool_calls = None
        choice.message = msg
        resp.choices = [choice]
        resp.usage = mock.MagicMock()
        resp.usage.prompt_tokens = 100
        resp.usage.completion_tokens = 50
        resp.usage.cost = cost
        resp.usage.prompt_tokens_details = mock.MagicMock()
        resp.usage.prompt_tokens_details.cached_tokens = 0
        resp.usage.prompt_tokens_details.cache_write_tokens = 0
        resp.usage.completion_tokens_details = mock.MagicMock()
        resp.usage.completion_tokens_details.reasoning_tokens = 0
        return resp

    responses = []
    for i, cost in enumerate(TURN_COSTS):
        has_tools = i < len(TURN_COSTS) - 1  # last turn has no tool calls
        responses.append(_make_response(cost, has_tools))

    fake_client = mock.MagicMock()
    fake_client.chat.completions.create = mock.MagicMock(side_effect=responses)

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"
        repo_dir.mkdir()
        # 5b: the adapter asserts a PREPARED repo (install_repo_script's job);
        # the test's repo doubles as the prepared checkout, so git-init it.
        import subprocess

        subprocess.run(  # noqa: PLW1510
            ["git", "init"],
            cwd=repo_dir,
            capture_output=True,
            timeout=10,
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
            instance_id="test-instance",
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
            output = harness.run(harness_input)

    # The running total must equal the sum of per-turn costs.
    expected_total = sum(TURN_COSTS)
    assert output.usage.cost_usd == expected_total, (
        f"Expected cost_usd={expected_total} (sum of per-turn costs), "
        f"got {output.usage.cost_usd}.  If += was mutated to =, this "
        f"would be {TURN_COSTS[-1]} (only the last turn's cost)."
    )
    assert output.usage.source == "gateway", (
        f"Expected source='gateway' (API-reported cost used), " f"got '{output.usage.source}'"
    )
