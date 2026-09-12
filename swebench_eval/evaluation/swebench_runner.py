"""SWE-bench evaluation runner — thin wrapper around the official harness.

Per ADR-0005, this module does **not** reimplement any grading logic.  It
converts the project's own :class:`GradingInput` into the format the official
``swebench.harness.run_evaluation`` expects, calls it, and converts the result
back into a :class:`GradingOutput`.
"""

from __future__ import annotations

import dataclasses
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from swebench_eval.dataset.base import Instance

from swebench_eval.evaluation import grade_containers, grade_progress
from swebench_eval.evaluation.grade_limits import apply_grade_limits
from swebench_eval.evaluation.grading_adapter import (
    GradingAdapter,
    GradingInput,
    GradingOutput,
)
from swebench_eval.evaluation.resource_sampling import (
    ContainerOOMWatcher,
    ContainerResourceSampler,
    _oom_watch_enabled,
    _resource_sampling_enabled,
)

logger = logging.getLogger(__name__)

# Idempotence guard for _enable_eval_stdout_logging (mirrors the warm job's
# _BUILD_STDOUT_WRAPPED; the eval worker grades many jobs in one container).
_EVAL_STDOUT_WRAPPED = False


def _enable_eval_stdout_logging() -> None:
    """Mirror SWE-bench run-instance logs to container stdout -> CloudWatch.

    5b (review, 2026-08-18): the ~3-minute grade was silent in CloudWatch — the
    run_instance progress lines all went to log files under
    ``RUN_EVALUATION_LOG_DIR``. swebench's ``setup_logger`` seam exists for
    exactly this.  In 5.x it lives in ``swebench.logger``; ``run_instance``
    binds its own ``setup_logger`` name at import, so wrapping the defining
    module alone would MISS the run logger — wrap both module attributes to the
    same wrapper. Idempotent.
    """
    global _EVAL_STDOUT_WRAPPED
    if _EVAL_STDOUT_WRAPPED:
        return

    from swebench import logger as swebench_logger
    from swebench.harness import run_evaluation

    original = swebench_logger.setup_logger

    def _streaming_setup_logger(*args: object, **kwargs: object) -> object:
        kwargs["add_stdout"] = True  # kwargs is dict[str, object]
        return original(*args, **kwargs)

    swebench_logger.setup_logger = _streaming_setup_logger
    run_evaluation.setup_logger = _streaming_setup_logger
    _EVAL_STDOUT_WRAPPED = True


