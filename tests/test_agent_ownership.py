"""G2 follow-up (2026-09-06): adapters hand their root-created config dirs to
the agent user.

The first opencode run under G2 died on every instance with
``EACCES: mkdir '<cfg_dir>/opencode'`` — ``tempfile.TemporaryDirectory`` is
0700 root:root whatever the umask, and opencode runs as ``agent``.  These
tests pin the helper's behaviour and that each adapter calls it on the right
path, so the defect cannot come back silently.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from swebench_eval.harnesses import routing
from swebench_eval.harnesses.routing import AgentUser, grant_agent_ownership


def _fake_agent() -> AgentUser:
    return AgentUser(name="agent", uid=os.getuid(), gid=os.getgid(), home="/home/agent")


def test_grant_agent_ownership_is_a_noop_without_an_agent_user(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(routing, "agent_user", lambda: None)
    monkeypatch.setattr(os, "chown", lambda p, u, g: calls.append((str(p), u, g)))
    d = tmp_path / "cfg"
    d.mkdir()
    grant_agent_ownership(d)
    assert calls == []


def test_grant_agent_ownership_skips_a_missing_path(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(routing, "agent_user", _fake_agent)
    monkeypatch.setattr(os, "chown", lambda p, u, g: calls.append((str(p), u, g)))
    grant_agent_ownership(tmp_path / "does-not-exist")
    assert calls == []


def test_grant_agent_ownership_chowns_the_whole_tree(tmp_path, monkeypatch) -> None:
    agent = _fake_agent()
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(routing, "agent_user", lambda: agent)
    monkeypatch.setattr(os, "chown", lambda p, u, g: calls.append((str(p), u, g)))
    d = tmp_path / "cfg"
    (d / "opencode").mkdir(parents=True)
    (d / "opencode" / "opencode.json").write_text("{}")
    grant_agent_ownership(d)
    paths = {c[0] for c in calls}
    assert paths == {str(d), str(d / "opencode"), str(d / "opencode" / "opencode.json")}
    assert all((u, g) == (agent.uid, agent.gid) for _, u, g in calls)


def test_grant_agent_ownership_handles_a_single_file(tmp_path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(routing, "agent_user", _fake_agent)
    monkeypatch.setattr(os, "chown", lambda p, u, g: calls.append(str(p)))
    f = tmp_path / "config.toml"
    f.write_text("x")
    grant_agent_ownership(f)
    assert calls == [str(f)]


def _harness_input(tmp_path: Path, repo: Path):
    from swebench_eval.harnesses.base import HarnessInput, ModelConfig

    return HarnessInput(
        instance_id="scikit-learn__scikit-learn-25102",
        repo_url="https://github.com/scikit-learn/scikit-learn",
        base_commit="c",
        problem_statement="fix it",
        attempt_number=1,
        repo_checkout_path=str(repo),
        output_dir=str(tmp_path),
        timeout_seconds=60,
        model_config=ModelConfig(
            gateway_base_url="http://127.0.0.1:4000/v1",
            gateway_api_key="sk-per-run-secret",
            model_name="cheap-oss-model",
        ),
    )


def test_opencode_hands_its_config_dir_to_the_agent_and_masks_the_key(
    tmp_path, monkeypatch, caplog
) -> None:
    from swebench_eval.harnesses.opencode import harness as oc
    from swebench_eval.harnesses.opencode.harness import OpenCodeHarness

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(oc, "ensure_prepared_repo", lambda _p: None)
    monkeypatch.setattr(
        oc,
        "git_diff_or_classify",
        lambda _p: type("D", (), {"patch": None, "timed_out": False, "patch_extract_s": 0.0})(),
    )
    monkeypatch.setattr(oc, "_export_native_trajectory", lambda *a, **k: True)
    import urllib.request as _ur

    monkeypatch.setattr(_ur, "urlopen", lambda *a, **k: None)

    granted: list[Path] = []
    monkeypatch.setattr(oc, "grant_agent_ownership", lambda p: granted.append(Path(p)))
    seen: dict[str, str] = {}

    def _fake_run_streaming(cmd, cwd, env, timeout, on_line):
        seen["xdg"] = env.get("XDG_CONFIG_HOME", "")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False})()

    monkeypatch.setattr(oc, "run_streaming", _fake_run_streaming)

    with caplog.at_level(logging.INFO, logger="swebench_eval.harnesses.opencode.harness"):
        OpenCodeHarness(api_base_url="http://127.0.0.1:4000/v1", api_key="sk-per-run-secret").run(
            _harness_input(tmp_path, repo)
        )

    # the ownership grant targets exactly the XDG config dir opencode is launched with
    assert granted and str(granted[0]) == seen["xdg"]
    # the config file (which carries the key) is logged, but the key itself never is
    assert any("OPENCODE config content" in r.getMessage() for r in caplog.records)
    assert not any("sk-per-run-secret" in r.getMessage() for r in caplog.records)
    assert any("***REDACTED***" in r.getMessage() for r in caplog.records)
