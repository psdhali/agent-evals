"""A2 — grade integrity: strip gold test paths + INVALID grade on failed apply.

Round-review 2026-08-19, E1/E1b: ``django__django-10924`` claude_code was
graded RESOLVED against the agent's own test file.  The mechanism:

* SWE-bench's reset is ``git checkout <base> <test_files>`` where
  ``test_files`` comes from ``get_modified_files`` — which skips CREATED
  files (``source_file == "/dev/null"``), so for a test_patch that creates a
  file the reset degenerates to a bare ``git checkout <base>`` that leaves
  untracked (agent-authored) files in place.
* The gold test_patch then fails to apply ("already exists in working
  directory"), and because the eval script runs ``set -uxo pipefail`` WITHOUT
  ``-e`` the run continues and grades the agent's own file.

Two controls close it: strip gold-test-path hunks from the patch before
grading (so the file never lands), and grade INVALID (not a verdict) if a
gold test apply still fails.
"""

from __future__ import annotations

from unittest import mock

from swebench_eval.database import state_machine as sm
from swebench_eval.evaluation.grading_adapter import GradingOutput
from swebench_eval.evaluation.swebench_runner import (
    _detect_gold_test_apply_failure,
    _strip_patch_file_blocks,
)

# The exact failure the reviewer reconstructed from test_output.txt (E1):
GOLD_PATCH_PATHS = {"tests/model_fields/test_filepathfield.py"}


def _fix_patch() -> str:
    return (
        "diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py\n"
        "index 0000000..1111111 100644\n"
        "--- a/django/db/models/fields/__init__.py\n"
        "+++ b/django/db/models/fields/__init__.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def __init__(self, path):\n"
        "+    self.path = os.path.abspath(path)\n"
        "\n"
    )


def _test_creation_patch() -> str:
    return (
        "diff --git a/tests/model_fields/test_filepathfield.py b/tests/model_fields/test_filepathfield.py\n"
        "new file mode 100644\n"
        "index 0000000..2222222\n"
        "--- /dev/null\n"
        "+++ b/tests/model_fields/test_filepathfield.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+from django.test import SimpleTestCase\n"
        "+class FilePathFieldTests(SimpleTestCase):\n"
        "+    def test_path(self):\n"
    )


def test_strip_keeps_fix_drops_gold_test_creation() -> None:
    """A model-authored creation of a gold test file is dropped; the real fix
    is byte-for-byte preserved (so a genuine resolution is not altered)."""
    patch = _fix_patch() + _test_creation_patch()
    kept, stripped = _strip_patch_file_blocks(patch, GOLD_PATCH_PATHS)

    assert stripped == GOLD_PATCH_PATHS
    assert "test_filepathfield.py" not in kept
    assert "def __init__(self, path):" in kept
    assert "self.path = os.path.abspath(path)" in kept
    assert kept.strip() == _fix_patch().strip()


def test_strip_only_drops_matching_paths() -> None:
    """.gold fix twin untouched when the blocked set is a DIFFERENT directory."""
    patch = _fix_patch() + _test_creation_patch()
    kept, stripped = _strip_patch_file_blocks(patch, {"other/tests/x_test.py"})
    assert stripped == set()
    assert kept.strip() == patch.strip()


def test_strip_rename_into_gold_path() -> None:
    """Renaming a file TO a gold test path is test tampering and is stripped."""
    patch = (
        "diff --git a/old_test.py b/tests/model_fields/test_filepathfield.py\n"
        "similarity index 90%\n"
        "rename from old_test.py\n"
        "rename to tests/model_fields/test_filepathfield.py\n"
    )
    kept, stripped = _strip_patch_file_blocks(patch, GOLD_PATCH_PATHS)
    assert stripped == GOLD_PATCH_PATHS
    assert kept.strip() == ""


def test_strip_empty_patch_is_noop() -> None:
    assert _strip_patch_file_blocks("", GOLD_PATCH_PATHS) == ("", set())
    assert _strip_patch_file_blocks(" ", GOLD_PATCH_PATHS) == (" ", set())


def test_strip_no_blocked_paths_returns_unchanged() -> None:
    patch = _fix_patch()
    assert _strip_patch_file_blocks(patch, set()) == (patch, set())


def test_detect_apply_failure_finds_gold_test_error(tmp_path) -> None:
    """The reviewer's exact failure — a git apply error naming the gold test
    path — is detected as an INVALID grade signal."""
    log_dir = tmp_path / "run"
    log_dir.mkdir()
    (log_dir / "test_output.txt").write_text(
        "+ git checkout bceadd2788dc2dad53eba0caae172bd8522fd483      \n"
        "+ git apply -v -                                             \n"
        "Checking patch tests/model_fields/test_filepathfield.py...\n"
        "error: tests/model_fields/test_filepathfield.py: already exists in working directory\n"
        "+ : '>>>>> Start Test Output'\n"
        "test_callable_path ... ok\n"
    )
    reason = _detect_gold_test_apply_failure(str(log_dir), GOLD_PATCH_PATHS)
    assert reason and "already exists in working directory" in reason


