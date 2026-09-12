"""Tests for the ADR-0019 local proxy shim (Part-3 streaming redesign).

Covers the shim's R5-1 / ADR-0019 responsibilities without a live gateway:
usage accumulation parsed from a forwarded response's bounded tail, the
additive stream_options injection, verbatim path forwarding, and the
shim-routing guard (mini_swe_agent in, custom_minimal out).
"""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any, ClassVar, cast
from unittest import mock

import httpx
import pytest
from starlette.responses import StreamingResponse

from swebench_eval.gateway.local_proxy import LocalProxy
from swebench_eval.harnesses.base import Usage
from swebench_eval.harnesses.registry import HARNESS_ADAPTERS, SHIM_ROUTED_HARNESSES


def _all_subprocess_adapters() -> list[Any]:
    """Every shim-routed (subprocess) adapter, from the ONE registry map (P4C-2).

    mini_swe_agent in, custom_minimal out — custom_minimal is in-process and must
    NOT be routed through the shim (R5-2).
    """
    return [HARNESS_ADAPTERS[name]() for name in SHIM_ROUTED_HARNESSES]


def test_adapters_route_to_shim_not_gateway() -> None:
    """R5-1: every shim-routed adapter resolves to the shim, never the gateway.

    Once the worker points LITELLM_BASE_URL at the per-worker shim (127.0.0.1),
    every adapter follows via the shared routing helper.  A harness that
    bypasses the shim reports zero observed calls — this asserts none of the
    adapters keep a hardcoded gateway literal.
    """
    with mock.patch.dict("os.environ", {"LITELLM_BASE_URL": "http://127.0.0.1:41500/v1"}):
        for adapter in _all_subprocess_adapters():
            base = adapter._api_base_url
            assert base.startswith(
                "http://127.0.0.1:41500"
            ), f"{type(adapter).__name__} base {base!r} did not follow shim env"
            assert "localhost:4000" not in base  # not the gateway


class _StubUpstream(http.server.BaseHTTPRequestHandler):
    """A minimal SSE/JSON upstream that records what it received."""

    protocol_version = "HTTP/1.1"
    captured: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, *args: Any) -> None:
        return

    def _chunked(self, payload: bytes) -> bytes:
        return b"%x\r\n" % len(payload) + payload + b"\r\n"

    def _handle(self) -> None:
        ln = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(ln) if ln else b""
        type(self).captured.append({"path": self.path, "headers": dict(self.headers), "body": body})
        # Streaming SSE: a couple of content events then a final usage event.
        events = [
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            (
                b'data: {"choices":[],"usage":{"prompt_tokens":10,'
                b'"completion_tokens":5,"cost":0.001}}\n\n'
            ),
            b"data: [DONE]\n\n",
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for ev in events:
            self.wfile.write(self._chunked(ev))
        self.wfile.write(b"0\r\n\r\n")

    def do_POST(self) -> None:
        self._handle()


def _start_upstream() -> tuple[http.server.ThreadingHTTPServer, threading.Thread]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_shim_streams_verbatim_injects_stream_options_and_counts_usage() -> None:
    """The shim forwards the path verbatim, adds stream_options.include_usage to an
    OpenAI streaming request, streams the SSE response through, and accumulates
    usage from the tail once the stream ends (Part-3 design)."""
    _StubUpstream.captured.clear()
    upstream, up_thread = _start_upstream()
    try:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        usage = Usage()
        shim = LocalProxy(
            upstream_url=upstream_url,
            run_id="run-1",
            harness="opencode",
            instance_id="i1",
            attempt_number=1,
            usage=usage,
        ).start()
        try:
            # The shim looks to harnesses like the gateway: base ends in /v1.
            assert shim.local_base_url.endswith("/v1"), shim.local_base_url

            port = shim._bound_port
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    json={"model": "cheap-oss-model", "stream": True},
                )
                assert resp.status_code == 200
                text = resp.text
                # SSE forwarded through (content + usage event + [DONE]).
                assert "delta" in text
                assert "[DONE]" in text
                assert '"usage"' in text

            # Path forwarded VERBATIM (no double /v1).
            assert _StubUpstream.captured[0]["path"] == "/v1/chat/completions"
            # stream_options was injected (review P-4).
            sent = json.loads(_StubUpstream.captured[0]["body"])
            assert sent["stream_options"] == {"include_usage": True}
            # eval metadata attached.
            up_headers = {k.lower(): v for k, v in _StubUpstream.captured[0]["headers"].items()}
            assert up_headers["x-eval-harness"] == "opencode"
            assert up_headers["accept-encoding"].lower() == "identity"

            # Usage accumulated from the tail after the stream ends.
            assert usage.input_tokens == 10
            assert usage.output_tokens == 5
            assert usage.cost_usd == 0.001
        finally:
            shim.stop()
    finally:
        upstream.shutdown()
        up_thread.join(timeout=5)


