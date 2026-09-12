"""Shared git helpers for harness adapters.

Repo preparation for 5b is owned by the WORKER via SWE-bench's
``install_repo_script`` (``swebench_eval.harnesses.repo_prep``) — ``clone_and_checkout``
was retired here because it kept ``origin`` + the upstream tip and one command
returned the gold patch (image-environment-pipeline.md §7).  What the adapters
still need from this module is the diff capture below.

`git_diff` is the SINGLE patch-extraction boundary for five of the six harness
adapters (custom_minimal historically duplicated it — see R2-3).  It therefore
owns the ``patch_extract_s`` measurement (M0 §4.3): the interval from "the agent
stopped" to "we hold a patch" is a term in M0 §4.4's phase-sum reconciliation
against ``task_observed_s``.  Timing it here, one layer below the worker, is
what makes ``patch_extract_s`` real instead of NULL.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class GitDiffTimeout(Exception):
    """The git add/diff step exceeded its limit.

    A DISTINCT outcome from "no patch": R2-3 (review) — the old handler caught
    ``subprocess.TimeoutExpired`` (an ``Exception``) and collapsed it to
    ``None``, so a slow ``git add -A`` was silently recorded as "the agent
    produced no patch", discarding the agent's whole work with no error and no
    log line.

    Raising (rather than swallowing) is only HALF the fix — R3-2 (round-3
    review): if it propagates out of the adapter entirely, the worker's
    ``_process_job`` ``except`` catches it and discards every artifact AND the
    llm_calls rows, then redelivers the job five times (deterministic failure,
    so five lots of model spend, all lost).  Adapters therefore catch it via
    :func:`git_diff_or_classify` and return a normal classified ``HarnessOutput``
    so the artifacts and rows are kept and no retry happens.
    """

    def __init__(self, repo_dir: Path) -> None:
        super().__init__(f"git diff timed out (30s) in {repo_dir}")
        self.repo_dir = repo_dir


class GitDiffFailed(Exception):
    """``git diff`` exited non-zero, so its (empty) stdout is NOT a patch.

    G2 found the silent version of this: root's git refusing an agent-owned
    ``/testbed`` ("dubious ownership") printed usage to stderr, exit 129, and
    the empty stdout was recorded as "the agent produced no patch".  The exit
    code and stderr are carried so the log names the real cause.
    """

    def __init__(self, repo_dir: Path, returncode: int, stderr: str) -> None:
        detail = stderr.strip().splitlines()[0] if stderr.strip() else ""
        super().__init__(f"git diff failed rc={returncode} in {repo_dir}: {detail}")
        self.repo_dir = repo_dir
        self.returncode = returncode
        self.stderr = stderr


@dataclass
class DiffResult:
    """The outcome of a :func:`git_diff` for the adapter to classify.

    ``patch`` is None for BOTH "nothing changed" and "timed out" — the two are
    separated by ``timed_out`` so the adapter can return a distinct classified
    ``HarnessOutput`` (R3-2) instead of both collapsing to an empty-patch row.
    A non-zero ``git diff`` exit (:class:`GitDiffFailed`) is reported the same
    way — ``timed_out=True`` is the adapters' "extraction failed, not an empty
    patch" signal — with ``error`` carrying the git message.
    """

    patch: str | None
    patch_extract_s: float | None
    timed_out: bool = False
    error: str = ""


def git_diff_or_classify(repo_dir: Path) -> DiffResult:
    """Run :func:`git_diff`, converting a timeout into a :class:`DiffResult`.

    Called by the adapters inside their ``run()`` (where trajectory/log are
    already on disk).  On timeout it returns ``patch=None, timed_out=True`` with
    ``patch_extract_s`` recorded (the ``finally`` in git_diff set it) so the
    adapter returns a normal ``HarnessOutput`` with a distinct classification —
    artifacts uploaded, call rows shipped, no evidence lost, no retry of a
    deterministic failure.  Also surfaces the sibling failures the blanket
    ``except Exception`` used to hide (git absent from the image =
    ``FileNotFoundError``, ``NotADirectoryError``, ``PermissionError``) as the
    same ``timed_out`` outcome so they cannot silently destroy an attempt.
    """
    out: dict[str, float] = {}
    try:
        patch = git_diff(repo_dir, out=out)
        return DiffResult(patch=patch, patch_extract_s=out.get("patch_extract_s"), timed_out=False)
    except GitDiffTimeout:
        return DiffResult(patch=None, patch_extract_s=out.get("patch_extract_s"), timed_out=True)
    except GitDiffFailed as exc:
        logger.error("patch extraction failed: %s\n%s", exc, exc.stderr.strip()[-2000:])
        return DiffResult(
            patch=None,
            patch_extract_s=out.get("patch_extract_s"),
            timed_out=True,
            error=str(exc),
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        # R3-2 secondary: these used to collapse to None-as-no-patch; treat them
        # the same as a timeout — a distinct, non-retryable classified outcome.
        return DiffResult(patch=None, patch_extract_s=out.get("patch_extract_s"), timed_out=True)


def git_diff(repo_dir: Path, out: dict[str, Any] | None = None) -> str | None:
    """Get the working-tree diff, including newly created files.

    Stages all changes first so that file-creation (untracked files) is
    included in the diff.

    ``out``, when given, is filled with ``{"patch_extract_s": <seconds>}`` (M0
    §4.3 / R2-3): the wall time of the whole ``git add -A; git diff --cached
    HEAD`` pair.  A value pinned near the 30s timeout is exactly the signal that
    a slow working-tree stat is being paid.

    Raises :class:`GitDiffTimeout` when either subprocess exceeds the timeout —
    that is a real failure to surface, never a silent ``None``-as-no-patch.
    Raises :class:`GitDiffFailed` when ``git diff`` itself exits non-zero.

    G2 (2026-09-05): the worker runs as root while the agent ran as the
    unprivileged ``agent`` user, so ``/testbed`` is agent-owned when this
    runs.  git >= 2.35.2 refuses a repository owned by another user ("dubious
    ownership") — for root too — and ``git diff`` then prints usage to stderr
    and NOTHING to stdout.  Before this fix that empty stdout was returned as
    "no patch" and every G2 run graded EMPTY_PATCH (run …-c2ff840c, 10/10
    completions).  ``safe.directory=*`` is the documented opt-out and is safe
    here: the worker is the trusted process and the directory is its own
    scratch checkout.  The exit code is now checked as well, so a future git
    refusal of any kind is a classified failure with its stderr in the log,
    never a silent empty patch.
    """
    start = time.monotonic()
    git = ["git", "-c", "safe.directory=*"]
    try:
        subprocess.run(  # noqa: PLW1510
            [*git, "add", "-A"],
            cwd=repo_dir,
            capture_output=True,
            timeout=30,
        )
        result = subprocess.run(  # noqa: PLW1510
            [*git, "diff", "--cached", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitDiffTimeout(repo_dir) from exc
    finally:
        if out is not None:
            out["patch_extract_s"] = time.monotonic() - start
    # 2026-09-07 (run 05bd05b8, sphinx-9461): the agent's test runs left build
    # artefacts (doctree pickles) in the tree; `git add -A` staged them and, for
    # a file git judged as text, `git diff` emitted its raw bytes.  Decoding that
    # stdout as strict UTF-8 raised UnicodeDecodeError — nothing above caught
    # it, so 216 turns of work became HARNESS_CRASH.  Decode leniently: the
    # patch is kept (a hunk with replacement characters simply will not apply,
    # which the grade then reports honestly) and the substitution is logged.
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise GitDiffFailed(repo_dir, result.returncode, stderr)
    replaced = stdout.count("�")
    if replaced:
        logger.warning(
            "git diff in %s contained %d undecodable byte(s) (binary or non-UTF-8 content "
            "staged by the agent); replaced with U+FFFD so the patch could be captured",
            repo_dir,
            replaced,
        )
    return stdout if stdout.strip() else None
