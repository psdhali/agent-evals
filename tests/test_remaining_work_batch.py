"""R1 / R2 / R5.1 / R5.3 — the AWS-free half of builder1-REMAINING-WORK.

Each test mutation-proves one fix from the single-handover file:

- R1: the codex workdir base is /var/harness (not the system temp dir), and the
  codex PATH-alias warning is FATAL (a run that could not create its sandbox
  helpers must be a crash, never a clean "completed with empty patch").
- R2: the opencode server log is appended to raw_log.
- R5.1: a recognised terminal subtype (max turns) overrides the generic crash
  in the four subprocess adapters.
- R5.3: a shim-routed run with ZERO model calls is an infrastructure failure.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from swebench_eval.database.state_machine import (
    map_terminated_reason_to_error_category,
    map_terminated_reason_to_state,
)
from swebench_eval.harnesses.base import HarnessOutput, recognized_terminal_reason
from swebench_eval.workers import harness_worker as hw

# ---------------------------------------------------------------------------
# R1 — workdir outside the process temp dir
# ---------------------------------------------------------------------------


def test_worker_workdir_base_defaults_to_var_harness() -> None:
    """R1: the job workdir must be outside the process temp dir — codex refuses
    when CODEX_HOME lands inside /tmp-style temp dirs (its helper binaries are
    created there at runtime).  The DEFAULT base the deployed container runs
    with is /var/harness (overridable — see below)."""
    assert hw._WORKDIR_BASE == Path(
        "/var/harness"
    ), f"workdir base must default to /var/harness, got {hw._WORKDIR_BASE}"


def test_worker_workdir_uses_dir_under_base(monkeypatch, tmp_path) -> None:
    """R1: the workdir is created UNDER the base (dir=), NOT by making the base
    itself the temp dir (TMPDIR-style — which /var/tmp, /opt, /root all refuse).
    A writable base yields a child harness-* dir directly beneath it."""
    b = tmp_path / "mybase"
    monkeypatch.setattr(hw, "_WORKDIR_BASE", b)
    workdir = hw._make_worker_workdir()
    assert workdir.parent == b, workdir
    assert workdir.name.startswith("harness-")


# ---------------------------------------------------------------------------
# R1 — the codex PATH-alias warning is fatal
# ---------------------------------------------------------------------------


def _codex_harness_with_output(
    combined: str, returncode: int = 1, tmp_path: Path | None = None
) -> HarnessOutput:
    """Run CodexHarness.run with a stubbed run_streaming so stdout carries
    *combined* (and the on_line callback is fed each line live, like the real
    Popen reader), then read the classified output."""
    from swebench_eval.harnesses.codex.harness import CodexHarness

    harness = CodexHarness(codex_bin="codex")
    fake = mock.MagicMock()
    fake.returncode = returncode
    fake.stdout = combined
    fake.stderr = ""
    fake.timed_out = False

    def _fake_streaming(cmd, *, cwd, env, timeout, on_line=None):
        # Mirror the real reader: feed the combined text line-by-line live.
        for ln in (combined + "\n").splitlines():
            if on_line is not None:
                on_line(ln)
        return fake

    # A real output dir so the adapter's artifact writes land in tmp_path, NOT
    # under a leaked `MagicMock/mock.output_dir` in the repo root (an unset
    # Mock() attribute is truthy and became the adapter's output_dir).
    out_dir = (tmp_path or Path("/tmp")) / "codex-job"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (
        mock.patch(
            "swebench_eval.harnesses.codex.harness.run_streaming", side_effect=_fake_streaming
        ),
        mock.patch("swebench_eval.harnesses.codex.harness.ensure_prepared_repo"),
        mock.patch("swebench_eval.harnesses.codex.harness.git_diff_or_classify") as gd,
        mock.patch("swebench_eval.harnesses.repo_prep.ensure_prepared_repo"),
        mock.patch("swebench_eval.harnesses.routing.gateway_base_url", return_value="http://g/v1"),
        mock.patch("swebench_eval.harnesses.routing.gateway_api_key", return_value="k"),
        mock.patch("swebench_eval.harnesses.routing.agent_environment", return_value={}),
    ):
        gd.return_value = mock.MagicMock(patch="", patch_extract_s=None, timed_out=False)
        with mock.patch("swebench_eval.harnesses.codex.harness._write_config"):
            out = harness.run(
                mock.MagicMock(
                    instance_id="i",
                    repo_url="https://github.com/x/y",
                    base_commit="abc",
                    problem_statement="ps",
                    attempt_number=1,
                    repo_checkout_path=str(out_dir / "repo"),
                    output_dir=str(out_dir),
                    model_config=mock.MagicMock(),
                    timeout_seconds=100,
                )
            )
    return out


def test_codex_path_alias_warning_is_fatal(tmp_path) -> None:
    """R1: codex prints "WARNING: proceeding, even though we could not create
    PATH aliases" and exits 0 after burning calls with a dead sandbox.  A
    warning that guarantees total failure is not a warning — the run must be a
    crash, never a clean completed/empty-patch."""
    out = _codex_harness_with_output(
        "WARNING: proceeding, even though we could not create PATH aliases.  "
        "codex will not be able to execute commands.\n",
        tmp_path=tmp_path,
    )
    assert out.terminated_reason == "crash", out.terminated_reason
    assert "PATH aliases" in out.error


def test_codex_path_alias_warning_overrides_completed(tmp_path) -> None:
    """The fatal override applies even when codex exits 0 (it 'proceeded')."""
    out = _codex_harness_with_output(
        "WARNING: proceeding, even though we could not create PATH aliases.\n",
        returncode=0,
        tmp_path=tmp_path,
    )
    assert out.terminated_reason == "crash"


def test_codex_normal_exit_stays_completed(tmp_path) -> None:
    """An exit WITHOUT the PATH-alias warning keeps the normal classification."""
    out = _codex_harness_with_output('{"type":"turn.completed"}\n', returncode=0, tmp_path=tmp_path)
    assert out.terminated_reason == "completed"


# ---------------------------------------------------------------------------
# R2 — opencode server log appended to raw_log
# ---------------------------------------------------------------------------


def test_opencode_server_log_appended_to_raw_log(tmp_path) -> None:
    """R2: the opencode server log (isolated under XDG_DATA_HOME for the run)
    rides in raw_log at the existing write site — no new artifact kind.  A
    missing log must not raise."""
    from swebench_eval.harnesses.opencode.harness import OpenCodeHarness

    output_dir = tmp_path / "job"
    data_home = output_dir / "opencode-data"
    lg = data_home / "opencode" / "log" / "opencode.log"
    lg.parent.mkdir(parents=True)
    lg.write_text("LINE1 server started\nERROR something\n", encoding="utf-8")

    fake = mock.MagicMock()
    fake.returncode = 0
    fake.stdout = "stdout-line\n"
    fake.stderr = ""
    fake.timed_out = False

    def _fake_streaming(cmd, *, cwd, env, timeout, on_line=None):
        return fake

    with (
        mock.patch(
            "swebench_eval.harnesses.opencode.harness.run_streaming",
            side_effect=_fake_streaming,
        ),
        mock.patch("swebench_eval.harnesses.opencode.harness.ensure_prepared_repo"),
        mock.patch("swebench_eval.harnesses.opencode.harness.git_diff_or_classify") as gd,
        mock.patch(
            "swebench_eval.harnesses.opencode.harness.gateway_base_url", return_value="http://g/v1"
        ),
        mock.patch("swebench_eval.harnesses.opencode.harness.gateway_api_key", return_value="k"),
        mock.patch("swebench_eval.harnesses.opencode.harness.agent_environment", return_value={}),
    ):
        gd.return_value = mock.MagicMock(patch="", patch_extract_s=None, timed_out=False)
        harness = OpenCodeHarness()
        out = harness.run(
            mock.MagicMock(
                instance_id="i",
                repo_url="https://github.com/org/repo",
                base_commit="abc",
                problem_statement="ps",
                attempt_number=1,
                repo_checkout_path="/tmp/repo",
                model_config={"gateway_base_url": "http://g/v1", "gateway_api_key": "k"},
                output_dir=str(output_dir),
                timeout_seconds=100,
                # compaction build: None disables the window → no compaction
                # config is rendered (a MagicMock attribute would be truthy and
                # drive compact_messages/opencode limit generation).
                context_window_tokens=None,
            )
        )

    raw = Path(out.raw_log_path).read_text()
    assert "--- OPENCODE SERVER LOG ---" in raw
    assert "ERROR something" in raw
    assert raw.index("--- OPENCODE SERVER LOG ---") > raw.index("--- STDERR ---")


def test_opencode_missing_server_log_still_writes_raw_log(tmp_path) -> None:
    """A run with no server log (or an unreadable one) must not fail or leave
    raw_log empty — the normal stdout+stderr still lands."""
    from swebench_eval.harnesses.opencode.harness import OpenCodeHarness

    output_dir = tmp_path / "job2"
    output_dir.mkdir()  # the worker's mkdtemp creates the job dir before the adapter
    harness = OpenCodeHarness()
    fake = mock.MagicMock()
    fake.returncode = 0
    fake.stdout = "hello-extraction\n"
    fake.stderr = ""
    fake.timed_out = False

    def _fake_streaming(cmd, *, cwd, env, timeout, on_line=None):
        return fake

    with (
        mock.patch(
            "swebench_eval.harnesses.opencode.harness.run_streaming",
            side_effect=_fake_streaming,
        ),
        mock.patch("swebench_eval.harnesses.opencode.harness.ensure_prepared_repo"),
        mock.patch("swebench_eval.harnesses.opencode.harness.git_diff_or_classify") as gd,
        mock.patch("swebench_eval.harnesses.repo_prep.ensure_prepared_repo"),
        mock.patch(
            "swebench_eval.harnesses.opencode.harness.gateway_base_url", return_value="http://g/v1"
        ),
        mock.patch("swebench_eval.harnesses.opencode.harness.gateway_api_key", return_value="k"),
        mock.patch("swebench_eval.harnesses.opencode.harness.agent_environment", return_value={}),
    ):
        gd.return_value = mock.MagicMock(patch="", patch_extract_s=None, timed_out=False)
        out = harness.run(
            mock.MagicMock(
                instance_id="i",
                repo_id="https://github.com/a/b",
                base_commit="abc",
                problem_statement="ps",
                attempt_number=1,
                repo_checkout_path="/tmp/repo",
                model_config=mock.MagicMock(),
                output_dir=str(output_dir),
                timeout_seconds=100,
                context_window_tokens=None,  # compaction build: see above
            )
        )
    raw = Path(out.raw_log_path).read_text()
    assert "hello-extraction" in raw
    assert "--- OPENCODE SERVER LOG ---" not in raw


# ---------------------------------------------------------------------------
# R5.1 — max_turns recognition in the subprocess adapters
# ---------------------------------------------------------------------------


def test_recognized_terminal_reason_max_turns():
    assert recognized_terminal_reason("codex: reached max turns, stopping.") == "max_turns_exceeded"
    assert recognized_terminal_reason("clean output\n") is None


def test_claude_code_max_turns_subtype_overrides_crash() -> None:
    """R5.1/M1-b: the real captured result event carries subtype
    "error_max_turns" (harness-02b-claude-code-ROOT-CAUSE.md — NOT the fabricated
    "max_turns_exceeded").  A turn-capped run must be recorded max_turns_exceeded
    / FAILED_HARNESS, never crash (exit != 0) or completed→PATCH_READY (exit 0).
    Asserts on the string the CLI actually emits, so a recognizer regression is
    caught by CI."""
    from swebench_eval.harnesses.claude_code.harness import _terminal_subtype_from_result

    stream = (
        '{"type":"user","message":{"content":"hi"}}\n'
        '{"type":"result","subtype":"error_max_turns",'
        '"errors":["Reached maximum number of turns (50)"],"is_error":false}\n'
    )
    assert _terminal_subtype_from_result(stream) == "max_turns_exceeded"


def test_claude_code_unknown_subtype_stays_crash() -> None:
    from swebench_eval.harnesses.claude_code.harness import _terminal_subtype_from_result

    stream = '{"type":"result","subtype":"error_during_execution"}\n'
    assert _terminal_subtype_from_result(stream) is None  # caller keeps crash


def test_m1_success_subtype_with_max_turns_mention_stays_clean() -> None:
    """M1 regression: exit 0, subtype "success", and the assistant merely
    MENTIONS "max turns" in its reasoning.  The whole-transcript text scan used
    to catch that phrase and reclassify a clean, patch-producing run as
    max_turns_exceeded → FAILED_HARNESS → NEVER graded.  The structured
    subtype is the only authority on a zero exit.

    (The reviewer's probe: 'I'll be efficient here so we don't burn max turns on
    exploration.'  That single line destroyed the run's gradeability.)"""
    from swebench_eval.harnesses.claude_code.harness import (
        _terminal_subtype_from_result,
    )

    stream = (
        '{"type":"assistant","message":{"content":[{"type":"text",'
        '"text":"I\'ll be efficient here so we don\'t burn max turns on '
        'exploration."}]}}\n'
        '{"type":"result","subtype":"success","is_error":false}\n'
    )
    # _terminal_subtype_from_result must return None (trusts ONLY the structured
    # subtype), NEVER the whole-transcript scan it used to fall back to.
    assert _terminal_subtype_from_result(stream) is None


def test_m1_zero_exit_success_keeps_completed(tmp_path) -> None:
    """M1 end-to-end at the run level: exit 0 + subtype success + a patch, and
    the model merely mentioned "max turns" — the run STAYS completed (and the
    patch stays gradeable), it is not reclassified."""
    from swebench_eval.harnesses.claude_code.harness import ClaudeCodeHarness

    harness = ClaudeCodeHarness()
    # The adapter writes raw_log/traj to the job dir; a real dir like the worker's.
    out_dir = tmp_path / "cc-job"
    out_dir.mkdir()
    fake = mock.MagicMock()
    fake.returncode = 0
    fake.stdout = (
        '{"type":"assistant","message":{"content":[{"type":"text",'
        '"text":"I\'ll be efficient here so we don\'t burn max turns on '
        'exploration."}]}}\n'
        '{"type":"result","subtype":"success","is_error":false}\n'
    )
    fake.stderr = ""
    fake.timed_out = False

    def _fake_streaming(cmd, *, cwd, env, timeout, on_line=None):
        for ln in fake.stdout.splitlines():
            if on_line is not None:
                on_line(ln)
        return fake

    with (
        mock.patch(
            "swebench_eval.harnesses.claude_code.harness.run_streaming",
            side_effect=_fake_streaming,
        ),
        mock.patch("swebench_eval.harnesses.claude_code.harness.ensure_prepared_repo"),
        mock.patch("swebench_eval.harnesses.claude_code.harness.git_diff_or_classify") as gd,
        mock.patch("swebench_eval.harnesses.routing.gateway_base_url", return_value="http://g"),
        mock.patch("swebench_eval.harnesses.routing.gateway_api_key", return_value="k"),
        mock.patch("swebench_eval.harnesses.routing.agent_environment", return_value={}),
    ):
        gd.return_value = mock.MagicMock(
            patch="--- a/py\n+++ b/py\n@@ -1 +1 @@\n-old\n+new\n",
            patch_extract_s=None,
            timed_out=False,
        )
        out = harness.run(
            mock.MagicMock(
                instance_id="i",
                repo_url="https://github.com/o/r",
                base_commit="abc",
                problem_statement="Fix the bug.",
                attempt_number=1,
                repo_checkout_path=str(out_dir / "repo"),
                output_dir=str(out_dir),
                model_config=mock.MagicMock(),
                timeout_seconds=100,
                context_window_tokens=None,  # compaction build: no window → no threshold
            )
        )
    # The model got a patch and finished cleanly — the run must be completed, so
    # it auto-enqueues an eval job (PATCH_READY) instead of a never-graded
    # max-turns failure.
    assert out.terminated_reason == "completed", out.terminated_reason
    assert out.error == ""
    assert out.patch  # the patch is non-empty → PATCH_READY → auto-eval
    assert map_terminated_reason_to_state("completed", out.patch) == "PATCH_READY"


def test_claude_code_still_detects_real_max_turns_subtype() -> None:
    """M1-b must not mute the real signal: the genuine captured subtype
    "error_max_turns" maps to max_turns_exceeded even on a zero exit (the
    exit-0 turn-cap — which would otherwise be recorded completed→PATCH_READY)."""
    from swebench_eval.harnesses.claude_code.harness import _terminal_subtype_from_result

    stream = '{"type":"result","subtype":"error_max_turns","is_error":false}\n'
    assert _terminal_subtype_from_result(stream) == "max_turns_exceeded"


def test_state_machine_zero_model_calls_mapping() -> None:
    """R5.3: zero_model_calls → HARNESS_ZERO_MODEL_CALLS, retryable, FAILED_HARNESS."""
    assert (
        map_terminated_reason_to_error_category("zero_model_calls", None)
        == "HARNESS_ZERO_MODEL_CALLS"
    )
    assert map_terminated_reason_to_state("zero_model_calls", None) == "FAILED_HARNESS"
    from swebench_eval.database.state_machine import is_retryable

    assert is_retryable("HARNESS_ZERO_MODEL_CALLS")


def test_worker_classify_zero_model_calls() -> None:
    """R5.3: a shim-routed run that 'completed' with zero calls is infra."""

    class _Shim:
        calls_made = 0
        paused = False

    out = HarnessOutput(
        patch=None,
        success=False,
        trajectory_path="",
        raw_log_path="",
        terminated_reason="completed",
    )
    hw._classify_zero_model_calls(out, _Shim())
    assert out.terminated_reason == "zero_model_calls"
    assert out.error_category == "HARNESS_ZERO_MODEL_CALLS"


def test_worker_classify_zero_model_calls_preserves_timeout() -> None:
    """R5.3: a zero-call run that already crashed/timed out keeps its cause."""

    class _Shim:
        calls_made = 0
        paused = False

    out = HarnessOutput(
        patch="",
        success=False,
        trajectory_path="",
        raw_log_path="",
        terminated_reason="timeout",
    )
    hw._classify_zero_model_calls(out, _Shim())
    assert out.terminated_reason == "timeout"


# ---------------------------------------------------------------------------
# R3 — post-hoc backstop preserves timeout
# ---------------------------------------------------------------------------


def _budget_job(max_tokens=1000, max_cost=5.0):
    return mock.MagicMock(
        instance_id="mini",
        max_tokens_per_instance=max_tokens,
        max_cost_usd_per_instance=max_cost,
    )


def test_budget_breach_preserves_timeout() -> None:
    """R3 core: a run that hit the WALL CLOCK (timeout) but ALSO crossed the
    budget must keep timeout as the cause — mini's 1800.188s/28x case."""
    from swebench_eval.harnesses.base import Usage

    usage = Usage()
    usage.input_tokens = 14_000_000  # 28x over a 500k ceiling
    out = HarnessOutput(
        patch="",
        success=False,
        trajectory_path="",
        raw_log_path="",
        terminated_reason="timeout",
        wall_clock_seconds=1800.188,
    )
    hw._classify_budget_breach(out, usage, _budget_job(max_tokens=500_000), mock.Mock())
    assert (
        out.terminated_reason == "timeout"
    ), f"budget_exceeded must not overwrite timeout, got {out.terminated_reason}"
    assert out.error_category != "HARNESS_BUDGET_EXCEEDED"


