"""Image validation (dev/IMAGE-PARITY-ROOT-CAUSE-AND-FIX-2026-09-05 Part B):
gold-patch grades under the synthetic ``image-validation`` run.

DB logic is driven through a scripted cursor (no Postgres): the property under
test is the seed-then-send shape, the attempt numbering, the in-flight skip
and the EvalJob's ``use_gold_patch`` flag — the eval worker's gold branch is
covered separately below.
"""

from __future__ import annotations

from typing import Any, Self
from unittest import mock

import pytest

from swebench_eval.evaluation.grading_adapter import GradingOutput
from swebench_eval.orchestrator.control_plane import image_validation as iv
from swebench_eval.queue.schemas import EvalJob


class _Cursor:
    def __init__(self, in_flight: list[str]) -> None:
        self.in_flight = in_flight
        self.executed: list[tuple[str, Any]] = []
        self._pending: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((" ".join(sql.split()), params))
        if "SELECT DISTINCT instance_id" in sql:
            self._pending = [(i,) for i in self.in_flight]
        else:
            self._pending = []

    def fetchall(self) -> Any:
        return list(self._pending)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Conn:
    def __init__(self, cur: _Cursor) -> None:
        self.cur = cur
        self.committed = False
        self.closed = False

    def cursor(self) -> _Cursor:
        return self.cur

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def _wire(monkeypatch: pytest.MonkeyPatch, cur: _Cursor) -> tuple[_Conn, list[Any]]:
    conn = _Conn(cur)
    monkeypatch.setattr(iv, "_db", lambda: conn)
    sent: list[Any] = []
    monkeypatch.setattr(iv, "send_message", lambda q, body: sent.append((q, body)))
    # the run-activity marker needs Redis; record the call instead
    from swebench_eval.control import state as control_state

    monkeypatch.setattr(control_state, "mark_runs_active", lambda: sent.append(("active", None)))
    return conn, sent


def test_validation_creates_a_named_run_and_sends_gold_eval_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cur = _Cursor(in_flight=[])
    conn, sent = _wire(monkeypatch, cur)

    out = iv.validate_images(["django__django-10097", "matplotlib__matplotlib-23314"], actor="me")

    run_id = out["run_id"]
    assert run_id.startswith("image-validation-") and len(run_id) > len("image-validation-")
    assert out["validated"] == [
        {"instance_id": "django__django-10097", "attempt_number": 1},
        {"instance_id": "matplotlib__matplotlib-23314", "attempt_number": 1},
    ]
    assert out["skipped"] == []
    # a NEW run row (status validation, provenance in config_snapshot), then the rows
    run_insert = next((s, p) for s, p in cur.executed if "INSERT INTO runs" in s)
    assert run_insert[1][0] == run_id and run_insert[1][2] == "validation"
    assert '"actor": "me"' in run_insert[1][1]
    inserts = [p for s, p in cur.executed if "INSERT INTO instance_results" in s]
    assert inserts == [
        (run_id, "django__django-10097", "image_validation"),
        (run_id, "matplotlib__matplotlib-23314", "image_validation"),
    ]
    assert conn.committed and conn.closed
    # the jobs: eval-jobs, gold flag on, no artifact key, the new run id; then the fleet is woken
    assert [q for q, _ in sent] == ["eval-jobs", "eval-jobs", "active"]
    body = sent[0][1]
    assert body["use_gold_patch"] is True
    assert body["run_id"] == run_id
    assert body["patch_s3_key"] == ""
    assert EvalJob(**body).attempt_number == 1


def test_two_clicks_make_two_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _Cursor(in_flight=[])
    _wire(monkeypatch, cur)
    a = iv.validate_images(["x"])["run_id"]
    b = iv.validate_images(["x"])["run_id"]
    assert a != b


