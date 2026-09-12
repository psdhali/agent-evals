"""Eval worker — polls eval-jobs, runs grading, pushes results.

Same poll-loop shape as the harness worker.  Receives an EvalJob from the
eval-jobs queue, downloads the patch from MinIO, runs SwebenchRunner.grade(),
uploads the eval report, and pushes a ResultMessage to the results queue.
"""

from __future__ import annotations

import logging
import os
import signal
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from swebench_eval.evaluation import grade_containers
from swebench_eval.evaluation.grading_adapter import GradingInput, GradingOutput
from swebench_eval.evaluation.swebench_runner import SwebenchRunner
from swebench_eval.queue.client import (
    change_message_visibility,
    delete_message,
    get_artifact,
    receive_message,
    send_message,
    upload_artifact,
)
from swebench_eval.queue.schemas import EvalJob, ResultMessage
from swebench_eval.workers.task_protection import set_protection

logger = logging.getLogger(__name__)

# Derived from the Phase 3 calibration sample (P3-1, design/calibration-note.md).
_HEARTBEAT_SECONDS = 34
_BASE_VISIBILITY_SECONDS = 300
# The heartbeat thread wakes this often so a SIGTERM mid-grade is acted on
# within a second, not at the next 34 s tick (the ECS stop grace is 30 s).
_HEARTBEAT_POLL_SECONDS = 1.0
# BUILDER4-EVAL-PACKING §6 (scale-in): ECS task scale-in protection is set
# for the duration of a grade and refreshed from the heartbeat thread. The
# expiry is the safety net for a worker that dies silently — long enough that
# a refresh never lapses mid-grade, short enough that a dead task cannot pin
# itself for hours (the SWE-bench test timeout is 30 min; refresh every 5).
_PROTECTION_MINUTES = 15
_PROTECTION_REFRESH_SECONDS = 300
# Eval scaling review F1: how often an idle worker sweeps the host for orphaned grading
# containers (sweb.eval.* older than any legitimate grade — grade_containers.ORPHAN_AGE_S).
_ORPHAN_SWEEP_SECONDS = 300
# MinIO bucket name locally; the deployed task sets ARTIFACTS_BUCKET to the
# real durable bucket (eval-dev-artifacts-…, persistent/).
_BUCKET = os.environ.get("ARTIFACTS_BUCKET", "eval-artifacts")

# BUILDER4-EVAL-PACKING §6: SIGTERM is how ECS stops this task — a scale-in of
# an idle worker (fine), a draining spot host, or a deployment. Same shape as
# the harness worker's M1.4 handler: the handler only sets a flag; the loop
# stops receiving at its next iteration, and the heartbeat thread of a grade
# in flight hands the job straight back to the queue (visibility 0) so another
# worker picks it up NOW instead of after the 300 s visibility lapses.
_SHUTDOWN = threading.Event()


def _install_sigterm_handler() -> None:
    signal.signal(signal.SIGTERM, lambda *_: _SHUTDOWN.set())


