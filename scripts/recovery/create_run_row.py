"""RECOVERY tool: backfill the ``runs`` / ``run_targets`` rows for a stray run.

**This is not part of the normal workflow.** Item 0.3/0.4 retired
hand-launched dispatch — the only sanctioned path is the S3-triggered dispatcher
(ADR-0024), which inserts the ``runs`` row itself. This script exists purely to
recover from a run that (for whatever reason) bypassed the dispatcher, so its
artifacts don't dead-letter.

Why: every artifact row (``instance_results``, ``llm_calls``) has a NOT NULL FK
to ``runs(run_id)``.  A manual ``aws ecs run-task`` with the reference in
``containerOverrides`` bypasses the dispatcher, so NO ``runs`` row exists; the
results_writer then hits ``ForeignKeyViolation: instance_results_run_id_fkey``,
redelivers seven times and DLQs, and the eval job is never enqueued (R4,
2026-08-24).

This is idempotent (``ON CONFLICT DO NOTHING``).  It mirrors
``dispatcher.py:270-278`` exactly (the two INSERTs it performs); it deliberately
does NOT call ``dispatcher.register_run`` so a hand-scoped script does not pull
in the control-state / Valkey publish that full-run start does.

Usage (recovery only)::

    python -m scripts.recovery.create_run_row \
        --run-id <run_id> --harness <harness> --model-alias <alias> \
        [--config-snapshot snapshot.json]
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("create_run_row")


def create_run_row(
    run_id: str,
    harness: str,
    model_alias: str,
    config_snapshot: dict[str, Any] | None = None,
) -> bool:
    """Insert the ``runs`` + ``run_targets`` rows idempotently.

    Returns True if a new ``runs`` row was created, False if it already existed
    (so a caller can tell "registered now" from "was already registered").
    """
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    created = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO runs (run_id, config_snapshot, status)
                   VALUES (%s, %s, 'running')
                   ON CONFLICT (run_id) DO NOTHING""",
                (run_id, json.dumps(config_snapshot or {})),
            )
            # rowcount > 0 → this INSERT actually created a row.
            created = bool(cur.rowcount > 0)
            cur.execute(
                """INSERT INTO run_targets (run_id, harness, model_alias)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (run_id, harness, model_alias),
            )
        conn.commit()
    finally:
        conn.close()
    return created


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "RECOVERY tool: create the runs/run_targets rows for a run that "
            "bypassed the dispatcher (R4.1). Normal path is S3-dispatch "
            "(ADR-0024), which inserts these rows itself."
        )
    )
    parser.add_argument("--run-id", required=True, help="The run_id (matches containerOverrides).")
    parser.add_argument(
        "--harness", required=True, help="Harness name (custom_minimal, claude_code, …)."
    )
    parser.add_argument("--model-alias", required=True, help="Gateway model alias.")
    parser.add_argument(
        "--config-snapshot",
        help="Optional JSON file to store in runs.config_snapshot (e.g. a reproducibility snapshot).",
    )
    args = parser.parse_args()

    snapshot: dict[str, Any] | None = None
    if args.config_snapshot:
        try:
            snapshot = json.loads(Path(args.config_snapshot).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("could not read --config-snapshot %s: %s", args.config_snapshot, exc)
            return 2

    try:
        created = create_run_row(args.run_id, args.harness, args.model_alias, snapshot)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not create run row (is AWS_AUTH/database reachable?): %s", exc)
        return 1

    logger.info(
        "%s runs row for %s (%s/%s)",
        "created" if created else "already had a",
        args.run_id,
        args.harness,
        args.model_alias,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
