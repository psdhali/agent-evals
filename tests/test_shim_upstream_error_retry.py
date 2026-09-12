"""The shim retries an upstream error that OpenRouter embedded in a 200 (2026-09-04).

Real data, deepseek x mini run 01788550040741118596-dd284a4f (spend log, request
gen-1788551498-YV13VIJ3o2oZB9pxPoFT): HTTP 200, ``choices[0].message.content == " "``,
``finish_reason "stop"``, ``provider_specific_fields.native_finish_reason "error"`` and
``provider_specific_fields.error == {code 502, "Upstream error from OpenInference: Service
temporarily unavailable", metadata.error_type "provider_unavailable"}``, usage
``completion_tokens 1``. Five instances got three of these in a row within ~2 s each and
mini-swe-agent exited on them (three no-action replies) -> EMPTY_PATCH.
"""

from __future__ import annotations

import http.server
import json
from typing import Any, ClassVar

import pytest

from swebench_eval.gateway import local_proxy
from swebench_eval.gateway.local_proxy import _embedded_upstream_error
from tests.test_local_proxy_pacing import FakePacer, _run_shim

# The LiteLLM-normalised body exactly as the spend log stored it (trimmed to the relevant fields).
_LITELLM_EMBEDDED_ERROR: dict[str, Any] = {
    "id": "gen-1788551498-YV13VIJ3o2oZB9pxPoFT",
    "model": "deepseek-v4-flash-0731-mini",
    "usage": {"prompt_tokens": 9252, "completion_tokens": 1, "total_tokens": 9253},
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": " ",
                "tool_calls": None,
                "provider_specific_fields": {"refusal": None, "reasoning": None},
            },
            "finish_reason": "stop",
            "provider_specific_fields": {
                "error": {
                    "code": 502,
                    "message": "Upstream error from OpenInference: Service temporarily unavailable",
                    "metadata": {"error_type": "provider_unavailable"},
                },
                "native_finish_reason": "error",
            },
        }
    ],
    "provider": "OpenInference",
}

# The raw OpenRouter shape (what LiteLLM normalises from).
_RAW_OPENROUTER_EMBEDDED_ERROR: dict[str, Any] = {
    "id": "gen-x",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": ""},
            "finish_reason": "stop",
            "native_finish_reason": "error",
            "error": {"code": 502, "message": "Upstream error", "metadata": {"raw": "..."}},
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 0},
}

