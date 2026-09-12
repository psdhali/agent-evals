"""EVAL-GRADE-RESOURCE-LIMITS (2026-09-01) §3.3 — OOM-killed grades are distinct.

The defect this closes: SWE-bench's ``run_instance`` never reads the eval
exec's exit code, and the eval script's test command is not its last command,
so a pytest OOM-killed mid-run grades as a clean-looking ``resolved: false``
— an infrastructure limit rendered as a benchmark result.  Detection is
therefore evidence-based, from outside the container:

* primary — the docker daemon's ``oom`` container event (ContainerOOMWatcher);
* belt — START_TEST_OUTPUT present with END_TEST_OUTPUT missing (the exec
  stream itself died; bash survives a killed pytest and still echoes END, so
  this is specifically the whole-exec-killed shape).

Either way the outcome is ``GradingOutput.oom_killed=True`` → category
``EVAL_OOM_KILLED`` (never a verdict, retryable-in-nature, not terminal) →
state ``FAILED_EVAL`` with the evidence in ``error_detail``.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any, ClassVar, cast
from unittest import mock

if TYPE_CHECKING:
    from swebench_eval.dataset.base import Instance

import docker
import pytest

import swebench_eval.database.state_machine as sm
import swebench_eval.evaluation.swebench_runner as sr
from swebench_eval.evaluation.grading_adapter import GradingInput, GradingOutput
from swebench_eval.evaluation.resource_sampling import (
    ContainerOOMWatcher,
    ResourceMeasurement,
    _oom_watch_enabled,
)
from swebench_eval.evaluation.swebench_runner import (
    SwebenchRunner,
    _detect_truncated_test_run,
)
from swebench_eval.queue.schemas import EvalJob


def _wait_until(predicate: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# ContainerOOMWatcher
# ---------------------------------------------------------------------------


class _FakeEventsStream:
    """Finite events, then block (like a live stream) until close()."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = list(events)
        self._closed = threading.Event()
        self.close_called = False

    def __iter__(self) -> _FakeEventsStream:
        return self

    def __next__(self) -> dict[str, Any]:
        if self._events:
            return self._events.pop(0)
        self._closed.wait(5)
        raise StopIteration

    def close(self) -> None:
        self.close_called = True
        self._closed.set()


class _FakeEventsClient:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.filters: dict[str, Any] | None = None

    def events(self, decode: bool = True, filters: dict[str, Any] | None = None) -> Any:
        self.filters = filters
        return self._stream


def test_watcher_counts_oom_events_and_filters_by_container() -> None:
    stream = _FakeEventsStream([{"Action": "oom"}, {"Action": "oom"}])
    client = _FakeEventsClient(stream)
    watcher = ContainerOOMWatcher(client, "sweb.eval.x.abc")
    watcher.start()
    assert _wait_until(lambda: watcher.oom_event_count() == 2)
    assert watcher.stop() == 2
    # The server-side filter names the exact container — never a host-wide
    # subscription that could count another grade's OOM as ours.
    assert client.filters == {
        "type": "container",
        "event": "oom",
        "container": "sweb.eval.x.abc",
    }


def test_watcher_ignores_non_oom_frames() -> None:
    stream = _FakeEventsStream([{"Action": "die"}, {"status": "start"}])
    watcher = ContainerOOMWatcher(_FakeEventsClient(stream), "c")
    watcher.start()
    assert watcher.stop() == 0


def test_watcher_never_raises_and_a_dead_watcher_reports_zero() -> None:
    class _Boom:
        def events(self, **kwargs: Any) -> Any:
            raise RuntimeError("daemon gone")

    watcher = ContainerOOMWatcher(_Boom(), "c")
    watcher.start()  # thread body swallows the exception
    assert watcher.stop() == 0


