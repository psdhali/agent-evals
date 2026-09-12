"""R8.1 — the shared streaming subprocess runner (proc.py).

`subprocess.run(capture_output=True)` blocks until exit, so every TRAJ line
flushes in one burst at the end and a 30-minute run shows nothing until minute
30.  run_streaming drains stdout line-by-line in a reader thread, invoking
on_line per line AS IT ARRIVES while buffering the same text for the post-hoc
parse.  These tests prove the live delivery contract and the timeout mapping.
"""

from __future__ import annotations

import sys

from swebench_eval.harnesses.proc import run_streaming

# The invoked CLI is `python3 -u` so stdout is unbuffered even when piped.
_PY = sys.executable


def test_run_streaming_delivers_lines_live() -> None:
    """on_line fires per line as it arrives, and the buffered stdout is the
    concatenation (the post-hoc parse reads the same text)."""
    seen: list[str] = []
    code = (
        "import sys, time\n"
        "for i in range(5):\n"
        "    print(f'line{i}')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.02)\n"
    )
    result = run_streaming(
        [_PY, "-u", "-c", code],
        cwd="/tmp",
        env={"PATH": "/usr/bin:/bin"},
        timeout=30,
        on_line=seen.append,
    )
    assert result.returncode == 0
    assert result.timed_out is False
    assert seen == [f"line{i}" for i in range(5)], seen
    assert result.stdout == "line0\nline1\nline2\nline3\nline4\n"
    assert result.stderr == ""


def test_run_streaming_on_timeout_sets_flag_and_kills() -> None:
    """A process that outlives the timeout is killed and timed_out is set (the
    adapter maps it to the `timeout` terminated reason, exactly as the old
    except subprocess.TimeoutExpired did)."""
    code = "import time; time.sleep(60); print('never')"
    result = run_streaming(
        [_PY, "-u", "-c", code],
        cwd="/tmp",
        env={"PATH": "/usr/bin:/bin"},
        timeout=1,
    )
    assert result.timed_out is True
    # The process was killed, so it did not exit 0 on its own.
    assert result.returncode != 0


def test_run_streaming_stdin_is_closed() -> None:
    """Every adapter must close stdin (an inherited TTY blocks codex exec).  A
    child that tries to read stdin should get EOF immediately, not hang."""
    code = "import sys\n" "data = sys.stdin.read()\n" "print(f'read={len(data)}')\n"
    result = run_streaming(
        [_PY, "-u", "-c", code],
        cwd="/tmp",
        env={"PATH": "/usr/bin:/bin"},
        timeout=30,
    )
    assert result.returncode == 0
    assert "read=0" in result.stdout


def test_run_streaming_captures_stderr() -> None:
    """stderr is drained (separate thread) so a chatty stderr cannot deadlock
    the pipes, and is returned for the raw_log stderr section."""
    code = "import sys; print('oops', file=sys.stderr); print('ok')"
    result = run_streaming(
        [_PY, "-u", "-c", code],
        cwd="/tmp",
        env={"PATH": "/usr/bin:/bin"},
        timeout=30,
    )
    assert result.returncode == 0
    assert "ok" in result.stdout
    assert "oops" in result.stderr


def test_run_streaming_terminates_the_process_when_the_stop_event_is_set() -> None:
    """Abort 2026-09-04: the worker's SIGTERM flag is installed as proc.STOP_EVENT; once
    set, the agent is SIGTERMed (so it can flush its own trajectory), then killed after
    the grace — the runner returns `aborted=True` within seconds, not after ECS's
    SIGKILL (all five aborted tasks exited 137 with nothing reported)."""
    import threading
    import time

    from swebench_eval.harnesses import proc

    stop = threading.Event()
    proc.STOP_EVENT = stop
    try:
        # Prints a line, then sleeps far past the test's patience; SIGTERM ends it.
        code = "import sys, time\nprint('started'); sys.stdout.flush()\ntime.sleep(60)\n"
        seen: list[str] = []
        holder: dict[str, proc.StreamResult] = {}

        def _run() -> None:
            holder["r"] = run_streaming(
                [_PY, "-u", "-c", code],
                cwd="/tmp",
                env={"PATH": "/usr/bin:/bin"},
                timeout=120,
                on_line=seen.append,
            )

        t = threading.Thread(target=_run)
        t.start()
        deadline = time.monotonic() + 10
        while not seen and time.monotonic() < deadline:
            time.sleep(0.05)
        assert seen == ["started"]
        started = time.monotonic()
        stop.set()
        t.join(timeout=15)
        assert not t.is_alive(), "runner did not return after the stop event"
        result = holder["r"]
        assert result.aborted is True
        assert result.timed_out is False
        assert result.returncode != 0
        assert time.monotonic() - started < 10  # well inside the 120 s stop window
        assert result.stdout == "started\n"  # the partial output is kept
    finally:
        proc.STOP_EVENT = None


def test_run_streaming_without_a_stop_event_is_unchanged() -> None:
    from swebench_eval.harnesses import proc

    assert proc.STOP_EVENT is None
    result = run_streaming(
        [_PY, "-u", "-c", "print('ok')"],
        cwd="/tmp",
        env={"PATH": "/usr/bin:/bin"},
        timeout=30,
    )
    assert result.returncode == 0 and result.aborted is False and result.timed_out is False