def test_detect_apply_failure_ignores_unrelated_error_lines(tmp_path) -> None:
    """A test that itself prints 'error: <some path>' must not trip the check —
    only a git apply error naming a GOLD TEST path counts."""
    log_dir = tmp_path / "run"
    log_dir.mkdir()
    (log_dir / "test_output.txt").write_text(
        "+ git apply -v -                                             \n"
        "Checking patch src/main.py...\n"
        "error: src/main.py: patch does not apply\n"  # a MODEL-patch failure, not gold test
        "test_callable_path ... ok\n"
    )
    assert _detect_gold_test_apply_failure(str(log_dir), GOLD_PATCH_PATHS) == ""


def test_detect_apply_failure_no_gold_paths_is_noop(tmp_path) -> None:
    log_dir = tmp_path / "run"
    log_dir.mkdir()
    (log_dir / "test_output.txt").write_text("error: tests/x.py: already exists\n")
    assert _detect_gold_test_apply_failure(str(log_dir), set()) == ""
    assert _detect_gold_test_apply_failure(str(log_dir / "missing"), GOLD_PATCH_PATHS) == ""


def test_invalid_grade_maps_to_terminal_non_retryable_category() -> None:
    """EVAL_GRADE_INVALID is terminal (a real outcome) and NOT retryable."""
    assert sm.map_eval_outcome_to_error_category(False, grade_invalid=True) == "EVAL_GRADE_INVALID"
    assert sm.is_terminal("EVAL_GRADE_INVALID")
    assert not sm.is_retryable("EVAL_GRADE_INVALID")
    # unchanged mappings still hold
    assert sm.map_eval_outcome_to_error_category(True) == "RESOLVED"
    assert sm.map_eval_outcome_to_error_category(False) == "UNRESOLVED"
    assert (
        sm.map_eval_outcome_to_error_category(False, patch_apply_ok=False) == "PATCH_APPLY_FAILED"
    )


def test_eval_worker_records_invalid_grade(tmp_path) -> None:
    """_run_eval surfaces an INVALID grade distinctly: FAILED_EVAL state,
    EVAL_GRADE_INVALID category, verdict 'invalid', reason in error_detail."""
    from swebench_eval.queue.schemas import EvalJob
    from swebench_eval.workers.eval_worker import _run_eval

    job = EvalJob(
        run_id="r1",
        instance_id="django__django-10924",
        attempt_number=1,
        patch_s3_key="runs/r1/harness/django__django-10924/1/patch.diff",
        fail_to_pass="",
        pass_to_pass="",
    )

    fake_patch = "diff --git a/x b/x\nindex 1..2\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-x\n+y\n"
    load_single_instance = mock.patch("swebench_eval.dataset.swebench_loader.load_single_instance")
    get_artifact = mock.patch(
        "swebench_eval.workers.eval_worker.get_artifact", return_value=fake_patch.encode()
    )
    grade = mock.patch("swebench_eval.workers.eval_worker.SwebenchRunner.grade")
    upload = mock.patch("swebench_eval.workers.eval_worker.upload_artifact", return_value="k")

    # load_single_instance is imported INSIDE _run_eval — patch it at its
    # defining module, not on eval_worker.
    grade_output = GradingOutput(
        instance_id=job.instance_id,
        resolved=False,
        report_json="{}",
        wall_clock_seconds=3.0,
        error="gold test patch failed to apply: error: tests/model_fields/test_filepathfield.py: already exists in working directory",
        invalid=True,
    )
    with get_artifact, load_single_instance as load, grade as grade_mock, upload:
        load.return_value = _FakeInstance()
        grade_mock.return_value = grade_output
        result = _run_eval(job)

    assert result.state == "FAILED_EVAL"
    assert result.error_category == "EVAL_GRADE_INVALID"
    assert result.verdict == "invalid"
    assert (
        result.touches_test_files is False
        or result.touches_test_files == grade_output.touches_test_files
    )
    assert "test_filepathfield.py" in result.error_detail


class _FakeInstance:
    """Minimal stand-in for a loaded Instance used only to fill eval-job fields."""

    repo = "django/django"
    base_commit = "bceadd2788"
    test_patch = (
        "diff --git a/tests/model_fields/test_filepathfield.py b/tests/model_fields/test_filepathfield.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/model_fields/test_filepathfield.py\n"
    )
    fail_to_pass = ""
    pass_to_pass = ""
    environment_setup_commit = ""
    version = ""