def run_eval_worker() -> None:
    """Poll ``eval-jobs`` indefinitely.  Returns only on SIGTERM."""
    # Review finding (2026-08-16): configure INFO logging first so CloudWatch
    # records receive/enqueue/grade decisions, not only crash tracebacks.
    from swebench_eval.logging_bootstrap import configure_logging

    configure_logging()
    _install_sigterm_handler()
    logger.info("eval-worker starting (polling eval-jobs)")
    # Eval scaling review F1: a recycled host heals itself — sweep orphaned grading
    # containers at start and every _ORPHAN_SWEEP_SECONDS while idle (the sweep only ever
    # touches containers older than any live grade can be).
    _sweep_orphans()
    last_sweep = time.monotonic()
    while True:
        if _SHUTDOWN.is_set():
            logger.info("SIGTERM received; eval-worker stops receiving and exits")
            return
        if time.monotonic() - last_sweep >= _ORPHAN_SWEEP_SECONDS:
            _sweep_orphans()
            last_sweep = time.monotonic()
        # ADR-0034 M1.3 gate #3: never receive while eval is paused — a paused
        # consumer that receives and returns a message DLQs it.
        if _pool_paused("eval"):
            time.sleep(5)
            continue
        msg = receive_message("eval-jobs", wait_seconds=20)
        if msg is None:
            continue

        receipt = msg["receipt_handle"]
        job = _parse_eval_job(msg["body"])

        # Scale-in protection for the whole grade (refreshed by the heartbeat
        # thread). False = not under ECS or the agent refused — logged there;
        # the grade runs regardless, it is just eligible for scale-in as before.
        protected = set_protection(True, _PROTECTION_MINUTES)

        # ADR-0037 / M0 §5: eval_queue_wait_s from the SQS system attributes.
        from swebench_eval.workers import timing as tmod

        sent, received = tmod.parse_sqs_attributes(msg.get("attributes"))
        eval_timing: dict[str, object] = {"eval_queue_wait_s": tmod.seconds_between(sent, received)}

        logger.info(
            "Received eval job: %s/%s attempt %d",
            job.run_id,
            job.instance_id,
            job.attempt_number,
        )

        heartbeat_stop = threading.Event()
        released = threading.Event()  # set by the heartbeat if SIGTERM hands the job back
        heartbeat_thread = threading.Thread(
            target=_heartbeat,
            args=("eval-jobs", receipt, heartbeat_stop),
            kwargs={"protected": protected, "released": released, "job": job},
            daemon=True,
        )
        heartbeat_thread.start()

        # Delete ONLY on success — a crash (e.g. a transient ECR/registry
        # failure during grading) must return the message to the queue for
        # retry, never silently discard the grade. The unconditional `finally`
        # delete deleted the job on the first failure (found live: the eval
        # job vanished after the env-image login 400).
        completed = False
        try:
            # D6 / run-launch §6.2: the eval RUNNING ledger notice, emitted
            # when the job is picked up — BEFORE launching DinD grading, so a
            # worker that dies mid-grade still leaves a state ahead of the
            # pre-seeded PENDING row for the reaper's rule 2 to measure a
            # deadline from.  Best-effort: a failed emit must not abort an
            # otherwise-gradeable job.
            try:
                send_message(
                    "results",
                    _dataclass_to_dict(
                        ResultMessage(
                            run_id=job.run_id,
                            instance_id=job.instance_id,
                            attempt_number=job.attempt_number,
                            phase="eval",
                            state="EVAL_RUNNING",
                        )
                    ),
                )
            except Exception:
                logger.exception(
                    "could not emit EVAL_RUNNING for %s/%s attempt %d (grading continues)",
                    job.run_id,
                    job.instance_id,
                    job.attempt_number,
                )

            result = _run_eval(job, eval_timing)
            send_message("results", _dataclass_to_dict(result))
            logger.info("Completed eval job: %s/%s → %s", job.run_id, job.instance_id, result.state)
            completed = True
        except Exception:
            logger.exception("Eval worker crashed on %s", job.instance_id)
            completed = False
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=5)
            if completed:
                if released.is_set():
                    # SIGTERM mid-grade handed the job back and the grade still
                    # finished inside the stop grace: the verdict is sent (the
                    # writer's upsert makes a duplicate from the re-grade
                    # harmless) and the delete is attempted on a receipt that
                    # may already belong to another worker.
                    logger.warning(
                        "grade for %s completed after SIGTERM released it; "
                        "a duplicate re-grade may follow",
                        job.instance_id,
                    )
                try:
                    delete_message("eval-jobs", receipt)
                except Exception:
                    logger.exception(
                        "Failed to delete eval-jobs message for %s; "
                        "it will be redelivered after the visibility timeout",
                        job.instance_id,
                    )
            if protected and not _SHUTDOWN.is_set():
                # Idle again: eligible for scale-in. Skipped on shutdown — the
                # task is stopping anyway and the agent is on its way out.
                set_protection(False)


def _sweep_orphans() -> None:
    """Best-effort host sweep (grade_containers.sweep_orphans); never affects the loop."""
    try:
        grade_containers.sweep_orphans()
    except Exception:
        logger.warning("orphan sweep failed", exc_info=True)


def _timing_float(timing: dict[str, Any], key: str) -> float | None:
    v = timing.get(key)
    return v if isinstance(v, float) else None


