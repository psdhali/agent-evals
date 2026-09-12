"""R4-1 (stage-c-prerequisites-handover.md §3): the two round-3 fixes must be
guarded at the layer that breaks, not just at a layer that was never touched.

The review proved by mutation that neither fix was guarded:

- removing ``patch_extract_s=_timing_float(timing, "patch_extract_s")`` from
  ``harness_worker.py`` -> 293 passed
- removing the ``if diff.timed_out:`` block from an adapter -> 293 passed

The two tests added in round 3 asserted layers that never broke (a
``ResultMessage`` built directly, and the helper).  These two tests close the
class by driving the real pipeline paths: the worker MUST forward a measured
``patch_extract_s`` / ``patch_extract_timeout``, and the ADAPTER MUST act on the
helper's ``timed_out`` flag.  Each test must FAIL under its corresponding
mutation (DoD #9 — prove it by removing one) — verified by mutation below.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from swebench_eval.dataset.base import Instance
from swebench_eval.harnesses.base import HarnessInput, HarnessOutput, ModelConfig, Usage
from swebench_eval.harnesses.custom_minimal import CustomMinimalHarness
from swebench_eval.harnesses.git_utils import DiffResult
from swebench_eval.queue.schemas import HarnessJob
from swebench_eval.workers import harness_worker as hw

_INSTANCE = Instance(
    instance_id="astropy__astropy-12907",
    repo="astropy/astropy",
    base_commit="d16bfe05a744909de4b27f5875fe0d4ed41ce607",
    problem_statement="Model separability is wrong.",
    version="4.3",
)


def _job() -> HarnessJob:
    return HarnessJob(
        run_id="run-1",
        instance_id=_INSTANCE.instance_id,
        repo_url=f"https://github.com/{_INSTANCE.repo}",
        base_commit=_INSTANCE.base_commit,
        problem_statement=_INSTANCE.problem_statement,
        attempt_number=1,
        harness_name="custom_minimal",
        model_alias="cheap-oss-model",
        timeout_seconds=600,
        max_cost_usd_per_instance=5.0,
    )


class _StubHarness:
    """Adapter stub returning the round-3 fixed HarnessOutput fields."""

    def __init__(self) -> None:
        self.calls: list[HarnessInput] = []

    def run(self, input_: HarnessInput) -> HarnessOutput:
        self.calls.append(input_)
        return HarnessOutput(
            patch=None,
            success=False,
            trajectory_path="",
            raw_log_path="",
            patch_extract_s=1.25,  # R3-1 hop 2: the adapter returns a number
            wall_clock_seconds=5.0,
            terminated_reason="patch_extract_timeout",  # R3-2: the classification
            error="git diff timed out (30s) capturing the patch",
        )


def test_worker_forwards_measured_patch_extract_s_and_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R3-1 + R3-2 worker hop: a measured patch_extract_s and the
    patch_extract_timeout classification must BOTH reach the ResultMessage.

    Fails under mutation 1 (removing the forward kwarg in harness_worker.py) —
    patch_extract_s would be NULL and this assert trips.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()

    monkeypatch.setattr(hw, "_prepare_harness_repo", lambda *a, **k: tmp_path / "testbed")
    monkeypatch.setattr(hw, "_detect_and_log_stuck", lambda output: None)
    monkeypatch.setattr("swebench_eval.database.redis_client.write_progress", lambda *a, **k: None)

    result = hw._run_and_collect(_job(), _StubHarness(), Usage(), None, workdir, timing={})

    # The measured number must arrive, not stop one hop short of Postgres.
    assert result.patch_extract_s == 1.25, f"patch_extract_s lost: {result.patch_extract_s}"
    # The timeout classification must not be lost or downgraded.
    assert result.error_category == "HARNESS_PATCH_EXTRACT_TIMEOUT"
    assert result.state == "FAILED_HARNESS"


def _make_tool_call_response() -> mock.MagicMock:
    """A response with a tool call (so the custom_minimal loop acts, not DONE)."""
    resp = mock.MagicMock()
    choice = mock.MagicMock()
    choice.message.content = "let me do that"
    choice.message.refusal = None
    func = mock.MagicMock()
    func.name = "read_file"
    func.arguments = '{"path":"x"}'
    tool = mock.MagicMock()
    tool.id = "call_x"
    tool.type = "function"
    tool.function = func
    choice.message.tool_calls = [tool]
    choice.finish_reason = "tool_calls"
    resp.choices = [choice]
    resp.usage.prompt_tokens = 5
    resp.usage.completion_tokens = 3
    resp.usage.cost = 0  # no API-reported cost (the `> 0` guard must see an int)
    resp.usage.prompt_tokens_details = mock.MagicMock()
    resp.usage.prompt_tokens_details.cached_tokens = 0
    resp.usage.prompt_tokens_details.cache_write_tokens = 0
    resp.usage.completion_tokens_details = mock.MagicMock()
    resp.usage.completion_tokens_details.reasoning_tokens = 0
    return resp


def test_adapter_acts_on_timed_out_flag() -> None:
    """R3-2 adapter hop: when git_diff_or_classify reports timed_out, the REAL
    adapter must return patch_extract_timeout + patch_extract_s — not a silent
    'no patch' and not an unclassified crash.

    Fails under mutation 2 (removing the ``if diff.timed_out:`` block from the
    adapter) — the classification would be lost and this assert trips.
    """
    fake_client = mock.MagicMock()
    fake_client.chat.completions.create = mock.MagicMock(
        side_effect=[_make_tool_call_response(), _make_tool_call_response()]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"
        repo_dir.mkdir()
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

        harness_input = HarnessInput(
            instance_id=_INSTANCE.instance_id,
            repo_url=f"file://{repo_dir}",
            base_commit="HEAD",
            problem_statement=_INSTANCE.problem_statement,
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
            api_base_url="http://test/v1", api_key="test-key", model="test-model"
        )

        # The git diff step times out -> the adapter must classify, not swallow.
        timed_out_diff = DiffResult(patch=None, patch_extract_s=1.25, timed_out=True)
        with (
            mock.patch("openai.OpenAI", return_value=fake_client),
            mock.patch(
                "swebench_eval.harnesses.custom_minimal.harness.git_diff_or_classify",
                return_value=timed_out_diff,
            ),
        ):
            output = harness.run(harness_input)

    assert output.terminated_reason == "patch_extract_timeout", output.terminated_reason
    assert output.patch_extract_s == 1.25
    assert output.error == "git diff timed out (30s) capturing the patch"
