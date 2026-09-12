"""R2-3 (round-2 review) — git_utils.git_diff times the patch-extraction boundary
and surfaces a timeout as a DISTINCT failure instead of collapsing to None (which
was silently recorded as "the agent produced no patch", discarding its work).
"""

from __future__ import annotations

from unittest import mock

from swebench_eval.harnesses import git_utils


def test_git_diff_times_and_returns_patch(tmp_path) -> None:
    """The shared helper fills out["patch_extract_s"] (M0 §4.3) and returns the
    diff when one exists; the empty-diff case returns None without crashing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    out: dict[str, float] = {}

    # A clean tree -> `git diff --cached HEAD` yields nothing -> None, timed.
    with mock.patch(
        "swebench_eval.harnesses.git_utils.subprocess.run",
        side_effect=[mock.Mock(returncode=0), mock.Mock(returncode=0, stdout=b"", stderr=b"")],
    ):
        patch = git_utils.git_diff(repo, out=out)
    assert patch is None
    assert "patch_extract_s" in out
    assert out["patch_extract_s"] >= 0


def test_git_diff_raises_distinct_outcome_on_timeout(tmp_path) -> None:
    """A `git add -A` that exceeds the 30s timeout raises GitDiffTimeout — a
    distinct outcome from "no patch" (the old `except Exception: return None`
    silently turned a slow stat into an unresolved-with-empty-patch row)."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    out: dict[str, float] = {}

    timeout = subprocess.TimeoutExpired("git add -A", timeout=30)
    with mock.patch("swebench_eval.harnesses.git_utils.subprocess.run", side_effect=timeout):
        try:
            git_utils.git_diff(repo, out=out)
            raise AssertionError("expected GitDiffTimeout")
        except git_utils.GitDiffTimeout as exc:
            assert "timed out" in str(exc)
    # Even on the failure path the timing was recorded (finally runs).
    assert "patch_extract_s" in out


# ---------------------------------------------------------------------------
# R3-2 (round-3 review) — the timeout is classified in-adapter, not propagated
# ---------------------------------------------------------------------------


def test_git_diff_or_classify_converts_timeout_to_flag(tmp_path) -> None:
    """R3-2: git_diff still raises GitDiffTimeout, but the adapter-facing
    git_diff_or_classify converts it into a DiffResult(timed_out=True) with
    patch_extract_s RECORDED (the finally set it) — so the adapter returns a
    normal classified HarnessOutput instead of letting the exception destroy
    this attempt's artifacts + call rows and redeliver a deterministic failure."""
    import subprocess

    from swebench_eval.harnesses.git_utils import git_diff_or_classify

    repo = tmp_path / "repo"
    repo.mkdir()
    timeout = subprocess.TimeoutExpired("git add -A", timeout=30)
    with mock.patch("swebench_eval.harnesses.git_utils.subprocess.run", side_effect=timeout):
        res = git_diff_or_classify(repo)
    assert res.patch is None
    assert res.timed_out is True
    assert (
        res.patch_extract_s is not None and res.patch_extract_s >= 0
    )  # the finally recorded it even on timeout


def test_git_diff_or_classify_surfaces_git_absent(tmp_path) -> None:
    """R3-2 secondary: FileNotFoundError (git absent from the image) used to
    collapse to None-as-no-patch via the blanket except; it is now the same
    distinct classified outcome as a timeout, so it cannot silently destroy an
    attempt."""
    from swebench_eval.harnesses.git_utils import git_diff_or_classify

    repo = tmp_path / "repo"
    repo.mkdir()
    with mock.patch(
        "swebench_eval.harnesses.git_utils.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    ):
        res = git_diff_or_classify(repo)
    assert res.patch is None
    assert res.timed_out is True


# ---------------------------------------------------------------------------
# G2 (2026-09-05) — root extracting a patch from the agent-owned /testbed
# ---------------------------------------------------------------------------


def test_git_diff_opts_out_of_the_ownership_check_and_reads_a_real_diff(tmp_path) -> None:
    """Real git, no mocks: both invocations carry ``-c safe.directory=*`` (git
    refuses another user's repository otherwise — for root too — which is
    exactly the worker's situation after G2 chowns /testbed to the agent) and
    the helper returns the staged diff including a new file."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True, env=env)
    (repo / "a.py").write_text("x = 2\n")
    (repo / "new.py").write_text("y = 1\n")

    real_run = subprocess.run
    seen: list[list[str]] = []

    def spy(cmd, **kw):
        seen.append(list(cmd))
        return real_run(cmd, **kw)

    with mock.patch("swebench_eval.harnesses.git_utils.subprocess.run", side_effect=spy):
        patch = git_utils.git_diff(repo)

    assert patch is not None and "+x = 2" in patch and "new.py" in patch
    assert len(seen) == 2
    for argv in seen:
        assert argv[:3] == ["git", "-c", "safe.directory=*"], argv


def test_git_diff_nonzero_exit_is_a_failure_not_an_empty_patch(tmp_path) -> None:
    """git refusing the repository prints usage to stderr and nothing to stdout
    (exit 129).  That must surface as GitDiffFailed — the empty stdout is NOT
    "the agent produced no patch" (10/10 EMPTY_PATCH in run …-c2ff840c)."""
    from swebench_eval.harnesses.git_utils import git_diff_or_classify

    repo = tmp_path / "repo"
    repo.mkdir()
    refused = mock.Mock(returncode=129, stdout=b"", stderr=b"fatal: detected dubious ownership")
    with mock.patch(
        "swebench_eval.harnesses.git_utils.subprocess.run",
        side_effect=[mock.Mock(returncode=128, stdout=b"", stderr=b""), refused],
    ):
        try:
            git_utils.git_diff(repo)
            raise AssertionError("expected GitDiffFailed")
        except git_utils.GitDiffFailed as exc:
            assert exc.returncode == 129 and "dubious ownership" in str(exc)

    with mock.patch(
        "swebench_eval.harnesses.git_utils.subprocess.run",
        side_effect=[mock.Mock(returncode=128, stdout=b"", stderr=b""), refused],
    ):
        res = git_diff_or_classify(repo)
    assert res.patch is None
    assert res.timed_out is True  # the adapters' "extraction failed" signal
    assert "dubious ownership" in res.error


def test_git_diff_survives_non_utf8_bytes_in_the_staged_tree(tmp_path, caplog) -> None:
    """2026-09-07 (run 05bd05b8, sphinx-9461): the agent's test runs left build
    artefacts in the tree that git diffed as text with raw non-UTF-8 bytes; the
    strict decode raised UnicodeDecodeError past every classifier and 216 turns
    became HARNESS_CRASH.  The diff must come back (with U+FFFD for the
    undecodable bytes, and a warning), never raise."""
    import logging
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True, env=env)
    (repo / "a.py").write_text("x = 2\n")
    # no NUL in the first 8 KB, so git treats it as text and diffs the raw bytes
    (repo / "env.pickle").write_bytes(b"header line\n" + b"\x90\xff\xfe" * 50 + b"\n")

    with caplog.at_level(logging.WARNING, logger=git_utils.__name__):
        patch = git_utils.git_diff(repo)

    assert patch is not None
    assert "+x = 2" in patch and "env.pickle" in patch
    assert "�" in patch  # the undecodable bytes were replaced, not fatal
    assert any("undecodable byte" in r.message for r in caplog.records)
