"""BatchResult's per-call latency + honest offered rate —
BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03.md §2.1. Pure, no network."""

from __future__ import annotations

import pytest

from swebench_eval.gateway.ceiling_discovery import BatchResult


def _batch(latencies: tuple[float, ...], real_total: int, n: int = 15) -> BatchResult:
    return BatchResult(
        concurrency=n,
        elapsed_s=0.0,
        window_s=60.0,
        tokens_in_window=0,
        success_count=len(latencies),
        overload_count=0,
        error_count=n - len(latencies),
        latencies_s=latencies,
        real_tokens_total=real_total,
    )


def test_median_latency_odd_even_and_none() -> None:
    assert _batch((13.0, 11.0, 15.0), 1).median_latency_s == 13.0
    assert _batch((10.0, 14.0), 1).median_latency_s == 12.0
    assert _batch((), 0).median_latency_s is None


def test_offered_rate_uses_the_real_offering_span_not_the_window() -> None:
    """15 calls at a 2s stagger with 10s latency: the last call launches at 28s and completes
    at 38s — that is the offering span. The window (60s) must play no part: the old
    ``tokens/window`` figure is what seeded laguna's r_tok ~4-9x low."""
    b = _batch(tuple([10.0] * 15), 15 * 213_000)
    assert b.offered_rate_tok_s(2.0) == pytest.approx(15 * 213_000 / (14 * 2.0 + 10.0))
    assert b.offered_rate_tok_s(2.0) != pytest.approx(b.tokens_in_window / b.window_s)


def test_offered_rate_is_zero_without_successes_never_a_fabrication() -> None:
    assert _batch((), 0).offered_rate_tok_s(2.0) == 0.0
    # Successes with no parseable usage: tokens unknown -> 0, not a guess from target size.
    assert _batch((10.0,), 0).offered_rate_tok_s(2.0) == 0.0


def test_defaults_keep_existing_constructors_valid() -> None:
    """Every pre-existing BatchResult(...) call site omits the two new fields."""
    b = BatchResult(
        concurrency=3,
        elapsed_s=1.0,
        window_s=60.0,
        tokens_in_window=100,
        success_count=3,
        overload_count=0,
        error_count=0,
    )
    assert b.latencies_s == ()
    assert b.real_tokens_total == 0
    assert b.median_latency_s is None
