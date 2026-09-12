"""scripts/local_instance_image.py — the laptop per-instance image helper (adoption F3)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import local_instance_image as lii


def test_base_reference_is_the_pinned_digest_never_the_tag() -> None:
    pull_ref, row_image, digest = lii.base_reference("django__django-11099")
    assert digest.startswith("sha256:")
    assert pull_ref == f"swebench/sweb.eval.x86_64.django_1776_django-11099@{digest}"
    assert row_image == "swebench/sweb.eval.x86_64.django_1776_django-11099:latest"


def test_base_reference_rejects_unknown_instance() -> None:
    with pytest.raises(SystemExit):
        lii.base_reference("nobody__nothing-1")


def test_local_tag_carries_harness_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SWEBENCH_VERSION", raising=False)
    assert lii.local_tag("django__django-11099") == "swebench-eval-local:5.0.2-django__django-11099"


def test_host_url_rewrites_loopback_for_the_container() -> None:
    assert lii._host_url("http://localhost:4000/v1") == "http://host.docker.internal:4000/v1"
    assert lii._host_url("http://127.0.0.1:9000") == "http://host.docker.internal:9000"
    assert lii._host_url("http://minio:9000") == "http://minio:9000"


def test_run_local_job_builds_the_docker_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(
        cmd: list[str], check: bool = False, **_: object
    ) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost:4000/v1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test")
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("EVAL_PACER_ENABLED", raising=False)
    rc = lii.run_local_job(
        "django__django-11099", tmp_path / "out", "cheap-oss-model", image="img:x"
    )
    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "--platform" in cmd and cmd[cmd.index("--platform") + 1] == "linux/amd64"
    assert f"{(tmp_path / 'out').resolve()}:/out" in cmd
    assert "LITELLM_BASE_URL=http://host.docker.internal:4000/v1" in cmd
    assert "EVAL_PACER_ENABLED=0" in cmd  # no shared Redis ledger on a laptop
    assert "HARNESS_AGENT_USER=agent" in cmd  # G2 parity
    assert cmd[cmd.index("img:x") + 1] == "local-job"
    assert "--instance-id" in cmd and "django__django-11099" in cmd
