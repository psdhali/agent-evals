#!/usr/bin/env python3
"""V6 (switch-to-swebench-verified §8) — how often the token ceiling binds.

We cannot match the published budgets (Poolside ran Laguna at 500 steps, Qwen at
300 turns — that order of magnitude needs ~100M+ tokens).  The defensible
position is to MEASURE whether OUR per-instance ceiling constrains anything, and
quantify the gap either way:

  - low bind-rate  -> the cap is NOT the explanation for any gap vs 70.9/71.1%,
                      and we can say so with evidence;
  - high bind-rate -> exactly how much of the gap the cap accounts for.

Either answer is publishable; not knowing is not.  The data already exists, in
two places:

  * ``instance_results`` — ``error_category = HARNESS_BUDGET_EXCEEDED`` /
    ``state = BUDGET_EXCEEDED`` (the worker set this when the shim tripped);
  * ``llm_calls`` — per-call tokens, summed per attempt for the cumulative figure
    to compare against ``runs.config_snapshot.max_tokens_per_instance``.

Harness and model come from ``llm_calls`` (the shim records ``harness`` and
``model_resolved`` per call), so an attempt is attributed exactly to the harness
that made its calls, with no run_targets ambiguity.

Usage:
  .venv/bin/python scripts/ceiling_bind_rate.py [--run-id <run_id>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swebench_eval.database.connection import get_connection

# Config_snapshot values come out as strings; the RunConfig field is int|None.
_QUERY = """
WITH ceilings AS (
    SELECT run_id,
           NULLIF((config_snapshot->>'max_tokens_per_instance'), '')::bigint AS ceiling_tokens
      FROM runs
),
per_attempt AS (
    SELECT run_id, harness,
           COALESCE(model_resolved, model_requested) AS model_label,
           instance_id, attempt_number,
           COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0)::bigint AS tokens
      FROM llm_calls
     GROUP BY run_id, harness, COALESCE(model_resolved, model_requested),
              instance_id, attempt_number
),
budget_rows AS (
    SELECT DISTINCT run_id, instance_id, attempt_number
      FROM instance_results
     WHERE phase = 'harness'
       AND (error_category = 'HARNESS_BUDGET_EXCEEDED' OR state = 'BUDGET_EXCEEDED')
)
SELECT pa.harness,
       pa.model_label,
       COUNT(*)                                              AS attempts,
       COUNT(*) FILTER (WHERE b.run_id IS NOT NULL)          AS budget_exceeded,
       COUNT(*) FILTER (
           WHERE c.ceiling_tokens IS NOT NULL
             AND pa.tokens >= c.ceiling_tokens)              AS over_ceiling_by_tokens,
       COALESCE(AVG(c.ceiling_tokens), 0)::bigint            AS ceiling_tokens
  FROM per_attempt pa
  JOIN ceilings c        ON c.run_id = pa.run_id
  LEFT JOIN budget_rows b ON b.run_id = pa.run_id
                        AND b.instance_id = pa.instance_id
                        AND b.attempt_number = pa.attempt_number
 WHERE (%(run_id)s IS NULL OR pa.run_id = %(run_id)s)
 GROUP BY pa.harness, pa.model_label
 ORDER BY pa.harness, pa.model_label
"""


def report(run_id: str | None = None) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_QUERY, {"run_id": run_id})
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("No attempts found." + (f" for run {run_id}" if run_id else ""))
        return 0

    header = f"{'harness':<16}{'model':<28}{'attempts':>9}{'budget_call':>13}{'over_tok':>10}{'bind%':>8}{'ceiling':>10}"
    print(header)
    print("-" * len(header))
    for harness, model_label, attempts, budget_exceeded, over_ceiling, ceiling in rows:
        # "bound" = the shim tripped the budget, OR cumulative tokens met the cap.
        # budget_exceeded is authoritative; over_ceiling is the token-level cross-check.
        bound = max(budget_exceeded, over_ceiling)
        pct = (100.0 * bound / attempts) if attempts else 0.0
        print(
            f"{harness:<16}{model_label:<28}{attempts:>9}{budget_exceeded:>13}"
            f"{over_ceiling:>10}{pct:>7.1f}%{ceiling:>10,}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=None, help="restrict to one run_id (default: all)")
    args = parser.parse_args()
    return report(run_id=args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
