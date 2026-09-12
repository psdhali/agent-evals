"""Tests for the parallel multi-harness runner's completion logic.

The runner's original poll loop demanded an eval verdict for EVERY run, but a
FAILED/EMPTY/STUCK/BUDGET harness never produces an eval row — so a batch with
one non-PATCH_READY run never satisfied the condition, ran to its deadline, and
the daemon eval worker was killed before a late PATCH_READY eval was written
(codex's eval was lost).  ``_run_is_done`` is the corrected predicate.
"""

from __future__ import annotations

import importlib.util
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "multi_harness_runner", _ROOT / "scripts" / "multi_harness_runner.py"
)
assert _spec is not None and _spec.loader is not None
_runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runner)


def test_patch_ready_waiting_on_eval_is_not_done() -> None:
    """A PATCH_READY harness with no eval verdict yet is still pending."""
    assert _runner._run_is_done("PATCH_READY", None) is False


def test_patch_ready_with_terminal_eval_is_done() -> None:
    for verdict in ("RESOLVED", "UNRESOLVED", "PATCH_APPLY_FAILED", "FAILED_EVAL"):
        assert _runner._run_is_done("PATCH_READY", verdict) is True, verdict


def test_non_patch_ready_without_eval_is_done() -> None:
    """FAILED/EMPTY/STUCK/BUDGET produce no eval row — an absent eval is NOT pending."""
    for state in ("FAILED_HARNESS", "EMPTY_PATCH", "STUCK", "BUDGET_EXCEEDED"):
        assert _runner._run_is_done(state, None) is True, state


def test_no_harness_state_is_pending() -> None:
    assert _runner._run_is_done(None, None) is False


def test_run_id_fits_bigint() -> None:
    """run_id must stay inside Postgres bigint (max 19 digits) so numeric casts work.

    The earlier `time.time_ns()` form was 21 digits and overflowed `run_id::bigint`
    (review round-3 §2).  The epoch-ms + sibling-index form is 15 digits.
    """
    for i in range(3):
        rid = _runner._run_id(i)
        assert len(rid) == 15, f"{rid} is {len(rid)} digits, not 15"
        assert int(rid) < 9_223_372_036_854_775_807  # bigint max
        assert rid.isdigit()