def test_budget_breach_still_stamps_on_completed() -> None:
    """R3: a CLI that ignores the shim's 400 refusal and 'completes' over the
    ceiling is caught by the post-hoc backstop."""
    from swebench_eval.harnesses.base import Usage

    usage = Usage()
    usage.input_tokens = 600_000
    out = HarnessOutput(
        patch="",
        success=False,
        trajectory_path="",
        raw_log_path="",
        terminated_reason="completed",
    )
    hw._classify_budget_breach(out, usage, _budget_job(max_tokens=500_000), mock.Mock())
    assert out.terminated_reason == "budget_exceeded"
    assert out.error_category == "HARNESS_BUDGET_EXCEEDED"


# ---------------------------------------------------------------------------
# R4.1 — scripts/recovery/create_run_row.py (backfill the runs row that
# FK-failed eval for a run that bypassed the S3 dispatcher)
# ---------------------------------------------------------------------------


def _run_row_with_fake_db(
    monkeypatch, rowcount: int = 1
) -> tuple[bool, list[tuple[str, tuple[object, ...]]]]:
    import scripts.recovery.create_run_row as crr
    import swebench_eval.database.connection as db_conn

    executed: list[tuple[str, tuple[object, ...]]] = []

    class _FakeCursor:
        def __init__(self):
            self.rowcount = rowcount

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql: str, params=None) -> None:
            executed.append((sql, params))

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def commit(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(db_conn, "get_connection", lambda: _FakeConn())
    return crr.create_run_row("run-abc", "claude_code", "claude-code-model"), executed


def test_create_run_row_inserts_both_rows(monkeypatch) -> None:
    """R4.1: the script inserts the runs + run_targets rows (mirrors
    dispatcher.py) — the FK parent that was missing, so result messages FK-fail
    and eval never ran."""
    created, executed = _run_row_with_fake_db(monkeypatch, rowcount=1)
    assert created is True

    inserts = [sql for sql, _ in executed if sql.strip().startswith("INSERT INTO runs")]
    targets = [sql for sql, _ in executed if sql.strip().startswith("INSERT INTO run_targets")]
    assert len(inserts) == 1 and len(targets) == 1
    assert "ON CONFLICT (run_id) DO NOTHING" in inserts[0]
    assert "ON CONFLICT DO NOTHING" in targets[0]
    # idempotent: the run target uses the SAME run_id that lands in overrides.
    assert executed[0][1][0] == "run-abc"
    assert executed[1][1] == ("run-abc", "claude_code", "claude-code-model")


def test_create_run_row_is_idempotent(monkeypatch) -> None:
    """R4.1: ON CONFLICT DO NOTHING — re-running after a row exists reports
    created=False (rowcount 0) rather than raising, so repeat creates are safe."""
    created, _ = _run_row_with_fake_db(monkeypatch, rowcount=0)
    assert created is False


# ---------------------------------------------------------------------------
# R4.3 — results_writer names the missing run_id instead of silently DLQ-ing
# ---------------------------------------------------------------------------


def test_log_missing_run_id_names_run_id_and_emits_metric(monkeypatch, caplog) -> None:
    """R4.3: an FK violation on run_id means "run never registered" — the log
    must NAME the missing run_id (the actionable fix is create_run_row.py) and
    emit a metric, so the week of silent DLQs cannot recur."""
    import logging

    import swebench_eval.orchestrator.control_plane.results_writer as rw

    put = mock.MagicMock()
    monkeypatch.setattr(
        "boto3.client",
        lambda *a, **k: mock.MagicMock(put_metric_data=put),
    )
    with caplog.at_level(
        logging.ERROR, logger="swebench_eval.orchestrator.control_plane.results_writer"
    ):
        rw._log_missing_run_id(
            mock.MagicMock(run_id="20m-claude_code-1787545149"),
            RuntimeError("simulated FK"),
        )

    assert "20m-claude_code-1787545149" in caplog.text
    assert "registered" in caplog.text
    assert "create_run_row.py" in caplog.text
    put.assert_called_once()
    assert put.call_args.kwargs["MetricData"][0]["MetricName"] == "ResultsWriterMissingRunId"


def test_process_result_fk_logs_and_reraises(monkeypatch, caplog) -> None:
    """R4.3: when the INSERT raises ForeignKeyViolation, _process_result logs the
    missing run_id AND re-raises so the message still retries/DLQs (the FK is
    kept — the fix is the runs row, not a tolerant insert)."""
    import logging

    import psycopg2

    from swebench_eval.orchestrator.control_plane.results_writer import (
        _process_result,
    )
    from swebench_eval.queue.schemas import ResultMessage

    class _RaisingCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            raise psycopg2.errors.ForeignKeyViolation(
                "insert or update on table instance_results violates foreign key "
                "constraint instance_results_run_id_fkey"
            )

    class _FakeConn:
        def cursor(self):
            return _RaisingCursor()

        def close(self):
            pass

    monkeypatch.setattr("swebench_eval.database.connection.get_connection", lambda: _FakeConn())
    # The metric emit must not break the retry path.
    monkeypatch.setattr("boto3.client", lambda *a, **k: mock.MagicMock())

    result = ResultMessage(
        run_id="20m-claude_code-1787545149",
        instance_id="scikit-learn__scikit-learn-25102",
        attempt_number=1,
        phase="harness",
        state="PATCH_READY",
    )
    with caplog.at_level(
        logging.ERROR, logger="swebench_eval.orchestrator.control_plane.results_writer"
    ):
        try:
            _process_result(result)
            raise AssertionError("_process_result must re-raise the FK violation")
        except psycopg2.errors.ForeignKeyViolation:
            pass

    assert "20m-claude_code-1787545149" in caplog.text
    assert "never registered" in caplog.text


# ---------------------------------------------------------------------------
# R7 — task framing (OWNER DECISION MADE): shared module, 3 adapters wired
# ---------------------------------------------------------------------------


def test_r7_task_framing_constant_and_framed() -> None:
    """R7: the framing is the reviewer's exact shape, in ONE shared module."""
    from swebench_eval.harnesses.task_framing import TASK_FRAMING, framed

    assert "Fix the issue described below by editing the source code" in TASK_FRAMING
    assert "Do not modify test files" in TASK_FRAMING
    # 2026-09-06 (owner decision): text-only models — 5 of 82 claude_code/codex
    # sessions died on an OpenRouter "No endpoints found that support image
    # input" 404 after the agent Read a PNG it had rendered.
    assert "text-only" in TASK_FRAMING
    assert "Never open, read, view or attach" in TASK_FRAMING
    out = framed("It would be nice to preserve dtypes")
    assert out.startswith(TASK_FRAMING)
    assert out.endswith("Issue:\nIt would be nice to preserve dtypes")


def test_r7_framing_wired_into_three_adapters() -> None:
    """R7: claude_code, codex and opencode all call framed() — a harness that
    stops using it fails this test (the drift guard for the ONE-module rule)."""
    import inspect

    import swebench_eval.harnesses.claude_code.harness as cc
    import swebench_eval.harnesses.codex.harness as codex
    import swebench_eval.harnesses.opencode.harness as oc

    for mod in (cc, codex, oc):
        src = inspect.getsource(mod)
        assert "framed(" in src, f"{mod.__name__} does not frame its prompt"
        assert "task_framing import framed" in src, f"{mod.__name__} imports framing"


def test_r7_custom_minimal_cap_500_not_double_framed() -> None:
    """R7: custom_minimal's turn cap is 500 and it does NOT get the new framing
    (it already has its own SYSTEM_PROMPT — double-framing skews the comparison)."""
    import inspect

    import swebench_eval.harnesses.custom_minimal.harness as cm

    assert "max_turns: int = 500" in inspect.getsource(cm.CustomMinimalHarness.__init__)
    # custom_minimal's own framing lives in its module — the shared module must
    # NOT import the shared framing (a stray import here is a double-frame leak).
    cm_src = inspect.getsource(cm)
    assert "task_framing" not in cm_src


def test_r7_no_turn_cap_for_codex_opencode_established() -> None:
    """R7 finding (established, not assumed): neither codex exec nor opencode
    run expose a turn-cap flag — recorded so two harnesses' real (implicit)
    limits are not 'unknown'.  The adapters don't pass a turn-cap arg because
    none exists; R3's shim is the bound."""
    import inspect

    import swebench_eval.harnesses.codex.harness as codex
    import swebench_eval.harnesses.opencode.harness as oc

    codex_cmd = inspect.getsource(codex.CodexHarness.run)
    oc_cmd = inspect.getsource(oc.OpenCodeHarness.run)
    assert "max-turns" not in codex_cmd and "max_turns" not in codex_cmd
    assert "max-turns" not in oc_cmd and "max_turns" not in oc_cmd


# ---------------------------------------------------------------------------
# R8.1 — TRAJ per-turn logging (always-on, ~$0.13)
# ---------------------------------------------------------------------------


def test_traj_log_empty_turn_is_explicit() -> None:
    """R8.1: the empty-turn case must be EXPLICIT — tool=NONE content_chars=0 —
    exactly the line that would have made the custom_minimal mid-word stop and
    claude's 0-Write/Edit obvious in seconds instead of an S3 dig."""
    from swebench_eval.harnesses.custom_minimal.trajectory import traj_log

    line = traj_log("assistant", 8, tool="NONE", content="")
    assert "turn=8" in line
    assert "role=assistant" in line
    assert "tool=NONE" in line
    assert "content_chars=0" in line


def test_traj_log_truncates_output_and_carries_extras() -> None:
    from swebench_eval.harnesses.custom_minimal.trajectory import _trunc, traj_log

    line = traj_log("tool", 6, tool="bash", output="x" * 500, finish="stop")
    assert "bytes=500" in line
    assert "head=" in line
    # the head is truncated to ~200 chars + ellipsis, never the raw 500 chars.
    assert "x" * 400 not in line
    assert _trunc("x" * 500).endswith("…")
    assert "finish=stop" in line


def test_subprocess_adapters_emit_traj_at_turn_boundary() -> None:
    """R8.1: each subprocess adapter's _write_trajectory loop emits a TRAJ log
    line per turn — grep for the TRAJ marker in each module's source (a future
    refactor that drops live logging fails this test)."""
    import inspect

    import swebench_eval.harnesses.claude_code.harness as cc
    import swebench_eval.harnesses.codex.harness as codex
    import swebench_eval.harnesses.mini_swe_agent.harness as mini
    import swebench_eval.harnesses.opencode.harness as oc

    for mod in (cc, codex, mini, oc):
        src = inspect.getsource(mod)
        assert "TRAJ %s" in src, f"{mod.__name__} has no TRAJ per-turn logging"
        assert "traj_log(" in src, f"{mod.__name__} does not use the shared traj_log"


# ---------------------------------------------------------------------------
# D5 (owner decision, option C) — empty_response reason + taxonomy
# ---------------------------------------------------------------------------


def test_state_machine_empty_response_mapping() -> None:
    """D5: empty_response → HARNESS_EMPTY_RESPONSE, FAILED_HARNESS (never
    EMPTY_PATCH/never graded), and RETRYABLE (provider-side)."""
    from swebench_eval.database.state_machine import is_retryable

    assert (
        map_terminated_reason_to_error_category("empty_response", None) == "HARNESS_EMPTY_RESPONSE"
    )
    assert map_terminated_reason_to_state("empty_response", "some patch") == "FAILED_HARNESS"
    assert is_retryable("HARNESS_EMPTY_RESPONSE")
    # Contrast: completed with no patch = EMPTY_PATCH (the bug D5 fixes — an
    # empty turn used to read as completed).
    assert map_terminated_reason_to_error_category("completed", None) == "EMPTY_PATCH"


def test_custom_minimal_retries_empty_then_terminates() -> None:
    """D5: three consecutive empty responses (no content, no tools, no
    reasoning) terminate with empty_response, NOT completed→EMPTY_PATCH.  The
    run is FAILED_HARNESS and retryable, never graded as a clean empty."""
    import subprocess as _sp
    import tempfile

    from swebench_eval.harnesses.base import HarnessInput, ModelConfig
    from swebench_eval.harnesses.custom_minimal.harness import CustomMinimalHarness

    def _empty_response():
        resp = mock.MagicMock()
        choice = mock.MagicMock()
        msg = mock.MagicMock()
        msg.content = ""
        msg.tool_calls = None
        msg.refusal = None
        msg.reasoning_content = None
        choice.message = msg
        resp.choices = [choice]
        resp.usage = mock.MagicMock()
        resp.usage.prompt_tokens = 5
        resp.usage.completion_tokens = 1
        resp.usage.cost = 0.0
        resp.usage.prompt_tokens_details = mock.MagicMock()
        resp.usage.prompt_tokens_details.cached_tokens = 0
        resp.usage.prompt_tokens_details.cache_write_tokens = 0
        resp.usage.completion_tokens_details = mock.MagicMock()
        resp.usage.completion_tokens_details.reasoning_tokens = 0
        return resp

    fake_client = mock.MagicMock()
    fake_client.chat.completions.create = mock.MagicMock(
        side_effect=[_empty_response() for _ in range(3)]
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"
        repo_dir.mkdir()
        _sp.run(["git", "init"], cwd=repo_dir, capture_output=True, timeout=10, check=False)
        _sp.run(
            ["git", "config", "user.email", "t@t"],
            cwd=repo_dir,
            capture_output=True,
            timeout=10,
            check=False,
        )
        _sp.run(
            ["git", "config", "user.name", "T"],
            cwd=repo_dir,
            capture_output=True,
            timeout=10,
            check=False,
        )
        _sp.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=repo_dir,
            capture_output=True,
            timeout=10,
            check=False,
        )

        hi = HarnessInput(
            instance_id="d5",
            repo_url=f"file://{repo_dir}",
            base_commit="HEAD",
            problem_statement="Fix the bug.",
            attempt_number=1,
            repo_checkout_path=str(repo_dir),
            model_config=ModelConfig(
                gateway_base_url="http://t/v1", gateway_api_key="k", model_name="m"
            ),
            timeout_seconds=30,
            max_tokens_per_instance=None,
            max_cost_usd_per_instance=5.0,
        )
        harness = CustomMinimalHarness(
            api_base_url="http://t/v1", api_key="k", model="m", max_turns=10
        )
        with mock.patch("openai.OpenAI", return_value=fake_client):
            out = harness.run(hi)

    assert out.terminated_reason == "empty_response", out.terminated_reason
    assert out.error_category == "HARNESS_EMPTY_RESPONSE"
    assert out.exit_code == 2
    assert "empty" in out.error.lower()
