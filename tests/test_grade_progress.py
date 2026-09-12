"""Live grade progress: the exec-stream tee (2026-09-06, django-10097's silent 67-min grade)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from swebench_eval.evaluation import grade_progress as gp


class _Api:
    def __init__(self, chunks: list[bytes], *, block_s: float = 0.0) -> None:
        self._chunks = chunks
        self._block_s = block_s
        self.killed: list[str] = []

    def exec_create(self, container_id: str, cmd: str) -> dict[str, str]:
        return {"Id": "exec-1"}

    def exec_start(self, exec_id: str, stream: bool = True):
        for c in self._chunks:
            if self._block_s:
                time.sleep(self._block_s)
            yield c

    def exec_inspect(self, exec_id: str) -> dict[str, int]:
        return {"Pid": 4242}


class _Container:
    def __init__(self, api: _Api) -> None:
        self.id = "c1"
        self.client = type("C", (), {"api": api})()
        self.exec_runs: list[str] = []

    def exec_run(self, cmd: str, detach: bool = False) -> None:
        self.exec_runs.append(cmd)


def test_feed_counts_lines_and_keeps_the_last_complete_line() -> None:
    p = gp.GradeProgress("django__django-10097", log_every_s=9999)
    first = b"test_a ... ok\ntest_b ... "
    p.feed(first)
    s = p.snapshot()
    assert s["lines"] == 1 and s["last_line"] == "test_a ... ok" and s["bytes"] == len(first)
    p.feed(b"ok\n\n")
    s = p.snapshot()
    assert s["lines"] == 3  # "test_b ... ok" + the empty line
    assert s["last_line"] == "test_b ... ok"  # blank lines never replace the last line
    p.finish()
    assert p.snapshot()["done"] is True


def test_finish_flushes_a_trailing_partial_line() -> None:
    p = gp.GradeProgress("x", log_every_s=9999)
    p.feed(b"Ran 42 tests in 3.1s")
    p.finish()
    s = p.snapshot()
    assert s["lines"] == 1 and s["last_line"] == "Ran 42 tests in 3.1s"


def test_periodic_progress_line_is_logged(caplog: Any) -> None:
    p = gp.GradeProgress("sympy__sympy-1", log_every_s=0.0)
    with caplog.at_level(logging.INFO, logger=gp.__name__):
        p.feed(b"one\ntwo\n")
    assert any("grade progress sympy__sympy-1" in r.message for r in caplog.records)


def test_tee_returns_exactly_what_upstream_returns_and_feeds_progress() -> None:
    api = _Api([b"hello\n", b"wor", b"ld\n", b"\xff bad byte\n"])
    c = _Container(api)
    p = gp.GradeProgress("i", log_every_s=9999)
    gp.set_current(p)
    try:
        out, timed_out, elapsed = gp._exec_run_with_timeout_tee(c, "/bin/bash /eval.sh", 30)
    finally:
        gp.set_current(None)
    assert out == "hello\nworld\n� bad byte\n"
    assert timed_out is False and elapsed >= 0
    s = p.snapshot()
    assert s["lines"] == 3 and s["last_line"] == "� bad byte" and s["done"] is True


def test_tee_without_a_current_progress_is_a_pure_passthrough() -> None:
    api = _Api([b"a\n"])
    out, timed_out, _ = gp._exec_run_with_timeout_tee(_Container(api), "cmd", 5)
    assert out == "a\n" and timed_out is False


def test_tee_timeout_kills_the_exec_pid_like_upstream() -> None:
    api = _Api([b"slow\n"] * 50, block_s=0.05)
    c = _Container(api)
    # upstream annotates timeout as int, but it only ever reaches Thread.join(), which takes
    # a float; 0.2 s keeps this test fast without changing what it proves.
    out, timed_out, _ = gp._exec_run_with_timeout_tee(c, "cmd", 0.2)  # type: ignore[arg-type]
    assert timed_out is True
    assert c.exec_runs == ["kill -TERM 4242"]
    assert out.startswith("slow\n")


def test_tee_reraises_the_stream_exception() -> None:
    class _Boom(_Api):
        def exec_start(self, exec_id: str, stream: bool = True):
            raise RuntimeError("409 Conflict")
            yield b""  # pragma: no cover

    try:
        gp._exec_run_with_timeout_tee(_Container(_Boom([])), "cmd", 5)
    except RuntimeError as exc:
        assert "409" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected the stream exception to propagate")


def test_install_patches_both_names_idempotently() -> None:
    from swebench.harness import docker_utils, run_evaluation

    orig_du = docker_utils.exec_run_with_timeout
    orig_re = run_evaluation.exec_run_with_timeout
    try:
        gp._TEE_INSTALLED = False
        gp.install_exec_tee()
        assert docker_utils.exec_run_with_timeout is gp._exec_run_with_timeout_tee
        assert run_evaluation.exec_run_with_timeout is gp._exec_run_with_timeout_tee
        gp.install_exec_tee()  # second call is a no-op
        assert run_evaluation.exec_run_with_timeout is gp._exec_run_with_timeout_tee
    finally:
        docker_utils.exec_run_with_timeout = orig_du
        run_evaluation.exec_run_with_timeout = orig_re
        gp._TEE_INSTALLED = False


def test_current_is_thread_safe_to_set_and_clear() -> None:
    p = gp.GradeProgress("i", log_every_s=9999)
    errors: list[BaseException] = []

    def flip() -> None:
        try:
            for _ in range(200):
                gp.set_current(p)
                gp.current()
                gp.set_current(None)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=flip) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors and gp.current() is None


def test_silence_is_logged_once_per_interval_and_reported_in_the_snapshot(
    caplog: Any, monkeypatch: Any
) -> None:
    """2026-09-08: a hung suite used to go silent in CloudWatch after its last line."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])  # gp does `import time`
    p = gp.GradeProgress("django__django-10097", log_every_s=60.0)
    p.feed(b"one\n")
    assert p.snapshot()["silent_s"] == 0.0
    with caplog.at_level(logging.WARNING, logger=gp.__name__):
        clock["t"] += 30
        assert p.maybe_log_silence() is False  # not yet
        clock["t"] += 31
        assert p.maybe_log_silence() is True  # 61 s silent
        assert p.maybe_log_silence() is False  # once per interval
        clock["t"] += 60
        assert p.maybe_log_silence() is True
    lines = [r.message for r in caplog.records if "no output for" in r.message]
    assert len(lines) == 2 and "django__django-10097" in lines[0] and "last: one" in lines[0]
    assert p.snapshot()["silent_s"] == 121.0
    # new output resets the silence; a finished grade never logs
    p.feed(b"two\n")
    assert p.snapshot()["silent_s"] == 0.0
    clock["t"] += 600
    p.finish()
    assert p.maybe_log_silence() is False
