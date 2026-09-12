"""Grading adapter protocol.

Defines the interface that every evaluation/grading backend must implement.
For v1 the only implementation is :class:`SwebenchRunner`, which wraps the
official ``swebench`` package unmodified (ADR-0005).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from swebench_eval.evaluation.resource_sampling import ResourceMeasurement


@dataclass(frozen=True)
class GradingInput:
    """What the grading runner needs to evaluate a single patch."""

    instance_id: str
    patch: str  # the raw diff produced by the harness
    # Test lists — passed through to the official harness unmodified.
    fail_to_pass: str
    pass_to_pass: str


@dataclass(frozen=True)
class GradingOutput:
    """The verdict produced by the grading runner."""

    instance_id: str
    resolved: bool  # True iff FAIL_TO_PASS is empty after applying the patch
    # The official harness's own report blob, stored verbatim.
    report_json: str
    # Wall-clock time for the evaluation phase alone.
    wall_clock_seconds: float
    error: str = ""
    # Whether the patch touches any file that SWE-bench's test_patch also touches
    # (cheat-detection pre-check, architecture §9.2).
    touches_test_files: bool = False
    # SWE-bench's per-run log directory (build_instance + test_output), so the
    # eval worker can upload the full logs alongside the report (5b review: the
    # grade was silent in CloudWatch). Empty when the runner could not build one.
    run_log_dir: str = ""
    # Gold-test-file hunks stripped from the graded patch (A2/E1, 2026-08-19).
    # The graded patch never carries the model's work on a file the gold
    # test_patch also touches — SWE-bench's reset drops CREATED files (no
    # pathspec, ``source_file == "/dev/null"``), so a model-authored test file
    # survives into grading, the gold test_patch cannot apply, and the tests
    # that run are the model's own. Recording the stripped paths keeps the
    # signal (the model DID touch test files) without letting the content in.
    stripped_test_paths: tuple[str, ...] = ()
    # True when the gold tests could NOT be established — an INVALID grade,
    # never a verdict. Distinct from ``resolved=False`` (a legitimate
    # unresolved) and from a crash: the official harness can run to completion
    # even when its test_patch failed to apply (``set -uxo pipefail`` has no
    # ``-e``), so the tests that ran are not the gold tests.
    invalid: bool = False
    # 2026-09-06: the test run exceeded the grade timeout (SWE-bench wrote
    # "Timeout error: N seconds exceeded." and raised EvaluationError).  The
    # worker lands it as state UNRESOLVED with the terminal EVAL_TIMEOUT
    # category (owner decision: a hang is the patch's fault and counts as a
    # failed, gradeable attempt — upstream's convention); never a retry.
    timed_out: bool = False
    # EVAL-GRADE-RESOURCE-LIMITS (2026-09-01) §3.3: the kernel OOM-killed a
    # process inside the grading container (docker ``oom`` event), or the eval
    # exec died mid-test-run (END marker never arrived).  NEVER a verdict —
    # the tests that "ran" are incomplete, and a 137 landing as ``unresolved``
    # converts an infrastructure limit into a benchmark result.  Distinct from
    # ``invalid`` (gold tests never established) and from ``resolved=False``
    # (a legitimate unresolved).  ``error`` names the evidence that fired.
    oom_killed: bool = False
    # SWE-bench 5.x (ADR-0043): the harness itself now flags a grade whose test
    # output could not be parsed as a likely ENVIRONMENT fault (``infra_failure``
    # in report.json, advisory in upstream, #586) and records the test command's
    # exit status (``>>>>> Test Exit Code``) — a log claiming no failures while
    # the command exited non-zero is refused as evidence (#620).  An
    # infra-flagged grade is voided like an OOM: never a verdict.
    infra_failure: bool = False
    infra_failure_reason: str = ""
    test_exit_code: int | None = None
    # BUILDER3B eval resource instrumentation: per-grade container usage,
    # serialised by the eval worker to resource_usage.json BESIDE the report.
    # None = sampling disabled (EVAL_RESOURCE_SAMPLING off) — the worker then
    # writes no artifact, so "absent" is "not measured", never healthy.
    # Deliberately NOT plumbed into ResultMessage/instance_results (a sizing
    # probe, not a product feature — see BUILDER3B "Where the numbers go").
    resource_measurement: ResourceMeasurement | None = None


class GradingAdapter(Protocol):
    """Protocol for grading a single patch against SWE-bench's test suite."""

    def grade(self, input: GradingInput) -> GradingOutput:
        """Apply the patch and run FAIL_TO_PASS / PASS_TO_PASS tests."""
        ...