def test_shim_prices_anthropic_local_when_no_cost() -> None:
    """An Anthropic-messages tail has tokens but never gateway cost (F-1), so the
    shim prices the call locally from the shared pricing table."""

    class _Anthropic(_StubUpstream):
        def _handle(self) -> None:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(ln) if ln else b""
            type(self).captured.append(
                {"path": self.path, "headers": dict(self.headers), "body": body}
            )
            events = [
                (
                    b'event: message_delta\ndata: {"type":"message_delta",'
                    b'"usage":{"input_tokens":50000,"output_tokens":10000}}\n\n'
                )
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for ev in events:
                self.wfile.write(b"%x\r\n" % len(ev) + ev + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

    _Anthropic.captured = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Anthropic)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        usage = Usage()
        shim = LocalProxy(
            upstream_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            run_id="r",
            harness="claude_code",
            instance_id="i",
            attempt_number=1,
            usage=usage,
        ).start()
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"http://127.0.0.1:{shim._bound_port}/v1/messages",
                    json={
                        "model": "cheap-oss-model",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
                assert resp.status_code == 200
            # tokens accumulated; cost priced locally at the VERIFIED deepseek-
            # v4-flash rate (B8; 2026-09-04: OpenInference's $0.05/$0.16 after the
            # account allowlist change): 50k/1M*0.05 + 10k/1M*0.16
            assert usage.input_tokens == 50000
            assert usage.output_tokens == 10000
            assert usage.cost_usd == pytest.approx(0.05 * 0.05 + 0.01 * 0.16)
            assert usage.source != "gateway"  # local pricing, not gateway-reported
            # No stream_options injected on the Anthropic path.
            sent = json.loads(_Anthropic.captured[0]["body"])
            assert "stream_options" not in sent
        finally:
            shim.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5)


class _Fake503Upstream:
    """A 503 upstream that can carry (or omit) the framework_paused marker.

    The marker is decided in the body HEAD scan, so ``aiter_raw`` must yield the
    body for the shim to inspect it.
    """

    status_code = 503
    headers: ClassVar[dict[str, str]] = {}

    def __init__(self, body: bytes) -> None:
        self.body = body

    async def aiter_raw(self):
        yield self.body

    async def aclose(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _Fake503Client:
    def __init__(self, body: bytes):
        self._body = body
        self.aclosed = False

    async def send(self, *a, **k):
        return _Fake503Upstream(body=self._body)

    async def aclose(self):
        self.aclosed = True


def _run_503_proxy(body: bytes) -> tuple[LocalProxy, bool]:
    import asyncio

    import starlette.requests

    shim = LocalProxy(
        upstream_url="http://127.0.0.1:1/v1",
        run_id="r",
        harness="claude_code",
        instance_id="i",
        attempt_number=1,
        usage=Usage(),
    )
    fake_client = _Fake503Client(body=body)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }

    async def _run():
        with mock.patch(
            "swebench_eval.gateway.local_proxy.httpx.AsyncClient", lambda *a, **k: fake_client
        ):
            request = starlette.requests.Request(scope)
            request._body = b'{"model":"cheap-oss-model"}'
            # ``_proxy`` is annotated ``-> Response`` but always returns a
            # StreamingResponse; cast so the test can consume its stream.
            response = cast(StreamingResponse, await shim._proxy(request))
            # Consume the stream so the body HEAD scan (which decides `paused`)
            # actually runs.
            async for _chunk in response.body_iterator:
                pass
            return shim.paused

    paused = asyncio.run(_run())
    return shim, paused


def test_shim_sets_paused_on_framework_paused_503() -> None:
    """ADR-0034 M1.11: a paused gateway's stable 503 marker sets ``shim.paused``
    so the worker can classify the result PAUSED_BY_OPERATOR instead of
    MODEL_API_ERROR (a paused gateway is not a model failure).  This is the
    fastest money stop — it must be detectable."""
    shim, paused = _run_503_proxy(b'{"error":{"type":"framework_paused"}}')
    assert paused is True, "framework_paused 503 marker must set shim.paused"
    assert shim._paused_503_pending is True


def test_shim_does_not_set_paused_on_bare_503() -> None:
    """ADR-0034 M1.11: a BARE 503 (wedged LiteLLM, no marker) must NOT set
    ``shim.paused`` — recording a real API failure as PAUSED_BY_OPERATOR is the
    same data corruption M1.11 exists to prevent, run backwards."""
    shim, paused = _run_503_proxy(b'{"error":{"type":"upstream_error"}}')
    assert paused is False, "bare 503 (no framework_paused marker) must not set paused"
    assert shim._paused_503_pending is True


class _Fake401Upstream(_Fake503Upstream):
    """Same shape as _Fake503Upstream, a 401 instead — the key-block marker
    (BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §6)."""

    status_code = 401


class _Fake401Client(_Fake503Client):
    async def send(self, *a, **k):
        return _Fake401Upstream(body=self._body)


def _run_401_proxy(body: bytes) -> tuple[LocalProxy, bool]:
    import asyncio

    import starlette.requests

    shim = LocalProxy(
        upstream_url="http://127.0.0.1:1/v1",
        run_id="r",
        harness="claude_code",
        instance_id="i",
        attempt_number=1,
        usage=Usage(),
    )
    fake_client = _Fake401Client(body=body)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }

    async def _run():
        with mock.patch(
            "swebench_eval.gateway.local_proxy.httpx.AsyncClient", lambda *a, **k: fake_client
        ):
            request = starlette.requests.Request(scope)
            request._body = b'{"model":"cheap-oss-model"}'
            response = cast(StreamingResponse, await shim._proxy(request))
            async for _chunk in response.body_iterator:
                pass
            return shim.paused

    paused = asyncio.run(_run())
    return shim, paused


