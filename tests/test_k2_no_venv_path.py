"""K-2 regression: the framework's venv must never shadow the agent's python.

The reviewer's finding (phase-5a-ii-dod1-root-cause-handover): every framework
image set ``ENV PATH="/app/.venv/bin:$PATH"``, so the uv venv (openai, boto3 — the
FRAMEWORK's deps) was first on PATH for every child process, INCLUDING the agent's
``bash`` tool. ``python`` resolved to the framework interpreter, ``pip`` to a pip
that installs where that interpreter never looks, and 5b's ``install_repo_script``
(``conda activate testbed`` + ``pip install -e .[test]``) would build the right env
and still hand the agent the wrong python.

The fix: the venv is NOT on PATH in any image; ``entrypoint.sh`` invokes the venv
binaries by absolute path. This test pins that invariant so the defect cannot be
reintroduced without a CI failure — it does NOT need docker, it inspects the
committed files.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DOCKER_DIR = _ROOT / "infra" / "docker"

# The framework images the harness can run inside. Gateway/dispatch are excluded:
# gateway is the stock LiteLLM image + config, dispatch is the Lambda container.
_FRAMEWORK_DOCKERFILES = [
    "Dockerfile.harness-worker",
    "Dockerfile.orchestrator",
    "Dockerfile.eval-worker",
    "Dockerfile.warm-job",
]

_BANNED_LINE_MARKERS = [
    # The exact ENV directive that put the venv first on PATH (K-2).
    'PATH="/app/.venv/bin:$PATH"',
    'PATH="/app/.venv/bin:',
]


def _dockerfile(path: Path) -> str:
    return path.read_text()


def test_no_framework_image_puts_the_venv_on_path() -> None:
    """No image may prepend /app/.venv/bin to PATH (the K-2 defect)."""
    for name in _FRAMEWORK_DOCKERFILES:
        content = _dockerfile(_DOCKER_DIR / name)
        for marker in _BANNED_LINE_MARKERS:
            assert marker not in content, (
                f"{name} reintroduces the K-2 shadow: {marker!r} puts the framework "
                "uv venv first on PATH for every child process, so the agent's `bash` "
                "tool would get the framework python (openai/boto3) instead of the "
                "repo's, and 5b's install_repo_script would be shadowed too."
            )


def test_entrypoint_invokes_the_venv_by_absolute_path() -> None:
    """The service processes run the venv explicitly, not via a PATH lookup."""
    entrypoint = (_DOCKER_DIR / "entrypoint.sh").read_text()

    # Every framework binary that must come from the venv is referenced by an
    # absolute path — a bare `python`/`uvicorn` would hit the system interpreter,
    # which does not have the framework deps installed.
    assert "/app/.venv/bin/uvicorn" in entrypoint
    assert "/app/.venv/bin/python" in entrypoint


def test_harness_worker_image_still_has_git() -> None:
    """K-2 must not drop 5a-ii's git install — the harness clones the repo."""
    content = _dockerfile(_DOCKER_DIR / "Dockerfile.harness-worker")
    assert "apt-get install" in content
    assert "git" in content