def test_watcher_stop_closes_the_stream_and_join_is_bounded() -> None:
    stream = _FakeEventsStream([])  # no events: the thread blocks on the stream
    watcher = ContainerOOMWatcher(_FakeEventsClient(stream), "c")
    watcher.start()
    assert _wait_until(lambda: stream is not None and watcher._stream is not None)
    t0 = time.monotonic()
    assert watcher.stop(timeout=2.0) == 0
    assert time.monotonic() - t0 < 2.5  # bounded, and the close unblocked it
    assert stream.close_called


def test_oom_watch_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_OOM_WATCH", raising=False)
    assert _oom_watch_enabled()  # default ON
    monkeypatch.setenv("EVAL_OOM_WATCH", "0")
    assert not _oom_watch_enabled()
    # Deliberately independent of the sampler's switch: integrity is not a
    # sizing probe.
    monkeypatch.setenv("EVAL_RESOURCE_SAMPLING", "0")
    monkeypatch.setenv("EVAL_OOM_WATCH", "1")
    assert _oom_watch_enabled()


# ---------------------------------------------------------------------------
# _detect_truncated_test_run (the belt)
# ---------------------------------------------------------------------------


def _write_test_output(tmp_path: Any, content: str) -> str:
    from swebench.harness.constants import LOG_TEST_OUTPUT

    (tmp_path / LOG_TEST_OUTPUT).write_text(content)
    return str(tmp_path)


def test_truncation_detected_when_end_marker_missing(tmp_path: Any) -> None:
    from swebench.harness.constants import START_TEST_OUTPUT

    log_dir = _write_test_output(tmp_path, f"+ : '{START_TEST_OUTPUT}'\ntest_a PASSED\n")
    reason = _detect_truncated_test_run(log_dir)
    assert "truncated" in reason
    assert "no\nverdict exists" not in reason  # sanity: message is one line-ish


def test_no_truncation_when_both_markers_present(tmp_path: Any) -> None:
    from swebench.harness.constants import END_TEST_OUTPUT, START_TEST_OUTPUT

    log_dir = _write_test_output(
        tmp_path, f"+ : '{START_TEST_OUTPUT}'\ntest_a FAILED\n+ : '{END_TEST_OUTPUT}'\n"
    )
    assert _detect_truncated_test_run(log_dir) == ""


def test_no_truncation_when_output_missing_or_start_absent(tmp_path: Any) -> None:
    # No file at all (a different failure shape, handled elsewhere) — not ours.
    assert _detect_truncated_test_run(str(tmp_path / "missing")) == ""
    # START never reached (died during install): grading already reports
    # patch_successfully_applied=False; the narrow belt stays narrow.
    log_dir = _write_test_output(tmp_path, "+ git status\n")
    assert _detect_truncated_test_run(log_dir) == ""


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------


def test_oom_category_is_infra_shaped_never_terminal() -> None:
    assert sm.map_eval_outcome_to_error_category(False, oom_killed=True) == "EVAL_OOM_KILLED"
    # Even a "resolved-looking" killed grade is voided — infra outranks logs.
    assert sm.map_eval_outcome_to_error_category(True, oom_killed=True) == "EVAL_OOM_KILLED"
    assert (
        sm.map_eval_outcome_to_error_category(False, grade_invalid=True, oom_killed=True)
        == "EVAL_OOM_KILLED"
    )
    assert sm.is_retryable("EVAL_OOM_KILLED")
    assert not sm.is_terminal("EVAL_OOM_KILLED")
    # unchanged mappings still hold
    assert sm.map_eval_outcome_to_error_category(False, grade_invalid=True) == "EVAL_GRADE_INVALID"
    assert sm.map_eval_outcome_to_error_category(True) == "RESOLVED"


# ---------------------------------------------------------------------------
# SwebenchRunner.grade wiring (run_instance + docker mocked at their seams)
# ---------------------------------------------------------------------------


