"""Live progress of a grade (2026-09-06, the first 500-run's django-10097).

SWE-bench's ``exec_run_with_timeout`` streams ``/eval.sh``'s output over the
docker exec socket into ONE in-memory buffer and hands it back only when the
command ends, so a 67-minute Django suite is completely silent on the eval
host: no log line, no heartbeat anyone can read, nothing the reaper can use to
tell "still grading" from "worker gone".  That silence is what let the
run-supervisor's deadline rule mark a live grade ABANDONED (no progress key)
and what kept the operator guessing whether the grade had started at all.

This module tees that stream:

* :func:`install_exec_tee` replaces ``exec_run_with_timeout`` (on the defining
  module AND on the name ``run_evaluation`` bound at import — the same
  two-names trap as ``setup_logger``) with a byte-for-byte equivalent that also
  feeds every chunk into the current :class:`GradeProgress`.
* :class:`GradeProgress` keeps a thread-safe snapshot — bytes, lines, the last
  complete line, elapsed — and logs a progress line every
  :data:`LOG_EVERY_S` while output flows (and while it does NOT: a stall shows
  as an unchanged line count, which is the useful signal).
* The eval worker's heartbeat publishes :meth:`GradeProgress.snapshot` to the
  Redis progress key every beat, so the reaper's rule 2 sees a live key and
  the dashboard's live panel shows "grading: N lines, last: …".

Never raises into the grade: every tee failure is logged and the original
byte stream still reaches SWE-bench unchanged.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

LOG_EVERY_S = 60.0
_LAST_LINE_MAX = 200

_lock = threading.Lock()
_current: GradeProgress | None = None
_TEE_INSTALLED = False


class GradeProgress:
    """Thread-safe running summary of one grade's exec output."""

    def __init__(self, instance_id: str, *, log_every_s: float = LOG_EVERY_S) -> None:
        self.instance_id = instance_id
        self.started_at = time.time()
        self._log_every_s = log_every_s
        self._lock = threading.Lock()
        self._bytes = 0
        self._lines = 0
        self._last_line = ""
        self._partial = b""
        self._done = False
        self._last_log = time.monotonic()
        self._lines_at_last_log = 0
        # 2026-09-08: when the stream goes quiet the feed() path never logs again, so a
        # hung suite is silent in CloudWatch after its last line (the liveness key keeps
        # refreshing — the reaper is fine, the OPERATOR is not). The heartbeat calls
        # maybe_log_silence() every beat; it logs "no output for N min" once per
        # log_every_s while nothing arrives, and the snapshot carries silent_s.
        self._last_output_mono = time.monotonic()
        self._last_silence_log = time.monotonic()

    def maybe_log_silence(self) -> bool:
        """Log a "no output for N min" line if the stream has been silent for at least
        ``log_every_s`` since the last output AND since the last such line. Returns True
        when a line was logged. Called from the eval worker's heartbeat; never raises."""
        with self._lock:
            if self._done:
                return False
            now = time.monotonic()
            silent = now - self._last_output_mono
            if silent < self._log_every_s or now - self._last_silence_log < self._log_every_s:
                return False
            self._last_silence_log = now
            snap = self._snapshot_locked()
        logger.warning(
            "grade progress %s: no output for %.1f min (%.1f min elapsed, %d lines, last: %s)",
            self.instance_id,
            silent / 60,
            snap["elapsed_s"] / 60,
            snap["lines"],
            snap["last_line"] or "—",
        )
        return True

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._last_output_mono = time.monotonic()
            self._bytes += len(chunk)
            data = self._partial + chunk
            parts = data.split(b"\n")
            self._partial = parts[-1]
            complete = parts[:-1]
            if complete:
                self._lines += len(complete)
                # the last NON-blank complete line — a trailing blank line must
                # not erase the test name that came just before it
                for raw in reversed(complete):
                    last = raw.decode(errors="replace").rstrip("\r")
                    if last.strip():
                        self._last_line = last[:_LAST_LINE_MAX]
                        break
            now = time.monotonic()
            due = now - self._last_log >= self._log_every_s
            if due:
                self._last_log = now
                delta = self._lines - self._lines_at_last_log
                self._lines_at_last_log = self._lines
                snap = self._snapshot_locked()
        if due:
            logger.info(
                "grade progress %s: %.1f min, %d lines (+%d), last: %s",
                self.instance_id,
                snap["elapsed_s"] / 60,
                snap["lines"],
                delta,
                snap["last_line"] or "—",
            )

    def finish(self) -> None:
        with self._lock:
            self._done = True
            if self._partial.strip():
                self._lines += 1
                self._last_line = self._partial.decode(errors="replace")[:_LAST_LINE_MAX]
                self._partial = b""

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "elapsed_s": round(time.time() - self.started_at, 1),
            "bytes": self._bytes,
            "lines": self._lines,
            "last_line": self._last_line,
            "done": self._done,
            # seconds since the last byte of output (0.0 while the stream flows)
            "silent_s": round(max(0.0, time.monotonic() - self._last_output_mono), 1),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()


def set_current(progress: GradeProgress | None) -> None:
    global _current
    with _lock:
        _current = progress


def current() -> GradeProgress | None:
    with _lock:
        return _current


def _exec_run_with_timeout_tee(container: Any, cmd: str, timeout: int | None = 60) -> Any:
    """SWE-bench 5.0.2's ``exec_run_with_timeout``, with the stream teed.

    Kept line-for-line equivalent in behaviour (thread + join(timeout) +
    kill -TERM of the exec pid on timeout; bytes decoded with replace) so the
    verdict cannot change because of the tee.
    """
    exec_result = b""
    exec_id = None
    exception = None
    timed_out = False
    progress = current()

    def run_command() -> None:
        nonlocal exec_result, exec_id, exception
        try:
            exec_id = container.client.api.exec_create(container.id, cmd)["Id"]
            exec_stream = container.client.api.exec_start(exec_id, stream=True)
            for chunk in exec_stream:
                exec_result += chunk
                if progress is not None:
                    try:
                        progress.feed(chunk)
                    except Exception:
                        logger.debug("grade progress tee failed", exc_info=True)
        except Exception as e:  # noqa: BLE001 - mirrors upstream: re-raised below
            exception = e

    thread = threading.Thread(target=run_command)
    start_time = time.time()
    thread.start()
    thread.join(timeout)
    if exception:
        raise exception
    if thread.is_alive():
        if exec_id is not None:
            exec_pid = container.client.api.exec_inspect(exec_id)["Pid"]
            container.exec_run(f"kill -TERM {exec_pid}", detach=True)
        timed_out = True
    end_time = time.time()
    if progress is not None:
        progress.finish()
    return exec_result.decode(errors="replace"), timed_out, end_time - start_time


def install_exec_tee() -> None:
    """Route SWE-bench's exec stream through the tee. Idempotent.

    Patches the defining module and the name ``run_evaluation`` imported at
    module load — patching only ``docker_utils`` would miss ``run_instance``,
    exactly the ``setup_logger`` lesson in ``swebench_runner``.
    """
    global _TEE_INSTALLED
    if _TEE_INSTALLED:
        return
    from swebench.harness import docker_utils, run_evaluation

    docker_utils.exec_run_with_timeout = _exec_run_with_timeout_tee
    run_evaluation.exec_run_with_timeout = _exec_run_with_timeout_tee
    _TEE_INSTALLED = True
