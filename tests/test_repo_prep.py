"""Repo preparation — the per-instance image sentinel contract (ADR-0043).

The harness worker never prepares a repo at runtime any more (SWE-bench 5.x
publishes one image per instance with /testbed baked; the harness API that
generated ``install_repo_script`` is gone).  What can silently go wrong is now:

  * an empty/unprepared checkout must FAIL the adapter assert (not pass),
  * a missing sentinel must fail closed (never a fallback),
  * a sentinel for another instance must raise naming BOTH ids,
  * a corrupt sentinel must read as absent, never as a match.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swebench_eval.harnesses import repo_prep


def test_ensure_prepared_repo_requires_a_git_checkout(tmp_path: Path) -> None:
    """An empty/unprepared checkout must raise (a prepared env is a hard
    precondition, not something the adapter papered over)."""
    with pytest.raises(RuntimeError, match="no .git"):
        repo_prep.ensure_prepared_repo(tmp_path)

    (tmp_path / ".git").mkdir()
    repo_prep.ensure_prepared_repo(tmp_path)  # must not raise


def test_runtime_repo_prep_is_gone() -> None:
    """ADR-0043: no code path may regenerate/run install_repo_script at runtime."""
    for name in (
        "run_install_repo_script",
        "_build_install_repo_script",
        "configure_git_mirror_redirect",
    ):
        assert not hasattr(
            repo_prep, name
        ), f"{name} must not exist (runtime repo prep was removed)"


# ---------------------------------------------------------------------------
# Per-instance image sentinel (builder1-per-instance-images-build-plan §2a.1/§2a.2)
# ---------------------------------------------------------------------------


def _write_sentinel(tmp_path: Path, instance_id: str) -> Path:
    """Write a valid per-instance sentinel and return its path."""
    p = tmp_path / "testbed-prepared.json"
    p.write_text(
        json.dumps(
            {
                "instance_id": instance_id,
                "base_commit": "abc123",
                "swebench_version": "5.0.2",
                "framework_sha": "0123456789ab",
                "base_kind": "official",
                "base_image_digest": "sha256:" + "a" * 64,
                "prepared_at": "2026-09-05T00:00:00Z",
            }
        )
    )
    return p


def test_verify_testbed_prebaked_returns_sentinel_when_matching(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2a.2 case 4 — sentinel present and matching: returns the sentinel, no raise."""
    sentinel_path = _write_sentinel(tmp_path, "scikit-learn__scikit-learn-25102")
    monkeypatch.setattr(repo_prep, "_SENTINEL_PATH", sentinel_path)

    data = repo_prep.verify_testbed_prebaked("scikit-learn__scikit-learn-25102")
    assert data["instance_id"] == "scikit-learn__scikit-learn-25102"
    assert data["base_image_digest"].startswith("sha256:")


def test_verify_testbed_prebaked_raises_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2a.2 case 2: no sentinel — the image is not a per-instance image; fail
    closed.  Mutation-proved: delete the raise in verify_testbed_prebaked and
    this test fails."""
    monkeypatch.setattr(repo_prep, "_SENTINEL_PATH", tmp_path / "no-such-file.json")

    with pytest.raises(RuntimeError, match="NOT a per-instance image"):
        repo_prep.verify_testbed_prebaked("scikit-learn__scikit-learn-25102")


def test_verify_testbed_prebaked_raises_naming_both_on_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2a.2 case 3: sentinel presents a DIFFERENT instance than the job — the
    wrong-family launch (image for A, job for B). Must raise naming BOTH."""
    sentinel_path = _write_sentinel(tmp_path, "astropy__astropy-12907")  # BAKED image
    monkeypatch.setattr(repo_prep, "_SENTINEL_PATH", sentinel_path)

    with pytest.raises(RuntimeError) as exc:
        repo_prep.verify_testbed_prebaked("scikit-learn__scikit-learn-25102")  # JOB
    msg = str(exc.value)
    assert "scikit-learn__scikit-learn-25102" in msg
    assert "astropy__astropy-12907" in msg


def test_read_testbed_sentinel_none_on_malformed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A corrupt sentinel must read as ABSENT (None), never as a match — the
    sentinel's whole point is to be trustworthy."""
    p = tmp_path / "testbed-prepared.json"
    p.write_text("not json{")
    monkeypatch.setattr(repo_prep, "_SENTINEL_PATH", p)

    assert repo_prep.read_testbed_sentinel() is None
