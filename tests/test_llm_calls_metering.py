"""Metering fixes — to-the-verified-run review (STEP 1 + STEP 3, local_proxy).

Each test exists to catch a specific regression the reviewer found in the Stage 6
meter: G-7 (wrong usage block), G-1 (Anthropic cache reads never seen), G-6
(model_resolved is the alias), G-3 (provider name lost on streaming), G-4/G-5
(codex Responses completion + unparseable usage), the 1.6 new fields, and the
STEP 3 latency breakdown.  Mutation-check each: revert the corresponding fix and
the matching test must FAIL (a test that cannot fail is how G-1 and G-7 both
survived this long).
"""

from __future__ import annotations

import http.server
import threading
from typing import Any, ClassVar

import httpx

from swebench_eval.gateway.local_proxy import (
    LocalProxy,
    _accumulate_tail,
    _apply_gateway_timing_headers,
    _usage_from_tail,
)
from swebench_eval.harnesses.base import Usage


def _parse(data: bytes, **kw: Any) -> dict[str, Any]:
    call: dict[str, Any] = {}
    usage = Usage()
    _accumulate_tail(data, usage, "cheap-oss-model", call, **kw)
    return call


# ---------------------------------------------------------------------------
# G-7 — _usage_from_tail must take the MOST COMPLETE usage block, not the first
# ---------------------------------------------------------------------------


def test_g7_picks_nonzero_usage_over_message_start_zeros() -> None:
    """Anthropic emits usage TWICE — an all-zero block in message_start and the
    real one in message_delta.  The naive parser returned the FIRST block, so
    47% of claude_code's calls recorded input_tokens == 0.  Must pick the
    non-zero block.

    Mutation: revert _usage_from_tail to return the FIRST usage object found;
    this test fails (input_tokens comes back 0)."""
    tail = (
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":0,'
        b'"output_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},'
        b'"usage":{"input_tokens":100,"output_tokens":20,'
        b'"cache_creation_input_tokens":60,"cache_read_input_tokens":3}}\n'
    )
    call = _parse(tail)
    assert call["input_tokens"] == 100, call.get("input_tokens")
    assert call["output_tokens"] == 20
    assert call["input_tokens"] != 0


def test_g7_returns_last_when_all_usage_blocks_are_zero() -> None:
    """When every usage object is zero (a genuinely empty response), return the
    LAST one rather than inventing a value or dropping the parse."""
    tail = (
        b'{"usage":{"input_tokens":0,"output_tokens":0}}\n'
        b'{"usage":{"input_tokens":0,"output_tokens":0,"cache_read_input_tokens":0}}'
    )
    assert _usage_from_tail(tail) is not None
    # The last block is returned whole (it still has the expected keys).
    u = _usage_from_tail(tail)
    assert u is not None and "cache_read_input_tokens" in u


def test_g7_deduplicates_not_conflates_sse_events() -> None:
    """Two SEPARATE calls' usage blocks in one tail must not be merged — the
    parser returns ONE complete block, not a union."""
    tail = (
        b'data: {"type":"message_delta","usage":{"input_tokens":50,"output_tokens":5}}\n'
        b'data: {"type":"message_delta","usage":{"input_tokens":70,"output_tokens":7}}'
    )
    u = _usage_from_tail(tail)
    assert u is not None
    # Most complete = first non-zero = the 50/5 block; never the sum 120/12.
    assert u["input_tokens"] == 50
    assert u["output_tokens"] == 5


# ---------------------------------------------------------------------------
# G-1 — Anthropic cache keys map to the OpenAI-shaped cache columns
# ---------------------------------------------------------------------------


def test_g1_maps_anthropic_top_level_cache_reads() -> None:
    """Anthropic reports cache as TOP-LEVEL usage keys
    (cache_read_input_tokens / cache_creation_input_tokens), not nested in
    prompt_tokens_details.  Without this mapping claude_code recorded 0 cached
    tokens and priced cache reads at full input rate (defeating B8).

    Mutation: remove the top-level cache_read_input_tokens fallback; this test
    fails (cached_tokens comes back 0)."""
    tail = (
        b'{"usage":{"input_tokens":100,"output_tokens":20,'
        b'"cache_creation_input_tokens":60,"cache_read_input_tokens":3}}'
    )
    call = _parse(tail)
    assert call["cached_tokens"] == 3, call.get("cached_tokens")
    assert call["cache_write_tokens"] == 60, call.get("cache_write_tokens")


def test_g1_keeps_openai_nested_shape_working() -> None:
    """The OpenAI nested shape (prompt_tokens_details.cached_tokens) must keep
    working — the mapping is additive, not a replacement."""
    tail = (
        b'{"usage":{"prompt_tokens":100,"completion_tokens":20,'
        b'"prompt_tokens_details":{"cached_tokens":9}}}'
    )
    call = _parse(tail)
    assert call["cached_tokens"] == 9


