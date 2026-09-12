"""Regression tests for the service logging bootstrap (review §3, 2026-08-16).

The deployed control-plane / harness / eval workers run via ``python -c`` with NO
logging handler configured, so Python's last-resort handler emits WARNING+ only and
every ``logger.info`` decision point (received job, enqueued eval, processed
result) was discarded — CloudWatch streams stayed empty unless a traceback landed.
A clean run and a silently-doing-nothing container looked identical.

These tests are deliberately run in SUBPROCESSES so pytest's own handler cannot
mask the behaviour: they prove the defect (INFO dropped without the bootstrap),
the fix (INFO visible with it), and that each deployed entrypoint actually emits
its known startup line to stdout in a real interpreter.
"""

from __future__ import annotations

import subprocess
import sys

_RUNNER = sys.executable


def _emit(code: str) -> str:
    """Run *code* in a fresh interpreter; return merged stdout+stderr."""
    r = subprocess.run(  # noqa: PLW1510 - check=False deliberately: we inspect output
        [_RUNNER, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return (r.stdout or "") + (r.stderr or "")


def test_info_is_dropped_without_bootstrap() -> None:
    """The defect, encoded: without a bootstrap, INFO never reaches any stream."""
    out = _emit("import logging\n" "logging.getLogger('test').info('NO_BOOTSTRAP_MARKER')\n")
    assert "NO_BOOTSTRAP_MARKER" not in out


def test_info_is_visible_with_bootstrap() -> None:
    """The fix: configure_logging() makes INFO reach stdout."""
    out = _emit(
        "import logging\n"
        "from swebench_eval.logging_bootstrap import configure_logging\n"
        "configure_logging()\n"
        "logging.getLogger('test').info('BOOTSTRAP_MARKER_VISIBLE')\n"
    )
    assert "BOOTSTRAP_MARKER_VISIBLE" in out


def test_harness_worker_startup_line_emitted_in_real_process() -> None:
    """The deployed entrypoint's own configure_logging() + startup INFO line."""
    out = _emit(
        "from swebench_eval.workers.harness_worker import run_harness_worker\n"
        "import swebench_eval.workers.harness_worker as hw\n"
        # M1 gate: without a control store the fail-closed read returns 'paused'
        # and the loop sleeps on the gate before ever reaching receive. This test
        # is about the LOGGING bootstrap, not the gate — stub the gate open.
        "hw._pool_paused = lambda pool: False\n"
        "hw.receive_message = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('stop'))\n"
        "try:\n"
        "    run_harness_worker()\n"
        "except RuntimeError:\n"
        "    pass\n"
    )
    assert "harness-worker starting" in out


def test_eval_worker_startup_line_emitted_in_real_process() -> None:
    out = _emit(
        "from swebench_eval.workers.eval_worker import run_eval_worker\n"
        "import swebench_eval.workers.eval_worker as ew\n"
        "ew._pool_paused = lambda pool: False\n"
        "ew.receive_message = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('stop'))\n"
        "try:\n"
        "    run_eval_worker()\n"
        "except RuntimeError:\n"
        "    pass\n"
    )
    assert "eval-worker starting" in out


def test_results_writer_startup_line_emitted_in_real_process() -> None:
    # results_writer also runs migrations + ensure_databases at start — stub them
    # so the subprocess doesn't try to reach a local Postgres.  The
    # Aurora→Valkey publisher tick and the reaper (rules 2/3) moved to
    # run_supervisor.py (CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md) —
    # results_writer's own startup no longer touches Aurora beyond
    # migrations/ensure_databases, so there is nothing else to stub here.
    out = _emit(
        "from swebench_eval.orchestrator.control_plane.results_writer import run_results_writer\n"
        "import swebench_eval.orchestrator.control_plane.results_writer as rw\n"
        "import swebench_eval.database.connection as dbc\n"
        "dbc.run_migrations = lambda *a, **k: None\n"
        "dbc.ensure_additional_databases = lambda *a, **k: None\n"
        "rw.receive_message = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('stop'))\n"
        "try:\n"
        "    run_results_writer()\n"
        "except RuntimeError:\n"
        "    pass\n"
    )
    assert "results-writer starting" in out


def test_run_supervisor_startup_line_emitted_in_real_process() -> None:
    # CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md: run-supervisor is the
    # new control-plane singleton (heartbeat + reaper) — same startup-logging
    # requirement as every other deployed entrypoint (this file's own
    # rationale), so it gets the same subprocess proof.  Stub migrations/
    # ensure_databases (no local Postgres in this test) and the Aurora rebuild
    # at start (_publish_control_from_aurora), then make the loop's own
    # time.sleep raise so the process exits after logging its startup line
    # instead of looping forever.
    out = _emit(
        "from swebench_eval.orchestrator.control_plane.run_supervisor import run_run_supervisor\n"
        "import swebench_eval.orchestrator.control_plane.run_supervisor as rs\n"
        "import swebench_eval.database.connection as dbc\n"
        "dbc.run_migrations = lambda *a, **k: None\n"
        "dbc.ensure_additional_databases = lambda *a, **k: None\n"
        "rs._publish_control_from_aurora = lambda: None\n"
        "rs.time.sleep = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('stop'))\n"
        "try:\n"
        "    run_run_supervisor()\n"
        "except RuntimeError:\n"
        "    pass\n"
    )
    assert "run-supervisor starting" in out


def test_bootstrap_is_idempotent() -> None:
    """Calling configure_logging() twice must not double-configure or blow up."""
    out = _emit(
        "import logging\n"
        "from swebench_eval.logging_bootstrap import configure_logging\n"
        "configure_logging()\n"
        "configure_logging()\n"
        "logging.getLogger('test').info('TWICE_OK')\n"
    )
    assert "TWICE_OK" in out


def test_warm_job_script_emits_startup_line() -> None:
    # The real warm job would build images / load HF; --dry-run must emit the
    # startup line and exit 0 without any of that (CI-safe).
    out = _emit(
        "import sys\n"
        "sys.argv = ['warm_image_cache.py', '--dry-run']\n"
        "code = open('scripts/warm_image_cache.py').read()\n"
        "ns = {'__name__': '__main__'}\n"
        "exec(compile(code, 'warm_image_cache.py', 'exec'), ns)\n"
    )
    assert "warm-job starting" in out
