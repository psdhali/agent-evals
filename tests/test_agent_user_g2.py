"""G2 (HARNESS-ISOLATION-AUDIT-2026-09-05 §3): the agent subprocess runs as an
unprivileged user; the worker hands it the trees it needs.  No real uid switch
here — the properties under test are the decision logic and the plumbing
(Popen kwargs, HOME override, chown calls), driven with fakes."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from swebench_eval.harnesses import proc, routing


@pytest.fixture(autouse=True)
def _reset_warned() -> None:
    routing._agent_user_warned = False


def _pwd_with(name: str, uid: int = 1000) -> Any:
    def getpwnam(n: str) -> Any:
        if n != name:
            raise KeyError(n)
        return SimpleNamespace(pw_uid=uid, pw_gid=uid, pw_dir=f"/home/{name}")

    return SimpleNamespace(getpwnam=getpwnam)


def test_agent_user_resolves_when_root_and_user_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_AGENT_USER", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setitem(__import__("sys").modules, "pwd", _pwd_with("agent"))
    u = routing.agent_user()
    assert u == routing.AgentUser(name="agent", uid=1000, gid=1000, home="/home/agent")
    assert routing.agent_spawn_kwargs() == {"user": 1000, "group": 1000, "extra_groups": []}
    env = routing.agent_environment()
    assert env["HOME"] == "/home/agent" and env["USER"] == "agent" and env["LOGNAME"] == "agent"


def test_agent_user_off_when_not_root_or_missing_or_disabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(__import__("sys").modules, "pwd", _pwd_with("agent"))
    # not root: cannot switch
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    assert routing.agent_user() is None and routing.agent_spawn_kwargs() == {}
    # root, but the image has no such user (pre-G2 image): warned once, root agent
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("HARNESS_AGENT_USER", "nobody-here")
    with caplog.at_level("WARNING"):
        assert routing.agent_user() is None
        assert routing.agent_user() is None
    assert sum("does not exist" in r.message for r in caplog.records) == 1
    # explicitly off
    monkeypatch.setenv("HARNESS_AGENT_USER", "root")
    assert routing.agent_user() is None
    assert "HOME" not in routing.agent_environment() or routing.agent_environment()[
        "HOME"
    ] == os.environ.get("HOME")


def test_run_streaming_passes_the_drop_privileges_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _P:
        stdout = iter(["hello\n"])
        stderr: Iterator[str] = iter([])
        returncode = 0

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def fake_popen(cmd: Any, **kw: Any) -> _P:
        seen.update(kw)
        return _P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)  # proc does `import subprocess`
    monkeypatch.setattr(
        routing, "agent_spawn_kwargs", lambda: {"user": 1000, "group": 1000, "extra_groups": []}
    )
    proc.run_streaming(["true"], cwd="/", env={}, timeout=5)
    assert seen["user"] == 1000 and seen["group"] == 1000 and seen["extra_groups"] == []


def test_grant_agent_access_chowns_and_opens_umask(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from swebench_eval.workers import harness_worker as hw

    monkeypatch.setattr(
        routing, "agent_user", lambda: routing.AgentUser("agent", 1000, 1000, "/home/agent")
    )
    calls: list[list[str]] = []

    def _run(cmd: list[str], **kw: Any) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(
        hw.subprocess if hasattr(hw, "subprocess") else __import__("subprocess"), "run", _run
    )
    umasks: list[int] = []

    def _umask(m: int) -> int:
        umasks.append(m)
        return 0o022

    monkeypatch.setattr(os, "umask", _umask)
    missing = tmp_path / "missing"
    hw.grant_agent_access([tmp_path, missing])
    assert calls == [["chown", "-R", "1000:1000", str(tmp_path)]]  # absent paths skipped
    assert umasks == [0]


def test_grant_agent_access_is_a_noop_without_separation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from swebench_eval.workers import harness_worker as hw

    monkeypatch.setattr(routing, "agent_user", lambda: None)
    with mock.patch("subprocess.run") as run:
        hw.grant_agent_access([tmp_path])
    run.assert_not_called()
