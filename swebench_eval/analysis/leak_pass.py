"""Pass A entry point for the offline analysis CLI (offline-analysis-design.md §2,
extended §10.5).

Thin wrapper over ``scripts/backfill_leak_detection.backfill_run`` — the
backfill logic itself already lives there (B5/DoD #2's "the backfill path is
what makes the probe deferrable"), this just gives Pass A a stable import
path under ``swebench_eval.analysis`` alongside Pass B, using
``backfill_run``'s own ``instance_ids`` filter for §10.1's judge-launch
instance selector rather than re-implementing the scan loop here.

Called as step 1 of every ``llm-judge`` task invocation, before Pass B
(§10.5 — locked: not a separate task, not optional, because an un-scanned
run silently empties the `always_judge: leaked_node_ids_nonempty` stratum
and corrupts the Pass A vs Pass B confusion matrix with no error raised).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run_leak_pass(run_id: str, instance_ids: list[str] | None = None) -> int:
    """Scan every un-scanned harness-phase row for *run_id* (or just
    *instance_ids*, if given). Returns the number of rows updated.
    Idempotent and cheap — safe to run on every judge launch."""
    from scripts.backfill_leak_detection import backfill_run

    n = backfill_run(run_id, instance_ids=instance_ids)
    logger.info("leak_pass: run %s — %d instance_results row(s) scanned", run_id, n)
    return n
