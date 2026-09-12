"""Test-only harness whose trajectory is deterministically stuck (P4-2/P4-8).

Lives in ``tests/``, NOT in a production module, so test instrumentation can
never survive into the worker (the P4-2 refinement).  Its ``run()`` writes
trajectory events of repeated identical ``bash`` tool calls — the strongest
stuck signal, §9.5 signal 1 — so the stuck-loop detector is *guaranteed* to
fire, not hoped to.

It has a HARD TURN CAP: after ``_TURN_CAP`` turns it completes normally with an
empty patch.  A broken stuck detector is therefore a red test (the worker's
active-stuck path never fires → the DB row is not STUCK), not a wedged run.

Registered in the worker's adapter map ONLY under the ``STUCK_ACTIVE_KILL=1``
env flag (see harness_worker._run_harness).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from swebench_eval.harnesses.base import (
    HarnessAdapter,
    HarnessInput,
    HarnessOutput,
    Usage,
)
from swebench_eval.harnesses.git_utils import git_diff
from swebench_eval.harnesses.repo_prep import ensure_prepared_repo

# Hard cap so a broken detector produces a red test, not a hang (P4-2).
_TURN_CAP = 40
_IDENTICAL_CMD = "ls -la /tmp"  # same command, turns 1..cap


class StuckStubHarness(HarnessAdapter):
    """Write a trajectory of identical tool calls, then exit at the turn cap."""

    def run(self, input: HarnessInput) -> HarnessOutput:
        start = time.monotonic()

        repo_dir = Path(input.repo_checkout_path)
        try:
            ensure_prepared_repo(repo_dir)
        except Exception as exc:  # noqa: BLE001
            return HarnessOutput(
                patch=None,
                success=False,
                trajectory_path="",
                raw_log_path="",
                exit_code=1,
                error=f"repo not prepared: {exc}",
                terminated_reason="crash",
                wall_clock_seconds=time.monotonic() - start,
            )

        output_dir = Path(input.output_dir or repo_dir.parent)
        trajectory_path = str(output_dir / "trajectory.jsonl")
        raw_log_path = str(output_dir / "harness_stdout.log")
        Path(raw_log_path).write_text("stuck stub harness\n")

        # Make one real edit so there IS a partial patch for the forceful kill to
        # capture (ADR-0016: a stuck kill's partial patch is persisted, never
        # graded).  Without this the git diff is empty and there's nothing to tag.
        if repo_dir.is_dir():
            (repo_dir / "stuck_stub_change.txt").write_text("partial progress\n")

        # Write a trajectory of REPEAT_IDENTICAL identical bash calls — no diff
        # change.  The stuck detector must fire on this (signal 1).
        lines: list[str] = []
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(
            json.dumps({"turn": 0, "role": "user", "content": input.problem_statement, "ts": ts})
        )
        for t in range(1, _TURN_CAP + 1):
            lines.append(
                json.dumps(
                    {
                        "turn": t,
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": f"call_{t}",
                                "name": "bash",
                                "arguments": json.dumps({"command": _IDENTICAL_CMD}),
                            }
                        ],
                        "ts": ts,
                    },
                    ensure_ascii=False,
                )
            )
            lines.append(
                json.dumps(
                    {
                        "turn": t,
                        "role": "tool",
                        "tool_call_id": f"call_{t}",
                        "name": "bash",
                        "output": "total 0",
                        "normalized": {"command": _IDENTICAL_CMD},
                        "ts": ts,
                    },
                    ensure_ascii=False,
                )
            )
        Path(trajectory_path).write_text("\n".join(lines) + "\n")

        patch = git_diff(repo_dir)
        return HarnessOutput(
            patch=patch,
            success=bool(patch),
            trajectory_path=trajectory_path,
            raw_log_path=raw_log_path,
            usage=Usage(),
            wall_clock_seconds=time.monotonic() - start,
            exit_code=0,
            error="",
            terminated_reason="completed",
        )
