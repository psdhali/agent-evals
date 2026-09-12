"""Bug-findings B-1/B-2 regression: the wall-clock deadline must be reachable.

The hang was a stalled upstream call blocking for the SDK's default timeout
(~600 s read, retried 2x) while the loop's wall-clock check sat between turns
and could never fire.  The fix bounds every call to a per-call read timeout that
is STRICTLY SMALLER than the run's wall-clock budget, so the deadline can fire
within at most one call (option (a) in the findings doc).  These tests pin that
invariant.
"""

from __future__ import annotations

from swebench_eval.harnesses.custom_minimal.harness import _per_call_read_timeout


def test_per_call_read_is_smaller_than_wall_clock_budget() -> None:
    """The per-call read timeout must be < timeout_seconds so the loop can fire."""
    for budget in (30, 60, 120, 300, 600, 1800):
        assert _per_call_read_timeout(budget) < budget, f"budget={budget}"


def test_per_call_read_is_bounded() -> None:
    """Bounded for realistic budgets; never reaches the SDK's ~600 s default."""
    assert _per_call_read_timeout(30) == 29  # degenerate budget: strictly smaller
    assert _per_call_read_timeout(100) == 33
    assert _per_call_read_timeout(300) == 100
    assert _per_call_read_timeout(600) == 120
    assert _per_call_read_timeout(1800) == 120  # capped, never grows unbounded
    for budget in (100, 300, 600, 3600):
        read = _per_call_read_timeout(budget)
        assert 30 <= read <= 120
        assert read < budget
