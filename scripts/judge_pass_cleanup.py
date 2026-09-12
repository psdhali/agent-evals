#!/usr/bin/env python3
"""Clean up after a judge pass whose process died before finalising (2026-09-08).

A judge task killed mid-pass (ECS stop on an image without the SIGTERM handler, or a
stalled report call the operator had to end) leaves three things behind: the per-model
row in ``judge_pass_lock`` (the next launch is refused with "another judge pass is
already using judge-model"), the pass's LiteLLM virtual key, and its OpenRouter key.
This removes all three by pass id alone; it is idempotent.

Runs INSIDE the VPC (Aurora and the gateway are private) — as a one-off task on the
llm-judge task definition with a command override:

    aws ecs run-task ... --overrides '{"containerOverrides":[{"name":"llm-judge",
      "command":["python","scripts/judge_pass_cleanup.py","--pass-id","judge-…"]}]}'
"""

from __future__ import annotations

import argparse
import json
import sys

from swebench_eval.analysis import judge_keys
from swebench_eval.database.connection import get_connection


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pass-id", required=True)
    args = parser.parse_args()
    conn = get_connection()
    try:
        report = judge_keys.cleanup_pass(conn, args.pass_id)
    finally:
        conn.close()
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