def test_in_flight_gold_grade_is_skipped_and_no_run_is_made_for_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cur = _Cursor(in_flight=["matplotlib__matplotlib-23314"])
    _, sent = _wire(monkeypatch, cur)

    out = iv.validate_images(["django__django-10097", "matplotlib__matplotlib-23314"])
    assert out["validated"] == [{"instance_id": "django__django-10097", "attempt_number": 1}]
    assert out["skipped"] == [
        {"instance_id": "matplotlib__matplotlib-23314", "reason": "validation still in flight"}
    ]
    assert [b["instance_id"] for q, b in sent if q == "eval-jobs"] == ["django__django-10097"]

    cur2 = _Cursor(in_flight=["y"])
    _, sent2 = _wire(monkeypatch, cur2)
    out2 = iv.validate_images(["y"])
    assert out2["validated"] == [] and sent2 == []
    assert not any("INSERT INTO runs" in s for s, _ in cur2.executed)


def test_empty_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(iv.ImageValidationError, match="non-empty"):
        iv.validate_images([])


def test_duplicate_ids_collapse(monkeypatch: pytest.MonkeyPatch) -> None:
    cur = _Cursor(in_flight=[])
    _, sent = _wire(monkeypatch, cur)
    out = iv.validate_images(["a", "a", "", "b"])
    assert [v["instance_id"] for v in out["validated"]] == ["a", "b"]
    assert [q for q, _ in sent] == ["eval-jobs", "eval-jobs", "active"]


# ---------------------------------------------------------------------------
# eval worker: the gold branch
# ---------------------------------------------------------------------------


class _GoldInstance:
    instance_id = "django__django-10097"
    repo = "django/django"
    base_commit = "abc"
    fail_to_pass = '["t1"]'
    pass_to_pass = "[]"
    test_patch = "diff --git a/tests/t.py b/tests/t.py\n"
    patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-x\n+y\n"


def _job(**kw: Any) -> EvalJob:
    base: dict[str, Any] = {
        "run_id": "image-validation",
        "instance_id": "django__django-10097",
        "attempt_number": 1,
        "patch_s3_key": "",
        "fail_to_pass": "",
        "pass_to_pass": "",
    }
    base.update(kw)
    return EvalJob(**base)


def test_parse_eval_job_reads_the_gold_flag_and_defaults_off() -> None:
    from swebench_eval.workers.eval_worker import _parse_eval_job

    body = {"run_id": "r", "instance_id": "i", "attempt_number": 1, "patch_s3_key": "k"}
    assert _parse_eval_job(body).use_gold_patch is False
    assert _parse_eval_job({**body, "use_gold_patch": True}).use_gold_patch is True


def test_run_eval_grades_the_gold_patch_without_fetching_an_artifact() -> None:
    from swebench_eval.workers.eval_worker import _run_eval

    grade_output = GradingOutput(
        instance_id="django__django-10097",
        resolved=True,
        report_json="{}",
        wall_clock_seconds=1.0,
    )
    seen: dict[str, Any] = {}

    def _grade(self: Any, grading_input: Any, instance: Any = None) -> GradingOutput:
        seen["patch"] = grading_input.patch
        return grade_output

    with (
        mock.patch("swebench_eval.dataset.swebench_loader.load_single_instance") as load,
        mock.patch("swebench_eval.workers.eval_worker.get_artifact") as fetch,
        mock.patch("swebench_eval.workers.eval_worker.SwebenchRunner.grade", _grade),
        mock.patch("swebench_eval.workers.eval_worker.upload_artifact", return_value="k"),
        mock.patch("swebench_eval.workers.eval_worker._upload_run_logs", return_value={}),
    ):
        load.return_value = _GoldInstance()
        result = _run_eval(_job(use_gold_patch=True))

    fetch.assert_not_called()
    assert seen["patch"] == _GoldInstance.patch
    assert result.state == "RESOLVED"
    assert result.verdict == "resolved"


def test_run_eval_refuses_a_gold_grade_when_the_row_has_no_gold() -> None:
    from swebench_eval.workers.eval_worker import _run_eval

    class _NoGold(_GoldInstance):
        patch = ""

    with (
        mock.patch("swebench_eval.dataset.swebench_loader.load_single_instance") as load,
        mock.patch("swebench_eval.workers.eval_worker.get_artifact") as fetch,
    ):
        load.return_value = _NoGold()
        with pytest.raises(RuntimeError, match="no gold patch"):
            _run_eval(_job(use_gold_patch=True))
    fetch.assert_not_called()