def _pool_paused(pool: str) -> bool:
    """M1 gate: is *pool* paused?  Fail-closed — an unreadable/stale control
    read returns paused (ADR-0034 §2)."""
    from swebench_eval.control import state as control_state

    return control_state.is_paused(pool)


def _parse_eval_job(body: dict[str, Any]) -> EvalJob:
    return EvalJob(
        run_id=str(body["run_id"]),
        instance_id=str(body["instance_id"]),
        attempt_number=int(body["attempt_number"]),
        patch_s3_key=str(body["patch_s3_key"]),
        fail_to_pass=str(body.get("fail_to_pass", "")),
        pass_to_pass=str(body.get("pass_to_pass", "")),
        use_gold_patch=bool(body.get("use_gold_patch", False)),
    )


def _run_eval(job: EvalJob, timing: dict[str, Any] | None = None) -> ResultMessage:
    """Run grading and build a ResultMessage.

    ``timing`` (M0 §5) carries the eval phase-timing columns.
    """
    from swebench_eval.database.state_machine import map_eval_outcome_to_error_category
    from swebench_eval.dataset.swebench_loader import load_single_instance

    timing = dict(timing or {})

    # The eval job message carries instance_id only (ADR-0007: queue carries
    # pointers), so the full instance — repo, base_commit, test_patch, gold
    # patch, environment_setup_commit — is loaded here to build the official
    # TestSpec.  This is the grading path: it legitimately needs the gold row,
    # and 5b's S3 mirror is its source (review §2) rather than a per-eval
    # HuggingFace fetch.  fail_to_pass/pass_to_pass fall back to the job message
    # when present, then to the loaded instance.
    instance = load_single_instance(job.instance_id, include_gold=True)
    if instance is None:
        raise RuntimeError(f"Instance '{job.instance_id}' not found in dataset")

    _patch_t0 = time.monotonic()
    if job.use_gold_patch:
        # Image validation: the dataset's own fix is the patch.  No artifact
        # fetch — the row already carries it.  Empty means the mirror row was
        # loaded WITHOUT gold, which this path must never silently grade.
        patch_data = str(getattr(instance, "patch", "") or "")
        if not patch_data.strip():
            raise RuntimeError(
                f"image validation for {job.instance_id}: the loaded instance carries no "
                "gold patch (mirror loaded without include_gold?)"
            )
        logger.info("Grading %s with the GOLD patch (image validation)", job.instance_id)
    else:
        # Download the patch from MinIO — eval_patch_fetch_s (M0 §5).
        patch_data = get_artifact(_BUCKET, job.patch_s3_key).decode("utf-8")
    timing["eval_patch_fetch_s"] = time.monotonic() - _patch_t0

    # Write patch to a temp file for the grading runner.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as patch_file:
        patch_file.write(patch_data)
        patch_path = patch_file.name
    fail_to_pass = job.fail_to_pass or instance.fail_to_pass
    pass_to_pass = job.pass_to_pass or instance.pass_to_pass

    runner = SwebenchRunner(
        namespace=_eval_image_namespace(), docker_available=True, timeout=_grade_timeout_s()
    )
    # M0 §5 eval_test_s: the whole grade (image prep incl. pull, test run).  The
    # official harness performs the image build/pull inside grade(), so
    # eval_image_pull_s is NOT cleanly isolable here — per §5's explicit rule
    # we record NULL rather than guess a boundary we cannot cleanly reach
    # ("A NULL here is acceptable; an invented number is not").
    _grade_t0 = time.monotonic()
    verdict = runner.grade(
        GradingInput(
            instance_id=job.instance_id,
            patch=patch_data,
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
        ),
        instance=instance,
    )
    timing["eval_test_s"] = time.monotonic() - _grade_t0

    # Clean up temp file.
    try:
        os.unlink(patch_path)
    except OSError:
        pass

    # Upload eval report to MinIO.
    s3_prefix = f"runs/{job.run_id}/eval/{job.instance_id}/{job.attempt_number}"
    report_key = upload_artifact(
        _BUCKET,
        f"{s3_prefix}/eval_report.json",
        verdict.report_json,
    )

    # 5b (review): SWE-bench's run logs (test_output.txt, run_instance.log)
    # used to live only on the eval host — a failed grade was silent. Upload
    # them next to the report so the reviewer can pull the full evidence.
    # M0 §5 eval_log_upload_s: the run-log + report upload phase.
    _log_t0 = time.monotonic()
    run_log_keys = _upload_run_logs(_BUCKET, s3_prefix, verdict.run_log_dir)
    timing["eval_log_upload_s"] = time.monotonic() - _log_t0

    # BUILDER3B eval resource instrumentation: the per-grade sizing probe,
    # written beside the report as its own artifact.  NO ResultMessage field,
    # NO instance_results column (deliberately — the report plumbing is a
    # known trap; this is a sizing probe, not a product feature; the six files
    # are aggregated by a one-off script).  None = sampling disabled, so no
    # artifact is written at all — absent is "not measured", never a healthy 0.
    if verdict.resource_measurement is not None:
        import dataclasses
        import json

        resource_key = upload_artifact(
            _BUCKET,
            f"{s3_prefix}/resource_usage.json",
            json.dumps(dataclasses.asdict(verdict.resource_measurement), indent=2, sort_keys=True),
        )
        logger.info("uploaded resource usage -> %s", resource_key)

    # A2 (E1/E1b, 2026-08-19): a grade where the gold test patch failed to
    # apply is INVALID, not a verdict — the tests that ran are not the gold
    # tests.  Terminal + non-retryable (the outcome will not change), and kept
    # distinct from an unresolved model result.
    # EVAL-GRADE-RESOURCE-LIMITS §3.3: an OOM-killed grade is likewise never a
    # verdict — EVAL_OOM_KILLED, its own category, so an infrastructure limit
    # can never render as a model failure.  Both void the verdict field: the
    # row carries state FAILED_EVAL + the category + the evidence instead.
    # ADR-0043 (SWE-bench 5.x): the harness's own infra_failure flag (#586) and
    # its exit-code cross-check (#620) void the grade the same way — an
    # environment fault is never a model result.
    error_category = map_eval_outcome_to_error_category(
        verdict.resolved,
        grade_invalid=verdict.invalid,
        oom_killed=verdict.oom_killed,
        infra_failure=verdict.infra_failure,
        timed_out=verdict.timed_out,
    )
    state, verdict_str, error_detail = _eval_row_shape(verdict, error_category)

    # ADR-0038 §3: gold-patch similarity (difflib) — a near-exact reproduction
    # of the gold patch is a memorization signal.  The eval path is the ONLY
    # place both patches exist (the harness deliberately has no gold patch).
    import difflib

    gold_similarity: float | None = None
    gold_patch = getattr(instance, "patch", "")  # the gold patch (never graded)
    if gold_patch:
        gold_similarity = difflib.SequenceMatcher(None, patch_data, gold_patch).ratio()

    return ResultMessage(
        run_id=job.run_id,
        instance_id=job.instance_id,
        attempt_number=job.attempt_number,
        phase="eval",
        state=state,
        error_category=error_category,
        error_detail=error_detail,
        verdict=verdict_str,
        wall_clock_s=verdict.wall_clock_seconds,
        report_json_s3_key=report_key,
        # dev/BUILDER4-EXPOSE-MISSING-ARTIFACTS-2026-08-28.md: absent (empty
        # string, ResultMessage's own default) when the runner never produced
        # a log dir or the specific file didn't exist — never fabricated.
        test_output_s3_key=run_log_keys.get("test_output.txt", ""),
        run_log_s3_key=run_log_keys.get("run_instance.log", ""),
        touches_test_files=verdict.touches_test_files,
        # ADR-0037 / M0 §5 eval timing (None = not measured, Trap 3).
        eval_queue_wait_s=_timing_float(timing, "eval_queue_wait_s"),
        eval_patch_fetch_s=_timing_float(timing, "eval_patch_fetch_s"),
        eval_test_s=_timing_float(timing, "eval_test_s"),
        eval_log_upload_s=_timing_float(timing, "eval_log_upload_s"),
        # ADR-0038 — contamination + honesty (the runner computes these; persist
        # them, never discard — ADR-0038 §1).
        stripped_test_paths=verdict.stripped_test_paths,
        grade_invalid=verdict.invalid,
        gold_patch_similarity=gold_similarity,
    )