def _log_run_tail(run_log_dir: str) -> None:
    """Surface the verdict-bearing tail of the grade in CloudWatch.

    The full test output is uploaded by the eval worker (run_log_dir); here we
    log just the tail so the ~3-minute grade is never silent AND a failed grade
    leaves its evidence in the log stream. Best-effort: never raises.
    """
    try:
        import os

        from swebench.harness.constants import LOG_INSTANCE, LOG_TEST_OUTPUT

        log_dir = run_log_dir
        test_output = os.path.join(log_dir, LOG_TEST_OUTPUT)
        if os.path.exists(test_output):
            with open(test_output, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            logger.info(
                "eval test output tail (%d lines of %s):\n%s",
                len(lines),
                LOG_TEST_OUTPUT,
                "\n".join(lines[-200:]),
            )
        else:
            build_log = os.path.join(log_dir, LOG_INSTANCE)
            if os.path.exists(build_log):
                with open(build_log, encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
                logger.info(
                    "no test_output yet; build/run log tail (%d lines of %s):\n%s",
                    len(lines),
                    LOG_INSTANCE,
                    "\n".join(lines[-200:]),
                )
    except Exception:  # log streaming must never break grading
        logger.exception("failed to stream the eval run tail from %s", run_log_dir)


class SwebenchRunner(GradingAdapter):
    """Grade a single patch using the official ``swebench`` package unmodified.

    Parameters
    ----------
    namespace: Where the grading image comes from.  ``None`` (the deployed
        setting, ``EVAL_IMAGE_NAMESPACE`` in ``{"", "none", "build"}``) makes the
        eval host pre-pull OUR per-instance ``-inst`` image from ECR and tag it
        to the name the harness looks up, so the mutable ``:latest`` on Docker
        Hub is never pulled (ADR-0043).  ``"swebench"`` (local dev) lets the
        harness pull ``test_spec.image`` from Docker Hub itself.
    docker_available: If ``True``, use the full Docker-based official harness.
        If ``False``, raise an error (the official harness requires Docker for
        sandboxed test execution; there is no non-Docker fallback).
    rm_image: Kept for call-site compatibility; SWE-bench 5.x no longer builds
        or removes instance images inside ``run_instance`` (it assumes the
        image exists), so the eval host's image housekeeping is
        ``grade_containers``' and the pre-pull's, not the harness's.
    timeout: Per-instance test execution timeout in seconds.  ``None`` uses
        the official harness's internal default.
    """

    def __init__(
        self,
        namespace: str | None = "swebench",
        docker_available: bool = True,
        rm_image: bool = True,
        timeout: int | None = None,
    ) -> None:
        self._namespace = namespace
        self._docker_available = docker_available
        self._rm_image = rm_image
        self._timeout = timeout

    def grade(self, input: GradingInput, instance: Instance | None = None) -> GradingOutput:
        if not self._docker_available:
            raise RuntimeError(
                "The official SWE-bench evaluation harness requires Docker. "
                "Ensure Docker is running and set docker_available=True."
            )

        start = time.monotonic()

        # Imports are inline so the module is importable without Docker / swebench.
        from swebench.harness.constants import LOG_REPORT, RUN_EVALUATION_LOG_DIR
        from swebench.harness.run_evaluation import run_instance
        from swebench.harness.utils import make_test_spec

        # Build the instance dict from the passed Instance (preferred) or
        # fall back to the dataset re-fetch (backward compat).
        instance_data: dict[str, object]
        if instance is not None:
            instance_data = _instance_to_dict(instance)
        else:
            fallback = _load_instance_data(input.instance_id)
            if fallback is None:
                raise RuntimeError(
                    f"Instance '{input.instance_id}' not found in the pinned dataset. "
                    "Cannot construct a TestSpec for grading."
                )
            instance_data = fallback

        # ADR-0043 / SWE-bench 5.x: the TestSpec is read off the row (image,
        # eval_script, log_parser, eval_type); the harness synthesises nothing.
        # A row without them is a pre-5.x mirror — refuse loudly rather than
        # let make_test_spec KeyError halfway into a grade.
        missing = [
            k
            for k in ("image", "eval_script", "log_parser", "eval_type")
            if not instance_data.get(k)
        ]
        if missing:
            raise RuntimeError(
                f"Instance '{input.instance_id}' lacks the SWE-bench 5.x columns {missing}: "
                "re-seed the dataset mirror at the pinned revision (ADR-0043)."
            )
        test_spec = make_test_spec(instance_data)
        test_paths = _parse_patch_paths(str(instance_data.get("test_patch", "")))

        # A2 (round-review 2026-08-19, E1): the graded patch must never carry
        # the model's version of a gold test file.  SWE-bench's reset drops
        # CREATED test files (``source_file == "/dev/null"`` → no pathspec →
        # a bare ``git checkout <base>`` that leaves untracked files in place),
        # so a model-authored test file survives into grading, the gold
        # test_patch cannot apply, and the tests that run are the model's own
        # — a false RESOLVED (found live on django-10924).  Strip the gold
        # test paths from the patch BEFORE it is graded; the gold tests then
        # establish themselves and are the things graded.
        graded_patch, stripped_test_paths = _strip_patch_file_blocks(input.patch, test_paths)
        if stripped_test_paths:
            logger.warning(
                "Grading %s: stripped model-patch hunks for gold test file(s): %s",
                input.instance_id,
                ", ".join(sorted(stripped_test_paths)),
            )

        pred = {
            "instance_id": input.instance_id,
            "model_name_or_path": "eval-framework",
            "model_patch": graded_patch,
        }

        # Run the official harness unmodified.
        import docker

        client = docker.from_env()

        # EVAL-GRADE-RESOURCE-LIMITS §3.2 / BUILDER4-EVAL-PACKING (2026-09-03): the
        # grading container is a sibling on the host daemon that SWE-bench creates
        # with no limits.  Four grades share a host now, so the limit is applied on
        # THIS client's create seam before run_instance touches it — a limit that
        # cannot be applied raises, because an unlimited grade on a packed host is
        # the host-loss case (one runaway grade takes three others with it).
        apply_grade_limits(client)

        # ADR-0043: on the deployed path (namespace None) pre-pull OUR per-instance
        # image from ECR and tag it to ``test_spec.image`` — the name the
        # harness's create_container looks up — so the mutable ``:latest`` on
        # Docker Hub is never pulled and the digest graded in is recorded.
        eval_image_digest: str | None = None
        if self._namespace is None:
            from swebench_eval.evaluation.env_image import ensure_instance_image

            eval_image_digest = ensure_instance_image(test_spec)
            # Image-parity provenance: WHICH -inst graded this attempt.  Logged
            # (CloudWatch) rather than columned for now — the validation gate
            # (image_validation) is where the digest becomes a row.
            logger.info(
                "Grading %s in instance image digest %s", input.instance_id, eval_image_digest
            )

        # 5b (review): stream run_instance progress to container stdout ->
        # CloudWatch; the grade used to be silent.
        _enable_eval_stdout_logging()
        # 2026-09-06 (django-10097's silent 67-min grade): tee SWE-bench's exec
        # stream so the suite logs progress every minute and the worker's
        # heartbeat can publish a live key the reaper and the dashboard read.
        grade_progress.install_exec_tee()

        run_id = str(uuid.uuid4())[:8]
        # 5.x names the container itself (create_container); mirror the exact
        # shape so the sampler/watcher/registry watch the right one.
        container_name = f"sweb.eval.{test_spec.instance_id.lower()}.{run_id}"

        # BUILDER3B resource instrumentation: the grading container is a
        # SIBLING on the host daemon, created by SWE-bench with no mem_limit
        # and no nano_cpus (swebench/harness/docker_build.py:516) — invisible
        # to ECS accounting.  Watch it from OUTSIDE while run_instance grades.
        # The container name is derivable before run_instance (it is
        # test_spec.get_instance_container_name(run_id) and we own run_id),
        # so we can watch a container we never create.  Start/stop mirrors the
        # eval worker's SQS-heartbeat shape (daemon thread + stop event +
        # bounded join).  Gated by EVAL_RESOURCE_SAMPLING (task-def env,
        # default on) so it can be disabled mid-run without an image rebuild.
        sampler: ContainerResourceSampler | None = None
        if _resource_sampling_enabled():
            sampler = ContainerResourceSampler(client, container_name)
            sampler.start()

        # EVAL-GRADE-RESOURCE-LIMITS §3.3: watch for the daemon's ``oom``
        # container event — the one authoritative signal that the kernel
        # killed a process inside the grade.  Its own kill switch
        # (EVAL_OOM_WATCH), NOT the sampler's: grade integrity must not
        # switch off with the sizing probe.  Started before run_instance so
        # the event stream is live before the container even exists.
        oom_watcher: ContainerOOMWatcher | None = None
        if _oom_watch_enabled():
            oom_watcher = ContainerOOMWatcher(client, container_name)
            oom_watcher.start()

        # Image size is read BEFORE run_instance — on the deployed path
        # ensure_instance_image above already pulled+retagged it, so this is a
        # local lookup.  On the Docker Hub pull path the image only appears
        # during run_instance, so this stays None (never fabricated).
        image_size_bytes = _instance_image_size_bytes(client, test_spec.image)

        # Eval scaling review F1: register the container so the worker can force-remove it
        # on SIGTERM — SWE-bench's own `finally` cleanup never runs when the worker is
        # SIGKILLed, and the container is a sibling on a host four grades share.
        grade_containers.set_current(container_name)
        grade_progress.set_current(grade_progress.GradeProgress(input.instance_id))
        try:
            # 5.x returns ``(instance_id, report)`` on completion and ``None``
            # when the run raised (patch apply failure, timeout, docker error —
            # all logged to run_instance.log); normalise to the dict shape the
            # rest of this method has always reasoned about.
            raw = run_instance(
                test_spec=test_spec,
                pred=pred,
                client=client,
                run_id=run_id,
                timeout=self._timeout,
            )
            result = _normalise_run_result(raw, input.instance_id)
        finally:
            grade_containers.set_current(None)
            grade_progress.set_current(None)
            oom_events = oom_watcher.stop() if oom_watcher is not None else 0
            measurement = sampler.stop() if sampler is not None else None
            if measurement is not None:
                measurement = dataclasses.replace(
                    measurement,
                    image_size_bytes=image_size_bytes,
                    oom_killed=oom_events > 0,
                )
            # 2026-09-06 (500 gate): the graded -inst image is ~4 GB and nothing
            # removed it — eval hosts filled up after ~35 grades and every later
            # pull failed. Release it now; a re-grade re-pulls from ECR.
            if self._namespace is None:
                from swebench_eval.evaluation.env_image import release_instance_image

                try:
                    release_instance_image(test_spec)
                except Exception:  # hygiene must never fail a grade
                    logger.warning("could not release instance image", exc_info=True)

        wall_clock = time.monotonic() - start

        # The run's log directory, for the worker to upload and for the
        # verdict-bearing tail to reach CloudWatch even when grading fails.
        model_name = str(pred["model_name_or_path"]).replace("/", "__")
        run_log_dir = RUN_EVALUATION_LOG_DIR / run_id / model_name / input.instance_id
        _log_run_tail(str(run_log_dir))

        # Read the full report from the log directory for the report_json field.
        report_path = run_log_dir / LOG_REPORT
        report_json = "{}"
        if report_path.exists():
            report_json = report_path.read_text()

        # Cheat-detection pre-check (architecture §9.2): does the harness patch
        # touch any file that SWE-bench's test_patch also touches?
        touches_test = False
        test_patch = str(instance_data.get("test_patch", ""))
        if test_patch and input.patch:
            touches_test = bool(_parse_patch_paths(input.patch) & _parse_patch_paths(test_patch))

        # EVAL-GRADE-RESOURCE-LIMITS (2026-09-01) §3.3: an OOM-killed grade
        # must never land as a silent ``unresolved``.  Primary evidence: the
        # daemon's ``oom`` event.  Belt (kill-shaped truncation): the eval
        # exec died before echoing END_TEST_OUTPUT — bash survives a pytest
        # OOM kill and still echoes the marker, so this branch specifically
        # catches the whole-exec-killed shape the event stream can miss on
        # cgroup v1 host-pressure kills.  Checked BEFORE the gold-apply
        # backstop: a killed grade's logs are not evidence of anything.
        oom_evidence = ""
        if oom_events:
            oom_evidence = (
                f"docker reported {oom_events} oom event(s) for grading container "
                f"{container_name} — the kernel killed a process inside the grade; "
                "the tests that ran are incomplete and no verdict exists"
            )
        elif result.get("completed"):
            oom_evidence = _detect_truncated_test_run(str(run_log_dir))
        if oom_evidence and result.get("completed"):
            logger.error("OOM-killed grade for %s: %s", input.instance_id, oom_evidence)
            return GradingOutput(
                instance_id=input.instance_id,
                resolved=False,
                error=oom_evidence,
                report_json=report_json,
                wall_clock_seconds=wall_clock,
                touches_test_files=touches_test,
                run_log_dir=str(run_log_dir),
                stripped_test_paths=tuple(sorted(stripped_test_paths)),
                oom_killed=True,
                resource_measurement=measurement,
            )

        # A2 backstop (round-review 2026-08-19, E1/E1b): even with the strip
        # above, a gold test_patch that failed to apply must NEVER yield a
        # verdict — the tests that ran are not the gold tests.  The official
        # harness's eval script runs ``set -uxo pipefail`` without ``-e``, so a
        # failed apply does NOT stop the run; detect it in the run logs and
        # grade INVALID (an eval worker maps it to a terminal category — not a
        # ``resolved`` and not an unresolved model result).
        apply_failure = _detect_gold_test_apply_failure(str(run_log_dir), test_paths)
        if apply_failure:
            logger.error("INVALID grade for %s: %s", input.instance_id, apply_failure)
            return GradingOutput(
                instance_id=input.instance_id,
                resolved=False,
                error=apply_failure,
                report_json=report_json,
                wall_clock_seconds=wall_clock,
                touches_test_files=touches_test,
                run_log_dir=str(run_log_dir),
                stripped_test_paths=tuple(sorted(stripped_test_paths)),
                invalid=True,
                resource_measurement=measurement,
            )

        if not result.get("completed"):
            # 2026-09-06: a test run that exceeded the grade timeout is a
            # terminal outcome of THIS patch (the suite hangs), not an infra
            # error to retry — django-10097 ran 67 min twice with no timeout at
            # all.  SWE-bench appends "Timeout error: N seconds exceeded." to
            # test_output.txt before raising; read it back here.
            timeout_note = _detect_timeout(str(run_log_dir))
            if timeout_note:
                logger.error("TIMED-OUT grade for %s: %s", input.instance_id, timeout_note)
                return GradingOutput(
                    instance_id=input.instance_id,
                    resolved=False,
                    error=timeout_note,
                    report_json=report_json,
                    wall_clock_seconds=wall_clock,
                    touches_test_files=touches_test,
                    run_log_dir=str(run_log_dir),
                    stripped_test_paths=tuple(sorted(stripped_test_paths)),
                    timed_out=True,
                    resource_measurement=measurement,
                )
            # The oom evidence rides the message: this path raises (SQS retry
            # via the worker's delete-only-on-success), and the operator
            # reading the eventual DLQ/ABANDONED detail should see the kill.
            oom_note = f" NOTE: {oom_evidence}." if oom_evidence else ""
            raise RuntimeError(
                f"Evaluation did not complete for {input.instance_id}. "
                "This is an infrastructure error, not an unresolved verdict. "
                f"Check Docker logs and swebench images (run log dir: {run_log_dir})."
                f"{oom_note}"
            )

        resolved = bool(result.get("resolved", False))

        # ADR-0043 / SWE-bench 5.x: the harness's own environment-fault flag
        # (#586) and its exit-code cross-check (#620).  ``infra_failure`` is
        # advisory upstream (``resolved`` stays False either way); here it voids
        # the grade — an environment fault must never land as a model result.
        infra_failure = bool(result.get("infra_failure", False))
        infra_reason = str(result.get("infra_failure_reason", "") or "")
        exit_code = _read_test_exit_code(str(run_log_dir))
        if infra_failure:
            logger.error(
                "INFRA-flagged grade for %s: %s (test exit code %s)",
                input.instance_id,
                infra_reason or "unclassified",
                exit_code,
            )

        return GradingOutput(
            instance_id=input.instance_id,
            resolved=resolved and not infra_failure,
            error=(
                f"harness flagged an environment fault: {infra_reason}" if infra_failure else ""
            ),
            report_json=report_json,
            wall_clock_seconds=wall_clock,
            touches_test_files=touches_test,
            run_log_dir=str(run_log_dir),
            stripped_test_paths=tuple(sorted(stripped_test_paths)),
            infra_failure=infra_failure,
            infra_failure_reason=infra_reason,
            test_exit_code=exit_code,
            resource_measurement=measurement,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_run_result(raw: Any, instance_id: str) -> dict[str, Any]:
    """Map 5.x ``run_instance``'s return to ``{completed, resolved, infra_failure, …}``.

    5.x returns ``(instance_id, report_map)`` when the eval script ran to
    grading (``report_map[instance_id]`` carries ``resolved``,
    ``patch_successfully_applied``, ``infra_failure``, ``infra_failure_reason``
    and ``tests_status``), and ``None`` when it caught an ``EvaluationError``
    or any other exception (patch apply failed, tests timed out, docker error)
    — those are logged to ``run_instance.log`` and are NOT verdicts.
    """
    if raw is None:
        return {"completed": False}
    if isinstance(raw, dict):  # a test double or a future upstream shape
        report = raw
    else:
        _, report = raw
    entry = report.get(instance_id) if isinstance(report, dict) else None
    if not isinstance(entry, dict):
        return {"completed": False}
    return {
        "completed": True,
        "resolved": bool(entry.get("resolved", False)),
        "patch_successfully_applied": bool(entry.get("patch_successfully_applied", False)),
        "infra_failure": bool(entry.get("infra_failure", False)),
        "infra_failure_reason": entry.get("infra_failure_reason", ""),
    }


_TIMEOUT_MARK = "Timeout error:"


def _detect_timeout(run_log_dir: str) -> str:
    """SWE-bench's timeout marker from the tail of test_output.txt, or ''.

    ``run_instance`` writes ``"\n\nTimeout error: {timeout} seconds exceeded."``
    as the LAST line of the test output before raising ``EvaluationError``, so
    the marker in the tail is the one unambiguous signal that the run ended
    on the clock rather than on a docker/apply failure.
    """
    import os

    try:
        from swebench.harness.constants import LOG_TEST_OUTPUT

        path = os.path.join(run_log_dir, LOG_TEST_OUTPUT)
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            tail = f.read()[-2000:]
    except (OSError, ImportError):  # a missing/garbled log is not a timeout
        return ""
    for line in reversed(tail.splitlines()):
        if line.strip().startswith(_TIMEOUT_MARK):
            return f"test run exceeded the grade timeout ({line.strip()})"
    return ""


def _read_test_exit_code(run_log_dir: str) -> int | None:
    """The test command's recorded exit status (5.x ``>>>>> Test Exit Code``), or None."""
    try:
        from pathlib import Path

        from swebench.harness.constants import LOG_TEST_OUTPUT
        from swebench.harness.grading import parse_test_exit_code

        path = Path(run_log_dir) / LOG_TEST_OUTPUT
        if not path.exists():
            return None
        code = parse_test_exit_code(path.read_text(encoding="utf-8", errors="replace"))
        return int(code) if code is not None else None
    except Exception:
        logger.warning("could not read the test exit code from %s", run_log_dir, exc_info=True)
        return None


def _detect_truncated_test_run(run_log_dir: str) -> str:
    """Kill-shaped truncation: START_TEST_OUTPUT present, END never arrived.

    EVAL-GRADE-RESOURCE-LIMITS §3.3, the belt beside the oom-event watcher.
    The eval script echoes ``: '>>>>> Start Test Output'`` before the test
    command and the END marker after it (``make_eval_script_list_py``); with
    no ``-e``, bash survives a killed test command and still echoes END — so
    a START-without-END means the exec stream itself died mid-test-run (the
    whole bash killed, or the stream torn down).  swebench's own grader
    treats missing markers as "patch did not apply" and silently grades
    ``resolved: false`` — this turns that shape into an infra outcome.
    Never raises: detection must not fail a grade.
    """
    try:
        from pathlib import Path

        from swebench.harness.constants import (
            END_TEST_OUTPUT,
            LOG_TEST_OUTPUT,
            START_TEST_OUTPUT,
        )

        path = Path(run_log_dir) / LOG_TEST_OUTPUT
        if not path.exists():
            return ""
        content = path.read_text(encoding="utf-8", errors="replace")
        if START_TEST_OUTPUT in content and END_TEST_OUTPUT not in content:
            return (
                f"test output truncated: {START_TEST_OUTPUT!r} present but "
                f"{END_TEST_OUTPUT!r} never arrived — the eval exec died "
                "mid-test-run; the tests that ran are incomplete and no "
                "verdict exists"
            )
        return ""
    except Exception:
        logger.exception("truncation detection failed for %s", run_log_dir)
        return ""


def _instance_image_size_bytes(client: Any, image_key: Any) -> int | None:
    """Local instance-image size (bytes) before ``rm_image`` removes it.

    BUILDER3B: the disk-per-grade figure upstream's "120GB free storage" is a
    proxy for.  ``None`` (not 0, not fabricated) when the image is not local
    yet — e.g. the Docker Hub pull path builds it inside run_instance, after
    this read.  Never raises: a read failure must not fail a grade.
    """
    try:
        import docker

        img = client.images.get(str(image_key))
        size = img.attrs.get("Size")
        return int(size) if isinstance(size, int) and size > 0 else None
    except docker.errors.NotFound:
        return None
    except Exception:
        logger.warning("could not read instance image size for %s", image_key, exc_info=True)
        return None


def _instance_to_dict(instance: Instance) -> dict[str, object]:
    """Convert an :class:`Instance` to the dict shape ``make_test_spec`` expects.

    5.x reads ``image``, ``eval_script``, ``log_parser`` and ``eval_type`` off
    the row; F2P/P2P are JSON strings (the loader guarantees that) and an empty
    string means ``[]``.
    """
    return {
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "problem_statement": instance.problem_statement,
        "hints_text": instance.hints,
        "patch": instance.patch,
        "test_patch": instance.test_patch,
        "FAIL_TO_PASS": instance.fail_to_pass or "[]",
        "PASS_TO_PASS": instance.pass_to_pass or "[]",
        "environment_setup_commit": instance.environment_setup_commit,
        "version": instance.version,
        "created_at": instance.created_at,
        "image": instance.image,
        "eval_script": instance.eval_script,
        "log_parser": instance.log_parser,
        "eval_type": instance.eval_type,
    }


def _load_instance_data(instance_id: str) -> dict[str, object] | None:
    """Load the raw SWE-bench instance dict for grading.

    Fallback path — only used when the caller does not pass an ``Instance``.
    5b (review §2): reads via the S3 mirror (full row) first, so grading never
    makes a per-eval HuggingFace fetch; the mirror's own HF fallback covers
    unseeded/dev environments.  Prefer passing the ``Instance`` directly.
    """
    from swebench_eval.dataset.swebench_loader import load_single_instance

    instance = load_single_instance(instance_id, include_gold=True)
    if instance is None:
        return None
    return _instance_to_dict(instance)


def _parse_patch_paths(patch: str) -> set[str]:
    """Extract the set of file paths touched by a unified diff.

    Parses ``--- a/path`` and ``+++ b/path`` headers.
    Returns an empty set for an empty or unparseable patch.
    """
    paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("--- a/"):
            paths.add(line[6:])  # strip "--- a/"
        elif line.startswith("+++ b/"):
            paths.add(line[6:])  # strip "+++ b/"
    return paths


def _strip_patch_file_blocks(patch: str, blocked_paths: set[str]) -> tuple[str, set[str]]:
    """Remove from a unified diff every file block whose path is blocked.

    A2 (E1, 2026-08-19): the graded patch must not carry the model's work on a
    file the gold test_patch also touches.  Blocks are split on ``diff --git``
    headers (standard ``git diff`` output, which every harness produces); a
    block whose target path (either side of a rename) is blocked is dropped
    wholesale.  Blocks without a parseable header are kept untouched — an
    unparseable patch will be rejected by SWE-bench's own ``git apply`` anyway.

    Returns ``(kept_patch, stripped_paths)``.  ``stripped_paths`` is the set
    of blocked paths whose blocks were removed, so the decision is recorded
    even when the stripped patch is itself empty.
    """
    if not patch or not blocked_paths:
        return patch, set()
    # A patch with no diff --git headers is not a block-delimited unified diff
    # we can split safely — return it untouched (SWE-bench's git apply rejects
    # it either way) rather than reflow it.
    if "diff --git " not in patch:
        return patch, set()

    blocks: list[list[str]] = [[]]
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            blocks.append([line])
        else:
            blocks[-1].append(line)

    kept: list[str] = []
    stripped: set[str] = set()
    for block in blocks:
        if not block:
            continue
        header = block[0]
        target = None if not header.startswith("diff --git ") else _block_paths(header)
        if target and target & blocked_paths:
            stripped |= target & blocked_paths
            continue  # drop the whole block, including its hunk lines
        kept.extend(block)

    cleaned = "\n".join(kept).rstrip("\n") + "\n"
    return cleaned, stripped


def _block_paths(header: str) -> set[str]:
    """Both paths named by a ``diff --git a/X b/Y`` header (rename-aware)."""
    rest = header[len("diff --git ") :]
    if " b/" not in rest:
        return set()
    a, _, b = rest.partition(" b/")
    paths = {a.strip().strip('"')}
    if b:
        paths.add(b.strip().strip('"'))
    return {p for p in paths if p} - {"/dev/null"}


def _detect_gold_test_apply_failure(run_log_dir: str, test_paths: set[str]) -> str:
    """Scan SWE-bench's run logs for a failed gold-test ``git apply``.

    A2 (E1/E1b, 2026-08-19): the official harness's eval script runs
    ``set -uxo pipefail`` WITHOUT ``-e``, so a test_patch that fails to apply
    ("… already exists in working directory") does not stop the run — the
    agent-authored file that caused the collision is then graded as if it were
    the gold test.  That must surface as an INVALID grade, never a verdict.

    Detection is log-based (reading what ``run_instance`` wrote): a git apply
    error line naming one of the gold test paths.  Naming the *test* path is
    what distinguishes "gold tests never landed" from a model-patch apply
    failure on its own paths (which SWE-bench reports and we leave alone).
    """
    if not test_paths:
        return ""
    from pathlib import Path

    root = Path(run_log_dir)
    if not root.is_dir():
        return ""
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.startswith("error: "):
                continue
            err_path = line[len("error: ") :].partition(":")[0].strip().strip('"')
            if err_path in test_paths:
                return f"gold test patch failed to apply ({path.name}): {line.strip()}"
    return ""
