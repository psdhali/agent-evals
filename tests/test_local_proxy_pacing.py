"""Shim ↔ L1 pacer wiring — BUILDER4-HARNESS-AUTOSCALER-EXACT-DESIGN-2026-09-01.md §4/§8b.

A scripted FakePacer isolates the PROXY's wiring (admission placement, retry loop, releases on
every terminal path, telemetry fields); the pacer's own semantics are covered by
test_pacer_integration.py against real Redis. Upstreams are the same local ThreadingHTTPServer
pattern the rest of test_local_proxy.py uses.
"""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any, ClassVar

import httpx

import swebench_eval.gateway.local_proxy as lp
from swebench_eval.gateway.local_proxy import LocalProxy
from swebench_eval.gateway.pacer import Admission, PacerTimeout
from swebench_eval.harnesses.base import Usage


class FakePacer:
    """Scripted pacer: a queue of outcomes for acquire(); records every interaction."""

    def __init__(self, outcomes: list[str] | None = None) -> None:
        self.outcomes = outcomes or []  # "admit" | "timeout"
        self.acquires: list[int] = []
        self.charges: list[int] = []  # F3: the weighted draw passed with each acquire
        self.releases: list[tuple[str, int | None]] = []  # (call_id, real_prompt_tokens)
        # F3: (charge, real_prompt, real_cached) per release — the settlement inputs.
        self.settlements: list[tuple[int, int | None, int | None]] = []
        self.cached_weight = 1.0
        self.overloads = 0

    async def estimate_tokens(self, alias: str, request_chars: int) -> int:
        return max(1, request_chars // 5)

    async def acquire(
        self, alias: str, est: int, *, hold_cap_s: float, charge_tokens: int | None = None
    ) -> Admission:
        self.acquires.append(est)
        self.charges.append(charge_tokens if charge_tokens is not None else est)
        outcome = self.outcomes.pop(0) if self.outcomes else "admit"
        if outcome == "timeout":
            raise PacerTimeout("scripted hold-cap timeout")
        return Admission(
            call_id=f"call-{len(self.acquires)}",
            est_tokens=est,
            paced_wait_ms=7,
            fallback=False,
            charge_tokens=charge_tokens if charge_tokens is not None else est,
        )

    async def release(
        self,
        alias,
        admission,
        *,
        real_prompt_tokens=None,
        request_chars=None,
        real_cached_tokens=None,
    ):
        self.releases.append((admission.call_id, real_prompt_tokens))
        self.settlements.append((admission.charge_tokens, real_prompt_tokens, real_cached_tokens))

    async def get_cached_weight(self, alias: str) -> float:
        return self.cached_weight

    async def record_overload(self, alias: str) -> None:
        self.overloads += 1


class _OkUpstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        payload = json.dumps(
            {
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 5, "total_tokens": 505},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Overload429ThenOkUpstream(http.server.BaseHTTPRequestHandler):
    """First N requests: a REAL provider overload (429, NO retry-after). Then 200."""

    fails_remaining = 2

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        cls = type(self)
        if cls.fails_remaining > 0:
            cls.fails_remaining -= 1
            payload = json.dumps(
                {"error": {"metadata": {"raw": "temporarily rate-limited upstream"}}}
            ).encode()
            self.send_response(429)  # deliberately NO retry-after — the real overload shape
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = json.dumps(
            {
                "choices": [{"message": {"content": "recovered"}}],
                "usage": {"prompt_tokens": 400, "completion_tokens": 3, "total_tokens": 403},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _HeaderCaptureUpstream(http.server.BaseHTTPRequestHandler):
    """200 OK upstream that records each request's headers (lower-cased)."""

    captured_headers: ClassVar[list[dict[str, str]]] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        type(self).captured_headers.append({k.lower(): v for k, v in self.headers.items()})
        payload = json.dumps(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _run_shim(
    tmp_path, handler_cls, fake_pacer, n_posts: int = 1, usage: Usage | None = None
) -> tuple[list[httpx.Response], list[dict[str, Any]]]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    responses = []
    try:
        shim = LocalProxy(
            upstream_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            run_id="r",
            harness="custom_minimal",
            instance_id="i",
            attempt_number=1,
            usage=usage if usage is not None else Usage(),
            output_dir=str(tmp_path),
            pacer=fake_pacer,  # explicit injection overrides the conftest's env kill-switch
        ).start()
        try:
            with httpx.Client(timeout=30.0) as client:
                for _ in range(n_posts):
                    responses.append(
                        client.post(
                            f"http://127.0.0.1:{shim._bound_port}/v1/chat/completions",
                            json={"model": "laguna-xs-2.1-custom_minimal", "messages": []},
                        )
                    )
        finally:
            shim.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5)
    records = [
        json.loads(line) for line in (tmp_path / "llm_calls.jsonl").read_text().strip().splitlines()
    ]
    return responses, records


def test_shim_sends_eval_coordinates_as_headers_and_spend_logs_metadata(tmp_path) -> None:
    """LiteLLM ≥1.99 stores NO request headers in its spend rows (header capture
    went enterprise-only), so the x-eval-* headers alone no longer reach the
    spend log.  The shim's ``x-litellm-spend-logs-metadata`` JSON is the
    version-stable channel (lands in metadata->spend_logs_metadata on every
    row) — BOTH channels must be present on every forwarded call."""
    _HeaderCaptureUpstream.captured_headers = []
    responses, _ = _run_shim(tmp_path, _HeaderCaptureUpstream, FakePacer())
    assert responses[0].status_code == 200
    hdrs = _HeaderCaptureUpstream.captured_headers[0]
    assert hdrs["x-eval-run-id"] == "r"
    assert hdrs["x-eval-instance-id"] == "i"
    assert hdrs["x-eval-harness"] == "custom_minimal"
    assert hdrs["x-eval-attempt"] == "1"
    assert json.loads(hdrs["x-litellm-spend-logs-metadata"]) == {
        "eval_run_id": "r",
        "eval_harness": "custom_minimal",
        "eval_instance_id": "i",
        "eval_attempt": "1",
    }


def test_admitted_call_records_paced_wait_and_releases_with_real_usage(tmp_path) -> None:
    pacer = FakePacer()
    responses, records = _run_shim(tmp_path, _OkUpstream, pacer)

    assert responses[0].status_code == 200
    assert records[0]["paced_wait_ms"] == 7
    assert len(pacer.acquires) == 1
    # Released exactly once, with the REAL reported prompt tokens for κ calibration.
    assert pacer.releases == [("call-1", 500)]
    # §2.5 diagnostics on an unqueued admit: explicit False/0, no deny axis.
    assert records[0]["pacer_was_queued"] is False
    assert records[0]["pacer_queue_len"] == 0
    assert "pacer_deny_axis" not in records[0]
    # Not a retried call: the retry decomposition fields stay absent (same convention as
    # overload_retries) rather than being written as zeros.
    assert "overload_backoff_ms" not in records[0]
    assert "retry_upstream_ms" not in records[0]


def test_queued_admission_diagnostics_reach_the_call_record(tmp_path) -> None:
    """A pacer that reports the call was denied before admitting (queue depth 3, last denied
    on the token axis) must land those facts on the record."""

    class _QueuedPacer(FakePacer):
        async def acquire(self, alias, est, *, hold_cap_s, charge_tokens=None):
            self.acquires.append(est)
            return Admission(
                call_id="call-q",
                est_tokens=est,
                paced_wait_ms=4_200,
                fallback=False,
                was_queued=True,
                queue_len_at_admit=3,
                deny_axis_last="tok",
            )

    _responses, records = _run_shim(tmp_path, _OkUpstream, _QueuedPacer())
    assert records[0]["paced_wait_ms"] == 4_200
    assert records[0]["pacer_was_queued"] is True
    assert records[0]["pacer_queue_len"] == 3
    assert records[0]["pacer_deny_axis"] == "tok"


def test_hold_cap_timeout_surfaces_a_paced_429_and_records_it(tmp_path) -> None:
    pacer = FakePacer(outcomes=["timeout"])
    responses, records = _run_shim(tmp_path, _OkUpstream, pacer)

    assert responses[0].status_code == 429
    assert responses[0].headers.get("x-eval-paced") == "1"
    assert responses[0].headers.get("retry-after") == "30"
    assert records[0]["http_status"] == 429
    assert records[0]["error_type"] == "pacer_hold_cap_exceeded"
    assert pacer.releases == []  # nothing was admitted, so nothing to release


def test_real_provider_429_is_retried_invisibly_then_succeeds(tmp_path, monkeypatch) -> None:
    """§8b: two real overloads then a 200 — the CLI sees only the 200; the record carries the
    retry count; each retried observation fed the congestion counter; every admission was
    released (2 retried + 1 final)."""
    monkeypatch.setattr(lp, "_OVERLOAD_BACKOFF_BASE_S", 0.01)
    _Overload429ThenOkUpstream.fails_remaining = 2
    pacer = FakePacer()
    responses, records = _run_shim(tmp_path, _Overload429ThenOkUpstream, pacer)

    assert responses[0].status_code == 200  # the CLI never saw a 429
    assert len(records) == 1  # one turn, one record
    assert records[0]["overload_retries"] == 2
    assert records[0]["http_status"] == 200
    assert pacer.overloads == 2  # each retried observation counted, final 200 not
    assert len(pacer.acquires) == 3  # initial + one per re-send (a re-send is a new arrival)
    assert len(pacer.releases) == 3
    # §2.6 decomposition: every admission wait summed (3 x the fake's 7ms), both backoff
    # sleeps summed (base 10ms x attempt, jittered 0.75-1.25 -> >= 2 x 7.5ms), the two 429
    # round-trips accounted, and latency_ms = the FINAL (200) attempt only.
    rec = records[0]
    assert rec["paced_wait_ms"] == 21
    assert rec["overload_backoff_ms"] >= 15
    assert rec["retry_upstream_ms"] >= 0
    assert rec["latency_ms"] >= 0


class _AlwaysOverloadUpstream(http.server.BaseHTTPRequestHandler):
    """Every request: a REAL provider overload (429, NO retry-after)."""

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        payload = b'{"error": {"metadata": {"raw": "temporarily rate-limited upstream"}}}'
        self.send_response(429)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_retries_exhausted_records_zero_latency_and_the_full_decomposition(
    tmp_path, monkeypatch
) -> None:
    """One real 429, one backoff, then the re-acquire hits the hold cap: no final upstream
    attempt happened, so latency_ms must be 0 (previously a mixture of the 429 round-trip +
    the backoff + part of the re-acquire wait), with the retry costs on their own fields."""
    monkeypatch.setattr(lp, "_OVERLOAD_BACKOFF_BASE_S", 0.01)
    pacer = FakePacer(outcomes=["admit", "timeout"])
    responses, records = _run_shim(tmp_path, _AlwaysOverloadUpstream, pacer)

    assert responses[0].status_code == 429
    rec = records[0]
    assert rec["error_type"] == "engine_overloaded_retries_exhausted"
    assert rec["overload_retries"] == 1
    assert rec["latency_ms"] == 0
    assert rec["overload_backoff_ms"] >= 7
    assert rec["retry_upstream_ms"] >= 0
    assert rec["paced_wait_ms"] >= 7  # the initial admission's 7ms + the failed re-acquire
    assert pacer.overloads == 1


def test_pacer_timeouts_reach_the_instance_rollups_on_both_paths(tmp_path, monkeypatch) -> None:
    """Review F7 (2026-09-03): the re-acquire timeout after a 429 retry wrote a complete call
    record but never touched Usage — an instance whose calls died at the pacer AFTER a retry
    showed pacer_timeouts = 0. Both timeout paths fold into the rollups, and a call is counted
    in paced_calls exactly once."""
    monkeypatch.setattr(lp, "_OVERLOAD_BACKOFF_BASE_S", 0.01)
    # Path 1: first-acquire timeout.
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    usage1 = Usage()
    _run_shim(tmp_path / "a", _OkUpstream, FakePacer(outcomes=["timeout"]), usage=usage1)
    assert usage1.pacer_timeouts == 1
    assert usage1.paced_calls == 1
    assert usage1.paced_wait_ms_total > 0
    # Path 2: admitted, real 429, backoff, re-acquire times out.
    usage2 = Usage()
    _run_shim(
        tmp_path / "b",
        _AlwaysOverloadUpstream,
        FakePacer(outcomes=["admit", "timeout"]),
        usage=usage2,
    )
    assert usage2.pacer_timeouts == 1
    assert usage2.paced_calls == 1  # once, not twice
    assert usage2.overload_retries_total == 1
    assert usage2.paced_wait_ms_total >= 7


def test_gateway_429_with_retry_after_is_not_retried(tmp_path) -> None:
    """The discriminator in the retry path: retry-after present = LiteLLM throttling US =
    NOT a provider overload — passes straight through to the CLI, no invisible retry."""

    class _GatewayLimit(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            payload = b'{"error": {"message": "litellm.RateLimitError"}}'
            self.send_response(429)
            self.send_header("Retry-After", "60")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    pacer = FakePacer()
    responses, records = _run_shim(tmp_path, _GatewayLimit, pacer)

    assert responses[0].status_code == 429  # surfaced, not retried
    assert "overload_retries" not in records[0]
    assert records[0]["rate_limit_scope"] == "gateway"
    assert pacer.overloads == 0  # a gateway self-limit is NOT a provider overload
    assert len(pacer.releases) == 1  # the admission was still released (stream finalize)


def test_probe_paths_are_not_paced(tmp_path) -> None:
    """GET /v1/models etc. are not model calls — they must not consume arrival budget."""

    class _ModelsUpstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            payload = b'{"data": []}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    pacer = FakePacer()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ModelsUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        shim = LocalProxy(
            upstream_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            run_id="r",
            harness="opencode",
            instance_id="i",
            attempt_number=1,
            usage=Usage(),
            output_dir=str(tmp_path),
            pacer=pacer,  # type: ignore[arg-type]  # FakePacer stands in for Pacer
        ).start()
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(f"http://127.0.0.1:{shim._bound_port}/v1/models")
                assert resp.status_code == 200
        finally:
            shim.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert pacer.acquires == []  # no admission consumed for a non-completion path


def test_hold_cap_is_per_harness_under_each_verified_client_timeout(tmp_path) -> None:
    """F2 (exact-design review): the cap must sit BELOW the CLI's own HTTP bound — and the
    KIND of bound decides the margin (the owner's 2026-09-01 finding): an IDLE bound resets
    on first byte, so hold-time is all it ever sees (floor - 60s is fine); a TOTAL bound
    covers hold + the whole completion, so the cap must leave real completion headroom.
    custom_minimal 100 (measured 120s total-ish clamp, 20s+stream headroom); mini 240
    (litellm 600s); claude_code 100 — its binding 300s bound was traced to the NON-STREAMING
    fallback path's SDK per-attempt timeout, a TOTAL bound, so 240 would have left only 60s
    for the entire completion (the exact §2.1 no-429 death); codex 240 (stream_idle_timeout_ms
    is idle by definition). opencode 240 — MEASURED 2026-09-01: 716+s of total silence
    through the CLI itself with the adapter's exact config, never gave up (no client-side
    bound exists); 240 is the bounded parity value so Run B's five-way comparison isn't
    skewed by our own constant. The env var can only TIGHTEN, never loosen past a bound."""
    import swebench_eval.gateway.local_proxy as lp_mod

    def _cap(harness: str) -> float:
        shim = lp_mod.LocalProxy(
            upstream_url="http://127.0.0.1:1/v1",
            run_id="r",
            harness=harness,
            instance_id="i",
            attempt_number=1,
            usage=Usage(),
            output_dir=str(tmp_path),
            pacer=FakePacer(),  # type: ignore[arg-type]
        )
        return shim._pacer_hold_cap_s

    assert _cap("custom_minimal") == 100.0
    assert _cap("mini_swe_agent") == 240.0
    assert _cap("claude_code") == 100.0
    assert _cap("codex") == 240.0
    assert _cap("opencode") == 240.0
    assert _cap("aider") == 45.0  # the one CLI still on the conservative default


def test_hold_cap_env_can_tighten_but_never_loosen(tmp_path, monkeypatch) -> None:
    import swebench_eval.gateway.local_proxy as lp_mod

    monkeypatch.setenv("EVAL_PACER_HOLD_CAP_S", "500")  # trying to loosen past the bound
    shim = lp_mod.LocalProxy(
        upstream_url="http://127.0.0.1:1/v1",
        run_id="r",
        harness="custom_minimal",
        instance_id="i",
        attempt_number=1,
        usage=Usage(),
        output_dir=str(tmp_path),
        pacer=FakePacer(),  # type: ignore[arg-type]
    )
    assert shim._pacer_hold_cap_s == 100.0  # min(table, env) — the verified bound holds

    monkeypatch.setenv("EVAL_PACER_HOLD_CAP_S", "30")  # tightening is allowed
    shim2 = lp_mod.LocalProxy(
        upstream_url="http://127.0.0.1:1/v1",
        run_id="r",
        harness="custom_minimal",
        instance_id="i",
        attempt_number=1,
        usage=Usage(),
        output_dir=str(tmp_path),
        pacer=FakePacer(),  # type: ignore[arg-type]
    )
    assert shim2._pacer_hold_cap_s == 30.0