def _eval_row_shape(verdict: GradingOutput, error_category: str) -> tuple[str, str, str]:
    """``(state, verdict, error_detail)`` for the eval row of a graded attempt.

    - grade VOIDED (invalid / OOM / infra): state FAILED_EVAL, the category and
      the evidence carry the story; the verdict field is "invalid" or EMPTY —
      there is no verdict, and an infrastructure limit must never render as a
      model failure.
    - grade TIMED OUT (2026-09-06, owner decision after django-10097 — the
      model's regex backtracked catastrophically and the suite could never
      finish): the hang is the patch's doing, so the attempt is a failed,
      GRADEABLE one — state UNRESOLVED / verdict "unresolved" — with the
      EVAL_TIMEOUT category and SWE-bench's timeout note kept on the row.
      Upstream SWE-bench counts a timed-out instance as not resolved the same
      way.  Every consumer that counts UNRESOLVED (writer summary, export, UI
      rates) therefore includes it without special-casing.
    - otherwise the category IS the state (RESOLVED / UNRESOLVED / PATCH_APPLY_FAILED).
    """
    if verdict.invalid or verdict.oom_killed or verdict.infra_failure:
        return "FAILED_EVAL", ("invalid" if verdict.invalid else ""), verdict.error
    if verdict.timed_out:
        return "UNRESOLVED", "unresolved", verdict.error
    return error_category, ("resolved" if verdict.resolved else "unresolved"), ""


