"""Ceiling discovery (Part 1) — pure-function coverage, no network.

`find_ceiling`'s prober is injected specifically so this can be tested without real HTTP calls;
these tests exercise the top-down-then-bisect procedure itself (design doc §2's cost-driven
revision of the original doubling-from-small ramp), the windowed-real-usage tpm calculation, and
the connection-pool sizing fix — all found and fixed via live end-to-end testing, not assumed
correct up front. Async tests use ``asyncio.run`` directly, matching this repo's existing pattern
(``tests/test_local_proxy.py``) — no pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from swebench_eval.gateway.ceiling_discovery import (
    BatchResult,
    DiscoveryProbeError,
    DiscoveryResult,
    _usage_tokens,
    find_ceiling,
    probe_batch,
    probe_content,
)


def _safe(concurrency: int, window_s: float = 60.0, tokens_in_window: int = 0) -> BatchResult:
    return BatchResult(
        concurrency=concurrency,
        elapsed_s=window_s,
        window_s=window_s,
        tokens_in_window=tokens_in_window,
        success_count=concurrency,
        overload_count=0,
        error_count=0,
    )


def _overloaded(concurrency: int, window_s: float = 60.0, overload_count: int = 1) -> BatchResult:
    return BatchResult(
        concurrency=concurrency,
        elapsed_s=window_s,
        window_s=window_s,
        tokens_in_window=0,
        success_count=concurrency - overload_count,
        overload_count=overload_count,
        error_count=0,
    )


def _inconclusive(concurrency: int, window_s: float = 60.0) -> BatchResult:
    """Every call failed for a non-overload reason (auth, network) — the exact real-world case
    found live: a discovery alias whose key rotation hadn't propagated yet 401'd every call."""
    return BatchResult(
        concurrency=concurrency,
        elapsed_s=window_s,
        window_s=window_s,
        tokens_in_window=0,
        success_count=0,
        overload_count=0,
        error_count=concurrency,
    )


class TestProbeContent:
    def test_two_calls_get_genuinely_distinct_content(self) -> None:
        a = probe_content(1000)
        b = probe_content(1000)
        assert a != b

    def test_length_is_roughly_proportional_to_target_tokens(self) -> None:
        small = probe_content(100)
        large = probe_content(10_000)
        assert len(large) > len(small)
        # ~4 chars/token — not exact, just in the right ballpark (design doc §2: false precision
        # isn't the goal on a constraint that moves with other tenants anyway). This only sizes
        # the REQUEST; the tpm figure never uses this ratio (it uses real reported usage).
        assert 3 * 10_000 < len(large) < 5 * 10_000

    def test_carries_a_nonce_prefix(self) -> None:
        assert probe_content(50).startswith("probe-nonce:")


class TestUsageTokens:
    """Real reported usage, not an estimate — the actual fix for the found ~18% discrepancy
    between requested and real token counts."""

    def test_reads_total_tokens_directly(self) -> None:
        assert _usage_tokens({"usage": {"total_tokens": 12345}}) == 12345

    def test_falls_back_to_prompt_plus_completion_when_total_absent(self) -> None:
        assert _usage_tokens({"usage": {"prompt_tokens": 100, "completion_tokens": 20}}) == 120

    def test_none_when_usage_missing(self) -> None:
        assert _usage_tokens({"choices": []}) is None

    def test_none_when_usage_is_not_a_dict(self) -> None:
        assert _usage_tokens({"usage": "not a dict"}) is None


class TestBatchResult:
    def test_overloaded_true_when_any_overload_seen(self) -> None:
        assert _overloaded(10, overload_count=1).overloaded is True

    def test_overloaded_false_when_zero_overloads(self) -> None:
        assert _safe(10).overloaded is False

    def test_tpm_from_windowed_real_usage(self) -> None:
        # 600,000 real tokens confirmed within a 30s window.
        result = _safe(10, window_s=30.0, tokens_in_window=600_000)
        assert result.tpm == pytest.approx(600_000 / 30.0 * 60.0)

    def test_tpm_is_zero_not_a_crash_on_zero_window(self) -> None:
        assert _safe(10, window_s=0.0, tokens_in_window=100).tpm == 0.0

    def test_inconclusive_true_when_every_call_errored_with_no_overload_signal(self) -> None:
        """The exact scenario found live: a batch that 401'd on every call (rotation hadn't
        propagated) must never be reported as 'safe' just because nothing was classified as an
        overload."""
        assert _inconclusive(2).inconclusive is True

    def test_inconclusive_false_when_any_success_exists(self) -> None:
        assert _safe(2).inconclusive is False

    def test_inconclusive_false_when_overloaded(self) -> None:
        """A real overload IS a real, interpretable signal — must not also be flagged
        inconclusive."""
        assert _overloaded(2).inconclusive is False


class TestProbeBatchWindowingAndUsage:
    """Real network path, mocked at the httpx.AsyncClient boundary — exercises the windowing and
    real-usage logic end to end without hitting a real provider."""

    def _fake_client(self, responses: list[dict[str, Any]]) -> type:
        captured_limits: list[httpx.Limits | None] = []
        call_index = {"i": 0}

        class _FakeResponse:
            def __init__(self, spec: dict[str, Any]) -> None:
                self.status_code = spec["status"]
                self.headers = spec.get("headers", {})
                self._body: dict[str, Any] = spec.get("body", {})

            def json(self) -> dict[str, Any]:
                return self._body

        class _FakeClient:
            def __init__(self, *, limits: httpx.Limits | None = None, **_: object) -> None:
                captured_limits.append(limits)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def post(self, *args: object, **kwargs: object) -> _FakeResponse:
                spec = responses[call_index["i"]]
                call_index["i"] += 1
                if spec.get("sleep"):
                    await asyncio.sleep(spec["sleep"])
                return _FakeResponse(spec)

        _FakeClient.captured_limits = captured_limits  # type: ignore[attr-defined]
        return _FakeClient

    def test_client_pool_is_sized_to_the_requested_concurrency(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Found live: httpx.AsyncClient defaults to max_connections=100 internally. Unsized, a
        concurrency=150 batch would silently cap at 100 truly-concurrent requests at the CLIENT —
        a lower load than claimed, substituted in without any error."""
        fake = self._fake_client([{"status": 200, "body": {"usage": {"total_tokens": 1000}}}] * 150)
        monkeypatch.setattr(httpx, "AsyncClient", fake)

        async def _run() -> BatchResult:
            return await probe_batch(
                base_url="http://gateway.local",
                api_key="k",
                model="m",
                concurrency=150,
                target_tokens=1_000,
            )

        result = asyncio.run(_run())

        assert result.success_count == 150
        limits = fake.captured_limits[0]  # type: ignore[attr-defined]
        assert limits is not None
        assert limits.max_connections == 150
        assert limits.max_keepalive_connections == 150

    def test_tpm_uses_real_usage_not_the_requested_target_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The actual fix: real usage.total_tokens drives tpm, never target_tokens."""
        fake = self._fake_client([{"status": 200, "body": {"usage": {"total_tokens": 5000}}}] * 3)
        monkeypatch.setattr(httpx, "AsyncClient", fake)

        async def _run() -> BatchResult:
            return await probe_batch(
                base_url="http://g",
                api_key="k",
                model="m",
                concurrency=3,
                target_tokens=999_999,  # deliberately different from the real usage above
                window_s=60.0,
            )

        result = asyncio.run(_run())

        assert result.tokens_in_window == 5000 * 3
        assert result.tpm == pytest.approx(5000 * 3 / 60.0 * 60.0)

    def test_a_straggler_past_the_window_is_excluded_from_tpm_not_averaged_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner's fix: bound the tpm divisor to a fixed window rather than the full batch's
        wall-clock span, so one slow call can't drag the whole rate estimate down."""
        fake = self._fake_client(
            [
                {"status": 200, "body": {"usage": {"total_tokens": 10_000}}},
                {"status": 200, "body": {"usage": {"total_tokens": 10_000}}, "sleep": 0.15},
            ]
        )
        monkeypatch.setattr(httpx, "AsyncClient", fake)

        async def _run() -> BatchResult:
            return await probe_batch(
                base_url="http://g",
                api_key="k",
                model="m",
                concurrency=2,
                target_tokens=1_000,
                window_s=0.05,  # the slow call (0.15s) falls outside this
            )

        result = asyncio.run(_run())

        assert result.success_count == 2  # both succeeded — the verdict isn't affected
        assert result.tokens_in_window == 10_000  # only the fast one counted toward tpm

    def test_a_200_with_no_parseable_usage_counts_as_success_but_not_toward_tpm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never silently zero-fill and never silently drop the success — a 200 with unreadable
        usage is real evidence the concurrency level survived, it just can't contribute a real
        number to the rate estimate."""
        fake = self._fake_client([{"status": 200, "body": {}}])
        monkeypatch.setattr(httpx, "AsyncClient", fake)

        async def _run() -> BatchResult:
            return await probe_batch(
                base_url="http://g", api_key="k", model="m", concurrency=1, target_tokens=1_000
            )

        result = asyncio.run(_run())

        assert result.success_count == 1
        assert result.tokens_in_window == 0


class TestFindCeiling:
    def test_returns_target_directly_when_target_succeeds(self) -> None:
        """The whole point of the top-down revision: if the target is safe, stop — no
        exploratory small-concurrency steps first."""
        calls: list[int] = []

        async def prober(concurrency: int) -> BatchResult:
            calls.append(concurrency)
            return _safe(concurrency, tokens_in_window=concurrency * 1000)

        async def _run() -> DiscoveryResult:
            return await find_ceiling(prober, target_concurrency=150)

        result = asyncio.run(_run())

        assert result.safe_concurrency == 150
        assert calls == [150]  # exactly one probe — no ramp, no bisection needed

    def test_bisects_downward_when_target_overloads(self) -> None:
        """A synthetic true threshold at 100: overloaded at or above 100, safe below."""
        threshold = 100

        async def prober(concurrency: int) -> BatchResult:
            if concurrency >= threshold:
                return _overloaded(concurrency)
            return _safe(concurrency, tokens_in_window=concurrency * 1000)

        async def _run() -> DiscoveryResult:
            return await find_ceiling(prober, target_concurrency=150, bisect_tolerance=5)

        result = asyncio.run(_run())

        assert result.safe_concurrency < threshold
        assert threshold - result.safe_concurrency <= 5  # converged within tolerance
        # Never reports a concurrency the fake provider actually overloaded at.
        assert not any(
            p.overloaded and p.concurrency == result.safe_concurrency for p in result.probes
        )

    def test_confirms_the_floor_when_even_the_floor_overloads(self) -> None:
        async def prober(concurrency: int) -> BatchResult:
            return _overloaded(concurrency)  # everything overloads, no exceptions

        async def _run() -> DiscoveryResult:
            return await find_ceiling(prober, target_concurrency=150, bisect_floor=5)

        result = asyncio.run(_run())

        # Never silently reports the floor as safe when it demonstrably wasn't probed as such.
        assert result.probes[-1].concurrency == 5
        assert result.probes[-1].overloaded is True
        assert result.safe_concurrency == 0

    def test_refuses_to_report_safe_from_an_inconclusive_target_probe(self) -> None:
        """The regression test for the real bug: a not-yet-ready alias 401'd on every call of
        the target probe, and got silently reported as a confirmed-safe ceiling with a
        fabricated tpm figure — found by actually running this end to end, not in a test first."""

        async def prober(concurrency: int) -> BatchResult:
            return _inconclusive(concurrency)

        async def _run() -> DiscoveryResult:
            return await find_ceiling(prober, target_concurrency=150)

        with pytest.raises(DiscoveryProbeError, match="inconclusive"):
            asyncio.run(_run())

    def test_refuses_to_report_safe_from_an_inconclusive_bisection_step(self) -> None:
        async def prober(concurrency: int) -> BatchResult:
            if concurrency == 150:
                return _overloaded(concurrency)  # force entry into bisection
            return _inconclusive(concurrency)

        async def _run() -> DiscoveryResult:
            return await find_ceiling(prober, target_concurrency=150)

        with pytest.raises(DiscoveryProbeError, match="inconclusive"):
            asyncio.run(_run())

    def test_audit_trail_includes_every_probe_fired(self) -> None:
        async def prober(concurrency: int) -> BatchResult:
            if concurrency >= 80:
                return _overloaded(concurrency)
            return _safe(concurrency, tokens_in_window=concurrency * 1000)

        async def _run() -> DiscoveryResult:
            return await find_ceiling(prober, target_concurrency=150, bisect_tolerance=10)

        result = asyncio.run(_run())

        assert isinstance(result, DiscoveryResult)
        assert len(result.probes) >= 2  # at least the initial target probe plus one bisection step
        assert result.probes[0].concurrency == 150  # target tested first, always
