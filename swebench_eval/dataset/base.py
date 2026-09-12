"""Base protocol for dataset loading — Task/Instance types."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Instance:
    """A single SWE-bench task instance.

    These map directly onto SWE-bench's own instance schema.  The full record
    is carried through so that downstream code (evaluation runner, reporting)
    never needs to re-fetch the dataset.
    """

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints: str = ""
    # The patch(es) that resolve the issue — used only for the "known-good"
    # sanity check in the smoke test, never for grading.
    patch: str = ""
    # FAIL_TO_PASS / PASS_TO_PASS test lists — passed through to the official
    # evaluation harness unmodified.
    fail_to_pass: str = ""
    pass_to_pass: str = ""
    # Additional fields needed by the evaluation runner and cheat-detection.
    test_patch: str = ""  # subset of gold patch touching test files (architecture §9.2)
    environment_setup_commit: str = ""  # kept for provenance; 5.x make_test_spec no longer reads it
    version: str = ""  # SWE-bench version
    created_at: str = ""  # instance creation timestamp
    # SWE-bench 5.x columns (ADR-0043).  ``make_test_spec`` needs all four; the
    # harness no longer synthesises any of them from repo/version.
    # ``image``: e.g. swebench/sweb.eval.x86_64.django_1776_django-10097:latest — a
    # MUTABLE tag; the digest snapshot in design/image-digests/ is the pin.
    image: str = ""
    # ``eval_script``: the full eval.sh.  It EMBEDS test_patch and the test
    # directives, so it is answer material: full mirror row only, never public.
    eval_script: str = ""
    log_parser: str = ""  # e.g. parse_log_django
    eval_type: str = ""  # pass_and_fail | fail_only


class DatasetLoader(Protocol):
    """Protocol for loading a set of SWE-bench instances.

    A single-method protocol so that swapping to a different split or dataset
    is a one-line change in the run config.
    """

    def load(self) -> list[Instance]:
        """Return all instances, in a stable order (sorted by instance_id)."""
        ...
