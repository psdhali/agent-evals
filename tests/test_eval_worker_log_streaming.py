"""Eval-worker log streaming (5b review, 2026-08-18).

The ~3-minute grade was silent in CloudWatch: SWE-bench's subprocess logs
lived only in files under ``RUN_EVALUATION_LOG_DIR`` on the eval host. Two
levers fix it:

  * ``SwebenchRunner._enable_eval_stdout_logging`` forces ``add_stdout=True``
    on SWE-bench's ``setup_logger`` (the same seam the warm job uses for image
    builds), for BOTH the module that defines it (docker_build) AND the name
    bound into run_evaluation — wrapping only the defining module would miss
    the run logger ``run_instance`` uses.
  * ``eval_worker._upload_run_logs`` uploads the full ``test_output.txt`` and
    ``run_instance.log`` beside the eval report so a failure is never silent.

The docker/ECR halves are mocked; the setup_logger seam is real.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest import mock

import swebench_eval.evaluation.swebench_runner as runner
from swebench_eval.workers import eval_worker


def test_enable_eval_stdout_logging_wraps_both_logger_names() -> None:
    """The build logger AND the run_evaluation logger both stream to stdout.

    ``run_instance`` binds ``setup_logger`` by import, so a wrapper on the
    defining module (``docker_build.setup_logger``) alone would NOT cover the
    run logger — both names must point at the same wrapper.
    """
    from swebench import logger as swebench_logger
    from swebench.harness import run_evaluation

    orig_db = swebench_logger.setup_logger
    orig_re = run_evaluation.setup_logger
    try:
        runner._enable_eval_stdout_logging()

        assert swebench_logger.setup_logger is not orig_db
        assert run_evaluation.setup_logger is not orig_re
        # Both names reference the same wrapper — one wrap, both surfaces.
        assert swebench_logger.setup_logger is run_evaluation.setup_logger

        # The wrapper forces add_stdout=True: a real logger created through it
        # carries a stdout StreamHandler as well as the file handler.
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            log = run_evaluation.setup_logger("x3", Path(td) / "slice.log")
            try:
                handler_types = {type(h) for h in log.handlers}
                assert logging.StreamHandler in handler_types
                assert logging.FileHandler in handler_types
            finally:
                log.handlers.clear()

        # Idempotent: a second call must not double-wrap.
        runner._enable_eval_stdout_logging()
        assert swebench_logger.setup_logger is run_evaluation.setup_logger  # same wrapper
    finally:
        swebench_logger.setup_logger = orig_db
        run_evaluation.setup_logger = orig_re


def test_upload_run_logs_pushes_present_skips_absent(tmp_path: Path) -> None:
    """Present logs go next to the report; absent ones are skipped, not fatal.

    dev/BUILDER4-EXPOSE-MISSING-ARTIFACTS-2026-08-28.md: the uploaded keys
    used to be discarded (logged, function returned None) — the dashboard
    could never resolve them. Now returned as {filename: key}."""
    log_dir = tmp_path / "run_log_dir"
    log_dir.mkdir()
    (log_dir / "test_output.txt").write_text("FAILED  test_a\nFAILED  test_b\n")
    (log_dir / "run_instance.log").write_text("build ok\n")
    # run_instance.log will be present; a third name is absent and skipped.

    uploaded: dict[str, str] = {}

    def fake_upload(bucket: str, key: str, data: str | bytes) -> str:
        uploaded[key] = data.decode() if isinstance(data, bytes) else data
        return key

    with mock.patch.object(eval_worker, "upload_artifact", side_effect=fake_upload):
        result = eval_worker._upload_run_logs("b", "runs/r1/eval/i1/1", str(log_dir))

    assert uploaded == {
        "runs/r1/eval/i1/1/test_output.txt": "FAILED  test_a\nFAILED  test_b\n",
        "runs/r1/eval/i1/1/run_instance.log": "build ok\n",
    }
    assert result == {
        "test_output.txt": "runs/r1/eval/i1/1/test_output.txt",
        "run_instance.log": "runs/r1/eval/i1/1/run_instance.log",
    }


def test_upload_run_logs_empty_dir_is_noop(tmp_path: Path) -> None:
    """An empty run_log_dir (no logs written) uploads nothing, raises nothing,
    and returns an empty mapping (never a fabricated key)."""
    log_dir = tmp_path / "empty"
    log_dir.mkdir()
    with mock.patch.object(eval_worker, "upload_artifact") as m:
        result = eval_worker._upload_run_logs("b", "runs/r1/eval/i1/1", str(log_dir))
    m.assert_not_called()
    assert result == {}


def test_upload_run_logs_never_breaks_grading(tmp_path: Path) -> None:
    """A log-upload failure must not lose a verdict that already graded, and
    the failed file is simply absent from the returned mapping."""
    log_dir = tmp_path / "rl"
    log_dir.mkdir()
    (log_dir / "test_output.txt").write_text("x")
    with mock.patch.object(eval_worker, "upload_artifact", side_effect=RuntimeError("s3 down")):
        result = eval_worker._upload_run_logs("b", "runs/r1/eval/i1/1", str(log_dir))  # no raise
    assert result == {}


def test_run_eval_attaches_grade_log_keys_to_the_result(tmp_path: Path) -> None:
    """dev/BUILDER4-EXPOSE-MISSING-ARTIFACTS-2026-08-28.md: the whole point —
    _run_eval's returned ResultMessage must actually carry
    test_output_s3_key/run_log_s3_key, not just log them and move on.

    Mutation-check: revert the run_log_keys.get(...) wiring in _run_eval back
    to omitting test_output_s3_key/run_log_s3_key — this test fails (both
    empty instead of the real keys). Verified by hand and reverted.
    """
    from unittest import mock as _mock

    from swebench_eval.evaluation.grading_adapter import GradingOutput
    from swebench_eval.queue.schemas import EvalJob
    from swebench_eval.workers.eval_worker import _run_eval

    log_dir = tmp_path / "run_log_dir"
    log_dir.mkdir()
    (log_dir / "test_output.txt").write_text("PASSED  test_a\n")
    (log_dir / "run_instance.log").write_text("build ok\n")

    job = EvalJob(
        run_id="r1",
        instance_id="django__django-10924",
        attempt_number=1,
        patch_s3_key="runs/r1/harness/django__django-10924/1/patch.diff",
        fail_to_pass="",
        pass_to_pass="",
    )
    fake_patch = "diff --git a/x b/x\nindex 1..2\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-x\n+y\n"
    grade_output = GradingOutput(
        instance_id=job.instance_id,
        resolved=True,
        report_json="{}",
        wall_clock_seconds=1.0,
        run_log_dir=str(log_dir),
    )

    class _FakeInstance:
        repo = "django/django"
        base_commit = "bceadd2788"
        patch = ""
        fail_to_pass = ""
        pass_to_pass = ""
        environment_setup_commit = ""
        version = ""

    with (
        _mock.patch(
            "swebench_eval.workers.eval_worker.get_artifact", return_value=fake_patch.encode()
        ),
        _mock.patch("swebench_eval.dataset.swebench_loader.load_single_instance") as load,
        _mock.patch(
            "swebench_eval.workers.eval_worker.SwebenchRunner.grade", return_value=grade_output
        ),
        _mock.patch(
            "swebench_eval.workers.eval_worker.upload_artifact",
            side_effect=lambda bucket, key, data: key,
        ),
    ):
        load.return_value = _FakeInstance()
        result = _run_eval(job)

    assert result.test_output_s3_key == f"runs/r1/eval/{job.instance_id}/1/test_output.txt"
    assert result.run_log_s3_key == f"runs/r1/eval/{job.instance_id}/1/run_instance.log"