def test_shim_sets_paused_on_blocked_key_401() -> None:
    """§6: a blocked-key 401 (real body, captured live against litellm:main-
    stable 2026-08-31) sets ``shim.paused`` the same as the framework_paused
    503 — harness_worker.py:658's PAUSED_BY_OPERATOR relabelling needs no
    change, only the detection gains this second marker."""
    real_body = (
        b'{"error":{"message":"Authentication Error, Key is blocked. Update via '
        b'`/key/unblock` if you\'re an admin.","type":"auth_error","param":"None","code":"401"}}'
    )
    shim, paused = _run_401_proxy(real_body)
    assert paused is True, "blocked-key 401 marker must set shim.paused"
    assert shim._blocked_401_pending is True


def test_shim_does_not_set_paused_on_a_genuinely_bad_key_401() -> None:
    """§6 negative case: an unrelated/expired key's 401 (different body, no
    'blocked' wording) must NOT set ``shim.paused`` — the same M1.11 data-
    corruption guard, for the second marker."""
    shim, paused = _run_401_proxy(
        b'{"error":{"message":"Authentication Error, token_not_found_in_db","type":"auth_error"}}'
    )
    assert paused is False, "a genuinely bad key must not be misclassified as an operator pause"
    assert shim._blocked_401_pending is True


# ---------------------------------------------------------------------------
# M0 §2.5 — per-call recorder (ADR-0037)
# ---------------------------------------------------------------------------


