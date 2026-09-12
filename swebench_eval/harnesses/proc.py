"""Streaming subprocess runner for the subprocess harness adapters (R8.1).

``subprocess.run(capture_output=True)`` blocks until exit, so stdout does not
exist until the run is over — every TRAJ line flushes in one burst at the end,
and a 30-minute run shows nothing until minute 30.  That is exactly the state
PART 3 was written to end (the owner's ask: see what the model is doing, live).

This drains stdout line-by-line in a reader thread, invoking ``on_line(line)``
for each line AS IT ARRIVES (the live CloudWatch view), while buffering the
same text so the adapter's existing post-hoc ``_parse_events(stdout)`` /
``_write_trajectory(events, ...)`` run unchanged.  stderr is drained in its own
thread so a chatty stderr cannot deadlock the pipes.  On timeout the process is
killed and ``timed_out`` is set (the adapter maps it to ``timeout`` exactly as
the old ``except subprocess.TimeoutExpired`` did).

The adapters call :func:`run_streaming` instead of ``subprocess.run``; tests
mock the seam by patching the adapter module's imported ``run_streaming``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Abort 2026-09-04 (run 01788550040741118596-dd284a4f): the worker's SIGTERM handler only
# set a flag that is read AFTER the harness returns, so a task mid agent-loop never
# returned before ECS's SIGKILL (stopTimeout 120 s): all five aborted tasks exited 137
# with nothing uploaded and nothing reported, and the abort burned its full 5-minute
# settle. The worker installs its shutdown event here (``STOP_EVENT``); the wait loop
# below polls it and, once set, SIGTERMs the agent (so it can flush its own trajectory),
# waits ``STOP_GRACE_S`` and kills. The adapter then runs its normal post-run path on the
# partial output (patch extraction, trajectory normalisation, upload) and the worker
# reclassifies the result to ABORTED_IN_FLIGHT — all inside the stop window.
STOP_EVENT: threading.Event | None = None
STOP_GRACE_S = 20.0
_WAIT_SLICE_S = 1.0
# 2026-09-06 (run b4338f67, django-15098 / django-16502): two agents died with
# `exited with code -15` — SIGTERM from INSIDE the container, no ECS stop, no
# worker SIGTERM, no abort — and nothing recorded who else was alive to send
# it.  When the agent dies by a signal we did not send, dump the container's
# process tree once so the next occurrence names the sender.
_PS_COLUMNS = "pid,ppid,pgid,sid,user,etime,args"


def _log_process_tree(agent: str, returncode: int) -> None:
    """Best-effort ``ps`` snapshot after an unexplained signal death. Never raises."""
    import signal as _signal

    try:
        sig = _signal.Signals(-returncode).name
    except (ValueError, AttributeError):
        sig = str(-returncode)
    try:
        ps = subprocess.run(
            ["ps", "-eo", _PS_COLUMNS],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        tree = ps.stdout.strip() or ps.stderr.strip() or "(ps printed nothing)"
    except Exception as exc:  # noqa: BLE001 - a post-mortem must never raise
        tree = f"(ps unavailable: {exc})"
    logger.warning(
        "%s died by signal %s that this worker did not send — container process tree:\n%s",
        agent,
        sig,
        tree,
    )


@dataclass
class StreamResult:
    """The outcome of a streamed run, shaped like a ``subprocess.CompletedProcess``."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    # True when the run was cut short by ``STOP_EVENT`` (an operator abort), never by
    # the wall-clock timeout. Adapters treat it like any non-zero exit: the partial
    # artifacts are kept; the worker owns the ABORTED_IN_FLIGHT reclassification.
    aborted: bool = False


def run_streaming(
    cmd: list[str],
    *,
    cwd: str | os.PathLike[str],
    env: dict[str, str],
    timeout: int,
    on_line: Callable[[str], None] | None = None,
) -> StreamResult:
    """Run ``cmd`` draining stdout line-by-line; call ``on_line`` per line live.

    stdin is closed (every adapter must close it — an inherited TTY blocks).
    ``for line in p.stdout`` in the reader thread blocks until a newline or EOF,
    so lines reach ``on_line`` as they are produced, not after exit.
    """
    # G2: the agent process drops to the unprivileged user (routing.agent_user)
    # — the worker stays root.  Empty kwargs when separation is off.
    from swebench_eval.harnesses.routing import agent_spawn_kwargs

    p = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
        **agent_spawn_kwargs(),
    )
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def drain_stdout() -> None:
        assert p.stdout is not None
        for line in p.stdout:
            stdout_lines.append(line)
            if on_line is not None:
                try:
                    on_line(line.rstrip("\n"))
                except Exception:  # a log callback must never kill the drain
                    logger.exception("on_line handler failed for line %r", line)

    def drain_stderr() -> None:
        assert p.stderr is not None
        stderr_lines.extend(p.stderr)

    t_out = threading.Thread(target=drain_stdout, daemon=True, name="proc-stdout")
    t_err = threading.Thread(target=drain_stderr, daemon=True, name="proc-stderr")
    t_out.start()
    t_err.start()

    timed_out = False
    aborted = False
    deadline = time.monotonic() + timeout
    returncode: int | None = None
    while returncode is None:
        stop = STOP_EVENT
        if stop is not None and stop.is_set():
            aborted = True
            logger.warning(
                "stop requested (operator abort): terminating %s, %.0fs grace before kill",
                cmd[0],
                STOP_GRACE_S,
            )
            p.terminate()
            try:
                returncode = p.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                p.kill()
                returncode = p.wait()
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            p.kill()
            returncode = p.wait()
            break
        try:
            returncode = p.wait(timeout=min(_WAIT_SLICE_S, remaining))
        except subprocess.TimeoutExpired:
            continue
    # Reap the reader threads (kill closed the pipes, so they finish promptly).
    t_out.join(timeout=10)
    t_err.join(timeout=10)
    if returncode is not None and returncode < 0 and not aborted and not timed_out:
        _log_process_tree(cmd[0], returncode)
    return StreamResult(
        returncode, "".join(stdout_lines), "".join(stderr_lines), timed_out, aborted=aborted
    )
