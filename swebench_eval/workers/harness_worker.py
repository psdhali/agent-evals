"""Harness worker — polls harness-jobs, runs the agent, pushes results.

The worker is a long-running poll loop.  It receives a HarnessJob from the
harness-jobs queue, dispatches to the named harness adapter, uploads
artifacts to MinIO, and pushes a ResultMessage to the results queue.

Heartbeat: ChangeMessageVisibility every 60s (provisional — P3-1).
If heartbeats stop, the message becomes visible again after the base
visibility timeout (5min provisional), and another worker reclaims it.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import signal
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swebench_eval.harnesses.base import HarnessInput, HarnessOutput, ModelConfig, Usage

if TYPE_CHECKING:
    from swebench_eval.dataset.base import Instance
    from swebench_eval.harnesses.stuck_detector import StuckVerdict
from swebench_eval.harnesses.routing import gateway_api_key, gateway_base_url
from swebench_eval.queue.client import (
    change_message_visibility,
    delete_message,
    receive_message,
    send_message,
    upload_artifact,
)
from swebench_eval.queue.schemas import HarnessJob, JobReference, ResultMessage

logger = logging.getLogger(__name__)

# ADR-0034 M1.4: SIGTERM capture so Abort (which reaches this task via
# ecs:StopTask) captures the partial patch instead of spending the tokens and
# losing the evidence (ADR-0016).  Set at task start; checked BETWEEN turns in
# _run_and_collect, never inside a turn where it would abandon a half-written
# patch.
_SHUTDOWN = threading.Event()

# R1 (builder1-REMAINING-WORK-single-handover): codex's helper binaries
# (codex-linux-sandbox etc.) are created at runtime under $CODEX_HOME, and codex
# REFUSES when CODEX_HOME lands inside the process's temp dir — so the sandbox
# never reaches PATH and every exec_command is rejected.  The job workdir must
# live OUTSIDE the system temp dir.  Overshadowed by an env var so the
# orchestrator / local dev can pick another base; the deployed container always
# gets /var/harness.
_WORKDIR_BASE = Path(os.environ.get("HARNESS_WORKDIR_BASE", "/var/harness"))


def _install_sigterm_handler() -> None:
    """Install a SIGTERM handler that only sets a flag (M1.4).

    2026-09-04: the flag is also handed to the subprocess runner (``proc.STOP_EVENT``) so
    a running agent is terminated when it is set — without that, a task mid agent-loop
    never reached the post-run check below before ECS's SIGKILL and the abort recorded
    nothing (five 137 exits, no artifacts, no ABORTED_IN_FLIGHT).
    """
    from swebench_eval.harnesses import proc

    signal.signal(signal.SIGTERM, lambda *_: _SHUTDOWN.set())
    proc.STOP_EVENT = _SHUTDOWN


# ADR-0037 / M0 §4: worker process start (epoch) — worker_boot_s = now − this.
# Module import time is the process start; the deployed task imports this at
# boot, so it is the right origin for the boot phase.
_PROCESS_START_EPOCH = time.time()


def _make_timing(
    queue_wait_s: float | None = None,
    dispatched_at: float | None = None,
    task_meta: Any | None = None,
) -> dict[str, Any]:
    """Seed the per-job timing dict (M0 §4) with the pre-job boundaries.

    ``worker_boot_s`` is filled at _process_job entry (the described boundary
    is process start → job ready).  The rest are measured / filled in
    _run_and_collect.  None means "not measured" (Trap 3) — never invented.
    """
    return {
        "queue_wait_s": queue_wait_s,
        "dispatched_at": dispatched_at,
        "task_meta": task_meta,
    }


def _timing_float(timing: dict[str, Any], key: str) -> float | None:
    v = timing.get(key)
    return v if isinstance(v, float) else None


# Derived from the Phase 3 calibration sample (P3-1, design/calibration-note.md).
_HEARTBEAT_SECONDS = 34  # observed p95 167s; cadence ≤ p95/5 and ≥ 30s
_BASE_VISIBILITY_SECONDS = 235  # observed p95 + 2× heartbeat margin
# MinIO bucket name locally; the deployed task sets ARTIFACTS_BUCKET to the
# real durable bucket (eval-dev-artifacts-…, persistent/ — do NOT hardcode the
# local name here, it only matches in docker-compose).
_BUCKET = os.environ.get("ARTIFACTS_BUCKET", "eval-artifacts")


def run_harness_worker() -> None:
    """Poll ``harness-jobs`` indefinitely (the pre-H3 service path).  Not meant to return.

    ADR-0030/0032: the deployed harness pool is task-per-instance, so AWS runs
    ``run_harness_worker_job`` instead. This poll loop remains for local dev and
    for tests of the message-driven path.
    """
    # Review finding (2026-08-16): the deployed containers had NO logging handler,
    # so every logger.info decision point was discarded and CloudWatch showed
    # nothing unless the worker crashed. Configure first, then announce.
    from swebench_eval.logging_bootstrap import configure_logging

    configure_logging()
    _install_sigterm_handler()
    logger.info("harness-worker starting (polling harness-jobs)")
    while True:
        # ADR-0034 M1.3 gate #4 (local-dev path only): never receive while
        # paused — a paused consumer that receives and returns a message DLQs it.
        if _pool_paused("harness"):
            time.sleep(5)
            continue

        msg = receive_message("harness-jobs", wait_seconds=20)
        if msg is None:
            continue

        job = _parse_harness_job(msg["body"])
        logger.info(
            "Received harness job: %s/%s attempt %d",
            job.instance_id,
            job.harness_name,
            job.attempt_number,
        )
        # ADR-0037 / M0 §4: queue_wait_s falls free from the SQS system
        # attributes delivered on receive.
        from swebench_eval.workers import timing as tmod

        sent, received = tmod.parse_sqs_attributes(msg.get("attributes"))
        timing = _make_timing(
            queue_wait_s=tmod.seconds_between(sent, received),
            dispatched_at=job.dispatched_at,
            task_meta=tmod.fetch_task_metadata(),
        )
        _process_job(job, msg["receipt_handle"], timing)


def _pool_paused(pool: str) -> bool:
    """M1 gate: is *pool* paused?  Fail-closed — an unreadable/stale control
    read returns paused (ADR-0034 §2), because under-stop is the un-recoverable
    direction."""
    from swebench_eval.control import state as control_state

    return control_state.is_paused(pool)


def _abort_requested() -> bool:
    """True if SIGTERM has arrived (M1.4) — the abort executor is stopping us."""
    return _SHUTDOWN.is_set()


def run_harness_worker_job() -> None:
    """Run ONE harness job from the ADR-0032 job reference, then exit.

    The H3 task entrypoint: reads the reference from the environment
    (containerOverrides), loads the instance from the pinned mirror (the same
    mirror the dispatcher derived the env key from — gold excluded), builds the
    HarnessJob from it, and runs the same job-processing core as the poll loop
    (heartbeat on the receipt handle, delete on success, re-delivery on
    failure). Returns when the job is done; the container exits.

    No pause gate here (M1.3 gate #5 is "none"): the deployed task was ALREADY
    launched, so pause cannot unrecommence it — abort reaches it by SIGTERM
    (M1.4), captured via :func:`_install_sigterm_handler`.
    """
    from swebench_eval.dataset.swebench_loader import load_single_instance
    from swebench_eval.logging_bootstrap import configure_logging

    configure_logging()
    _install_sigterm_handler()

    reference = JobReference.from_env()
    logger.info(
        "harness-worker-job: %s/%s attempt %d (run %s)",
        reference.instance_id,
        reference.harness_name,
        reference.attempt_number,
        reference.run_id,
    )

    # Per-instance images (builder1-per-instance-images-build-plan §2a.2 / Q10):
    # fail fast if this job was launched against an image that was NOT baked for
    # it.  Placed AFTER from_env, BEFORE load_single_instance — a wrong-family
    # launch must not even construct the instance (a late check would let it load
    # the wrong repo from the mirror and get further than it should).
    from swebench_eval.harnesses.repo_prep import verify_testbed_prebaked

    verify_testbed_prebaked(reference.instance_id)

    instance = load_single_instance(reference.instance_id, include_gold=False)
    if instance is None:
        raise RuntimeError(f"Instance '{reference.instance_id}' not found in the dataset mirror")
    job = _job_from_reference(reference, instance)
    # ADR-0037 / M0 §4 + M0-6: the deployed task has no SQS receive (it was
    # launched by RunTask), so queue_wait_s is not derivable here — but the
    # DISPATCHER computed it and carried it on the reference.  Use it, plus the
    # stamped dispatched_at and the task metadata.
    from swebench_eval.workers import timing as tmod

    timing = _make_timing(
        queue_wait_s=reference.queue_wait_s,
        dispatched_at=reference.dispatched_at,
        task_meta=tmod.fetch_task_metadata(),
    )
    _process_job(job, reference.receipt_handle, timing)


def _with_run_instructions(job: HarnessJob) -> str:
    """The problem statement every adapter frames on top of, with the run's operator
    instructions (2026-09-09 efficiency prompt arm) appended under a fixed heading when the
    run published any. Read from Redis once per job — the ADR-0032 reference carries no
    prompt text and the payload's statement is never used on the deployed path. No entry,
    or Redis unreachable (logged by the client): the statement is byte-identical to before."""
    from swebench_eval.database.redis_client import read_harness_instructions
    from swebench_eval.harnesses.task_framing import with_harness_instructions

    text = read_harness_instructions(job.run_id)
    if text:
        logger.info(
            "harness instructions applied for run %s (%d chars) to %s/%d",
            job.run_id,
            len(text),
            job.instance_id,
            job.attempt_number,
        )
    return with_harness_instructions(job.problem_statement, text)


def _job_from_reference(reference: JobReference, instance: Instance) -> HarnessJob:
    """Rebuild the HarnessJob the job-processing core expects, from the reference.

    ``repo``/``base_commit``/``problem_statement`` come from the mirror row, NOT
    the payload — ADR-0032 removes the second source of truth (the worker
    already read this instance from the mirror for repo prep; now it is the only
    source). ``env_image_key`` is not carried and not needed by the task.
    """
    return HarnessJob(
        run_id=reference.run_id,
        instance_id=reference.instance_id,
        repo_url=f"https://github.com/{instance.repo}",
        base_commit=instance.base_commit,
        problem_statement=instance.problem_statement,
        attempt_number=reference.attempt_number,
        harness_name=reference.harness_name,
        model_alias=reference.model_alias,
        timeout_seconds=reference.timeout_seconds,
        max_tokens_per_instance=reference.max_tokens_per_instance,
        max_cost_usd_per_instance=reference.max_cost_usd_per_instance,
        max_turns_per_instance=reference.max_turns_per_instance,
        context_window_tokens=reference.context_window_tokens,
        dispatched_at=reference.dispatched_at,
        queue_wait_s=reference.queue_wait_s,  # M0-6: computed by the dispatcher
    )


def _apply_resolved_window(result: ResultMessage, job: HarnessJob) -> None:
    """D-1 (review 2026-08-26): write the DISPATCHER's resolved context window
    onto every result row.

    The window is a property of the ENDPOINT, resolved ONCE by the dispatcher
    and carried on the JOB — not something the harness reports back.  Only
    custom_minimal echoes it on its HarnessOutput; the three CLI harnesses never
    do, so every Stage 6 row came out NULL (a resolve-rate table without the
    window is a number without conditions).  Overwrite whatever the harness
    echoed with the job's authoritative value.
    """
    if job.context_window_tokens is not None:
        result.context_window_tokens = job.context_window_tokens


def _process_job(job: HarnessJob, receipt: str, timing: dict[str, Any] | None = None) -> None:
    """Run one job with the shared heartbeat/delete semantics.

    Shared by the poll loop (``run_harness_worker``) and the ADR-0032 single-job
    task (``run_harness_worker_job``): heartbeat extends the message's
    visibility, the result is pushed to ``results``, and the message is deleted
    only on success — a crash returns it to the queue for retry.

    ``timing`` is the ADR-0037 / M0 §4 per-job timing dict; ``worker_boot_s`` is
    filled here (process start → job ready, the described boundary).
    """
    # Start heartbeat thread.
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat,
        args=("harness-jobs", receipt, heartbeat_stop),
        daemon=True,
    )
    heartbeat_thread.start()

    # Bound before the try so `finally` can never see it undefined — a
    # BaseException (e.g. KeyboardInterrupt) skips the `except Exception`
    # branch but still runs `finally`.
    completed = False

    timing = dict(timing or {})
    timing["worker_boot_s"] = time.time() - _PROCESS_START_EPOCH

    try:
        # run-launch ledger (results_writer.py's state_rank ladder): the
        # HARNESS_RUNNING notice, emitted on pickup, before the agent loop
        # starts.  Mirrors eval_worker.py's EVAL_RUNNING emit — without it a
        # row sits at DISPATCHED for its entire runtime (image pull, repo
        # prep, the whole agent loop), indistinguishable from "still pulling
        # a 3 GB image."  Also what abort.py's settle-wait watches for to
        # know a task is genuinely still in flight (2026-08-28 finding).
        # Best-effort: a failed emit must not abort an otherwise-gradeable job.
        try:
            send_message(
                "results",
                _dataclass_to_dict(
                    ResultMessage(
                        run_id=job.run_id,
                        instance_id=job.instance_id,
                        attempt_number=job.attempt_number,
                        phase="harness",
                        state="HARNESS_RUNNING",
                    )
                ),
            )
        except Exception:
            logger.exception(
                "could not emit HARNESS_RUNNING for %s/%s attempt %d (job continues)",
                job.run_id,
                job.instance_id,
                job.attempt_number,
            )

        result = _run_harness(job, timing)
        # D-1 (review 2026-08-26): ensure the dispatcher-resolved window lands
        # on the row regardless of what the harness echoed back.
        _apply_resolved_window(result, job)
        # ADR-0034 M1.4: if this task received SIGTERM (the abort executor's
        # StopTask), the result is ABORTED_IN_FLIGHT regardless of what the
        # harness produced — the patch/trajectory were captured by the harness
        # and are uploaded by _run_and_collect; the run is terminal and
        # excluded from the denominator.  Deleted below (the job finished).
        if _abort_requested():
            result = _reclassify_aborted(result, job)
        send_message("results", _dataclass_to_dict(result))
        logger.info(
            "Completed harness job: %s/%s → %s",
            job.instance_id,
            job.harness_name,
            result.state,
        )
        completed = True
    except Exception:
        logger.exception("Harness worker crashed on %s", job.instance_id)
        # Deliberately do NOT delete: let the visibility timeout expire so the
        # message is re-claimed and the instance retried.  The `finally` below
        # used to delete unconditionally, which silently discarded the job and
        # made this comment a lie.
        completed = False
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=5)
        if completed:
            try:
                delete_message("harness-jobs", receipt)
            except Exception:
                # A failed delete must not kill the worker.  Previously this
                # propagated out of `finally` and terminated the whole thread,
                # so one bad receipt stopped the worker consuming entirely.
                # Worst case the message is redelivered and the instance is
                # retried, which the state machine already handles idempotently.
                logger.exception(
                    "Failed to delete harness-jobs message for %s; "
                    "it will be redelivered after the visibility timeout",
                    job.instance_id,
                )


def _reclassify_aborted(result: ResultMessage, job: HarnessJob) -> ResultMessage:
    """Rebuild a result as ABORTED_IN_FLIGHT after a SIGTERM capture (M1.4).

    The harness ran to the next boundary with its patch/trajectory intact; the
    abort must preserve those artifacts while marking the attempt terminal and
    excluded from the resolve-rate numerator AND denominator (ADR-0034 M1.8).

    PERSIST-TURNS-USED (2026-08-28): this was an explicit field allow-list that
    silently dropped anything not named there — the exact shape that lost the
    live turn count once (end-of-run Redis write with turn_number=0). Every new
    metering field (turns_used, cached/cache_write/reasoning_tokens, cost_source,
    usage_parse_failed_calls) had to be added by hand or an aborted instance
    lost it. Rebuild structurally via `dataclasses.replace` instead: carry every
    field by default, override only state + the terminal error detail. A future
    field is now carried automatically — the failure mode inverts from silent
    loss to visible over-carry.
    """
    if result.state == "ABORTED_IN_FLIGHT":
        return result
    logger.info(
        "SIGTERM received; reclassifying %s/%s attempt %d (was %s) -> ABORTED_IN_FLIGHT",
        job.run_id,
        job.instance_id,
        job.attempt_number,
        result.state,
    )
    return dataclasses.replace(
        result,
        phase="harness",
        state="ABORTED_IN_FLIGHT",
        error_detail=result.error_detail or "aborted by operator mid-run",
    )


def _parse_harness_job(body: dict[str, Any]) -> HarnessJob:
    """Parse the SQS message body into a HarnessJob (shared with the dispatcher)."""
    return HarnessJob.from_dict(body)


def _make_worker_workdir() -> Path:
    """R1: create the per-job workdir OUTSIDE the system temp dir.

    codex creates its helper binaries at runtime under $CODEX_HOME and REFUSES
    when CODEX_HOME lands inside the process's temp dir — so the sandbox never
    reaches PATH and every exec_command is rejected (12 paid calls burned on a
    dead shell).  ``dir=`` to instead of setting TMPDIR (setting TMPDIR makes
    that directory *the* temp dir; /var/tmp /opt /root are all refused).  The
    base is /var/harness by default, overridable (orchestrator / local dev).
    A non-root dev shell falls back to the system temp — codex is never run
    there, and the deployed container always has /var/harness.
    """
    try:
        _WORKDIR_BASE.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="harness-", dir=str(_WORKDIR_BASE)))
    except OSError:
        logger.warning(
            "workdir base %s not writable (non-root dev?); falling back to system temp — "
            "codex must NOT run against this base",
            _WORKDIR_BASE,
        )
        return Path(tempfile.mkdtemp(prefix="harness-"))


def _testbed_env_prefix() -> Path:
    """The conda env the agent's `python`/`pip` resolve to (the -hw/-inst PATH
    puts it first).  Overridable for local runs."""
    return Path(os.environ.get("TESTBED_ENV_PREFIX", "/opt/miniconda3/envs/testbed"))


def grant_agent_access(paths: list[Path]) -> None:
    """G2 (HARNESS-ISOLATION-AUDIT-2026-09-05 §3): make *paths* owned by the
    unprivileged agent user before the agent runs, and stop masking group/
    other bits on anything this worker creates from now on.

    Why chown at RUNTIME and not in the image: a `chown -R` layer duplicates
    every file it touches (a metadata change is a copy in overlayfs), which for
    /testbed + the conda env is gigabytes PER instance image × 500.  At runtime
    it costs seconds of copy-up in the task's ephemeral storage instead.  The
    official harness runs the agent as root, so parity requires the agent to
    be able to edit the repo in place AND pip-install into the env — hence both
    trees, not just the checkout.  No-op when separation is off (root agent).
    """
    from swebench_eval.harnesses.routing import agent_user

    user = agent_user()
    if user is None:
        return
    os.umask(0)  # files the worker writes from here on are readable/writable by the agent
    import subprocess

    for path in paths:
        if not path.exists():
            continue
        t0 = time.monotonic()
        proc = subprocess.run(
            ["chown", "-R", f"{user.uid}:{user.gid}", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            logger.warning(
                "G2: chown -R %s failed (rc=%d): %s", path, proc.returncode, proc.stderr.strip()
            )
            continue
        try:
            os.chmod(path, 0o777)  # the top dir (mkdtemp creates 0700)
        except OSError:
            pass
        logger.info(
            "G2: %s now owned by %s (uid %d), %.1fs",
            path,
            user.name,
            user.uid,
            time.monotonic() - t0,
        )


def _redis_progress_cb(job: HarnessJob) -> Callable[[int, Any], None]:
    """master-handover 3.4: a per-turn closure that writes LIVE Redis progress.

    The worker has NO Postgres write path (ADR-0007) — live progress lives in
    Redis (ADR-0018), TTL'd, fail-closed on the read side.  Previously
    write_progress fired ONCE at the end with turn_number=0; this makes the
    ADR-0037 §5 channel actually carry live data.  Returns a best-effort
    callback for the shim to invoke each serviced turn; a write failure is
    logged by the shim's guard and never fails the run.
    """

    def _cb(turn_number: int, usage: Any) -> None:
        from swebench_eval.database.redis_client import write_progress

        # METERING-COMPLETENESS: pass the whole Usage through the ONE shared
        # serializer (redis_client._usage_payload) so this per-turn write and the
        # end-of-run write emit the same shape — they drifted once and a
        # finished instance read as one that never started.
        write_progress(
            job.run_id,
            job.instance_id,
            job.attempt_number,
            turn_number=turn_number,
            usage=usage,
            harness=job.harness_name,
            model_alias=job.model_alias,
        )

    return _cb


def _run_harness(job: HarnessJob, timing: dict[str, Any] | None = None) -> ResultMessage:
    """Run the harness adapter and build a ResultMessage.

    ``timing`` (M0 §4) accumulates the phase-timing boundaries and lands on the
    ResultMessage so the results writer persists them 1:1 with the instance row.
    """
    from swebench_eval.harnesses.custom_minimal import CustomMinimalHarness
    from swebench_eval.harnesses.registry import HARNESS_ADAPTERS, SHIM_ROUTED_HARNESSES

    # ADR-0037 / M0 §1 (sole-meter rule): EVERY harness (custom_minimal
    # included) is budget-metered by the per-worker shim — its base URL env is
    # pointed at a local forwarder that sums usage in-process and trips the
    # watchdog on breach.  custom_minimal ALSO keeps its own in-loop budget trip
    # (Phase 1) as belt-and-braces; the shim is still the authoritative meter
    # (its figure is what instance_results records).

    workdir = _make_worker_workdir()
    shim = None
    usage = Usage()
    base_url = gateway_base_url()
    if job.harness_name in SHIM_ROUTED_HARNESSES:
        from swebench_eval.gateway.local_proxy import LocalProxy

        shim = LocalProxy(
            upstream_url=base_url,
            run_id=job.run_id,
            harness=job.harness_name,
            instance_id=job.instance_id,
            attempt_number=job.attempt_number,
            usage=usage,
            # M0 §2.5: the shim appends one line per model call to
            # llm_calls.jsonl in the job's output dir.  Must exist before the
            # shim starts serving (the recorder opens it on first write).
            output_dir=str(workdir),
            # R3 (builder1-REMAINING-WORK-single-handover): the shim is the
            # KILLER now, not just the meter — pass the per-instance ceilings
            # so it can refuse further calls (400 since 3.8) once crossed.
            max_tokens_per_instance=job.max_tokens_per_instance,
            max_cost_usd_per_instance=job.max_cost_usd_per_instance,
            # master-handover 3.12: the shim-enforced turn cap, so all five
            # harnesses get the same 500-turn limit regardless of native caps.
            max_turns_per_instance=job.max_turns_per_instance,
            # master-handover 3.4: write LIVE progress to Redis on every
            # serviced turn, not just once at the end with turn_number=0.  The
            # callback runs in the shim (best-effort, non-blocking).
            on_turn=_redis_progress_cb(job),
            # F1 (2026-09-04): the resolved window clamps the shim's injected
            # max_tokens (prompt + output <= W) for harnesses that send none.
            context_window_tokens=job.context_window_tokens,
        ).start()
        base_url = shim.local_base_url

    harness: Any

    # Resolve the harness adapter.  Dispatch from the registry map (P4C-2);
    # custom_minimal and the test-only stuck_stub are the only special cases.
    if job.harness_name == "custom_minimal":
        # master-handover 3.12: drive custom_minimal's native max_turns from the
        # job's value (shim's cap is authoritative; this is belt-and-braces).
        harness = CustomMinimalHarness(
            model=job.model_alias,
            max_turns=job.max_turns_per_instance if job.max_turns_per_instance is not None else 500,
        )
    elif job.harness_name == "stuck_stub":
        # Test-only harness (P4-2): registered ONLY when STUCK_ACTIVE_KILL=1, so
        # a normal worker never knows this name.  Lives in tests/.
        if not os.environ.get("STUCK_ACTIVE_KILL") == "1":
            raise ValueError(f"Unknown harness: {job.harness_name}")
        from tests.stuck_stub_harness import StuckStubHarness

        harness = StuckStubHarness()
    else:
        # KeyError → unknown harness (fail closed, same as before).
        harness_class = HARNESS_ADAPTERS[job.harness_name]
        harness = harness_class(
            api_base_url=base_url, api_key=gateway_api_key(), model=job.model_alias
        )

    try:
        return _run_and_collect(job, harness, usage, shim, workdir, timing)
    finally:
        if shim is not None:
            shim.stop()


def _run_and_collect(
    job: HarnessJob,
    harness: Any,
    usage: Usage,
    shim: Any,
    workdir: Path,
    timing: dict[str, Any] | None = None,
) -> ResultMessage:
    """Run the harness, enforce the budget, and collect the ResultMessage.

    ``usage`` / ``shim`` are the ADR-0019 in-process running total: for subprocess
    CLIs the shim sums each response's usage; on breach the watchdog trips → the
    result is ``budget_exceeded`` / ``HARNESS_BUDGET_EXCEEDED`` (active, no
    detect-and-log for a ceiling).  The stuck detector runs detect-and-log only.

    ``timing`` (M0 §4) accumulates the phase boundaries; the metadata-derived
    columns (provision/image_pull/task_observed/task_billed) are resolved here
    against the worker-exit instant, and everything lands on the ResultMessage.
    """
    from swebench_eval.database.state_machine import (
        map_terminated_reason_to_error_category,
        map_terminated_reason_to_state,
    )

    # ADR-0043: the per-instance image carries the hardened, installed repo at
    # /testbed (SWE-bench's own published image, digest-pinned, plus our
    # layers); the worker verifies the sentinel and hands the tree over.  There
    # is no runtime repo preparation any more.
    from swebench_eval.dataset.swebench_loader import load_single_instance

    timing = dict(timing or {})
    # M0 §4.3 repo_prep_s: what remains of the phase is the sentinel check; the
    # number stays so the column's meaning is continuous across the upgrade.
    _repo_prep_start = time.monotonic()
    checkout = _prepare_harness_repo(job, load_single_instance)
    timing["repo_prep_s"] = time.monotonic() - _repo_prep_start
    # G2: the agent subprocess runs as the unprivileged user (routing.agent_user);
    # hand it the tree it works in + the env it pip-installs into + our scratch.
    grant_agent_access([checkout, _testbed_env_prefix(), workdir])
    # N-5 (review): repo_prep_cache_hit is deliberately NULL, not False.  Repo
    # prep always runs the full install today, so "measured False" and "not
    # implemented" would be indistinguishable — the one thing Trap 3 exists to
    # prevent.  Leave it NULL until ADR-0010/0031's cache makes the bimodal hit
    # measurable, then set the real boolean.
    run_id = job.run_id  # run_id comes from the dispatcher, not the worker

    # Model routing: subprocess harnesses were constructed with the shim base URL
    # (or the gateway when absent); custom_minimal reads model_config directly.
    model_base_url = shim.local_base_url if shim is not None else gateway_base_url()

    harness_input = HarnessInput(
        instance_id=job.instance_id,
        repo_url=job.repo_url,
        base_commit=job.base_commit,
        # 2026-09-09 efficiency prompt arm: the run's operator instructions (Redis, published
        # at launch) appended under a fixed heading — every adapter frames on top of this.
        problem_statement=_with_run_instructions(job),
        attempt_number=job.attempt_number,
        repo_checkout_path=str(checkout),
        output_dir=str(workdir),
        model_config=ModelConfig(
            gateway_base_url=model_base_url,
            gateway_api_key=gateway_api_key(),
            model_name=job.model_alias,
            # V8: NO temperature is sent (ModelConfig.temperature=None), so the
            # harness inherits the gateway's per-model pin (litellm_config.yaml).
            # The old temperature=0.0 made custom_minimal the only harness with
            # a request-body sampling param — greedy 0.0 vs everyone else's pin.
            # PART2 §1 (2026-08-23): no max_tokens field — the per-call
            # completion cap was REMOVED.  See harness.py's comment: it made
            # custom_minimal the only harness carrying a framework-imposed cap.
        ),
        timeout_seconds=job.timeout_seconds,
        max_tokens_per_instance=job.max_tokens_per_instance,
        max_cost_usd_per_instance=job.max_cost_usd_per_instance,
        max_turns_per_instance=job.max_turns_per_instance,
        # Compaction build (Stage 1.2): the resolved per-model context window,
        # threaded so the harness sizes its compaction threshold.  None =
        # compaction disabled.  The worker/adapter never look it up — one
        # resolution per run, in the dispatcher.
        context_window_tokens=job.context_window_tokens,
        # master-handover 3.3: hand the adapter the shim's LIVE Usage so its TRAJ
        # lines can carry cumulative tokens/cost without its own accounting.
        live_usage=usage,
    )

    # F3 (2026-08-24, harness-05 D1): an exception inside harness.run() used to
    # lose EVERY artifact — the upload block below never ran and the one run
    # that would tell us most about a crash was the one we could not inspect
    # (the malformed-tool-call crash: 22+ turns, nothing uploaded).  Catch it,
    # surface a minimal crash result, and STILL run the artifact upload: the
    # llm_calls.jsonl (shim) and any partial trajectory (adapter best-effort
    # flush) ride out on the artifact path instead of dying with the raise.
    try:
        output = harness.run(harness_input)
    except Exception as exc:  # BLE001
        logger.exception("harness.run raised for %s", job.instance_id)
        from swebench_eval.harnesses.base import HarnessOutput

        output = HarnessOutput(
            patch="",
            success=False,
            trajectory_path=str(workdir / "trajectory.jsonl"),
            raw_log_path=str(workdir / "harness_stdout.log"),
            wall_clock_seconds=time.time() - _PROCESS_START_EPOCH,
            error=f"harness.run raised: {exc}",
            terminated_reason="crash",
        )
        # The upload block below checks os.path.exists, so absent partial files
        # are skipped; llm_calls.jsonl (written by the shim as it serviced the
        # calls) WILL exist and is uploaded.

    # M0 §4.3 agent_s = the adapter's own wall-clock timer (already correct).
    timing["agent_s"] = output.wall_clock_seconds

    # ADR-0034 M1.11: if the gateway was paused (the ALB's stable
    # framework_paused 503, passed through the shim unmangled), this instance's
    # model calls failed BECAUSE the operator paused — not because the model or
    # the framework broke.  Surface it as PAUSED_BY_OPERATOR so the run is not
    # polluted with data-corrupting MODEl_API_ERROR failures that were never
    # failures.
    if shim is not None and shim.paused:
        output.terminated_reason = "model_api_error"
        output.error_category = "PAUSED_BY_OPERATOR"
        output.error = "gateway paused by operator (framework_paused 503)"

    # R5.3: a shim-routed harness that terminates with ZERO model calls never
    # reached the model — categorically an infrastructure failure (opencode
    # produced no llm_calls.jsonl at all and was written up as a gateway error).
    # Pure function for the two-sided test.
    _classify_zero_model_calls(output, shim)

    # Budget watchdog — see _classify_budget_breach (R3: the shim is the primary
    # killer via 429; this post-hoc check is the BACKSTOP and must NOT overwrite
    # `timeout`).
    _classify_budget_breach(output, usage, job, shim)

    # Stuck-loop detection — DETECT-AND-LOG ONLY (Phase 4) by default: log a
    # would-kill WARNING, do NOT change terminated_reason.  Phase 8 promotes to
    # active kill.  The ONE exception is the STUCK_ACTIVE_KILL=1 test path (P4-2)
    # which uses the stuck_stub harness to prove the kill works end-to-end.
    # MUST run before uploading artifacts so the patch carries the `stuck` tag
    # (ADR-0016 tagging happens at upload).
    verdict = _detect_and_log_stuck(output)
    if os.environ.get("STUCK_ACTIVE_KILL") == "1" and verdict is not None and verdict.stuck:
        output.terminated_reason = "stuck"
        output.error_category = "HARNESS_STUCK"
        output.error = " ".join(verdict.evidence)

    # Upload artifacts to MinIO.
    s3_prefix = (
        f"runs/{run_id}/{job.harness_name}/{job.model_alias}/{job.instance_id}/{job.attempt_number}"
    )

    # M0 §4.3 artifact_upload_s: the S3 artifact phase, timed till the last
    # graded artifact lands (the llm_calls ship below is transport, not a graded
    # artifact — it gets its own timing, not this).
    _upload_t0 = time.monotonic()

    patch_key = ""
    trajectory_key = ""
    raw_log_key = ""
    native_trajectory_key = ""

    # ADR-0016: a forceful (non-terminal) kill's partial patch is tagged with
    # its terminated_reason so Phase 7 surfaces it distinctly from a graded diff.
    patch_meta = {}
    if output.terminated_reason != "completed":
        patch_meta = {"terminated_reason": str(output.terminated_reason)}
    if output.patch:
        patch_key = upload_artifact(
            _BUCKET, f"{s3_prefix}/patch.diff", output.patch, metadata=patch_meta or None
        )
    if output.trajectory_path and os.path.exists(output.trajectory_path):
        trajectory_key = upload_artifact(
            _BUCKET,
            f"{s3_prefix}/trajectory.jsonl",
            Path(output.trajectory_path).read_text(),
        )
    if output.raw_log_path and os.path.exists(output.raw_log_path):
        raw_log_key = upload_artifact(
            _BUCKET,
            f"{s3_prefix}/harness_stdout.log",
            Path(output.raw_log_path).read_text(),
        )
    # B6/E11a: mini's NATIVE trajectory (pre-normalisation) used to die with the
    # task. Upload it beside the normalized trajectory when the adapter surfaced
    # one — an explicit path for adapters that have a separate native artifact,
    # absent for the CLI harnesses whose "native" stream IS harness_stdout.log.
    if output.native_trajectory_path and os.path.exists(output.native_trajectory_path):
        native_trajectory_key = upload_artifact(
            _BUCKET,
            f"{s3_prefix}/native_trajectory.json",
            Path(output.native_trajectory_path).read_text(),
        )
    # Compaction-point record (2026-09-02): the full per-compaction detail
    # (at_model_call / trigger / pre_tokens / post_tokens per pass) as its own
    # artifact — the instance_results columns keep only count + last pass, and
    # results analysis needs the positions.  Absent when the harness observed
    # none (None, never a fabricated []).
    if output.compaction_events:
        upload_artifact(
            _BUCKET,
            f"{s3_prefix}/compaction_events.json",
            json.dumps(output.compaction_events, indent=2) + "\n",
        )
    timing["artifact_upload_s"] = time.monotonic() - _upload_t0

    # ADR-0037 / M0 §3 — transport: llm_calls.jsonl rides the FREE S3 gateway
    # endpoint (ADR-0033 §75), with ONE pointer message to the dedicated
    # `llm-calls` queue (NOT results — M2 uses results depth as a signal, and 40
    # call-rows per result-row would ruin it).  No message when nothing was
    # recorded.  The writer bulk-inserts with ON CONFLICT DO NOTHING, so a
    # whole-batch redelivery changes no row count.
    llm_calls_path = workdir / "llm_calls.jsonl"
    if llm_calls_path.exists() and llm_calls_path.stat().st_size > 0:
        try:
            with llm_calls_path.open(encoding="utf-8") as fh:
                llm_calls_row_count = sum(1 for _ in fh)
            llm_calls_key = upload_artifact(
                _BUCKET,
                f"{s3_prefix}/llm_calls.jsonl",
                llm_calls_path.read_text(),
            )
            send_message(
                "llm-calls",
                {
                    "run_id": run_id,
                    "instance_id": job.instance_id,
                    "attempt_number": job.attempt_number,
                    "harness": job.harness_name,
                    "s3_key": llm_calls_key,
                    "row_count": llm_calls_row_count,
                },
            )
        except Exception:
            # A lost call-row is a lost data point, never a lost outcome — a
            # failure to ship must not fail the run itself.
            logger.exception("Failed to ship llm_calls artifact for %s", job.instance_id)

    state = map_terminated_reason_to_state(output.terminated_reason, output.patch)
    error_cat = (
        output.error_category
        or map_terminated_reason_to_error_category(output.terminated_reason, output.patch)
        or ""
    )

    # Write live progress to Redis (ADR-0018 / R3-2) — NOT Postgres.  Advisory
    # data; a TTL'd key expires itself if this worker dies.  The worker has NO
    # Postgres write path (ADR-0007 now has zero exceptions).
    try:
        from swebench_eval.database.redis_client import write_progress

        # M7 (review 2026-08-25): end-of-run write must keep the LIVE turn count
        # (the shim's serviced completions), not erase it with turn_number=0 on
        # the same Redis key after the per-turn writes. 0 made a finished
        # instance read as one that never started on the dashboard.
        #
        # METERING-COMPLETENESS: pass the whole Usage through the ONE shared
        # serializer (same shape as the per-turn callback), and the LIVE turn
        # count from the shim, so the final write reflects the full run.
        write_progress(
            run_id,
            job.instance_id,
            job.attempt_number,
            turn_number=getattr(shim, "turns_used", 0) if shim is not None else 0,
            usage=usage,
            harness=job.harness_name,
            model_alias=job.model_alias,
        )
    except Exception:
        logger.exception("Failed to write Redis progress for %s", job.instance_id)

    # ADR-0037 / M0 §1: the shim is the SOLE meter.  `usage` is the shim's
    # accumulated total (it now routes every harness, custom_minimal included).
    # The adapter's own parse, where one exists, rides as
    # `output.adapter_reported_usage` — a CROSS-CHECK never added, so a silent
    # 2× (the pre-M0 defect) surfaces as a disagreement rather than a doubled
    # figure.  The authoritative number is `usage`.
    #
    # R2-1 (review): NO fallback here.  `adapter_reported_usage` is None unless an
    # adapter populated it (the four that parse usage do; aider/mini_swe_agent do
    # not).  None → NULL in the cross-check columns, which the §8 gate can skip
    # honestly — a fallback copying `output.usage` would write 0 for the two
    # harnesses that never parse (false disagreement every run).
    _au = output.adapter_reported_usage
    adapter_input = int(_au.input_tokens) if _au is not None else None
    adapter_output = int(_au.output_tokens) if _au is not None else None
    adapter_cost = float(_au.cost_usd) if _au is not None else None

    # M0 §4.3 metadata-derived columns, resolved against the worker-exit
    # instant.  All NULL-able (Trap 3): off-ECS / no metadata / no dispatched_at
    # → the column stays NULL, never a fabricated number.
    #
    # ``patch_extract_s`` (R2-3): measured by the shared git helper INSIDE the
    # adapter (git_utils.git_diff fills out["patch_extract_s"]) and carried on
    # output.patch_extract_s — the adapter returns the only number it can.  NULL
    # when not measured (e.g. the harness crashed before the diff ran).
    timing["patch_extract_s"] = output.patch_extract_s
    from swebench_eval.workers import timing as tmod

    t_meta = timing.get("task_meta")
    if t_meta is not None:
        now = time.time()
        if t_meta.container_started_at is not None:
            timing["task_observed_s"] = tmod.seconds_between(t_meta.container_started_at, now)
        if t_meta.pull_started_at is not None:
            timing["task_billed_s"] = tmod.seconds_between(t_meta.pull_started_at, now)
        dispatched = _timing_float(timing, "dispatched_at")
        if dispatched is not None and t_meta.container_created_at is not None:
            timing["provision_s"] = tmod.seconds_between(dispatched, t_meta.container_created_at)
        timing["image_pull_s"] = t_meta.image_pull_s
        timing["image_pull_cold"] = t_meta.image_pull_cold

    # B5 (stage-c-handover-round-2 §4): leak detection MOVED OFF the harness
    # tier.  It previously ran here, which required the leak-detectable map in
    # the harness image — handing the unsandboxed agent the detector's answer
    # key (ADR-0038's premise depends on the agent NOT being able to read the
    # gold-derived absent-node-id list).  Detection is now an OFFLINE pass that
    # reads the stored patch.diff + trajectory.jsonl from S3 per attempt and
    # UPDATEs leaked_node_ids / leak_detectable (scripts/backfill_leak_detection
    # .py or the harness_result writer).  leaked_node_ids / leak_detectable are
    # NULL here until that backfill runs (Trap 3: unknown, not fabricated).
    return ResultMessage(
        run_id=run_id,
        instance_id=job.instance_id,
        attempt_number=job.attempt_number,
        phase="harness",
        state=state,
        error_category=error_cat,
        error_detail=output.error,
        wall_clock_s=output.wall_clock_seconds,
        patch_s3_key=patch_key,
        trajectory_s3_key=trajectory_key,
        raw_log_s3_key=raw_log_key,
        native_trajectory_s3_key=native_trajectory_key,
        # Sole meter: the shim's usage.  NOT the adapter's + the shim's.
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=usage.cached_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        # METERING-COMPLETENESS §2.3: reconcile the vocabulary.  Usage.source is
        # "harness"|"gateway"; the cost_source COLUMN is "provider"|"local_pricing".
        # We do NOT pipe source through raw (the UI would mislabel it "cost
        # source").  Map explicitly here: the authoritative provider-cost flag is
        # "was API-reported cost used", which is exactly `source == "gateway"`.
        # 0 stored as None keeps the existing "no usage / not harness phase"
        # convention; the shim always sets source, so this is never None on a
        # shim-routed run.
        cost_source=("provider" if usage.source == "gateway" else "local_pricing"),
        usage_parse_failed_calls=usage.usage_parse_failed_calls,
        # Pacer rollup (BUILDER4-PACER-SEED-AND-FAIRNESS §2.5): the shim folds every
        # admission/timeout/retry into Usage as it happens; this is the durable copy.
        paced_wait_ms_total=usage.paced_wait_ms_total,
        paced_calls=usage.paced_calls,
        pacer_timeouts=usage.pacer_timeouts,
        overload_retries_total=usage.overload_retries_total,
        # PERSIST-TURNS-USED (2026-08-28): the shim's serviced-completion count —
        # the ONE harness-neutral turn definition (a forwarded model completion;
        # probes/model-lists/count_tokens/refusals excluded).  harness-phase only;
        # NULL on eval rows.  Never derived from COUNT(*) of llm_calls (that is
        # >= turns_used — non-completions still get a call record).  This is one
        # of only two enforced bounds now (the other is cost), so which bound bit
        # must be readable from the column.
        turns_used=getattr(shim, "turns_used", 0) if shim is not None else None,
        cost_usd=usage.cost_usd,
        # M0 §1.3 cross-check: the adapter's own figure, stored not added.
        adapter_input_tokens=adapter_input,
        adapter_output_tokens=adapter_output,
        adapter_cost_usd=adapter_cost,
        # ADR-0037 / M0 §4 harness timing (None = not measured, Trap 3).
        queue_wait_s=_timing_float(timing, "queue_wait_s"),
        provision_s=_timing_float(timing, "provision_s"),
        image_pull_s=_timing_float(timing, "image_pull_s"),
        worker_boot_s=_timing_float(timing, "worker_boot_s"),
        repo_prep_s=_timing_float(timing, "repo_prep_s"),
        agent_s=_timing_float(timing, "agent_s"),
        patch_extract_s=_timing_float(timing, "patch_extract_s"),  # R3-1
        artifact_upload_s=_timing_float(timing, "artifact_upload_s"),
        task_observed_s=_timing_float(timing, "task_observed_s"),
        task_billed_s=_timing_float(timing, "task_billed_s"),
        repo_prep_cache_hit=timing.get("repo_prep_cache_hit"),
        image_pull_cold=timing.get("image_pull_cold"),
        # B5: leak fields are left at their ResultMessage None defaults here —
        # the offline backfill populates them (moved off the harness tier).
        # Compaction build (BUILD-SPEC §6): per-instance compaction measurement
        # carried to instance_results.  None = no pass ran (the common case;
        # the 0/absent IS the evidence, kept honest as NULL, not fabricated).
        compactions_fired=output.compactions_fired,
        compaction_tokens_before=output.compaction_tokens_before,
        compaction_tokens_after=output.compaction_tokens_after,
        context_window_tokens=output.context_window_tokens,
    )


def _prepare_harness_repo(
    job: HarnessJob,
    loader: Callable[[str], Instance | None],
) -> Path:
    """Hand the adapter the image's baked /testbed (ADR-0043: no runtime prep).

    Returns the checkout path.  The instance is loaded from the S3 dataset
    mirror (public row — gold patch excluded) so an unknown id fails here
    rather than in the adapter; ``loader`` is injected so tests can fake it.

    Per-instance images (§2a.2): the image carries a pre-baked, hardened repo
    and a sentinel naming the instance.  The H3 job entrypoint already failed
    closed on a missing/mismatched sentinel before reaching here; this is the
    same check for the local poll path and tests.  There is deliberately NO
    fallback when the sentinel is absent: the runtime has no internet, and a
    silent re-clone would reintroduce exactly the gold-leak the sentinel design
    removed.
    """
    instance = loader(job.instance_id)
    if instance is None:
        raise RuntimeError(f"Instance '{job.instance_id}' not found in dataset")
    checkout = Path("/testbed")

    from swebench_eval.harnesses.repo_prep import verify_testbed_prebaked

    sentinel = verify_testbed_prebaked(job.instance_id)
    logger.info(
        "testbed pre-baked (built %s from %s); handing /testbed to the adapter",
        sentinel.get("prepared_at"),
        sentinel.get("base_image_digest", "<no base digest recorded>"),
    )
    return checkout


def _classify_zero_model_calls(output: HarnessOutput, shim: Any) -> None:
    """R5.3: a shim-routed run that terminates with ZERO model calls is infra.

    The shim (when present) is the only component that sees every request, so
    ``calls_made == 0`` means the harness never reached the model — categorically
    an infrastructure failure (opencode produced no llm_calls.jsonl at all and
    was written up as a gateway error).  ONLY override a "completed" finish: a
    real crash/timeout/etc. keeps its own (more specific) cause.  A paused run
    made calls (and got the 503), so it can never have zero calls here.
    """
    if shim is not None and shim.calls_made == 0 and output.terminated_reason == "completed":
        output.terminated_reason = "zero_model_calls"
        output.error_category = "HARNESS_ZERO_MODEL_CALLS"
        output.error = "harness terminated without any model call reaching the shim"


def _classify_budget_breach(
    output: HarnessOutput, usage: Usage, job: HarnessJob, shim: Any
) -> None:
    """R3 post-hoc budget backstop (the shim is the PRIMARY killer).

    R3 (builder1-REMAINING-WORK-single-handover): the shim refuses further calls
    with 429 once the ceiling is crossed, bounding overshoot to one call.  This
    post-hoc check stays as the BACKSTOP for a CLI that ignores the error.  It
    must NOT overwrite ``timeout``: mini's real cause was a wall-clock stop
    (1800.188s == TIMEOUT_SECONDS); stamping budget_exceeded on top made every
    count of timeouts read zero.  Distinct terminal reasons (max_turns, refused,
    malformed_tool_calls, patch_extract_timeout…) also keep their own cause — the
    breach is recorded, not the cause.
    """
    if shim is None:
        return
    # N1 (review): read the shim's OWN refusal flag first — the shim is the
    # primary killer, so on a breach it set budget_refused=True before the CLI
    # gave up.  Reading it here (instead of only re-deriving from `usage`) keeps
    # the shim's decision and the worker's backstop from drifting apart, and the
    # attribute is not dead.  The usage re-derivation below is the equivalent
    # safety net.
    breached = bool(getattr(shim, "budget_refused", False))
    if not breached:
        if (
            job.max_tokens_per_instance is not None
            and usage.input_tokens + usage.output_tokens >= job.max_tokens_per_instance
        ):
            breached = True
        if (
            job.max_cost_usd_per_instance is not None
            and usage.cost_usd >= job.max_cost_usd_per_instance
        ):
            breached = True
    if not breached:
        return
    if output.terminated_reason == "timeout":
        logger.warning(
            "budget also exceeded for %s but run stopped by TIMEOUT "
            "(%ss); keeping timeout as the cause",
            job.instance_id,
            output.wall_clock_seconds,
        )
    elif output.terminated_reason in ("budget_exceeded", "completed", "crash"):
        output.terminated_reason = "budget_exceeded"
        output.error = (
            f"budget exceeded: {usage.input_tokens + usage.output_tokens} tokens "
            f"/ ${usage.cost_usd:.4f}"
        )
        output.error_category = "HARNESS_BUDGET_EXCEEDED"
    else:
        # A distinct terminal reason (max_turns, refused, malformed_tool_calls,
        # patch_extract_timeout...) — the run ended for a more specific cause;
        # the budget breach is a data point, not the cause.  Keep the reason.
        logger.warning(
            "budget also exceeded for %s but run ended %s; keeping that cause",
            job.instance_id,
            output.terminated_reason,
        )


def _detect_and_log_stuck(
    output: HarnessOutput,
) -> StuckVerdict | None:
    """Run the stuck-loop detector in DETECT-AND-LOG mode (§9.5, Phase 4).

    Logs a would-kill WARNING when a stuck verdict fires and returns the verdict
    (the caller may, under STUCK_ACTIVE_KILL=1, escalate it to a kill).  Does
    NOT change ``terminated_reason`` on its own — promotion to active early-kill
    is Phase 8's call; the stuck threshold is inferred and needs multi-harness
    validation before it can be trusted to kill.
    """
    from swebench_eval.harnesses.stuck_detector import evaluate_trajectory_file

    if not output.trajectory_path or not os.path.exists(output.trajectory_path):
        return None
    verdict = evaluate_trajectory_file(output.trajectory_path)
    if verdict.stuck:
        logger.warning(
            "LOGGED HARNESS_STUCK (would-kill): %s — %s",
            verdict.reason,
            verdict.evidence,
        )
    else:
        # Three-state (P4C-4): surface insufficient_data vs not_stuck so the phase
        # summary can tell "detector saw nothing" from "nothing to see".
        logger.info("stuck detector: state=%s reason=%s", verdict.state, verdict.reason)
    return verdict


def _heartbeat(queue_name: str, receipt_handle: str, stop: threading.Event) -> None:
    """Extend message visibility every _HEARTBEAT_SECONDS until stopped."""
    while not stop.wait(_HEARTBEAT_SECONDS):
        try:
            change_message_visibility(queue_name, receipt_handle, _BASE_VISIBILITY_SECONDS)
        except Exception:
            logger.exception("Heartbeat failed for %s", queue_name)


def _generate_run_id() -> str:
    ts = time.time_ns()
    suffix = uuid.uuid4().hex[:8]
    return f"{ts:020d}-{suffix}"


def _dataclass_to_dict(obj: Any) -> dict[str, Any]:
    import dataclasses

    return dataclasses.asdict(obj)
