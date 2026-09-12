"""Warm job (ADR-0043): manifest-only, merged from the build tier's records + live ECR.

The env-image build path is gone with SWE-bench 5.x (no env images exist).
What is load-bearing now:

  * ``instance_images`` is exactly the records whose ``-inst`` tag ECR still
    serves at the recorded digest — a deleted or moved tag is DROPPED (the
    manifest is the admission list; it must describe what a dispatch runs),
  * ``--images`` fails loudly instead of silently doing nothing,
  * ``--dry-run`` stays CI-safe (no docker, no S3).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import warm_image_cache as wic

from swebench_eval.cache_manifest import CacheManifest

_A = "sha256:" + "a" * 64
_B = "sha256:" + "b" * 64
_BASE = "sha256:" + "e" * 64


def _records() -> dict[str, dict[str, str]]:
    return {
        "astropy__astropy-12907": {
            "tag": "5.0.2-astropy__astropy-12907-inst",
            "digest": _A,
            "base_image_digest": _BASE,
            "framework_sha": "deadbeef",
        },
        "django__django-10097": {
            "tag": "5.0.2-django__django-10097-inst",
            "digest": _B,
            "base_image_digest": _BASE,
        },
        "gone__gone-1": {
            "tag": "5.0.2-gone__gone-1-inst",
            "digest": _A,
            "base_image_digest": _BASE,
        },
    }


def test_merge_keeps_only_records_ecr_still_serves_at_the_recorded_digest() -> None:
    ecr = {
        "5.0.2-astropy__astropy-12907-inst": _A,  # present, same digest -> kept
        "5.0.2-django__django-10097-inst": _A,  # present but MOVED (record says _B) -> dropped
        # gone__gone-1: not in ECR at all -> dropped
    }
    merged = wic.merge_instance_images(_records(), ecr)
    assert set(merged) == {"astropy__astropy-12907"}
    assert merged["astropy__astropy-12907"] == {
        "tag": "5.0.2-astropy__astropy-12907-inst",
        "digest": _A,
        "base_image_digest": _BASE,
    }


def test_merge_is_the_two_sided_derivation() -> None:
    """Add a record + tag and the entry appears; remove either and it is gone."""
    ecr = {"5.0.2-django__django-10097-inst": _B}
    assert set(wic.merge_instance_images(_records(), ecr)) == {"django__django-10097"}
    assert wic.merge_instance_images({}, ecr) == {}
    assert wic.merge_instance_images(_records(), {}) == {}


def test_write_manifest_publishes_instance_images_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wic, "_swebench_version", lambda: "5.0.2")
    monkeypatch.setattr(wic, "load_instance_records", lambda ver, bucket: _records())
    monkeypatch.setattr(
        wic,
        "_ecr_inst_digests",
        lambda ver: {"5.0.2-astropy__astropy-12907-inst": _A},
    )
    written: dict[str, object] = {}

    def _persist(manifest: object, bucket: str) -> str:
        written["manifest"] = manifest
        written["bucket"] = bucket
        return "cache-manifest/5.0.2.json"

    monkeypatch.setattr(wic, "persist_manifest", _persist)
    assert wic._write_manifest() == "cache-manifest/5.0.2.json"
    manifest = written["manifest"]
    assert isinstance(manifest, CacheManifest)
    assert manifest.swebench_version == "5.0.2"
    assert set(manifest.instance_images) == {"astropy__astropy-12907"}
    assert manifest.env_images == []  # the 4.1.0 halves stay empty


def test_images_mode_is_refused_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wic, "_write_manifest", lambda: pytest.fail("must not write"))
    assert wic.main(["--images"]) == 2


def test_default_mode_writes_the_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _record() -> str:
        calls.append("manifest")
        return "k"

    monkeypatch.setattr(wic, "_write_manifest", _record)
    assert wic.main([]) == 0
    assert calls == ["manifest"]
    assert wic.main(["--manifest"]) == 0


def test_failure_returns_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> str:
        raise RuntimeError("s3 down")

    monkeypatch.setattr(wic, "_write_manifest", _boom)
    assert wic.main(["--manifest"]) == 1


def test_dry_run_is_ci_safe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(wic, "_write_manifest", lambda: pytest.fail("must not write"))
    assert wic.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "manifest-only" in out and "swebench=" in out