class _FakeSpec:
    """The 5.x TestSpec surface grade() touches: the row's image + instance id."""

    instance_id = "astropy__astropy-1"
    image = "swebench/sweb.eval.x86_64.astropy_1776_astropy-1:latest"


class _FakeImages:
    def get(self, key: str) -> Any:
        raise docker.errors.NotFound("no image")


class _FakeContainers:
    def get(self, name: str) -> Any:
        raise docker.errors.NotFound("no container")


class _FakeApi:
    """The docker-py APIClient seam grade_limits wraps (create_container)."""

    def create_host_config(self, **kwargs: Any) -> dict[str, Any]:
        return dict(kwargs)

    def create_container(self, *args: Any, **kwargs: Any) -> dict[str, str]:
        return {"Id": "fake"}


class _FakeDockerClient:
    images = _FakeImages()
    containers = _FakeContainers()

    def __init__(self) -> None:
        self.api = _FakeApi()

    def events(self, **kwargs: Any) -> Any:
        return _FakeEventsStream([])

    def info(self) -> dict[str, Any]:
        return {}


class _FakeInstance:
    instance_id = "astropy__astropy-1"
    repo = "astropy/astropy"
    base_commit = "c0ffee"
    problem_statement = "p"
    hints = ""
    patch = "gold"
    test_patch = "--- a/tests/t.py\n+++ b/tests/t.py\n"
    fail_to_pass = "[]"
    pass_to_pass = "[]"
    environment_setup_commit = "c0ffee"
    version = "1.0"
    created_at = "2020"
    # SWE-bench 5.x columns (ADR-0043) — grade() refuses a row without them.
    image = "swebench/sweb.eval.x86_64.astropy_1776_astropy-1:latest"
    eval_script = "#!/bin/bash\nset -uxo pipefail\n: '>>>>> Start Test Output'\npytest\n: '>>>>> End Test Output'\n"
    log_parser = "parse_log_pytest"
    eval_type = "pass_and_fail"


class _ScriptedWatcher:
    """Stands in for ContainerOOMWatcher inside grade()."""

    events_to_report = 0
    stopped: ClassVar[list[bool]] = []

    def __init__(self, client: Any, name: str) -> None:
        self.name = name

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 2.0) -> int:
        type(self).stopped.append(True)
        return type(self).events_to_report