class _RecorderUpstream(_StubUpstream):
    """An SSE upstream with provider identity + cache-write + upstream cost."""

    def _handle(self) -> None:
        ln = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(ln) if ln else b""
        type(self).captured.append({"path": self.path, "headers": dict(self.headers), "body": body})
        events = [
            (
                b'data: {"id":"gen-123","model":"deepseek/deepseek-v4",'
                b'"provider":"OpenRouter","choices":[{"delta":{"content":"hi"}}]}\n\n'
            ),
            (
                b'data: {"id":"gen-123","model":"deepseek/deepseek-v4",'
                b'"provider":"OpenRouter","choices":[{"delta":{},"finish_reason":"stop"}],'
                b'"usage":{"prompt_tokens":10,"completion_tokens":5,"cost":0.001,'
                b'"prompt_tokens_details":{"cache_write_tokens":3},'
                b'"cost_details":{"upstream_inference_cost":0.0009}}}\n\n'
            ),
            b"data: [DONE]\n\n",
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for ev in events:
            self.wfile.write(self._chunked(ev))
        self.wfile.write(b"0\r\n\r\n")


def test_shim_records_every_call_to_llm_calls_jsonl(tmp_path) -> None:
    """M0 §2.5: one JSONL line per model call, with request shape, provider
    identity, tokens and cost mapped to the llm_calls columns."""
    _RecorderUpstream.captured = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RecorderUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        usage = Usage()
        shim = LocalProxy(
            upstream_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            run_id="run-1",
            harness="opencode",
            instance_id="i1",
            attempt_number=2,
            usage=usage,
            output_dir=str(tmp_path),
        ).start()
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"http://127.0.0.1:{shim._bound_port}/v1/chat/completions",
                    json={
                        "model": "cheap-oss-model",
                        "stream": True,
                        "max_tokens": 8192,
                        "temperature": 0.0,
                        "tools": [{"type": "function"}],
                    },
                )
                assert resp.status_code == 200
        finally:
            shim.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5)

    lines = (tmp_path / "llm_calls.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1, f"expected exactly one record, got {len(lines)}"
    rec = json.loads(lines[0])

    # Identity / PK.
    assert rec["run_id"] == "run-1"
    assert rec["harness"] == "opencode"
    assert rec["instance_id"] == "i1"
    assert rec["attempt_number"] == 2
    assert rec["call_index"] == 1

    # Request shape.
    assert rec["model_requested"] == "cheap-oss-model"
    assert rec["path"] == "/v1/chat/completions"
    assert rec["stream"] is True
    assert rec["max_tokens_requested"] == 8192
    assert rec["temperature"] == 0.0
    assert rec["n_messages"] is None  # no messages key in this request body
    assert rec["has_tools"] is True
    assert rec["request_bytes"] > 0

    # Provider identity — model_resolved is the RESPONSE model, not the alias.
    assert rec["model_resolved"] == "deepseek/deepseek-v4"
    assert rec["provider_name"] == "OpenRouter"
    assert rec["generation_id"] == "gen-123"
    assert rec["finish_reason"] == "stop"

    # Tokens / cost from the usage block.
    assert rec["input_tokens"] == 10
    assert rec["output_tokens"] == 5
    assert rec["cache_write_tokens"] == 3
    assert rec["cost_usd"] == pytest.approx(0.001)
    assert rec["cost_source"] == "provider"  # API-reported cost present
    assert rec["upstream_inference_cost_usd"] == pytest.approx(0.0009)

    # Outcome / timing.
    assert rec["http_status"] == 200
    assert rec["response_bytes"] > 0
    assert rec["latency_ms"] >= 0
    assert rec["ttft_ms"] >= 0


class _RateLimitedUpstream(_StubUpstream):
    """A 429 with a provider body + Retry-After header (no usage ever)."""

    def _handle(self) -> None:
        ln = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(ln) if ln else b""
        type(self).captured.append({"path": self.path, "headers": dict(self.headers), "body": body})
        payload = b'{"error":{"message":"OpenRouter: rate limited","type":"rate_limit_error"}}'
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        # §2.1 correction (2026-09-01): a REAL provider 429 carries NO retry-after — measured,
        # 0 of 2,651 historical + every live-captured overload body. The old fixture sent
        # Retry-After alongside a provider-shaped body, an unreal combination: under the
        # live-validated header discriminator that would (correctly) classify as 'gateway'.
        # A gateway-shaped 429 (retry-after present) is pinned by the companion test below.
        self.send_header("X-Ratelimit-Remaining-Requests", "0")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_shim_records_429_with_null_tokens_and_rate_limit_scope(tmp_path) -> None:
    """M0 §2.5 + §0.6 Trap 1: a 429 IS recorded, with NULL tokens (nothing was
    reported) and the correct rate_limit_scope — the field only the shim can
    produce (M0 §2.3)."""
    _RateLimitedUpstream.captured = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RateLimitedUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        usage = Usage()
        shim = LocalProxy(
            upstream_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            run_id="r",
            harness="claude_code",
            instance_id="i",
            attempt_number=1,
            usage=usage,
            output_dir=str(tmp_path),
        ).start()
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"http://127.0.0.1:{shim._bound_port}/v1/messages",
                    json={"model": "cheap-oss-model"},
                )
                assert resp.status_code == 429
        finally:
            shim.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5)

    rec = json.loads((tmp_path / "llm_calls.jsonl").read_text().strip().splitlines()[0])
    assert rec["http_status"] == 429
    # NULL tokens: no token fields present (Trap 3 — never zero).
    for f in ("input_tokens", "output_tokens", "cached_tokens", "cost_usd"):
        assert f not in rec, f"{f} should be NULL, got {rec.get(f)}"
    # §2.1 (live-validated): NO retry-after header => a real provider overload.
    assert rec["rate_limit_scope"] == "provider"
    assert "retry_after_s" not in rec
    assert rec["ratelimit_remaining_requests"] == 0
    assert rec["response_bytes"] > 0