def _upload_run_logs(bucket: str, s3_prefix: str, run_log_dir: str) -> dict[str, str]:
    """Upload SWE-bench's per-run log files next to the eval report.

    5b (review, 2026-08-18): the grade's subprocess logs (``test_output.txt``,
    ``run_instance.log``) were never in CloudWatch and only survived on the eval
    host — a silent ~3-minute grade. The runner streams the build + progress
    live; this makes the FULL logs durable beside the report. Best-effort by
    design: a log-upload failure must never lose a verdict that already graded.

    dev/BUILDER4-EXPOSE-MISSING-ARTIFACTS-2026-08-28.md: the uploaded keys used
    to be logged and discarded — the dashboard could never resolve them.
    Returns ``{filename: s3_key}`` for every upload that actually succeeded
    (a failed or skipped file is simply absent, never a fabricated key).
    """
    uploaded: dict[str, str] = {}
    if not run_log_dir:
        return uploaded
    log_dir = Path(run_log_dir)
    for name in ("test_output.txt", "run_instance.log"):
        path = log_dir / name
        if not path.exists():
            continue
        try:
            key = f"{s3_prefix}/{name}"
            upload_artifact(bucket, key, path.read_bytes())
            logger.info("uploaded %s -> %s", name, key)
            uploaded[name] = key
        except Exception:
            logger.exception("failed to upload %s from %s", name, run_log_dir)
    return uploaded


def _eval_image_namespace() -> str | None:
    """Select the SWE-bench image source for grading (5b, image-env §8).

    The eval host builds the instance image LOCALLY from the ECR env image —
    the full-split path with zero Docker Hub pulls after warm — when
    ``EVAL_IMAGE_NAMESPACE`` is one of {"", "none", "build"}.  The legacy
    ``"swebench"`` pull path stays for local/dev Docker runs that have no env
    image to build from.
    """
    raw = os.environ.get("EVAL_IMAGE_NAMESPACE")
    if raw is None:
        return "swebench"
    if raw.strip() in ("", "none", "build"):
        return None
    return raw.strip()


