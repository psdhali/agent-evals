"""``--base refresh`` (build_phase0_instances_v2): the framework layer ONLY, FROM the ``-inst``
already promoted in ECR — no Docker Hub pull, no CLI layer, no instance layer, no pip; the
official base's provenance is carried forward from the S3 record, never re-derived.
``--build-only`` stops after the build and leaves the image in the daemon.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_phase0_instances_v2 as p0

_ID = "astropy__astropy-14309"
_INST = f"{p0._REPO_URL}:{p0._VERSION}-{_ID}-inst"
_FINAL = f"{_INST}-official"
_OLD_BASE_DIGEST = "sha256:" + "b" * 64
_OLD_INST_DIGEST = "sha256:" + "0" * 64
_NEW_INST_DIGEST = "sha256:" + "1" * 64
_RECORD = {
    "tag": f"{p0._VERSION}-{_ID}-inst",
    "digest": _OLD_INST_DIGEST,
    "base_image_digest": _OLD_BASE_DIGEST,
    "base_image": "swebench/sweb.eval.x86_64.astropy_1776_astropy-14309:latest",
    "framework_sha": "0" * 40,
}


class _Calls:
    """Records every sh()/sh_quiet() argv; answers docker inspect."""

    def __init__(self) -> None:
        self.cmds: list[list[str]] = []

    def sh(self, cmd: list[str], *, check: bool = True) -> str:
        self.cmds.append(list(cmd))
        return ""

    def sh_quiet(self, cmd: list[str], *, check: bool = True) -> str:
        self.cmds.append(list(cmd))
        if cmd[:3] == ["docker", "image", "inspect"] and "RepoDigests" in cmd[-1]:
            return f"{_INST.rsplit(':', 1)[0]}@{_OLD_INST_DIGEST}\n"
        if cmd[:3] == ["docker", "image", "inspect"]:
            return "1234567890"
        return ""

    def of(self, *head: str) -> list[list[str]]:
        return [c for c in self.cmds if c[: len(head)] == list(head)]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    c = _Calls()
    monkeypatch.setattr(p0, "sh", c.sh)
    monkeypatch.setattr(p0, "sh_quiet", c.sh_quiet)
    monkeypatch.setattr(
        p0, "_ensure_disk_for_build", lambda what, workers: c.cmds.append(["disk", what])
    )
    monkeypatch.setattr(p0, "_FRAMEWORK_SHA", "f" * 40)
    monkeypatch.setattr(p0, "_SUPPORTS_PROGRESS_PLAIN", False)
    monkeypatch.setenv("HARNESS_BUILD_ROOT", "/app")
    return c


@pytest.fixture
def record(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from swebench_eval import cache_manifest

    monkeypatch.setattr(
        cache_manifest, "load_instance_record", lambda ver, iid, bucket=None: dict(_RECORD)
    )
    return dict(_RECORD)


def _mirror() -> dict[str, dict[str, Any]]:
    return {_ID: {"instance_id": _ID, "repo": "astropy/astropy", "base_commit": "abc"}}


def test_refresh_pulls_the_inst_runs_only_the_refresh_layer_pushes_and_promotes(
    calls: _Calls, record: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    promoted: list[str] = []
    written: list[tuple[str, str, dict[str, str]]] = []

    def _promote(iid: str) -> dict[str, str]:
        promoted.append(iid)
        return {"instance": iid, "new_digest": _NEW_INST_DIGEST}

    monkeypatch.setattr(p0, "promote_inst_tag", _promote)
    monkeypatch.setattr(
        p0, "_write_instance_record", lambda iid, d, prov: written.append((iid, d, dict(prov)))
    )

    out = p0._build_one(_ID, _mirror(), 6, promote=True, base="refresh")

    # exactly ONE pull, and it is the promoted -inst from ECR — never the Hub
    pulls = calls.of("docker", "pull")
    assert pulls == [["docker", "pull", _INST]]
    assert not any("swebench/" in " ".join(c) for c in pulls)
    # exactly ONE build: the refresh Dockerfile, FROM the -inst, amd64, tagged -inst-official
    builds = calls.of("docker", "build")
    assert len(builds) == 1
    b = builds[0]
    assert b[b.index("-f") + 1].endswith("Dockerfile.harness-worker-refresh")
    assert f"HW_IMAGE={_INST}" in b
    assert "FRAMEWORK_SHA_BUILD_ARG=" + "f" * 40 in b
    assert b[b.index("-t") + 1] == _FINAL
    assert b[b.index("--platform") + 1] == "linux/amd64"
    assert not any("Dockerfile.instance-layer" in " ".join(c) for c in calls.cmds)
    assert not any("Dockerfile.harness-worker-env" in " ".join(c) for c in calls.cmds)
    # pushed, promoted, and the record keeps the OLD base digest (carried forward)
    assert calls.of("docker", "push") == [["docker", "push", _FINAL]]
    assert promoted == [_ID]
    ((iid, digest, prov),) = written
    assert (iid, digest) == (_ID, _NEW_INST_DIGEST)
    assert prov["BASE_IMAGE_DIGEST"] == _OLD_BASE_DIGEST
    assert prov["BASE_IMAGE_REF"] == _RECORD["base_image"]
    assert prov["REFRESHED_FROM_DIGEST"] == _OLD_INST_DIGEST
    # the disk check still runs (a 1.1 GB pull per worker), the pulled base is reaped
    assert calls.of("disk") == [["disk", _ID]]
    assert ["docker", "rmi", "-f", _FINAL] in calls.cmds
    assert ["docker", "rmi", "-f", _INST] in calls.cmds
    assert not any(c[:3] == ["docker", "rmi", "-f"] and "swebench/" in c[3] for c in calls.cmds)
    assert out["base"] == "refresh"
    assert out["base_image_digest"] == _OLD_BASE_DIGEST
    assert out["refreshed_from_digest"] == _OLD_INST_DIGEST
    assert out["promotion"]["new_digest"] == _NEW_INST_DIGEST


def test_refresh_refuses_to_invent_provenance_without_a_record(
    calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swebench_eval import cache_manifest

    monkeypatch.setattr(cache_manifest, "load_instance_record", lambda ver, iid, bucket=None: None)

    with pytest.raises(RuntimeError, match="cannot invent provenance"):
        p0._build_one(_ID, _mirror(), 6, promote=True, base="refresh")

    # and nothing was pulled or built before the check
    assert calls.of("docker", "pull") == []
    assert calls.of("docker", "build") == []


def test_build_only_builds_but_never_pushes_promotes_or_records(
    calls: _Calls, record: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        p0, "promote_inst_tag", lambda iid: pytest.fail("must not promote under --build-only")
    )
    monkeypatch.setattr(
        p0,
        "_write_instance_record",
        lambda *a: pytest.fail("must not write a record under --build-only"),
    )

    out = p0._build_one(_ID, _mirror(), 6, promote=True, base="refresh", build_only=True)

    assert len(calls.of("docker", "build")) == 1
    assert calls.of("docker", "push") == []
    # the built image stays for inspection; the pulled base is still reaped
    assert ["docker", "rmi", "-f", _FINAL] not in calls.cmds
    assert ["docker", "rmi", "-f", _INST] in calls.cmds
    assert out["build_only"] is True and out["image"] == _FINAL
    assert "promotion" not in out


def test_refresh_record_carries_the_refreshed_from_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    from swebench_eval import cache_manifest

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        cache_manifest,
        "write_instance_record",
        lambda ver, iid, rec, bucket=None: seen.update(rec) or "k",
    )
    p0._write_instance_record(
        _ID,
        _NEW_INST_DIGEST,
        {
            "BASE_IMAGE_DIGEST": _OLD_BASE_DIGEST,
            "BASE_IMAGE_REF": "x",
            "REFRESHED_FROM_DIGEST": _OLD_INST_DIGEST,
        },
    )
    assert seen["base_image_digest"] == _OLD_BASE_DIGEST
    assert seen["refreshed_from_digest"] == _OLD_INST_DIGEST
    assert seen["digest"] == _NEW_INST_DIGEST


def test_official_base_path_is_unchanged_by_the_refresh_flag(
    calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default path still runs the full pipeline (Hub pull → CLI → instance → refresh)."""
    monkeypatch.setattr(
        p0,
        "_prepare_base_official",
        lambda iid, row, phase: (
            f"{p0._REPO_URL}:x-{iid}-inst-clis",
            {"BASE_IMAGE_DIGEST": "sha256:" + "c" * 64, "BASE_IMAGE_REF": "swebench/x"},
        ),
    )
    monkeypatch.setattr(p0, "extra_wheels_for", lambda iid: [])
    monkeypatch.setattr(p0.shutil, "copy", lambda *a, **k: None)
    monkeypatch.setattr(p0, "promote_inst_tag", lambda iid: {"instance": iid, "new_digest": "d"})
    monkeypatch.setattr(p0, "_write_instance_record", lambda *a: None)

    out = p0._build_one(_ID, _mirror(), 6, promote=True)

    builds = calls.of("docker", "build")
    assert [b[b.index("-f") + 1].rsplit("/", 1)[-1] for b in builds] == [
        "Dockerfile.instance-layer",
        "Dockerfile.harness-worker-refresh",
    ]
    assert out["base"] == "official"
    assert "refreshed_from_digest" not in out
