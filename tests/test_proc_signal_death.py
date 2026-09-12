"""An agent that dies by a signal we did not send gets a process-tree post-mortem (task #72)."""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from swebench_eval.harnesses import proc
from swebench_eval.harnesses.proc import run_streaming


def test_signal_death_logs_the_container_process_tree(caplog: Any) -> None:
    with caplog.at_level(logging.WARNING, logger=proc.__name__):
        result = run_streaming(
            ["bash", "-c", "kill -TERM $$"],
            cwd="/tmp",
            env={"PATH": "/usr/bin:/bin"},
            timeout=30,
        )
    assert result.returncode == -15
    assert result.timed_out is False and result.aborted is False
    msgs = [r.message for r in caplog.records if "process tree" in r.message]
    assert len(msgs) == 1
    assert "SIGTERM" in msgs[0]
    assert "PID" in msgs[0].upper()  # the ps header made it into the log


def test_clean_exit_and_nonzero_exit_log_no_post_mortem(caplog: Any) -> None:
    with caplog.at_level(logging.WARNING, logger=proc.__name__):
        ok = run_streaming(
            ["bash", "-c", "exit 0"], cwd="/tmp", env={"PATH": "/usr/bin:/bin"}, timeout=10
        )
        bad = run_streaming(
            ["bash", "-c", "exit 3"], cwd="/tmp", env={"PATH": "/usr/bin:/bin"}, timeout=10
        )
    assert ok.returncode == 0 and bad.returncode == 3
    assert not [r for r in caplog.records if "process tree" in r.message]


def test_timeout_kill_is_ours_and_logs_no_post_mortem(caplog: Any) -> None:
    with caplog.at_level(logging.WARNING, logger=proc.__name__):
        r = run_streaming(
            ["bash", "-c", "sleep 30"], cwd="/tmp", env={"PATH": "/usr/bin:/bin"}, timeout=1
        )
    assert r.timed_out is True and r.returncode < 0
    assert not [x for x in caplog.records if "process tree" in x.message]


def test_post_mortem_never_raises_when_ps_is_missing(monkeypatch: Any, caplog: Any) -> None:
    def _boom(*a: Any, **k: Any) -> Any:
        raise FileNotFoundError("ps")

    monkeypatch.setattr(subprocess, "run", _boom)  # proc does `import subprocess`
    with caplog.at_level(logging.WARNING, logger=proc.__name__):
        proc._log_process_tree("mini-swe-agent", -9)
    assert any("ps unavailable" in r.message and "SIGKILL" in r.message for r in caplog.records)