def _heartbeat(
    queue_name: str,
    receipt_handle: str,
    stop: threading.Event,
    *,
    protected: bool = False,
    released: threading.Event | None = None,
    job: EvalJob | None = None,
) -> None:
    """Keep the in-flight job invisible (visibility 300 s every 34 s) and, while
    *protected*, keep the task's scale-in protection fresh (every 5 min).

    With *job*, every beat also publishes the grade's live progress (the exec
    tee's snapshot, ``grade_progress.current()``) to the attempt's Redis
    progress key — the liveness signal the run-supervisor's deadline rule and
    the dashboard read (2026-09-06: a live 67-min grade was reaped ABANDONED
    because the eval phase wrote no key).  Best-effort: a Redis failure is
    logged once per beat and never touches the grade.

    On SIGTERM (``_SHUTDOWN``) the job is handed BACK to the queue at once —
    visibility 0 — so the re-grade starts on another worker immediately rather
    than after the 300 s window, and ``released`` is set so the main loop knows
    the receipt is no longer exclusively its own. Then this thread ends; there
    is nothing left to heartbeat.
    """
    last_visibility = time.monotonic()
    last_protection = time.monotonic()
    while not stop.wait(_HEARTBEAT_POLL_SECONDS):
        if _SHUTDOWN.is_set():
            try:
                change_message_visibility(queue_name, receipt_handle, 0)
                logger.warning(
                    "SIGTERM mid-grade: handed the %s job back (visibility 0) "
                    "for immediate redelivery",
                    queue_name,
                )
            except Exception:
                logger.exception(
                    "SIGTERM mid-grade: could not hand the %s job back; it will "
                    "be redelivered after the visibility timeout",
                    queue_name,
                )
            if released is not None:
                released.set()
            # Eval scaling review F1: the grading container is a sibling on the host daemon
            # and SWE-bench's `finally` never runs past the SIGKILL — take it down NOW, before
            # the 30 s grace ends, so it cannot outlive this worker on a packed host.
            grade_containers.remove_current()
            return
        now = time.monotonic()
        if now - last_visibility >= _HEARTBEAT_SECONDS:
            last_visibility = now
            try:
                change_message_visibility(queue_name, receipt_handle, _BASE_VISIBILITY_SECONDS)
            except Exception:
                logger.exception("Heartbeat failed for %s", queue_name)
            if job is not None:
                _publish_grade_progress(job)
        if protected and now - last_protection >= _PROTECTION_REFRESH_SECONDS:
            last_protection = now
            set_protection(True, _PROTECTION_MINUTES)


# 2026-09-06 (run b4338f67): the runner used to pass timeout=None, which SWE-bench
# turns into thread.join(None) — a hung suite graded forever (django-10097 ran
# 67 min twice).  Two hours covers Verified's whole-suite instances (the gold
# grade of the slowest took 8 min; a model patch that makes the suite crawl for
# an hour is already a lost cause) and stays under the reaper's deadline only
# because the eval heartbeat now publishes a live key.  Env-overridable per
# fleet; the value is the owner's (task #75).
_DEFAULT_GRADE_TIMEOUT_S = 1800


def _grade_timeout_s() -> int:
    raw = os.environ.get("EVAL_GRADE_TIMEOUT_S", "")
    if not raw:
        return _DEFAULT_GRADE_TIMEOUT_S
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "EVAL_GRADE_TIMEOUT_S=%r is not an integer; using %d", raw, _DEFAULT_GRADE_TIMEOUT_S
        )
        return _DEFAULT_GRADE_TIMEOUT_S
    if value <= 0:
        logger.warning(
            "EVAL_GRADE_TIMEOUT_S=%d must be > 0; using %d", value, _DEFAULT_GRADE_TIMEOUT_S
        )
        return _DEFAULT_GRADE_TIMEOUT_S
    return value


def _publish_grade_progress(job: EvalJob) -> None:
    """One heartbeat's worth of live progress → the attempt's Redis key. Never raises."""
    from swebench_eval.database import redis_client
    from swebench_eval.evaluation import grade_progress

    progress = grade_progress.current()
    snap = progress.snapshot() if progress is not None else {"elapsed_s": None, "lines": None}
    if progress is not None:
        # 2026-09-08: a hung suite is silent in CloudWatch after its last line — say so.
        try:
            progress.maybe_log_silence()
        except Exception:
            logger.debug("silence log skipped", exc_info=True)
    try:
        redis_client.write_eval_progress(job.run_id, job.instance_id, job.attempt_number, snap)
    except Exception as exc:  # noqa: BLE001 - liveness is best-effort; the grade goes on
        logger.warning("could not publish grade progress for %s: %s", job.instance_id, exc)


def _dataclass_to_dict(obj: Any) -> dict[str, Any]:
    import dataclasses

    return dataclasses.asdict(obj)
