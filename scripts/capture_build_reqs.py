"""Phase 0b — capture the wheelhouse build-requirements for a prepared repo.

Runs INSIDE the instance-layer docker build at warm time.  The instance image's
/testbed/pyproject.toml holds the FINAL, sed-rewritten build requirements that
eval-time Install B (a PEP 517 isolated build) would fetch from PyPI — which the
harness network cannot reach.  This reads them and leaves /opt/build-reqs.txt
for the wheelhouse `pip download` step.

Run with /app/.venv/bin/python (the framework venv, Python 3.12) — it has
tomllib, and this parse is a pure TOML read that never touches the testbed env
(which is the 3.9 python whose ABI the *wheels* must match, so that stays on
the `conda run -n testbed` side).  Asserting 3.11+ means no fallback: an
unreachable fallback that would silently regex-parse TOML is exactly the
"looks like robustness, cannot be exercised, wrong answer quietly" shape we
keep paying for (blocker-answer §2).
"""

from __future__ import annotations

import pathlib

# tomllib (3.11+) is guaranteed because this runs with /app/.venv/bin/python
# (3.12).  If it somehow runs on an older interpreter, the ImportError IS the
# loud failure — no silent regex fallback (blocker-answer §2).
import tomllib

PYPROJECT = pathlib.Path("/testbed/pyproject.toml")
OUT = pathlib.Path("/opt/build-reqs.txt")
# Per-instance EXTRA requirements (swebench_eval/dataset/extra_wheels.py),
# COPY'd into the build context by build_phase0_instances_v2.py — one line per
# requirement, empty for almost every instance.  They join the same wheelhouse
# download so a gold patch that ADDS a runtime dependency (pylint-4661:
# `appdirs>=1.4.0`) can be installed by the offline `pip install -e .` at grade
# time.  Nothing is installed here; the testbed env stays frozen.
EXTRA = pathlib.Path("/opt/extra-wheels.txt")


def _build_system_requires() -> list[str]:
    if not PYPROJECT.exists():
        print("no /testbed/pyproject.toml; build-reqs empty (grade loop will reveal gaps)")
        return []
    data = tomllib.loads(PYPROJECT.read_text())
    reqs = [str(r) for r in data.get("build-system", {}).get("requires", [])]
    print("build-system.requires from /testbed/pyproject.toml:")
    for r in reqs:
        print(f"  {r}")
    return reqs


def _extra_requires() -> list[str]:
    if not EXTRA.exists():
        return []
    reqs = [line.strip() for line in EXTRA.read_text().splitlines() if line.strip()]
    if reqs:
        print("per-instance extra wheels (instance_extra_wheels.json):")
        for r in reqs:
            print(f"  {r}")
    return reqs


def main() -> int:
    reqs = _build_system_requires() + _extra_requires()
    OUT.write_text("\n".join(reqs) + ("\n" if reqs else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
