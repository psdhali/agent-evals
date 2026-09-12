"""Manual close + reviewed restart — BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md.

Owner decision 2026-08-29: no automated resume. An operator reviews failed
instances in the UI and triggers restart by hand; finalising a run (which
revokes both the LiteLLM and OpenRouter keys — ``run_launch.finalise_run``)
is likewise a deliberate action, never automatic. This module is the only
caller of :func:`run_launch.finalise_run` and the only writer of new
``instance_results`` attempt rows outside the original dispatch.

Both :func:`close_run` and :func:`restart_instances` take a
``SELECT ... FOR UPDATE`` lock on the ``runs`` row for the run before acting
(M1 of the design review): that is what actually serialises a `/close`
against a concurrent `/restart` — whichever transaction acquires the row
lock first runs to completion (commit) before the other can even read
``status``, so there is no window where a restart's new row can land between
a close's "zero non-terminal rows" check and its key-revocation step.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from swebench_eval.database.redis_client import write_harness_instructions
from swebench_eval.database.state_machine import is_run_closed, is_terminal
from swebench_eval.orchestrator.control_plane.results_writer import (
    _ALL_NON_TERMINAL_STATES,
    non_terminal_row_counts,
)
from swebench_eval.orchestrator.control_plane.run_launch import finalise_run
from swebench_eval.queue.client import send_message
from swebench_eval.queue.schemas import EvalJob, HarnessJob

logger = logging.getLogger(__name__)


class CloseConflictError(Exception):
    """Run is not open for close (not 'running', or instances still in flight)."""


class RestartError(Exception):
    """A restart request could not be carried out as asked."""


def _db() -> Any:
    from swebench_eval.database.connection import get_connection

    return get_connection()


# ---------------------------------------------------------------------------
# Close (M1)
# ---------------------------------------------------------------------------


def close_run(run_id: str) -> dict[str, Any]:
    """Deliberately finalise *run_id* — the only path to key revocation.

    Raises :class:`CloseConflictError` (a 409 at the API layer) if the run is
    not ``'running'``/``'finalising'``, or still has non-terminal
    instance_results rows at the moment the lock is acquired — including one
    a concurrent ``/restart`` just landed. Never rolls the flip back on that
    path because with the row lock held throughout, the flip is only ever
    written once we already know the check passed — nothing to unwind.

    F1 (implementation review, 2026-08-31): also accepts ``'finalising'``,
    not just ``'running'`` — a run already mid-close. `finalise_run` makes
    three real network calls (ECS StopTask, LiteLLM key delete, OpenRouter
    key disable); if any of them raises, the commit above has already
    landed, so the run is stuck at 'finalising' with both keys still live.
    The old `_maybe_finalise_if_done` this replaced retried automatically on
    the next reaper tick — that retry was removed along with the
    auto-finalise behaviour, but the intermediate state that depended on it
    was not. `finalise_run` is documented idempotent
    (`run_launch.py:finalise_run`: "safe to call more than once"), so
    re-entering here and calling it again is safe and needs no rollback
    logic — that is the whole fix. Reachable only via ``/close`` called
    again by the operator (or a future automatic retry) after a failure;
    never automatic here.
    """
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM runs WHERE run_id = %s FOR UPDATE", (run_id,))
            row = cur.fetchone()
            if row is None:
                raise CloseConflictError(f"no such run: {run_id}")
            status = row[0]
            if status not in ("running", "finalising"):
                raise CloseConflictError(f"run {run_id} is not open for close (status={status!r})")
            non_terminal, total = non_terminal_row_counts(conn, run_id)
            if non_terminal > 0:
                raise CloseConflictError(
                    f"run {run_id} has {non_terminal} of {total} instance(s) still in "
                    "flight — a restart may have landed after your review; refresh and retry"
                )
            if status != "finalising":
                cur.execute(
                    "UPDATE runs SET status = 'finalising' WHERE run_id = %s",
                    (run_id,),
                )
        conn.commit()  # releases the row lock — /restart can now see 'finalising' and refuse
    finally:
        conn.close()

    finalise_run(run_id)
    logger.info("run %s: closed by operator action", run_id)
    return {"run_id": run_id, "status": "completed"}


# ---------------------------------------------------------------------------
# Restart (M2, M3, M4)
# ---------------------------------------------------------------------------


def _latest_attempt_rows(
    conn: Any, run_id: str, instance_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Per instance_id: its highest-attempt_number row, preferring the eval-
    phase row over the harness-phase row at that same attempt_number (the
    eval verdict is the more final descriptor when both exist for one
    attempt — a harness row's own PATCH_READY is a phase-1 success, not
    itself a restart-classifiable outcome).
    """
    if not instance_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (instance_id)
                   instance_id, attempt_number, phase, state, error_category
               FROM instance_results
               WHERE run_id = %s AND instance_id = ANY(%s)
               ORDER BY instance_id, attempt_number DESC, (phase = 'eval') DESC""",
            (run_id, instance_ids),
        )
        cols = [d[0] for d in cur.description]
        return {r[0]: dict(zip(cols, r, strict=True)) for r in cur.fetchall()}


def _classify(latest: dict[str, Any]) -> str:
    """retry_reason for a new attempt restarting *latest*.

    M4: attempt_number alone cannot disambiguate configured pass@k from an
    operator restart — this tag is the disambiguator, decided once, here,
    from the instance's own prior outcome; never inferred later.
    """
    category = latest.get("error_category")
    if category is not None and is_terminal(category):
        return "operator_rerun_pass_at_k"
    # Every other concluded category (infra-shaped _RETRYABLE ones, and the
    # deliberately-neither ones like HARNESS_MAX_TURNS_EXCEEDED /
    # HARNESS_CONTEXT_EXHAUSTED) is treated as an infra-shaped retry for
    # denominator purposes once a human has decided to restart it — the whole
    # point of manual review (v2 §2.2's non-goals) is that the operator's
    # judgment substitutes for the taxonomy, not that the taxonomy still gates
    # it. See BUILDER4-AUTOSCALER-FULL §6.2 for why that category boundary
    # was left open — this design resolves it by making the operator the
    # decision function.
    return "operator_infra_retry"


def restart_instances(
    run_id: str, instance_ids: list[str], *, actor: str = "operator"
) -> dict[str, Any]:
    """Create attempt N+1 for each of *instance_ids* and dispatch it.

    Returns a report with what was restarted and what was skipped, and why —
    never raises for a per-instance skip (unknown instance, still in flight,
    PAUSED_BY_OPERATOR), only for whole-batch problems (run closed, no such
    run, empty instance_ids).
    """
    if not instance_ids:
        raise RestartError("instance_ids must be non-empty")

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM runs WHERE run_id = %s FOR UPDATE", (run_id,))
            row = cur.fetchone()
            if row is None:
                raise RestartError(f"no such run: {run_id}")
            status = row[0]
            if is_run_closed(status):
                raise RestartError(
                    f"run {run_id} is closed (status={status!r}) — cannot restart against it"
                )

            latest = _latest_attempt_rows(conn, run_id, instance_ids)

            restarted: list[dict[str, Any]] = []
            skipped: list[dict[str, str]] = []

            for instance_id in instance_ids:
                row_ = latest.get(instance_id)
                if row_ is None:
                    skipped.append({"instance_id": instance_id, "reason": "unknown instance"})
                    continue
                if row_["state"] in _ALL_NON_TERMINAL_STATES:
                    skipped.append(
                        {"instance_id": instance_id, "reason": "attempt still in flight"}
                    )
                    continue
                # 2026-08-31 correction: PAUSED_BY_OPERATOR is NOT excluded.
                # /control/resume (or the future per-run gateway-resume) only
                # reopens the dispatch/gateway GATE — a paused instance's
                # harness-jobs message is already consumed and deleted by the
                # time it dies, same as any other terminal outcome. Excluding
                # it here with no other path back was a dead end: resume does
                # nothing for an already-dead attempt, so there was no way at
                # all to get it running again. _classify() below correctly
                # tags it operator_infra_retry (it's not in _TERMINAL) — an
                # operational interruption, not a legitimate finish.

                retry_reason = _classify(row_)
                next_attempt = row_["attempt_number"] + 1
                cur.execute(
                    """INSERT INTO instance_results
                           (run_id, instance_id, attempt_number, phase, state,
                            seeded_at, retry_reason)
                       VALUES (%s, %s, %s, 'harness', 'PENDING', now(), %s)""",
                    (run_id, instance_id, next_attempt, retry_reason),
                )
                restarted.append(
                    {
                        "instance_id": instance_id,
                        "attempt_number": next_attempt,
                        "retry_reason": retry_reason,
                    }
                )
        conn.commit()  # commits the new rows AND releases the runs row lock
    finally:
        conn.close()

    if restarted:
        _dispatch_restarted(run_id, restarted)

    logger.info(
        "run %s: operator %s restarted %d instance(s), skipped %d",
        run_id,
        actor,
        len(restarted),
        len(skipped),
    )
    return {"run_id": run_id, "restarted": restarted, "skipped": skipped}


# ---------------------------------------------------------------------------
# Regrade (EVAL-GRADE-RESOURCE-LIMITS follow-up, 2026-09-01)
# ---------------------------------------------------------------------------


def _latest_harness_patch(conn: Any, run_id: str, instance_id: str) -> str | None:
    """S3 key of the newest harness attempt's captured patch, or None.

    The newest HARNESS row with a patch — not the latest attempt per se: a
    prior regrade's attempt is eval-only and carries no patch of its own, so
    a regrade-of-a-regrade must reach back to the harness attempt that
    actually produced the diff.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT patch_path FROM instance_results
               WHERE run_id = %s AND instance_id = %s AND phase = 'harness'
                 AND patch_path IS NOT NULL AND patch_path <> ''
               ORDER BY attempt_number DESC LIMIT 1""",
            (run_id, instance_id),
        )
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def regrade_instances(
    run_id: str, instance_ids: list[str], *, actor: str = "operator"
) -> dict[str, Any]:
    """Grade each instance's EXISTING patch again, as an eval-only attempt N+1.

    The counterpart /restart lacks: for an instance whose harness phase
    succeeded (patch captured, paid for) but whose eval OOMed / was abandoned
    / dead-lettered, /restart re-runs the whole pipeline — new model spend and
    a *different* patch, so the thing that failed is never actually re-graded.
    This path re-enqueues ONLY the eval half, pointing at the newest captured
    patch.  The natural remedy after an EVAL_OOM_KILLED outcome, a host
    resize, or a mem_limit change.

    Shape decisions, recorded:
    - **Attempt N+1, eval phase only.** The (run, instance, attempt, phase)
      PK means the same attempt cannot hold a second eval row, and mutating
      the old row would rewrite the append-only ledger.  No harness row
      exists at N+1 by design; latest-attempt readers already prefer the
      eval row of an attempt (``_latest_attempt_rows``), and the harness-
      phase columns simply have nothing to say about a regrade.
    - **retry_reason 'operator_regrade'.** The same patch graded again is the
      same logical attempt — excluded from the resolve-rate denominator like
      'operator_infra_retry' (queries.resolve_rate_denominator), and kept
      distinct from it so the ledger still shows *what* the operator did.
    - **New attempt number = new S3 report prefix** — the failed grade's
      evidence (report, test_output, resource_usage) is never clobbered.
    - **No gateway/pause gate**: a regrade spends no model money (the reason
      /restart's UI arms a confirmation is that it launches real inference).
    - Same run-row lock and closed-run refusal as /restart; per-instance
      problems are skipped with a reason, never a whole-batch failure.
    - Seed-then-send, like _enqueue_eval_job: a committed PENDING row whose
      send then fails is a visible non-terminal row.  (Reaper rule 3 covers
      only harness-phase PENDING today — a pre-existing gap this shares with
      _enqueue_eval_job's identical shape, noted rather than widened here.)
    """
    if not instance_ids:
        raise RestartError("instance_ids must be non-empty")

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM runs WHERE run_id = %s FOR UPDATE", (run_id,))
            row = cur.fetchone()
            if row is None:
                raise RestartError(f"no such run: {run_id}")
            status = row[0]
            if is_run_closed(status):
                raise RestartError(
                    f"run {run_id} is closed (status={status!r}) — cannot regrade against it"
                )

            latest = _latest_attempt_rows(conn, run_id, instance_ids)

            regraded: list[dict[str, Any]] = []
            skipped: list[dict[str, str]] = []

            for instance_id in instance_ids:
                row_ = latest.get(instance_id)
                if row_ is None:
                    skipped.append({"instance_id": instance_id, "reason": "unknown instance"})
                    continue
                if row_["state"] in _ALL_NON_TERMINAL_STATES:
                    skipped.append(
                        {"instance_id": instance_id, "reason": "attempt still in flight"}
                    )
                    continue
                patch_key = _latest_harness_patch(conn, run_id, instance_id)
                if patch_key is None:
                    skipped.append(
                        {
                            "instance_id": instance_id,
                            "reason": "no captured patch to regrade — use /restart",
                        }
                    )
                    continue
                next_attempt = row_["attempt_number"] + 1
                cur.execute(
                    """INSERT INTO instance_results
                           (run_id, instance_id, attempt_number, phase, state,
                            seeded_at, retry_reason)
                       VALUES (%s, %s, %s, 'eval', 'PENDING', now(), 'operator_regrade')""",
                    (run_id, instance_id, next_attempt),
                )
                regraded.append(
                    {
                        "instance_id": instance_id,
                        "attempt_number": next_attempt,
                        "patch_s3_key": patch_key,
                    }
                )
        conn.commit()  # commits the new rows AND releases the runs row lock
    finally:
        conn.close()

    if regraded:
        _dispatch_regraded(run_id, regraded)

    logger.info(
        "run %s: operator %s regraded %d instance(s), skipped %d",
        run_id,
        actor,
        len(regraded),
        len(skipped),
    )
    return {"run_id": run_id, "regraded": regraded, "skipped": skipped}


def _dispatch_regraded(run_id: str, regraded: list[dict[str, Any]]) -> None:
    """One EvalJob per regraded instance — the existing patch, a new attempt.

    Outside the DB transaction, same as _dispatch_restarted.  The worker
    loads fail_to_pass/pass_to_pass from the dataset itself (ADR-0007: the
    queue carries pointers), so the job needs nothing but the patch key.
    """
    for entry in regraded:
        job = EvalJob(
            run_id=run_id,
            instance_id=entry["instance_id"],
            attempt_number=entry["attempt_number"],
            patch_s3_key=entry["patch_s3_key"],
            fail_to_pass="",
            pass_to_pass="",
        )
        send_message("eval-jobs", dataclasses.asdict(job))
        logger.info(
            "regrade %s: enqueued %s attempt %d (patch %s)",
            run_id,
            entry["instance_id"],
            entry["attempt_number"],
            entry["patch_s3_key"],
        )


def _run_config_snapshot(run_id: str) -> dict[str, Any]:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT config_snapshot FROM runs WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None or row[0] is None:
        raise RestartError(f"run {run_id} has no config_snapshot to restart against")
    snapshot: dict[str, Any] = row[0]
    return snapshot


def _dispatch_restarted(run_id: str, restarted: list[dict[str, Any]]) -> None:
    """Build and enqueue one HarnessJob per restarted instance.

    Outside the DB transaction deliberately — SQS was never transactional
    with Postgres for the original dispatch either. A row committed with no
    message yet is the same shape the never-dispatched reaper already exists
    to catch (results_writer._reap_never_dispatched), not a new failure mode.
    """
    from swebench_eval.dataset.swebench_loader import load_single_instance

    snapshot = _run_config_snapshot(run_id)
    harness = snapshot.get("harness", "")
    model_alias = snapshot.get("model_alias", "")

    # 2026-09-08: the launch-time pool -> alias pacer copy lives in Valkey and dies with every
    # eval-tier cycle; a restart after one ran the gateway pacer on its DEFAULTS (20k tok/s —
    # 19 of 24 admissions waited > 2 s on the opencode restarts). Re-fill whatever the alias
    # hash lacks from its pool before the first restarted job is enqueued; fields the alias
    # already has (an operator's r_qps) are kept. Best-effort, never raises.
    if model_alias:
        try:
            from swebench_eval.database.redis_client import _get_client
            from swebench_eval.orchestrator.control_plane import pacer_seeds

            pacer_seeds.seed_alias_from_pool(_get_client(), model_alias, fill_missing=True)
        except Exception:  # noqa: BLE001 — defaults are the fallback, as at launch
            logger.warning(
                "restart %s: could not re-seed pacer:cfg:%s from its pool (pacer on defaults)",
                run_id,
                model_alias,
            )

    # 2026-09-09 efficiency prompt arm: a restarted attempt must get the SAME prompt the run
    # was launched with — re-publish the snapshot's instructions (Redis may have been cycled
    # since launch). None clears the key so a plain run stays plain.
    write_harness_instructions(run_id, snapshot.get("harness_instructions"))

    for entry in restarted:
        instance_id = entry["instance_id"]
        instance = load_single_instance(instance_id)
        if instance is None:
            logger.error(
                "restart %s: %s not found in the pinned dataset — row inserted but "
                "NOT dispatched, will surface via the never-dispatched reaper",
                run_id,
                instance_id,
            )
            continue
        job = HarnessJob(
            run_id=run_id,
            instance_id=instance_id,
            repo_url=f"https://github.com/{instance.repo}",
            base_commit=instance.base_commit,
            problem_statement=instance.problem_statement,
            attempt_number=entry["attempt_number"],
            harness_name=harness,
            model_alias=model_alias,
            env_image_key="",  # ADR-0043: per-instance families, no env images
            timeout_seconds=snapshot.get("timeout_seconds", HarnessJob.timeout_seconds),
            max_tokens_per_instance=snapshot.get("max_tokens_per_instance"),
            max_cost_usd_per_instance=snapshot.get(
                "max_cost_usd_per_instance", HarnessJob.max_cost_usd_per_instance
            ),
            max_turns_per_instance=snapshot.get("max_turns_per_instance"),
            context_window_tokens=snapshot.get("context_window_tokens"),
        )
        send_message("harness-jobs", dataclasses.asdict(job))
        logger.info(
            "restart %s: dispatched %s attempt %d (%s)",
            run_id,
            instance_id,
            entry["attempt_number"],
            entry["retry_reason"],
        )