def _grade(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    oom_events: int,
    test_output: str | None = None,
    sampling: bool = False,
) -> GradingOutput:
    """Run SwebenchRunner.grade with run_instance/docker faked at their seams."""
    import swebench.harness.constants as const_mod
    import swebench.harness.run_evaluation as re_mod
    import swebench.harness.utils as utils_mod

    monkeypatch.setenv("EVAL_RESOURCE_SAMPLING", "1" if sampling else "0")
    monkeypatch.setattr(utils_mod, "make_test_spec", lambda *a, **k: _FakeSpec())
    monkeypatch.setattr("docker.from_env", lambda: _FakeDockerClient())
    monkeypatch.setattr(sr, "_enable_eval_stdout_logging", lambda: None)
    # grade() reads the log root from swebench.harness.constants (5.x).
    monkeypatch.setattr(const_mod, "RUN_EVALUATION_LOG_DIR", tmp_path)
    _ScriptedWatcher.events_to_report = oom_events
    _ScriptedWatcher.stopped = []
    monkeypatch.setattr(sr, "ContainerOOMWatcher", _ScriptedWatcher)

    def fake_run_instance(
        test_spec: Any,
        pred: dict[str, Any],
        client: Any,
        run_id: str,
        timeout: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """5.x shape: ``(instance_id, report_map)`` on completion."""
        if test_output is not None:
            from swebench.harness.constants import LOG_TEST_OUTPUT

            log_dir = tmp_path / run_id / "eval-framework" / pred["instance_id"]
            log_dir.mkdir(parents=True)
            (log_dir / LOG_TEST_OUTPUT).write_text(test_output)
        iid = pred["instance_id"]
        return iid, {iid: {"resolved": False, "patch_successfully_applied": True}}

    monkeypatch.setattr(re_mod, "run_instance", fake_run_instance)

    runner = SwebenchRunner(namespace="swebench")
    return runner.grade(
        GradingInput(
            instance_id=_FakeInstance.instance_id,
            patch="--- a/src/x.py\n+++ b/src/x.py\n",
            fail_to_pass="",
            pass_to_pass="",
        ),
        instance=cast("Instance", _FakeInstance()),
    )


def test_grade_classifies_oom_event_and_never_yields_a_verdict(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _grade(tmp_path, monkeypatch, oom_events=2)
    assert output.oom_killed is True
    assert output.resolved is False
    assert output.invalid is False
    assert "oom event" in output.error
    assert _ScriptedWatcher.stopped  # the watcher was stopped on the way out


def test_grade_classifies_truncated_test_output_without_an_event(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swebench.harness.constants import START_TEST_OUTPUT

    output = _grade(
        tmp_path,
        monkeypatch,
        oom_events=0,
        test_output=f"+ : '{START_TEST_OUTPUT}'\ntest_a PASSED\n",
    )
    assert output.oom_killed is True
    assert "truncated" in output.error


def test_grade_without_oom_is_unchanged(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from swebench.harness.constants import END_TEST_OUTPUT, START_TEST_OUTPUT

    output = _grade(
        tmp_path,
        monkeypatch,
        oom_events=0,
        test_output=f"+ : '{START_TEST_OUTPUT}'\ntest_a FAILED\n+ : '{END_TEST_OUTPUT}'\n",
    )
    assert output.oom_killed is False
    assert output.resolved is False
    assert output.error == ""


def test_grade_stamps_oom_into_resource_measurement(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sizing artifact must tell 'peaked at X and was killed' apart from a
    survived peak — a killed grade's peak is a floor, not a peak."""
    output = _grade(tmp_path, monkeypatch, oom_events=1, sampling=True)
    assert output.oom_killed is True
    assert output.resource_measurement is not None
    assert output.resource_measurement.oom_killed is True


# ---------------------------------------------------------------------------
# Eval worker mapping (mirrors test_eval_worker_records_invalid_grade)
# ---------------------------------------------------------------------------


def test_eval_worker_records_oom_grade_distinctly(tmp_path: Any) -> None:
    from swebench_eval.workers.eval_worker import _run_eval

    job = EvalJob(
        run_id="r1",
        instance_id="astropy__astropy-1",
        attempt_number=1,
        patch_s3_key="runs/r1/harness/astropy__astropy-1/1/patch.diff",
        fail_to_pass="",
        pass_to_pass="",
    )
    fake_patch = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-x\n+y\n"
    grade_output = GradingOutput(
        instance_id=job.instance_id,
        resolved=False,
        report_json="{}",
        wall_clock_seconds=3.0,
        error="docker reported 1 oom event(s) for grading container sweb.eval.x.abc",
        oom_killed=True,
        resource_measurement=ResourceMeasurement(status="ok", samples_taken=3, oom_killed=True),
    )
    with (
        mock.patch(
            "swebench_eval.workers.eval_worker.get_artifact", return_value=fake_patch.encode()
        ),
        mock.patch("swebench_eval.dataset.swebench_loader.load_single_instance") as load,
        mock.patch("swebench_eval.workers.eval_worker.SwebenchRunner.grade") as grade_mock,
        mock.patch("swebench_eval.workers.eval_worker.upload_artifact", return_value="k"),
    ):
        load.return_value = _FakeInstance()
        grade_mock.return_value = grade_output
        result = _run_eval(job)

    assert result.state == "FAILED_EVAL"
    assert result.error_category == "EVAL_OOM_KILLED"
    assert result.verdict == ""  # never a verdict — not even "invalid"
    assert result.grade_invalid is False
    assert "oom event" in result.error_detail