_GOOD: dict[str, Any] = {
    "choices": [
        {"message": {"role": "assistant", "content": "recovered"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 400, "completion_tokens": 3, "total_tokens": 403},
}


class TestDetector:
    def test_litellm_shape_from_the_spend_log(self) -> None:
        assert _embedded_upstream_error(json.dumps(_LITELLM_EMBEDDED_ERROR).encode()) == (
            "provider_unavailable"
        )

    def test_raw_openrouter_shape(self) -> None:
        # no metadata.error_type -> the numeric code
        assert (
            _embedded_upstream_error(json.dumps(_RAW_OPENROUTER_EMBEDDED_ERROR).encode()) == "502"
        )

    def test_native_finish_reason_alone_counts(self) -> None:
        body = {"choices": [{"message": {"content": " "}, "native_finish_reason": "error"}]}
        assert _embedded_upstream_error(json.dumps(body).encode()) == "native_finish_reason_error"

    def test_genuine_completions_and_garbage_are_not_errors(self) -> None:
        assert _embedded_upstream_error(json.dumps(_GOOD).encode()) is None
        # a one-space answer WITHOUT the marker is the model's answer, not an error
        short = {"choices": [{"message": {"content": " "}, "finish_reason": "stop"}]}
        assert _embedded_upstream_error(json.dumps(short).encode()) is None
        assert _embedded_upstream_error(b"not json") is None
        assert _embedded_upstream_error(b"[]") is None
        assert _embedded_upstream_error(json.dumps({"error": {"code": 400}}).encode()) is None


def _json_response(handler: http.server.BaseHTTPRequestHandler, obj: dict[str, Any]) -> None:
    payload = json.dumps(obj).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class _EmbeddedErrorThenOk(http.server.BaseHTTPRequestHandler):
    """The 19:51Z shape: two 200-with-embedded-error answers, then a real one."""

    fails_remaining: ClassVar[int] = 2
    seen: ClassVar[int] = 0

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        cls = type(self)
        cls.seen += 1
        ln = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(ln)
        if cls.fails_remaining > 0:
            cls.fails_remaining -= 1
            _json_response(self, _LITELLM_EMBEDDED_ERROR)
            return
        _json_response(self, _GOOD)


class _AlwaysEmbeddedError(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[int] = 0

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        type(self).seen += 1
        ln = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(ln)
        _json_response(self, _LITELLM_EMBEDDED_ERROR)


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    monkeypatch.setattr(local_proxy, "_UPSTREAM_ERROR_BACKOFF_BASE_S", 0.01)


def test_embedded_error_is_retried_invisibly_then_the_real_answer_is_forwarded(
    tmp_path,
) -> None:
    _EmbeddedErrorThenOk.fails_remaining = 2
    _EmbeddedErrorThenOk.seen = 0
    pacer = FakePacer()
    responses, records = _run_shim(tmp_path, _EmbeddedErrorThenOk, pacer)

    assert _EmbeddedErrorThenOk.seen == 3  # two failures + the recovery
    assert responses[0].status_code == 200
    assert responses[0].json()["choices"][0]["message"]["content"] == "recovered"  # not " "
    (rec,) = records  # ONE record for the call, with the retry decomposition
    assert rec["http_status"] == 200
    assert rec["upstream_error_retries"] == 2
    assert rec.get("error_type") is None  # it recovered
    assert rec.get("overload_retries") is None  # this is NOT the 429 path
    assert rec["output_tokens"] == 3  # the FINAL attempt's usage, not the placeholder's
    assert rec["retry_upstream_ms"] >= 0 and rec["overload_backoff_ms"] >= 0
    # the reservation was released during each backoff and re-acquired for each re-send
    assert len(pacer.releases) == 3 and len(pacer.acquires) == 3
    # a blip is not capacity pressure: the overload counter is untouched
    assert pacer.overloads == 0


def test_exhausted_retries_forward_the_gateways_body_and_flag_the_record(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(local_proxy, "_MAX_UPSTREAM_ERROR_RETRIES", 2)
    _AlwaysEmbeddedError.seen = 0
    responses, records = _run_shim(tmp_path, _AlwaysEmbeddedError, FakePacer())

    assert _AlwaysEmbeddedError.seen == 3  # the original + 2 retries
    # the CLI receives exactly what the gateway said (status and body untouched)...
    assert responses[0].status_code == 200
    body = responses[0].json()
    assert body["choices"][0]["message"]["content"] == " "
    assert body["choices"][0]["provider_specific_fields"]["native_finish_reason"] == "error"
    # ...and the ledger says why this "answer" is not one
    (rec,) = records
    assert rec["upstream_error_retries"] == 2
    assert rec["error_type"] == "upstream_error_embedded"
    assert rec["error_code"] == "provider_unavailable"
    assert rec["native_finish_reason"] == "error"
    assert rec["output_tokens"] == 1


def test_a_genuine_completion_is_not_retried_and_carries_no_retry_fields(tmp_path) -> None:
    from tests.test_local_proxy_pacing import _OkUpstream

    responses, records = _run_shim(tmp_path, _OkUpstream, FakePacer())
    assert responses[0].json()["choices"][0]["message"]["content"] == "hi"
    (rec,) = records
    assert "upstream_error_retries" not in rec
    assert rec.get("error_type") is None


class _StreamedEmbeddedError(http.server.BaseHTTPRequestHandler):
    """A streamed completion whose final chunk carries the marker — bytes are already with
    the CLI, so the shim records it and does NOT retry."""

    seen: ClassVar[int] = 0

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        type(self).seen += 1
        ln = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(ln)
        events = [
            (
                b'data: {"choices":[{"delta":{"content":" "},"finish_reason":"stop",'
                b'"native_finish_reason":"error","error":{"code":502,"message":"Upstream error"}}]}'
                b"\n\n"
            ),
            b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":1}}\n\n',
            b"data: [DONE]\n\n",
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for ev in events:
            self.wfile.write(b"%x\r\n" % len(ev) + ev + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")


def test_a_streamed_embedded_error_is_recorded_not_retried(tmp_path) -> None:
    import threading

    import httpx

    from swebench_eval.gateway.local_proxy import LocalProxy
    from swebench_eval.harnesses.base import Usage

    _StreamedEmbeddedError.seen = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StreamedEmbeddedError)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        shim = LocalProxy(
            upstream_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            run_id="r",
            harness="custom_minimal",
            instance_id="i",
            attempt_number=1,
            usage=Usage(),
            output_dir=str(tmp_path),
            pacer=FakePacer(),  # type: ignore[arg-type]  # FakePacer stands in for Pacer
        ).start()
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"http://127.0.0.1:{shim._bound_port}/v1/chat/completions",
                    json={"model": "laguna-xs-2.1-custom_minimal", "messages": [], "stream": True},
                )
                assert resp.status_code == 200
        finally:
            shim.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert _StreamedEmbeddedError.seen == 1  # no retry on a stream
    (rec,) = [
        json.loads(line) for line in (tmp_path / "llm_calls.jsonl").read_text().strip().splitlines()
    ]
    assert rec["stream"] is True
    assert "upstream_error_retries" not in rec
    assert rec["native_finish_reason"] == "error"
    assert rec["error_type"] == "upstream_error_embedded"
