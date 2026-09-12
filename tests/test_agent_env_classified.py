"""E3 (agent-env-denylist-handover.md §4): every env name in the harness task
definition is CLASSIFIED — either denied by the agent-environment denylist (or
the AWS_/ECS_ prefix rule) or explicitly known-safe.

The one genuine weakness of a denylist is that a variable added to the task def
later is forgotten.  This reads the task definition and asserts every env name
appears in _AGENT_ENV_DENY, matches an AWS_/ECS_ prefix, OR is in the explicit
known-safe set.  Adding a variable to terraform without classifying it then
fails CI.

Proves it fails (DoD #9): add a fake `{ name = "SOMETHING_NEW", value = "x" }`
to the task def and this test trips on it.
"""

from __future__ import annotations

import re
from pathlib import Path

_TASK_DEF = (
    Path(__file__).parent.parent
    / "infra"
    / "terraform"
    / "modules"
    / "ecs-task-def-harness-family"
    / "main.tf"
)

# Known-safe: env vars the task definition sets that are legitimate for the worker
# AND harmless/needed for the agent.  Anything not here and not denied must be
# added to the agent denylist (routing.py).
_KNOWN_SAFE = frozenset(
    {
        "HARNESS",
        "REPO_PREP",
        # master-handover 3b: BLAS thread limits — harmless/needed (pins OpenBLAS
        # to 1 thread so the LAPACK workspace query is stable on a 1-vCPU task).
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        # M1 (review 2026-08-25): disables opencode's file watcher so `opencode
        # run` exits (without it the inotify handle keeps the CLI alive past the
        # run → timeout → FAILED_HARNESS). Harmless/needed — forwarded by
        # agent_environment, also set by the adapter itself.
        "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER",
    }
)


def _task_def_env_names() -> set[str]:
    """All `{ name = "X"` env + secrets names in the harness-family task def."""
    text = _TASK_DEF.read_text()
    return set(re.findall(r'\{ name = "([A-Za-z0-9_]+)"', text))


def test_every_task_def_env_name_is_classified() -> None:
    from swebench_eval.harnesses.routing import (
        _AGENT_ENV_DENY,
        _AGENT_ENV_DENY_PREFIXES,
    )

    names = _task_def_env_names()
    assert names, "no env names parsed from the task def"

    unclassified = set()
    for name in names:
        if name in _AGENT_ENV_DENY or name in _KNOWN_SAFE:
            continue
        if any(name.startswith(p) for p in _AGENT_ENV_DENY_PREFIXES):
            continue
        unclassified.add(name)
    assert not unclassified, (
        f"task-def env name(s) {sorted(unclassified)} are not classified: add each to "
        "_AGENT_ENV_DENY (dangerous to the agent) or _KNOWN_SAFE (harmless/needed) in "
        "swebench_eval/harnesses/routing.py"
    )


def test_specific_names_classified_as_expected() -> None:
    """Spot-check the load-bearing classifications so a wrong 'safe' label cannot
    slip in silently."""
    from swebench_eval.harnesses.routing import (
        _AGENT_ENV_DENY,
        _AGENT_ENV_DENY_PREFIXES,
    )

    # Dangerous -> denied (exact or prefix).
    for name in (
        "LITELLM_MASTER_KEY",
        "LITELLM_BASE_URL",
        "REDIS_URL",
        "RESULTS_QUEUE_URL",
        "DATASET_BUCKET",
        "ARTIFACTS_BUCKET",
        "SQS_QUEUE_PREFIX",
        # G1: the mirror name must never reach the agent (full-history mirror).
        "GIT_MIRROR_URL",
    ):
        assert name in _AGENT_ENV_DENY, name
    assert "AWS_DEFAULT_REGION" not in _AGENT_ENV_DENY  # covered by the prefix rule
    assert any("AWS_DEFAULT_REGION".startswith(p) for p in _AGENT_ENV_DENY_PREFIXES)

    # Safe -> NOT in the deny set and not prefix-matched.
    for name in ("HARNESS", "REPO_PREP"):
        assert name in _KNOWN_SAFE
        assert name not in _AGENT_ENV_DENY
        assert not any(name.startswith(p) for p in _AGENT_ENV_DENY_PREFIXES)