class _GatewaySelfLimitUpstream(http.server.BaseHTTPRequestHandler):
    """LiteLLM's own self-imposed 429 — ALWAYS carries retry-after (deployed-source fact the
    §2.1 discriminator rests on)."""

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        payload = json.dumps(
            {"error": {"message": "litellm.RateLimitError: self-imposed limit"}}
        ).encode()
        self.send_response(429)
        self.send_header("Retry-After", "60")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_shim_classifies_a_retry_after_429_as_gateway_scope(tmp_path) -> None:
    """§2.1's other branch: retry-after present => LiteLLM throttled US ('gateway'), whatever
    the body text says — the body-string heuristic this replaces mislabeled wrapped provider
    throttles and was never live-validated."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GatewaySelfLimitUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        usage = Usage()
        shim = LocalProxy(
            upstream_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            run_id="r",
            harness="claude_code",
            instance_id="i",
            attempt_number=1,
            usage=usage,
            output_dir=str(tmp_path),
        ).start()
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"http://127.0.0.1:{shim._bound_port}/v1/messages",
                    json={"model": "cheap-oss-model"},
                )
                assert resp.status_code == 429
        finally:
            shim.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5)

    rec = json.loads((tmp_path / "llm_calls.jsonl").read_text().strip().splitlines()[0])
    assert rec["rate_limit_scope"] == "gateway"
    assert rec["retry_after_s"] == pytest.approx(60.0)


def test_shim_records_upstream_connection_error_even_without_streaming(tmp_path) -> None:
    """M0 §0.6 Trap 1: the record happens on a handler-wide path, so a connection
    failure (where the stream iterator's finally never runs) is STILL recorded —
    the exact failure the pre-M0 shim was blind to."""
    import asyncio

    import starlette.requests

    class _FailingClient:
        async def send(self, *a, **k):
            raise httpx.ConnectError(
                "connection refused", request=httpx.Request("POST", "http://127.0.0.1:1/v1")
            )

        async def aclose(self):
            return None

    shim = LocalProxy(
        upstream_url="http://127.0.0.1:1/v1",
        run_id="r",
        harness="codex",
        instance_id="i",
        attempt_number=1,
        usage=Usage(),
        output_dir=str(tmp_path),
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }

    async def _run():
        with mock.patch(
            "swebench_eval.gateway.local_proxy.httpx.AsyncClient", lambda *a, **k: _FailingClient()
        ):
            request = starlette.requests.Request(scope)
            request._body = b'{"model":"cheap-oss-model","stream":true}'
            response = await shim._proxy(request)
            assert response.status_code == 502

    asyncio.run(_run())

    rec = json.loads((tmp_path / "llm_calls.jsonl").read_text().strip().splitlines()[0])
    assert rec["http_status"] == 502
    assert rec["error_type"] == "ConnectError"
    assert rec["latency_ms"] == 0
    assert "cost_usd" not in rec  # nothing was reported


# ---------------------------------------------------------------------------
# M0-1 / M0-3 (review fixes) — sole-meter robustness
# ---------------------------------------------------------------------------


class _CostDetailsNullUpstream(_StubUpstream):
    """A 200 with cost_details explicitly null — the M0-1 crash shape."""

    def _handle(self) -> None:
        ln = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(ln) if ln else b""
        type(self).captured.append({"path": self.path, "headers": dict(self.headers), "body": body})
        events = [
            (
                b'data: {"usage":{"prompt_tokens":10,"completion_tokens":5,'
                b'"cost":0.001,"cost_details":null}}\n\n'
            ),
            b"data: [DONE]\n\n",
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for ev in events:
            self.wfile.write(self._chunked(ev))
        self.wfile.write(b"0\r\n\r\n")


def test_shim_survives_cost_details_null_and_records(tmp_path) -> None:
    """M0-1 (review): a 200 whose usage block has ``cost_details: null`` must
    NOT raise AttributeError in the sole meter's finally — a successful billed
    completion is recorded (not truncated, not lost), and the row lands."""
    _CostDetailsNullUpstream.captured = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CostDetailsNullUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        usage = Usage()
        shim = LocalProxy(
            upstream_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            run_id="r",
            harness="opencode",
            instance_id="i",
            attempt_number=1,
            usage=usage,
            output_dir=str(tmp_path),
        ).start()
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"http://127.0.0.1:{shim._bound_port}/v1/chat/completions",
                    json={"model": "cheap-oss-model", "stream": True},
                )
                assert resp.status_code == 200
        finally:
            shim.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5)

    # The response completed + the row was written (the old code lost both).
    assert usage.input_tokens == 10
    assert usage.output_tokens == 5
    lines = (tmp_path / "llm_calls.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["input_tokens"] == 10
    assert "upstream_inference_cost_usd" not in rec  # null cost_details -> no upstream cost
    assert "usage_parse_failed" not in rec  # usage WAS parsed


def test_long_non_streaming_identity_comes_from_head(tmp_path) -> None:
    """M0-3 (review): on a long NON-STREAMING JSON body (custom_minimal never
    streams), id/model/provider sit at the FRONT and usage at the END — the 16 KB
    tail keeps tokens+cost but evicts the identity.  The head buffer restores it,
    so model_resolved/provider_name are non-empty (DoD #6) instead of silently
    NULL while the record looks complete."""
    import swebench_eval.gateway.local_proxy as lp

    # A body > _TAIL_LIMIT (16 KB) so identity at the front is evicted from the
    # tail, with usage at the end.  _scan_json_scalar is last-wins per key, so
    # concatenating head + tail keeps BOTH the front identity and the tail usage.
    front = (
        b'{"id":"gen-HUGE","model":"deepseek/deepseek-v4","provider":"OpenRouter","choices":'
        b'[{"message":{"content":"' + b"x" * (20 * 1024) + b'"}}],'
    )
    tail = b'"usage":{"prompt_tokens":999,"completion_tokens":3,"cost":0.01}}'

    # Simulate directly: feed _accumulate_tail with a head that carries identity.
    call: dict[str, object] = {}
    lp._accumulate_tail(tail, Usage(), "cheap-oss-model", call, head=front)
    assert call["model_resolved"] == "deepseek/deepseek-v4"
    assert call["provider_name"] == "OpenRouter"
    assert call["generation_id"] == "gen-HUGE"
    assert call["input_tokens"] == 999


def test_usage_parse_failed_flag_set_on_success_without_usage(tmp_path) -> None:
    """M0-3 (review): a 200 with a body but no parseable usage sets
    usage_parse_failed so 'provider reported nothing' is distinguishable from a
    429 / 'we could not find it' — the record is byte-different from a throttled
    call in the data, not just in the logs."""
    import swebench_eval.gateway.local_proxy as lp

    usage = Usage()
    call: dict[str, object] = {}
    # A 2xx body with NO usage object at all.
    parsed = lp._accumulate_tail(b'{"foo":"bar"}', usage, "cheap-oss-model", call)
    assert parsed is None  # the caller now knows parsing failed


# ---------------------------------------------------------------------------
# R2-2 (round-2 review) — generation_id must be the TOP-LEVEL id, not a tool-call id
# ---------------------------------------------------------------------------


def test_generation_id_is_top_level_not_last_tool_call(tmp_path) -> None:
    """R2-2 (review): a non-streaming body with tool calls has the top-level
    `id` in the HEAD and a tool_calls[].id in the TAIL.  generation_id must be
    the top-level response id (the ledger join key), never the last tool-call id
    — a last-wins scan silently writes call_0039 and the reconciliation comes up
    short with no error."""
    import swebench_eval.gateway.local_proxy as lp

    content = b"working on it " * 300  # > head/tail eviction boundary
    head = (
        b'{"id":"gen-REALGENERATIONID","model":"deepseek/deepseek-v4",'
        b'"provider":"DeepSeek","choices":[{"message":{"content":"' + content + b'","tool_calls":['
    )
    # 40 tool calls with ids in the tail, interleaved with usage at the end.
    calls = ",".join(
        f'{{"id":"call_{i:04d}","type":"function","function":{{"name":"t","arguments":"{{}}"}}}}'
        for i in range(40)
    )
    tail = (
        calls.encode()
        + b'],"finish_reason":"tool_calls"}}],"usage":{"prompt_tokens":999,"completion_tokens":3,"cost":0.01}}'
    )

    call: dict[str, object] = {}
    lp._accumulate_tail(tail, Usage(), "cheap-oss-model", call, head=head)
    assert call["generation_id"] == "gen-REALGENERATIONID", call["generation_id"]
    assert call["model_resolved"] == "deepseek/deepseek-v4"
    assert call["provider_name"] == "DeepSeek"
    assert call["finish_reason"] == "tool_calls"
    assert call["input_tokens"] == 999


# ---------------------------------------------------------------------------
# R3 (builder1-REMAINING-WORK-single-handover) — the shim KILLS on budget breach
# ---------------------------------------------------------------------------


def test_shim_refuses_calls_after_token_ceiling(tmp_path) -> None:
    """R3: once accumulated usage crosses the per-instance token ceiling, the
    shim refuses every further call with 400 (terminal for every SDK/CLI —
    master-handover 3.8; mini retried 429 thirty times), bounding overshoot to
    one call.  The refusal is itself recorded as a call (Trap 1)."""
    _StubUpstream.captured = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        usage = Usage()
        shim = LocalProxy(
            upstream_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            run_id="run-b",
            harness="opencode",
            instance_id="i-b",
            attempt_number=1,
            usage=usage,
            output_dir=str(tmp_path),
            # Each _StubUpstream call reports 10 prompt + 5 completion tokens.
            # The FIRST call crosses the 10-token ceiling; the SECOND is refused.
            max_tokens_per_instance=10,
        ).start()
        try:
            with httpx.Client(timeout=30.0) as client:
                first = client.post(
                    f"http://127.0.0.1:{shim._bound_port}/v1/chat/completions",
                    json={"model": "cheap-oss-model", "stream": True},
                )
                assert first.status_code == 200
                assert shim.budget_refused is False  # no breach yet before first call

                second = client.post(
                    f"http://127.0.0.1:{shim._bound_port}/v1/chat/completions",
                    json={"model": "cheap-oss-model", "stream": True},
                )
                assert (
                    second.status_code == 400
                ), f"post-ceiling call must be refused, got {second.status_code}"
                assert shim.budget_refused is True
                assert second.headers.get("x-eval-budget-exceeded") == "1"
        finally:
            shim.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5)

    # The refusal was recorded (Trap 1): two call rows, the second a 400 refusal.
    lines = (tmp_path / "llm_calls.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2, f"expected 2 records (200 + 400 refusal), got {len(lines)}"
    rec2 = json.loads(lines[1])
    assert rec2["http_status"] == 400
    assert rec2["error_type"] == "budget_exceeded"


def test_shim_refuses_on_cost_ceiling_too() -> None:
    """R3: the cost ceiling (not just tokens) trips the same refusal."""
    _StubUpstream.captured = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        usage = Usage()
        shim = LocalProxy(
            upstream_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            run_id="run-c",
            harness="opencode",
            instance_id="i-c",
            attempt_number=1,
            usage=usage,
            # _StubUpstream reports cost 0.001; a $0.0005 ceiling is crossed
            # by the first call's accumulated cost.
            max_cost_usd_per_instance=0.0005,
        ).start()
        try:
            with httpx.Client(timeout=30.0) as client:
                first = client.post(
                    f"http://127.0.0.1:{shim._bound_port}/v1/chat/completions",
                    json={"model": "cheap-oss-model", "stream": True},
                )
                assert first.status_code == 200
                second = client.post(
                    f"http://127.0.0.1:{shim._bound_port}/v1/chat/completions",
                    json={"model": "cheap-oss-model", "stream": True},
                )
                assert second.status_code == 400
        finally:
            shim.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_shim_refuses_after_turn_cap(tmp_path) -> None:
    """master-handover 3.12: the shim enforces max_turns_per_instance, and a
    turn-killed run is marked x-eval-turns-exceeded (max_turns_exceeded), NOT
    the budget marker.  One serviced call reaches the upstream; the second is
    refused."""
    _StubUpstream.captured = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        usage = Usage()
        shim = LocalProxy(
            upstream_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            run_id="run-turn",
            harness="opencode",
            instance_id="i-turn",
            attempt_number=1,
            usage=usage,
            output_dir=str(tmp_path),
            max_turns_per_instance=1,
        ).start()
        try:
            with httpx.Client(timeout=30.0) as client:
                url = f"http://127.0.0.1:{shim._bound_port}/v1/chat/completions"
                assert shim.turns_breached is False  # cap not reached yet
                first = client.post(url, json={"model": "cheap-oss-model", "stream": True})
                assert first.status_code == 200, "the serviced (1st) call must pass"
                # with cap=1 the first serviced call exhausted the cap; the next
                # is refused (the check runs BEFORE each call).
                second = client.post(url, json={"model": "cheap-oss-model", "stream": True})
                assert second.status_code == 400, "post-cap call must be refused"
                assert second.headers.get("x-eval-turns-exceeded") == "1"
                assert second.headers.get("x-eval-budget-exceeded") is None
                assert shim.turns_breached is True
        finally:
            shim.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# R5.2 (builder1-REMAINING-WORK-single-handover) — stop_reason captured
# ---------------------------------------------------------------------------


def test_shim_captures_stop_reason_for_anthropic_messages() -> None:
    """R5.2: Anthropic /v1/messages (claude_code's wire format) carries the
    terminal marker as `stop_reason`, not `finish_reason`.  Without scanning
    stop_reason every claude_code call recorded None, so a truncation was
    indistinguishable from a clean stop."""
    import swebench_eval.gateway.local_proxy as lp

    call: dict[str, object] = {}
    data = (
        b'{"id":"msg_1","type":"message","stop_reason":"max_tokens",'
        b'"usage":{"input_tokens":10,"output_tokens":5}}'
    )
    parsed = lp._accumulate_tail(data, Usage(), "cheap-oss-model", call)
    assert parsed is not None
    assert call["stop_reason"] == "max_tokens", call.get("stop_reason")


def test_shim_captures_stop_reason_for_responses_api() -> None:
    """R5.2: OpenAI /v1/responses (codex's wire format) also uses stop_reason."""
    import swebench_eval.gateway.local_proxy as lp

    call: dict[str, object] = {}
    data = (
        b'{"id":"resp_1","object":"response","status":"completed",'
        b'"stop_reason":"max_output_tokens","usage":{"input_tokens":10,"output_tokens":5}}'
    )
    parsed = lp._accumulate_tail(data, Usage(), "cheap-oss-model", call)
    assert parsed is not None
    assert call["stop_reason"] == "max_output_tokens", call.get("stop_reason")


def test_is_completion_path_excludes_count_tokens() -> None:
    """Finding 2 (2026-08-27): `/v1/messages/count_tokens` is Anthropic's token-count
    utility, NOT a completion. It contains `/v1/messages` as a substring, so the old
    matcher miscounted each such call as a turn (inflating the turn budget and
    recording ~17 false `usage_parse_failed` rows). The `count_tokens` suffix must
    be excluded.

    Mutation-proof: revert to `"/v1/messages" in path` without the count_tokens
    exclusion; this test fails.
    """
    import swebench_eval.gateway.local_proxy as lp

    # Real completion paths still count as turns.
    assert lp._is_completion_path("/v1/messages?beta=true")
    assert lp._is_completion_path("/v1/chat/completions")
    assert lp._is_completion_path("/v1/responses")
    # The token-count utility must NOT count as a completion/turn.
    assert not lp._is_completion_path("/v1/messages/count_tokens?beta=true")
    # Non-completion probes / model lists still excluded.
    assert not lp._is_completion_path("/v1/models")
    assert not lp._is_completion_path("/health")


def test_streaming_tail_limit_is_widened() -> None:
    """Finding 3 (2026-08-27): streaming responses kept the original 16 KB tail while
    non-streaming got 4 MB. codex's Responses-API completions routinely run 500-611 KB —
    the terminal SSE event carries BOTH the full reasoning/output text AND the `usage`
    block, so the last 16 KB never reached `usage` -> `usage_parse_failed` on ~20% of
    codex's 200s even though codex's own adapter parsed usage every turn. The streaming
    tail must be widened to reliably contain the terminal event.

    Mutation-proof: revert `_STREAM_TAIL_LIMIT` to 16 KB (or reuse `_TAIL_LIMIT` for
    streams); this test fails.
    """
    import swebench_eval.gateway.local_proxy as lp

    # The streaming bound must be meaningfully larger than the original 16 KB tail.
    assert lp._STREAM_TAIL_LIMIT > lp._TAIL_LIMIT
    # It must cover codex's largest observed failing body (~611 KB) with headroom.
    assert lp._STREAM_TAIL_LIMIT >= 512 * 1024
