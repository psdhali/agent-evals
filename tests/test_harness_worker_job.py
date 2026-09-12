"""ADR-0032 single-job worker — the H3 task runs ONE job from a reference.

The task gets a fixed-width reference (not the job), reads the instance from
the mirror, and reuses the poll loop's job-processing core (heartbeat on the
receipt handle, delete only on success).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from swebench_eval.dataset.base import Instance
from swebench_eval.orchestrator.run_config import DEFAULT_MAX_TOKENS_PER_INSTANCE
from swebench_eval.queue.schemas import HarnessJob, JobReference
from swebench_eval.workers import harness_worker as hw

_REF = JobReference(
    run_id="run-1",
    instance_id="astropy__astropy-12907",
    attempt_number=1,
    receipt_handle="r-abc",
    harness_name="custom_minimal",
    model_alias="cheap-oss-model",
    timeout_seconds=600,
    max_tokens_per_instance=DEFAULT_MAX_TOKENS_PER_INSTANCE,
    max_cost_usd_per_instance=5.0,
)

_INSTANCE = Instance(
    instance_id="astropy__astropy-12907",
    repo="astropy/astropy",
    base_commit="d16bfe05a744909de4b27f5875fe0d4ed41ce607",
    problem_statement="Model separability is wrong for 4D compound models.",
    version="4.3",
)


def hw_sentinel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Stage the per-instance sentinel the H3 job entrypoint now requires.

    The image's baked testbed must claim the SAME instance the reference names,
    or ``run_harness_worker_job`` fails closed before doing any work.
    """
    from swebench_eval.harnesses import repo_prep

    p = tmp_path / "testbed-prepared.json"
    p.write_text(
        json.dumps(
            {
                "instance_id": _REF.instance_id,
                "base_commit": _INSTANCE.base_commit,
                "swebench_version": "4.1.0",
                "framework_sha": "0123456789ab",
                "prepared_at": "2026-08-23T00:00:00Z",
            }
        )
    )
    monkeypatch.setattr(repo_prep, "_SENTINEL_PATH", p)
    return p


def test_job_from_reference_uses_mirror_fields() -> None:
    """ADR-0032 point 2: repo/base_commit/problem_statement come from the MIRROR
    row, never from a payload — there is no second source of truth."""
    job = hw._job_from_reference(_REF, _INSTANCE)

    assert job.instance_id == _REF.instance_id
    assert job.run_id == _REF.run_id
    assert job.repo_url == "https://github.com/astropy/astropy"  # from the instance
    assert job.base_commit == _INSTANCE.base_commit  # from the instance
    assert job.problem_statement == _INSTANCE.problem_statement  # from the instance
    assert job.attempt_number == 1
    assert job.harness_name == "custom_minimal"
    assert job.max_tokens_per_instance == DEFAULT_MAX_TOKENS_PER_INSTANCE
    # env_image_key is not carried (the family encodes it).
    assert job.env_image_key == ""


def test_run_harness_worker_job_runs_and_deletes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The single-job entrypoint loads the instance, runs the job core, and the
    job core deletes the message on success."""
    processed: list[tuple[HarnessJob, str]] = []

    monkeypatch.setattr(JobReference, "from_env", lambda: _REF)
    # Per-instance images: the sentinel MUST be present and match the job or the
    # worker fails closed before doing anything.
    hw_sentinel(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "swebench_eval.dataset.swebench_loader.load_single_instance", lambda *a, **k: _INSTANCE
    )
    monkeypatch.setattr(
        hw, "_process_job", lambda job, receipt, timing=None: processed.append((job, receipt))
    )

    hw.run_harness_worker_job()

    assert len(processed) == 1
    job, receipt = processed[0]
    assert job.instance_id == _REF.instance_id
    assert receipt == "r-abc"


def test_run_harness_worker_job_unknown_instance_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A reference for an instance absent from the mirror fails loudly (never
    runs an agent against nothing)."""
    monkeypatch.setattr(JobReference, "from_env", lambda: _REF)
    hw_sentinel(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "swebench_eval.dataset.swebench_loader.load_single_instance", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hw,
        "_process_job",
        lambda job, receipt, timing=None: (_ for _ in ()).throw(
            AssertionError("must not run a job for an unknown instance")
        ),
    )

    with pytest.raises(RuntimeError, match="not found in the dataset mirror"):
        hw.run_harness_worker_job()


def test_process_job_does_not_delete_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash leaves the message (visibility timeout → retry → DLQ), never a
    silent delete that discards the job."""
    monkeypatch.setattr(hw, "_run_harness", mock.Mock(side_effect=RuntimeError("boom")))
    delete = mock.Mock()
    monkeypatch.setattr(hw, "delete_message", delete)

    hw._process_job(hw._job_from_reference(_REF, _INSTANCE), "r-abc")

    delete.assert_not_called()


def test_process_job_deletes_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Success deletes the message (at-least-once: delete = the job is done)."""
    result = mock.Mock(state="PATCH_READY")
    monkeypatch.setattr(hw, "_run_harness", mock.Mock(return_value=result))
    send = mock.Mock()
    monkeypatch.setattr(hw, "send_message", send)
    monkeypatch.setattr(hw, "_dataclass_to_dict", lambda obj: {"state": obj.state})
    delete = mock.Mock()
    monkeypatch.setattr(hw, "delete_message", delete)

    hw._process_job(hw._job_from_reference(_REF, _INSTANCE), "r-abc")

    # 2026-08-28: two sends now — the HARNESS_RUNNING pickup notice (mirrors
    # eval_worker's EVAL_RUNNING), then the terminal result. Without the
    # first, a row sat at DISPATCHED for its whole runtime with nothing to
    # distinguish "still pulling the image" from "agent actively working."
    assert send.call_count == 2
    first_call, second_call = send.call_args_list
    assert first_call.args == ("results", {"state": "HARNESS_RUNNING"})
    assert second_call.args == ("results", {"state": "PATCH_READY"})
    delete.assert_called_once_with("harness-jobs", "r-abc")


def test_apply_resolved_window_overrides_with_job_window() -> None:
    """D-1 (review 2026-08-26): the dispatcher-resolved context window (carried
    on the job) must land on EVERY instance_results row.  Only custom_minimal
    echoes it on its HarnessOutput; the three CLI harnesses never do, so all five
    Stage 6 rows came out NULL.  The override writes the job's authoritative
    value regardless of what the harness echoed.

    Mutation: drop the body of _apply_resolved_window; this test fails (the row
    keeps the harness echo's None / its own value)."""
    from swebench_eval.queue.schemas import ResultMessage

    job = HarnessJob(
        run_id="r",
        instance_id="i",
        repo_url="https://github.com/x/y",
        base_commit="HEAD",
        problem_statement="p",
        attempt_number=1,
        harness_name="claude_code",
        model_alias="cheap-oss-model",
        context_window_tokens=82_768,  # the dispatcher-resolved value
    )

    # Harness output did NOT set the window (the CLI-harness case) -> result None.
    res = ResultMessage(
        run_id="r",
        instance_id="i",
        attempt_number=1,
        phase="harness",
        state="RESOLVED",
        context_window_tokens=None,
    )
    hw._apply_resolved_window(res, job)
    assert res.context_window_tokens == 82_768