# ---------------------------------------------------------------------------
# G-6 — model_resolved from the x-litellm-model-name header, not the body alias
# ---------------------------------------------------------------------------


def test_g6_reads_model_resolved_from_header() -> None:
    """LiteLLM echoes the REQUESTED alias in the chat-shape body `model` (so
    custom_minimal/mini/opencode all recorded `cheap-oss-model`), but returns
    the literal upstream in the x-litellm-model-name response header.  The shim
    must prefer the header.

    Mutation: drop the header read for model_resolved; this test fails (it
    falls back to the alias in the body)."""
    body = (
        b'{"id":"gen-1","model":"cheap-oss-model",'
        b'"usage":{"prompt_tokens":10,"completion_tokens":5}}'
    )
    call = _parse(
        body, headers={"x-litellm-model-name": "openrouter/deepseek/deepseek-v4-flash-0731"}
    )
    assert call["model_resolved"] == "openrouter/deepseek/deepseek-v4-flash-0731"
    # generation_id prefers the x-generation-id header too (1.6).
    call2 = _parse(body, headers={"x-generation-id": "gen-HDR"})
    assert call2["generation_id"] == "gen-HDR"


def test_g6_falls_back_to_body_when_no_header() -> None:
    """Without the header (e.g. a parser with no header map), the body scan
    still supplies model_resolved."""
    body = b'{"model":"openrouter/x/y","usage":{"prompt_tokens":1,"completion_tokens":1}}'
    call = _parse(body, headers=None)
    assert call["model_resolved"] == "openrouter/x/y"


# ---------------------------------------------------------------------------
# G-3 — provider_name scanned head + tail, not head alone
# ---------------------------------------------------------------------------


def test_g3_provider_name_found_in_tail_head_plus_tail_scan() -> None:
    """provider sits near the front of a stream (SSE chunk 0), which lands in
    the head buffer — but an occurrence that falls in the tail past the head
    must still be found.  Scan head + tail, not head alone.

    Mutation: revert provider_name to _first_json_scalar(head_text, ...) with an
    empty head; this test fails."""
    body = b'{"provider":"DeepInfra","usage":{"prompt_tokens":5,"completion_tokens":2}}'
    # Simulate a case where head carried no provider but the full text does.
    call = _parse(body, head=b"")
    assert call["provider_name"] == "DeepInfra"


# ---------------------------------------------------------------------------
# G-4 — Responses-API completion fields (codex)
# ---------------------------------------------------------------------------


def test_g4_captures_responses_status_and_incomplete_reason() -> None:
    """codex (OpenAI Responses) signals its terminal state as `status` +
    `incomplete_details.reason`, NOT finish_reason/stop_reason — so it recorded
    neither before.  Both must now be captured.

    Mutation: remove the responses_status / responses_incomplete_reason reads;
    this test fails."""
    body = (
        b'data: {"type":"response.incomplete","response":{"status":"incomplete",'
        b'"incomplete_details":{"reason":"max_output_tokens"},'
        b'"output":[{"type":"message","content":[{"type":"output_text","text":"hi"}]}]}}\n'
        b'data: {"type":"response.completed","response":{"status":"completed",'
        b'"incomplete_details":{}},"usage":{"input_tokens":10,"output_tokens":5}}\n'
    )
    call = _parse(body)
    # Last-wins: the terminal event's status wins.
    assert call["responses_status"] == "completed", call.get("responses_status")
    # incomplete_details on the final event is empty -> no reason.
    assert call["responses_incomplete_reason"] is None or call["responses_incomplete_reason"] == ""


def test_g4_incomplete_reason_captured_on_length_stop() -> None:
    """A Responses terminal `incomplete` event with a non-empty reason must
    surface it (a length stop is the exact case that was indistinguishable)."""
    body = (
        b'{"type":"response.incomplete","response":{"status":"incomplete",'
        b'"incomplete_details":{"reason":"max_output_tokens"}},'
        b'"usage":{"input_tokens":10,"output_tokens":5}}'
    )
    call = _parse(body)
    assert call["responses_status"] == "incomplete"
    assert call["responses_incomplete_reason"] == "max_output_tokens"


# ---------------------------------------------------------------------------
# 1.6 — the new free fields
# ---------------------------------------------------------------------------


def test_new_identity_fields_captured() -> None:
    """native_finish_reason / system_fingerprint / service_tier — all free, all
    from the live response."""
    body = (
        b'{"id":"gen-1","model":"m","native_finish_reason":"length",'
        b'"system_fingerprint":"fp_abc","service_tier":"default",'
        b'"usage":{"prompt_tokens":5,"completion_tokens":2}}'
    )
    call = _parse(body)
    assert call["native_finish_reason"] == "length"
    assert call["system_fingerprint"] == "fp_abc"
    assert call["service_tier"] == "default"


