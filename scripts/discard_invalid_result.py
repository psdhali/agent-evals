"""Discard a result that is not a verdict (A3/E1c, 2026-08-19).

``django__django-10924`` claude_code was recorded ``resolved: true`` against the
agent's own test file: the harness worker had unrestricted internet egress (E9),
the agent downloaded the gold test file from GitHub mid-run, SWE-bench's reset
skips CREATED test files (no pathspec), the gold test_patch failed to apply
("already exists in working directory"), and the run — under
``set -uxo pipefail`` without ``-e`` — graded the agent's file.

That row must not reach Phase 8.  This script flips the eval row for a
run/instance/attempt to ``FAILED_EVAL`` / ``EVAL_GRADE_INVALID`` /
``verdict='invalid'`` with the reason in ``error_detail``.  It is idempotent —
re-running on an already-discarded row is a no-op and reports the current state.

Why a script rather than a direct UPDATE: the database is VPC-private (no public
endpoint, no Data API), so the flip must run from inside the VPC.  At the next
``make eval-up`` the ephemeral IAM role + Aurora ingress are recreated by
Terraform; then run this from the control-plane container (or a ``db-snap``-style
one-off Fargate task) exactly as:

    python scripts/discard_invalid_result.py \
      --run-id e2e-django-claude-1787115789 \
      --instance-id django__django-10924 --attempt 1 \
      --reason "A3 discard (2026-08-19): E1/E2 — graded against the agent's own test file"

The S3 artifacts for the run are retained (annotated with ``_A3_INVALID.txt``)
as evidence of the failure mode, not as a result.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from swebench_eval.database.connection import get_connection

_DEFAULT_REASON = (
    "A3 discard (2026-08-19): graded against the agent's own test file — the gold "
    "test_patch failed to apply ('already exists in working directory') because a "
    "model-authored file at the gold test path survived SWE-bench's reset (created "
    "files are skipped), and the run graded the agent's file. NOT a verdict."
)


def _flip(conn: Any, run_id: str, instance_id: str, attempt: int, reason: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, error_category, verdict FROM instance_results "
            "WHERE run_id=%s AND instance_id=%s AND attempt_number=%s AND phase='eval'",
            (run_id, instance_id, attempt),
        )
        before = cur.fetchone()
        print(f"BEFORE: {before}", flush=True)
        if before is None:
            print("no eval row found — nothing to discard", flush=True)
            return
        if before[0] == "FAILED_EVAL":
            print("already discarded — no-op", flush=True)
            return
        cur.execute(
            "UPDATE instance_results SET state='FAILED_EVAL', "
            "error_category='EVAL_GRADE_INVALID', error_detail=%s, verdict='invalid' "
            "WHERE run_id=%s AND instance_id=%s AND attempt_number=%s AND phase='eval'",
            (reason, run_id, instance_id, attempt),
        )
        print(f"UPDATED rows: {cur.rowcount}", flush=True)
        conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, error_category, verdict FROM instance_results "
            "WHERE run_id=%s AND instance_id=%s AND attempt_number=%s AND phase='eval'",
            (run_id, instance_id, attempt),
        )
        print(f"AFTER: {cur.fetchone()}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--reason", default=_DEFAULT_REASON)
    args = parser.parse_args()

    conn = get_connection()
    try:
        _flip(conn, args.run_id, args.instance_id, args.attempt, args.reason)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
