"""Warm-job task definition — VERIFIED dataset + no dead mirror env (S1).

S2 (updated by warm-job-verified-option-handover.md): the warm job builds the
ACTIVE eval dataset's env surface — VERIFIED (40 envs), which the dispatcher's
keys derive from.  Lite (35) and full (61, ~49 GB it never runs) are both the
wrong surface.  `_dataset_rows()` binds each ENV_DATASET name to its own pin
(the 2026-08-21 Lite@Verified 404; the map fixes it), and the deployed task
must AT LEAST carry the active dataset — an unset env would fall to the
in-code default, also `verified`, but the explicit value is what the deployed
task actually runs, so pin it in the task definition and assert it here (same
shape and reason as the R1 gate-enablement test).

S1: `GIT_MIRROR_ROOT` must NOT be present — the EFS mirror half is gone (the 12
repos are baked into the git-mirror image), and re-adding it would silently
re-arm a gate half that measures dead machinery.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_WARM = _ROOT / "infra/terraform/modules/ec2-task-warm-job/main.tf"


def _src() -> str:
    assert _WARM.exists(), f"missing terraform source: {_WARM}"
    return _WARM.read_text(encoding="utf-8")


def test_warm_job_pins_the_active_verified_dataset() -> None:
    """The deployed warm job builds the VERIFIED split — the dataset the
    dispatcher actually derives env keys from (S2, verified-option-handover)."""
    assert '{ name = "ENV_DATASET", value = "verified" }' in _src()


def test_warm_job_has_no_git_mirror_root_env() -> None:
    """The EFS mirror env ASSIGNMENT is gone (review S1); it must not return.
    (The bare name may appear in the comment explaining the removal.)"""
    src = _src()
    assert '{ name = "GIT_MIRROR_ROOT"' not in src
    assert 'ENV_DATASET", value = "lite"' not in src