def test_cost_details_split_captured() -> None:
    """1.6: the prompt/completion cost split from usage.cost_details, without
    re-deriving from a price table."""
    body = (
        b'{"usage":{"prompt_tokens":10,"completion_tokens":5,'
        b'"cost":1.5e-05,"cost_details":{'
        b'"upstream_inference_cost":1.2e-05,'
        b'"upstream_inference_prompt_cost":8e-06,'
        b'"upstream_inference_completions_cost":4e-06}}}'
    )
    call = _parse(body)
    assert call["upstream_inference_prompt_cost_usd"] == 8e-06
    assert call["upstream_inference_completions_cost_usd"] == 4e-06
    assert call["upstream_inference_cost_usd"] == 1.2e-05
    assert call["cost_source"] == "provider"


# ---------------------------------------------------------------------------
# STEP 3 — latency breakdown
# ---------------------------------------------------------------------------


def test_cost_details_null_does_not_raise() -> None:
    """cost_details present-but-null must not take down the meter (the old bare
    .get() raised AttributeError mid-parse)."""
    body = b'{"usage":{"prompt_tokens":3,"completion_tokens":1,"cost_details":null}}'
    call = _parse(body)  # must not raise
    assert call["input_tokens"] == 3
    assert "upstream_inference_cost_usd" not in call


def test_reasoning_tokens_from_anthropic_thinking_tokens() -> None:
    """Anthropic reports thinking tokens as output_tokens_details.thinking_tokens,
    not reasoning_tokens — claude_code always read 0 here before (review 2026-08-27
    §3). Must capture the Anthropic key (and still the OpenAI reasoning_tokens).

    Mutation: revert to `cd.get("reasoning_tokens", 0)` only; this test fails
    (reasoning_tokens comes back 0 for the Anthropic shape)."""
    anthropic = b'{"usage":{"input_tokens":10,"output_tokens":5,"output_tokens_details":{"thinking_tokens":13}}}'
    assert _parse(anthropic)["reasoning_tokens"] == 13

    openai = b'{"usage":{"prompt_tokens":10,"completion_tokens":5,"completion_tokens_details":{"reasoning_tokens":7}}}'
    assert _parse(openai)["reasoning_tokens"] == 7


def test_apply_gateway_timing_headers_maps_three_litellm_headers() -> None:
    """STEP 3 tier 2: the three LiteLLM timing headers we already receive are
    recorded as NUMERIC columns.

    Mutation: remove the header→field mapping in _apply_gateway_timing_headers;
    this test fails (the fields stay absent)."""
    call: dict[str, Any] = {}
    _apply_gateway_timing_headers(
        call,
        {
            "x-litellm-response-duration-ms": "1234.5",
            "x-litellm-overhead-duration-ms": "12",
            "x-litellm-callback-duration-ms": "3.25",
        },
    )
    assert call["gateway_response_ms"] == 1234.5
    assert call["gateway_overhead_ms"] == 12
    assert call["gateway_callback_ms"] == 3.25


def _chunked(payload: bytes) -> bytes:
    return b"%x\r\n" % len(payload) + payload + b"\r\n"


def test_shim_records_latency_breakdown_end_to_end(tmp_path) -> None:
    """STEP 3 through the real proxy: a record carries stream_ms,
    shim_preflight_ms and the three gateway timing headers.  This is the shape
    the full-run comparison is built on — a missing latency column means the
    affidavit is not reproducible.

    Mutation: drop the `call["stream_ms"] = ...` line in _proxy's finally; this
    test fails on stream_ms."""
    import json as _json

    class _TimingUpstream(http.server.BaseHTTPRequestHandler):
        captured: ClassVar[list[str]] = []

        def _handler(self) -> None:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            self.rfile.read(ln) if ln else None
            type(self).captured.append(self.path)
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
            self.send_header("x-litellm-response-duration-ms", "500.5")
            self.send_header("x-litellm-overhead-duration-ms", "8")
            self.send_header("x-litellm-callback-duration-ms", "1.5")
            self.end_headers()
            for ev in events:
                self.wfile.write(_chunked(ev))
            self.wfile.write(b"0\r\n\r\n")

        do_POST = _handler

    _TimingUpstream.captured = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TimingUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        shim = LocalProxy(
            upstream_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            run_id="run-lat",
            harness="opencode",
            instance_id="i",
            attempt_number=1,
            usage=Usage(),
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

    lines = (tmp_path / "llm_calls.jsonl").read_text().strip().splitlines()
    rec = _json.loads(lines[0])
    assert rec["latency_ms"] is not None and rec["latency_ms"] >= 0
    assert rec["stream_ms"] is not None and rec["stream_ms"] <= rec["latency_ms"]
    assert rec["shim_preflight_ms"] is not None and rec["shim_preflight_ms"] >= 0
    assert rec["gateway_response_ms"] == 500.5
    assert rec["gateway_overhead_ms"] == 8
    assert rec["gateway_callback_ms"] == 1.5
