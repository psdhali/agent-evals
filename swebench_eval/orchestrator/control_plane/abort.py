"""Abort executor (ADR-0034 M1.6-M1.7 / observability §M1.6).

Executes a run abort in the orchestrator (the API is the only caller).  The
ordered steps are the design:

1. **Record intent.**  ``runs.status = 'aborting'`` + ``stop_*`` in Aurora, and
   publish ``control:aborted`` to Redis.  From this point everything is
   idempotent and retryable — dispatcher gate #2 is already live, so no further
   tasks for the run launch.
2. **Stop in-flight** (scope includes harness): ``ListTasks(startedBy=run_id)``
   then ``StopTask`` each.  The harness worker's SIGTERM handler (M1.4)
   captures the partial patch and emits ``ABORTED_IN_FLIGHT``.
3. **Drain queued messages** (M1.7) — never ``PurgeQueue``.
4. **Wait for the results queue to settle** (bounded): the SIGTERM'd tasks are
   still uploading + emitting; the Results Writer is deliberately never paused,
   so it drains them.
5. **Finalise**: ``runs.status = 'aborted'``, ``stopped_at = now()``.

The drain REFUSES when a second run is active (M1.7): receiving increments a
message's receive count even when returned, so draining a shared queue pushes
another run's messages toward the DLQ.  In that case we skip the drain and rely
on gate #2's discard-on-pickup — always correct, merely slower.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from swebench_eval import aws_names
from swebench_eval.control import state as control_state

logger = logging.getLogger(__name__)

# Abort is not instantaneous (design M1.6): bounded by stopTimeout + upload +
# the results queue settling.  These are the bounds.
_STOP_TIMEOUT_S = 120  # must match the harness task-def stopTimeout (M1.4)
_SETTLE_TIMEOUT_S = 5 * 60
_SETTLE_POLL_S = 5
_UNSETTLE_ACTIVE_STATES = ("running", "aborting")


@dataclass(frozen=True)
class AbortReport:
    """What an abort did / is doing — for the API response + drain manifest."""

    run_id: str
    scope: str
    reason: str
    actor: str
    requested_at: float
    in_flight_stopped: int = 0
    drained: int = 0
    drain_skipped: bool = False
    drain_skip_reason: str = ""
    settled: bool = False
    swept: int = 0


def execute_abort(
    connection: Any,
    run_id: str,
    scope: str,
    reason: str,
    actor: str,
    ecs: Any | None = None,
    settle_timeout_s: int = _SETTLE_TIMEOUT_S,
    background_settle: bool = False,
) -> AbortReport:
    """Execute a run abort.

    *connection* is a live Aurora connection (the orchestrator's); *ecs* is an
    optional ECS client (injected in tests).

    ``background_settle`` (the API's mode, 2026-09-04): the fast steps — intent,
    StopTask, drain — run here (about a second), and the settle wait + sweep +
    finalise continue on a daemon thread with their own connection. The returned
    report has ``settled=False``; the run stays ``aborting`` until the thread
    finalises it, which is exactly the contract the API and the UI already
    documented ("the report is a draining description, the UI polls until the run
    is terminal"). Found live: the api ALB's idle timeout is 60 s and the settle
    wait is bounded at 5 minutes, so every abort that actually stopped a task
    answered the browser with a Gateway Time-out while the handler kept running.
    """
    if ecs is None:
        import boto3

        ecs = boto3.client("ecs", region_name=aws_names.region())

    report = _record_intent(connection, run_id, scope, reason, actor)
    # Redis fast-path: the dispatcher gate + any listening consumer see the
    # abort immediately, not after the next Aurora->Redis tick.
    control_state.request_abort(run_id)

    if scope in ("harness", "all"):
        report = _stop_in_flight(connection, ecs, report)

    report = _drain_queued(connection, run_id, scope, report)

    if background_settle:
        thread = threading.Thread(
            target=_settle_sweep_finalise_own_connection,
            args=(run_id, report, settle_timeout_s),
            name=f"abort-settle-{run_id[-8:]}",
            daemon=True,
        )
        thread.start()
        return report
    return _settle_sweep_finalise(connection, run_id, report, settle_timeout_s)


def _settle_sweep_finalise_own_connection(
    run_id: str, report: AbortReport, settle_timeout_s: int
) -> None:
    """The background half of :func:`execute_abort`: same steps, own connection,
    every failure logged (there is no HTTP response left to carry it)."""
    try:
        from swebench_eval.database.connection import get_connection

        connection = get_connection()
        try:
            _settle_sweep_finalise(connection, run_id, report, settle_timeout_s)
        finally:
            connection.close()
    except Exception:
        logger.exception(
            "abort: background settle/finalise failed for run %s — the run may be left "
            "'aborting'; a second abort request re-runs every step",
            run_id,
        )


def _settle_sweep_finalise(
    connection: Any, run_id: str, report: AbortReport, settle_timeout_s: int
) -> AbortReport:
    # 2026-08-28 fix: this used to be `if not report.drain_skipped:` — but
    # `_stop_in_flight` above runs independently of the drain (it's gated on
    # `scope`, not on whether draining happened), so a genuinely SIGTERM'd
    # task under concurrent-run conditions (drain skipped, the common case —
    # see §3.4/Finding 3) was never waited for at all. Settling is about
    # in-flight work, not about the drain specifically; wait unconditionally.
    _wait_for_settle(connection, run_id, settle_timeout_s)

    # §9.g/h — the leftover-non-terminal-row sweep, immediately before
    # finalising. By this point: in-flight tasks got SIGTERM (if scope
    # allows), queued messages were drained or will be caught by gate #2, and
    # we've waited up to settle_timeout_s for anything genuinely still
    # running to report its own terminal result. Whatever's still
    # non-terminal after all of that is a real straggler — a StopTask that
    # silently failed, a task that died before reaching any checkpoint that
    # could report anything, an eval job still grading — not something to
    # keep waiting on indefinitely.
    swept = _sweep_remaining_rows(connection, run_id, requested_at=report.requested_at)
    report = dataclasses.replace(report, swept=swept)

    report = _finalise(connection, run_id, report)
    # run-launch §8 (BUILDER4-RUN-LAUNCH-ORCHESTRATOR): "Abort must also clear
    # active_key and revoke the keys, or an aborted run blocks its pair
    # forever."  Additive-only: this run's active_key/keys may not exist at
    # all (an abort on a run predating run-launch, or one that never reached
    # PROVISION) — both calls are no-ops in that case, never an error.
    try:
        from swebench_eval.orchestrator.control_plane import run_launch

        run_launch.revoke_run_keys(run_id)
        run_launch.release_active_key(run_id)
    except Exception:
        # a stuck active_key/unrevoked key after this is a recoverable,
        # loudly-logged follow-up, not a reason to fail the abort itself.
        logger.exception("abort: could not revoke/release run-launch keys for %s", run_id)
    return report


def _record_intent(
    connection: Any, run_id: str, scope: str, reason: str, actor: str
) -> AbortReport:
    """M1.6 step 1: record intent in Aurora (truth) and publish to the gate."""
    cursor = connection.cursor()
    cursor.execute(
        """UPDATE runs
           SET status = 'aborting', stop_requested_at = now(),
               stop_scope = %s, stop_reason = %s, stopped_at = NULL
           WHERE run_id = %s""",
        (scope, reason, run_id),
    )
    connection.commit()
    cursor.close()

    control_state.request_abort(run_id)
    logger.info("abort: run %s marked aborting (scope=%s, actor=%s)", run_id, scope, actor)
    returning = AbortReport(
        run_id=run_id, scope=scope, reason=reason, actor=actor, requested_at=time.time()
    )
    return returning


def _stop_in_flight(connection: Any, ecs: Any, report: AbortReport) -> AbortReport:
    """M1.6 step 3: ``StopTask`` every in-flight task launched with
    ``startedBy=run_id`` (ADR-0034 M1.5 makes this a single-filter enumeration).

    2026-08-28 hardening (Builder 3's own flagged item, Tranche 3A §4 — "IAM
    is granted, the code is not wrapped"): ``ListTasks`` itself was unwrapped
    while ``StopTask`` already was (below). A transient AWS error on the
    *list* call — throttling, a network blip — used to raise straight out of
    this function, out of :func:`execute_abort` (nothing above it catches
    either), surfacing as a 500 with ``runs.status`` left at ``'aborting'``
    **permanently, with nothing to recover it** — the same "row with no
    owner" defect shape as the leftover-instance-row gap this round also
    fixes, just at the ``runs`` grain. One bad page of ``ListTasks`` must not
    strand the whole abort; log and stop paginating rather than raise, same
    failure direction as a per-task ``StopTask`` failure already gets.
    """
    stopped = 0
    stopped_ids: list[str] = []
    tok: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "cluster": _cluster(),
            "startedBy": report.run_id,
            "desiredStatus": "RUNNING",
            "maxResults": 100,
        }
        if tok:
            kwargs["nextToken"] = tok
        try:
            resp = ecs.list_tasks(**kwargs)
        except Exception:
            logger.exception(
                "ListTasks failed for run %s (stopped %d so far); "
                "the leftover-row sweep will catch anything still in flight",
                report.run_id,
                stopped,
            )
            break
        for arn in resp.get("taskArns", []):
            try:
                ecs.stop_task(cluster=_cluster(), task=arn, reason="Aborted by operator")
                stopped += 1
                stopped_ids.append(str(arn))
            except Exception:
                logger.exception("StopTask failed for %s", arn)
        tok = resp.get("nextToken")
        if not tok:
            break
    if stopped_ids:
        logger.info(
            "abort: stopped %d in-flight task(s) for run %s: %s",
            stopped,
            report.run_id,
            ", ".join(stopped_ids),
        )
    return dataclasses.replace(report, in_flight_stopped=stopped)


def _cluster() -> str:
    return os.environ.get("CLUSTER", "")


def _drain_queued(connection: Any, run_id: str, scope: str, report: AbortReport) -> AbortReport:
    """M1.7: drain the run's queued harness messages into S3, then delete.

    Refuse when a second run is active — the refusal condition is not optional
    (draining a shared queue pushes another run's messages toward the DLQ).  In
    that case the run relies on dispatcher gate #2's discard-on-pickup, which is
    always correct, merely slower.
    """
    if scope not in ("harness", "all"):
        # Nothing queued for a pure-eval abort scope; the harness job list is
        # covered by gate #2 either way.
        return dataclasses.replace(
            report, drain_skipped=True, drain_skip_reason="scope-not-harness"
        )
    if not _exactly_one_active(connection, run_id):
        return dataclasses.replace(
            report,
            drain_skipped=True,
            drain_skip_reason="another run active — skipping drain, relying on gate #2",
        )

    from swebench_eval.queue.client import (
        change_message_visibility,
        delete_message,
        receive_message,
        send_message,
    )
    from swebench_eval.queue.schemas import ResultMessage

    drained: list[dict[str, str]] = []

    # The eager dispatch enqueues every attempt up front (dispatcher.py:401),
    # so a full run's whole not-yet-launched backlog sits here.  A bounded sweep
    # is fine: after the first pass the gate + delete-on-pickup handle anything
    # that arrives later.
    for _ in range(500):
        msg = receive_message("harness-jobs", wait_seconds=1, visibility_timeout=60)
        if msg is None:
            break
        body = msg["body"]
        if not isinstance(body, dict):
            try:
                body = json.loads(body)
            except (TypeError, ValueError):
                body = {}
        if body.get("run_id") == run_id:
            instance_id = str(body.get("instance_id", ""))
            attempt_number = int(body.get("attempt_number", 0) or 0)
            drained.append({"run_id": run_id, "instance_id": instance_id})
            # 2026-08-28 fix (§9.f): a drained PENDING row used to just get
            # its queue message deleted, with no terminal write — unlike gate
            # #2 (harness_dispatcher._discard_aborted), which correctly emits
            # this for the exact same situation (a queued job belonging to an
            # aborted run). Mirror it here so the drain path and the gate
            # leave the ledger in the same state instead of the drain path
            # leaving the row PENDING forever.
            try:
                send_message(
                    "results",
                    dataclasses.asdict(
                        ResultMessage(
                            run_id=run_id,
                            instance_id=instance_id,
                            attempt_number=attempt_number,
                            phase="harness",
                            state="NEVER_DISPATCHED",
                            error_detail="run aborted (drained)",
                        )
                    ),
                )
            except Exception:
                logger.exception(
                    "could not emit NEVER_DISPATCHED for drained %s/%s attempt %d "
                    "(the leftover-row sweep will catch it before finalise)",
                    run_id,
                    instance_id,
                    attempt_number,
                )
            delete_message("harness-jobs", msg["receipt_handle"])
        else:
            # Not our run.  Deleting it loses another run's work (the commit
            # that M1.7 exists to prevent); returning it and stopping is the
            # honest option.  Reset visibility to 0 so the message is
            # immediately receivable again — the receive-count increment is one
            # against maxReceiveCount=5, which is the price of safety and far
            # cheaper than a lost job with no trace.  Because we only drain
            # when exactly one run is active, a foreign message is (by
            # construction) transient — return it and stop draining (the gate
            # will handle this run's remainder via discard-on-pickup).
            change_message_visibility("harness-jobs", msg["receipt_handle"], 0)
            logger.warning(
                "abort drain: received message for foreign run %s; returned it "
                "and stopped draining (relying on gate #2) rather than delete "
                "another run's work.",
                body.get("run_id"),
            )
            break
    if drained:
        _write_drain_manifest(run_id, drained)
    if report.drained or drained:
        logger.info("abort: drained %d queued message(s) for run %s", len(drained), run_id)
    return dataclasses.replace(report, drained=len(drained))


def _write_drain_manifest(run_id: str, drained: list[dict[str, str]]) -> None:
    """Persist the drained-messages manifest to S3 (M1.7: write-then-delete).

    The manifest is EVIDENCE, and M1.7 explicitly adds: *"Also snapshot that
    queue's DLQ contents into the same file."*  A drain that only records what
    it deleted leaves the dead-letter contents invisible — the review answering
    "why did this message die?" months later has nothing to look at.  The DLQ
    is read (bounded, non-destructive — visibility reset to 0 immediately) and
    each record is tagged ``source: drained|dlq`` so the two are never
    conflated when read back.
    """
    from swebench_eval.queue.client import (
        change_message_visibility,
        receive_message,
        upload_artifact,
    )

    # The abort drain only ever drains harness-jobs (the harness backlog); its
    # DLQ is the harness-jobs dead-letter queue (compose + Terraform both name
    # it <queue>-dlq).  Keep the pairing here rather than guessing at a caller.
    dlq_name = "harness-jobs-dlq"

    records: list[dict[str, object]] = [{**d, "source": "drained"} for d in drained]

    # Snapshot whatever is currently dead-lettered.  Read-and-return (visibility
    # 0 = immediately visible again) so the snapshot itself never loses work —
    # the DLQ is the backlog's last stand and we are only taking evidence.
    for _ in range(500):
        msg = receive_message(dlq_name, wait_seconds=0, visibility_timeout=0)
        if msg is None:
            break
        body = msg["body"]
        if not isinstance(body, dict):
            try:
                body = json.loads(body)
            except (TypeError, ValueError):
                body = {"unparsed": str(body)}
        records.append(
            {
                "run_id": str(body.get("run_id", "")),
                "instance_id": str(body.get("instance_id", "")),
                "source": "dlq",
                "receipt_handle": msg["receipt_handle"],
            }
        )
        # Return the message (receive-count-bump is the price of evidence; the
        # alternative — deleting it — loses the DLQ's final copy of the work).
        change_message_visibility(dlq_name, msg["receipt_handle"], 0)

    bucket = os.environ.get("ARTIFACTS_BUCKET", "eval-artifacts")
    key = f"runs/{run_id}/aborted/drained-messages.jsonl"
    upload_artifact(bucket, key, "".join(json.dumps(r) + "\n" for r in records))


def _exactly_one_active(connection: Any, run_id: str) -> bool:
    """True when *run_id* is the only run currently running/aborting.

    This is M1.7's refusal gate: drain may only touch a shared queue when
    exactly one run holds it, so draining cannot damage a concurrent run.
    """
    cursor = connection.cursor()
    cursor.execute(
        "SELECT count(*) FROM runs WHERE status IN %s",
        (_UNSETTLE_ACTIVE_STATES,),
    )
    active = int(cursor.fetchone()[0])
    cursor.close()
    return active <= 1


def _wait_for_settle(connection: Any, run_id: str, timeout: int) -> None:
    """M1.6 step 5: wait for the results queue to settle (the SIGTERM'd tasks
    are still uploading + emitting; the Results Writer drains them).

    2026-08-28 fix: previously counted ``state IN ('HARNESS_RUNNING',
    'ABORTED_IN_FLIGHT')`` — which counts a row that already REACHED its
    terminal outcome as still outstanding, so the wait could never see zero
    once anything genuinely settled and would silently burn the full
    ``timeout`` on every abort that actually stopped real work. This was
    masked until now by a second, independent bug: ``HARNESS_RUNNING`` was
    never emitted anywhere in production (harness_worker.py), so the
    harness half of the old query never matched anything either — the two
    bugs canceled out into "always returns immediately." Fixing the emission
    without fixing this would have made aborts start hanging for the full
    5 minutes whenever anything was genuinely stopped.

    Correct condition: count rows still in the ledger's own non-terminal set
    (the same ``_ALL_NON_TERMINAL_STATES`` the reaper and finalisation use,
    results_writer.py), across BOTH phases — a row that reached ANY terminal
    state, abort outcome or not, must stop counting. This also now covers
    ``DISPATCHED`` (a task can receive SIGTERM before its worker container
    even starts, i.e. before anything but ``DISPATCHED`` would exist for it)
    and ``EVAL_RUNNING`` (previously not watched at all, §9.h — abort has no
    task to stop for eval work, but can still wait for it to finish before
    declaring itself settled).
    """
    from swebench_eval.orchestrator.control_plane.results_writer import (
        _ALL_NON_TERMINAL_STATES,
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        cursor = connection.cursor()
        cursor.execute(
            """SELECT count(*) FROM instance_results
               WHERE run_id = %s AND state IN %s""",
            (run_id, tuple(_ALL_NON_TERMINAL_STATES)),
        )
        outstanding = int(cursor.fetchone()[0])
        cursor.close()
        if outstanding == 0:
            break
        time.sleep(_SETTLE_POLL_S)


def _sweep_remaining_rows(connection: Any, run_id: str, requested_at: float | None = None) -> int:
    """§9.g/h fix: give every still-non-terminal row a definitive abort outcome.

    ``requested_at`` (2026-09-04): a progress key only proves liveness if it was
    written AFTER the abort was requested. The keys carry a 300 s TTL and the
    settle wait is bounded at 300 s, so a task SIGKILLed at the start of the
    settle still had its last (pre-abort) key in Redis when the sweep ran —
    both HARNESS_RUNNING rows of run 01788550040741118596-dd284a4f were skipped
    that way and nothing else ever reaps a non-running run. A key whose
    ``observed_at`` predates the request is a ghost, not a live task.

    Builder 3's finding 2: once ``_finalise`` flips ``runs.status`` away from
    ``'running'``, the reaper never looks at this run again (its query is
    ``WHERE status = 'running'``) — so any row still ``PENDING``,
    ``DISPATCHED``, ``HARNESS_RUNNING`` or ``EVAL_RUNNING`` at that instant
    (a ``StopTask`` that failed, a task that died before reaching any
    checkpoint that could report anything, §9.g) stays non-terminal forever:
    nothing reaps it, nothing finalises it, ``run_summary`` reports a
    shortfall with no mechanism that can ever resolve it.

    Reuses the reaper's own reconciliation signal (results_writer.py's rule
    2: no live ECS task, no live progress key) rather than inventing a
    second one — a row that's still genuinely alive by either signal is left
    alone, not swept out from under it. Writes go through
    ``_emit_reap_result`` (the results queue), the same single write path
    every other reap uses (§7: "every reap is recorded, never silent") — so
    this sweep's outcomes are visible in ``instance_results`` and CloudWatch
    the same way a passive reaper timeout's are, not a bespoke direct UPDATE.

    Harness rows get the abort-specific terminal states (``NEVER_DISPATCHED``
    for a row that was never even dispatched, ``ABORTED_IN_FLIGHT`` for one
    that was) — these now rank explicitly at terminal (init.sql, "abort
    stands"), so a later straggler cannot un-terminal them. Eval rows get
    ``ABANDONED`` instead, deliberately: abort has no task to stop for eval
    work (it runs inside the standing eval-worker pool, not a per-run task,
    §9.h), grading burns no additional tokens either way (ADR-0034's own
    Context section), and ``ABANDONED`` ranks *below* terminal on purpose —
    if the grade genuinely finishes after this sweep runs, the real verdict
    should still win rather than being discarded.
    """
    from swebench_eval.database.redis_client import read_progress
    from swebench_eval.orchestrator.control_plane.results_writer import (
        _ALL_NON_TERMINAL_STATES,
        _emit_reap_result,
        _running_instance_ids_for_run,
    )

    cursor = connection.cursor()
    cursor.execute(
        """SELECT instance_id, attempt_number, phase, state FROM instance_results
           WHERE run_id = %s AND state IN %s""",
        (run_id, tuple(_ALL_NON_TERMINAL_STATES)),
    )
    rows = cursor.fetchall()
    cursor.close()
    if not rows:
        return 0

    running = _running_instance_ids_for_run(run_id)
    swept = 0
    for instance_id, attempt_number, phase, state in rows:
        if instance_id in running:
            continue  # still genuinely alive — do not sweep out from under it
        progress = read_progress(run_id, instance_id, attempt_number)
        if progress is not None and _progress_is_live(progress, requested_at):
            continue  # still reporting live progress — same reason
        if phase == "harness":
            new_state = "NEVER_DISPATCHED" if state == "PENDING" else "ABORTED_IN_FLIGHT"
        else:
            new_state = "ABANDONED"
        _emit_reap_result(
            run_id,
            instance_id,
            attempt_number,
            phase,
            new_state,
            f"swept by abort: no terminal result after settle timeout (was {state})",
        )
        swept += 1
    return swept


def _progress_is_live(progress: dict[str, Any], requested_at: float | None) -> bool:
    """A progress key counts as live evidence only if written after the abort request.
    Without a request time (or an unreadable stamp) the old rule stands: a key means live."""
    if requested_at is None:
        return True
    try:
        observed_at = float(progress.get("observed_at") or 0.0)
    except (TypeError, ValueError):
        return True
    if observed_at <= 0:
        return True
    return observed_at >= requested_at


def _finalise(connection: Any, run_id: str, report: AbortReport) -> AbortReport:
    """M1.6 step 6: status = 'aborted'.  The run is non-resumable (ADR-0034 §5)."""
    cursor = connection.cursor()
    cursor.execute(
        """UPDATE runs SET status = 'aborted', stopped_at = now() WHERE run_id = %s""",
        (run_id,),
    )
    connection.commit()
    cursor.close()
    logger.info("abort: run %s finalised (status=aborted)", run_id)
    return dataclasses.replace(report, settled=True)
