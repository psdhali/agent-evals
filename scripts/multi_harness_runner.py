#!/usr/bin/env python3
"""Run several harnesses through the shared queue pipeline in parallel.

The Phase 3 smoke test is single-run + queue-purge per run, so running it twice
concurrently corrupts evidence (each purges the other's queues and spawns workers
that steal jobs).  This runner instead dispatches N harness runs into the shared
queues and processes them with ONE worker pool sized to the harnesses — the
production-representative way to run a small batch, and ~Nx faster than the
sequential smoke tests.

It is not a benchmark runner: it dispatches one instance per harness (like the
smoke test) and waits for each run's terminal harness/eval state, printing a table.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Terminal harness-phase states; PATCH_READY then proceeds to eval.
_HARNESS_TERMINAL = ("PATCH_READY", "EMPTY_PATCH", "FAILED_HARNESS", "STUCK", "BUDGET_EXCEEDED")
_EVAL_TERMINAL = ("RESOLVED", "UNRESOLVED", "PATCH_APPLY_FAILED", "FAILED_EVAL")


def _run_is_done(harness_state: str | None, eval_state: str | None) -> bool:
    """A run is complete once harness is terminal AND (if PATCH_READY) eval landed.

    Eval is only produced for a PATCH_READY harness — a FAILED/EMPTY/STUCK/BUDGET
    result never gets an eval row, so an absent eval must NOT be treated as
    pending.  This is the predicate the old runner got wrong (it demanded eval
    for every run, so it ran to the deadline and the daemon eval worker was
    killed before a late PATCH_READY eval was written).
    """
    if harness_state is None:
        return False
    if harness_state == "PATCH_READY":
        return eval_state in _EVAL_TERMINAL
    return harness_state in _HARNESS_TERMINAL


def _run_id(i: int) -> str:
    """Mint a run_id in the epoch-ms form the rest of the system uses, plus a 2-digit
    sibling index for parallel runs in the same millisecond: 15 digits total, inside
    Postgres `bigint` (max 19).  The earlier `time.time_ns()` form was 21 digits and
    overflowed `run_id::bigint` casts (review round-3 §2)."""
    return f"{int(time.time() * 1000):013d}{i:02d}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Parallel multi-harness pipeline run")
    parser.add_argument("--harness", nargs="+", required=True, help="Harness names to run")
    parser.add_argument("--instance-id", default="astropy__astropy-12907")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-tokens", type=int, default=12_000_000)
    parser.add_argument("--budget", type=float, default=5.0)
    args = parser.parse_args()

    # One clean slate for the batch (we are the only active runner).
    from swebench_eval.queue.client import get_queue_url, get_sqs_client

    sqs = get_sqs_client()
    for q in ("harness-jobs", "eval-jobs", "results"):
        sqs.purge_queue(QueueUrl=get_queue_url(q))

    from swebench_eval.dataset.swebench_loader import (
        _PINNED_REVISION,
        load_single_instance,
    )
    from swebench_eval.orchestrator.control_plane.dispatcher import dispatch_run
    from swebench_eval.orchestrator.run_config import RunConfig

    instance = load_single_instance(args.instance_id)
    if instance is None:
        # A None here means the ID is absent from the pinned revision — do NOT
        # dispatch a bare-None instance into the pipeline (genuine latent bug,
        # flagged in the Phase-4 CI-red review). Fail fast with the ID named,
        # rather than an opaque downstream failure from a phantom instance.
        sys.exit(
            f"ERROR: instance '{args.instance_id}' not found in SWE-bench Lite "
            f"revision {_PINNED_REVISION} — cannot dispatch. Check the instance ID "
            "against the pinned dataset revision (see design/calibration-note.md)."
        )
    run_ids: dict[str, str] = {}
    for i, h in enumerate(args.harness):
        rid = _run_id(i)
        n = dispatch_run(
            run_id=rid,
            instances=[instance],
            config=RunConfig(
                harness=h,
                model_alias="cheap-oss-model",
                timeout_seconds=args.timeout,
                max_tokens_per_instance=args.max_tokens,
                max_cost_usd_per_instance=args.budget,
            ),
        )
        if n == 0:
            print(f"ERROR: no job dispatched for {h}")
            sys.exit(1)
        run_ids[h] = rid
        print(f"dispatched {h:14} run={rid}")

    # One worker pool: a harness worker PER harness (each is a single poll loop),
    # plus one eval worker and one results writer.  The shared SQS queue
    # distributes the jobs across the harness workers, so they run concurrently.
    from swebench_eval.orchestrator.control_plane.results_writer import run_results_writer
    from swebench_eval.workers.eval_worker import run_eval_worker
    from swebench_eval.workers.harness_worker import run_harness_worker

    workers = [
        threading.Thread(target=run_harness_worker, daemon=True, name=f"hw-{h}")
        for h in args.harness
    ]
    workers += [
        threading.Thread(target=run_eval_worker, daemon=True, name="eval-worker"),
        threading.Thread(target=run_results_writer, daemon=True, name="results-writer"),
    ]
    for t in workers:
        t.start()

    # Poll Postgres for each run's terminal harness + eval state.
    from swebench_eval.database.connection import get_connection

    # deadline: the harness wall-clock budget, plus a generous margin for the
    # eval phase of any PATCH_READY run (grading runs after the harness, so it
    # needs its own window — the previous +300 was too thin and killed the daemon
    # eval worker before codex's eval result was written).
    deadline = time.monotonic() + args.timeout + 900
    result: dict[str, list[str | None]] = {h: [None, None] for h in run_ids}  # [harness, eval]
    while time.monotonic() < deadline:
        if all(_run_is_done(r[0], r[1]) for r in result.values()):
            break
        conn = get_connection()
        with conn.cursor() as cur:
            for h, rid in run_ids.items():
                if result[h][0] is None:
                    cur.execute(
                        "SELECT state FROM instance_results WHERE run_id=%s "
                        "AND attempt_number=1 AND phase='harness'",
                        (rid,),
                    )
                    row = cur.fetchone()
                    if row:
                        result[h][0] = row[0]
                        print(f"  {h:14} harness → {row[0]}")
                if result[h][1] is None and result[h][0] == "PATCH_READY":
                    cur.execute(
                        "SELECT state FROM instance_results WHERE run_id=%s "
                        "AND attempt_number=1 AND phase='eval'",
                        (rid,),
                    )
                    row = cur.fetchone()
                    if row:
                        result[h][1] = row[0]
                        print(f"  {h:14} eval    → {row[0]}")
        conn.close()
        time.sleep(3)

    print("\n" + "=" * 66)
    print("Multi-harness run summary")
    print("=" * 66)
    incomplete = any(
        hs is None or (hs == "PATCH_READY" and es is None) for hs, es in result.values()
    )
    for h, rid in run_ids.items():
        hs, es = result[h]
        print(f"  {h:14} run={rid:16} harness={hs or 'NO-TERMINAL-STATE'}  eval={es or '-'}")
    print("=" * 66)
    sys.exit(1 if incomplete else 0)


if __name__ == "__main__":
    main()
