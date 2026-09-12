"""Guard: the git-mirror seed list and the live dataset name the same repos.

The mirror list and the dataset drifted (PyCQA/pylint vs pylint-dev/pylint) for
months and nothing noticed until every pylint instance failed in repo prep.
These tests make a re-rename impossible.

The split matters: the unit test runs in CI with no network (the socket guard
would block a real clone anyway — it is pure text parsing), and the integration
test keeps the hardcoded list honest against the live dataset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "docker" / "seed_mirrors.sh"

# The 12 distinct repos across Lite/Verified/full (seed_mirrors.sh:20-33).  If
# SWE-bench ever renames another org, BOTH tests below fail together.
EXPECTED_REPOS = frozenset(
    {
        "django/django",
        "sympy/sympy",
        "astropy/astropy",
        "scikit-learn/scikit-learn",
        "matplotlib/matplotlib",
        "sphinx-doc/sphinx",
        "pydata/xarray",
        "mwaskom/seaborn",
        "pytest-dev/pytest",
        "pylint-dev/pylint",
        "psf/requests",
        "pallets/flask",
    }
)


def _script_repos() -> set[str]:
    """Parse the ``REPOS=(...)`` array out of seed_mirrors.sh (no execution)."""
    text = SCRIPT.read_text()
    start = text.index("REPOS=(") + len("REPOS=(")
    end = text.index(")", start)
    return {line.strip() for line in text[start:end].strip().splitlines() if line.strip()}


def test_seed_mirrors_list_is_exact_twelve() -> None:
    """The mirror must seed exactly the dataset's repo set — no drift, no extras."""
    assert _script_repos() == EXPECTED_REPOS, (
        f"seed_mirrors.sh REPOS drifted from the dataset set.\n"
        f"  missing: {sorted(EXPECTED_REPOS - _script_repos())}\n"
        f"  extra:   {sorted(_script_repos() - EXPECTED_REPOS)}"
    )


def test_seed_mirrors_line_30_is_pylint_dev() -> None:
    """Line 30 names pylint-dev, not the old PyCQA org (the 2026-08-21 bug)."""
    lines = SCRIPT.read_text().splitlines()
    # Find the pylint entry by content, not by line number, so the assertion
    # survives a reorder.
    pylint_line = next(ln for ln in lines if "pylint" in ln and "REPOS" not in ln)
    assert pylint_line.strip() == "pylint-dev/pylint", pylint_line
    assert "PyCQA" not in SCRIPT.read_text(), "old PyCQA org leaked back into seed_mirrors.sh"


@pytest.mark.integration
def test_seed_mirrors_match_live_dataset() -> None:
    """The hardcoded list must equal the repos in the live dataset (the point)."""
    from swebench_eval.dataset.swebench_loader import SwebenchLiteLoader

    loader = SwebenchLiteLoader(include_gold=True)
    dataset_repos = {inst.repo for inst in loader.load()}
    assert _script_repos() == dataset_repos, (
        f"seed list drifted from live dataset.\n"
        f"  mirrored but not in dataset: {sorted(_script_repos() - dataset_repos)}\n"
        f"  in dataset but not mirrored: {sorted(dataset_repos - _script_repos())}"
    )
