"""F1 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): the shim injects ``max_tokens``
when a completion request carries none.

No harness sends an output cap; Parasail then reserves its own default (131,072
for qwen) and rejects every prompt past 131K with a 400 — sphinx-10614 crashed
there at call 165 while the harness's compaction threshold sat at 235,929. The
shim now injects ``min(OUTPUT_RESERVE, W - est_prompt)`` so prompt + output
stays <= W exactly where compaction hands over; the value must be the SHARED
16,384 reserve (harnesses/compaction.py), not the stale 32,768 copies.
"""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any, ClassVar

import httpx

import swebench_eval.gateway.local_proxy as lp
from swebench_eval.gateway.local_proxy import LocalProxy
from swebench_eval.gateway.rotatable_models import ROTATABLE_MODELS
from swebench_eval.harnesses.base import Usage
from swebench_eval.harnesses.compaction import OUTPUT_RESERVE
from swebench_eval.orchestrator.run_config import DEFAULT_OUTPUT_RESERVE_TOKENS

W = 262_144


# ── the pure request mutation ─────────────────────────────────────────────────


def _chat(**fields: Any) -> bytes:
    return json.dumps({"model": "qwen3-coder-next-mini", "messages": [], **fields}).encode()


def test_injects_the_shared_reserve_when_absent() -> None:
    body, injected = lp._inject_max_tokens(
        "/v1/chat/completions", _chat(), window=W, est_prompt_tokens=50_000
    )
    assert injected == OUTPUT_RESERVE == 16_384
    assert json.loads(body)["max_tokens"] == 16_384


def test_no_window_injects_the_bare_reserve() -> None:
    body, injected = lp._inject_max_tokens(
        "/v1/chat/completions", _chat(), window=None, est_prompt_tokens=10**9
    )
    assert injected == OUTPUT_RESERVE
    assert json.loads(body)["max_tokens"] == OUTPUT_RESERVE


def test_leaves_a_harness_set_cap_verbatim() -> None:
    original = _chat(max_tokens=4096)
    body, injected = lp._inject_max_tokens(
        "/v1/chat/completions", original, window=W, est_prompt_tokens=100
    )
    assert injected is None
    assert body == original  # byte-for-byte: ADR-0019 verbatim forwarding


def test_max_completion_tokens_counts_as_capped() -> None:
    original = _chat(max_completion_tokens=2048)
    body, injected = lp._inject_max_tokens(
        "/v1/chat/completions", original, window=W, est_prompt_tokens=100
    )
    assert injected is None
    assert body == original


def test_clamps_to_the_remaining_window_when_the_prompt_is_large() -> None:
    # Past the compaction threshold: only 10,000 tokens of window remain.
    body, injected = lp._inject_max_tokens(
        "/v1/chat/completions", _chat(), window=W, est_prompt_tokens=W - 10_000
    )
    assert injected == 10_000
    assert json.loads(body)["max_tokens"] == 10_000


def test_clamp_never_goes_below_the_floor() -> None:
    _, injected = lp._inject_max_tokens(
        "/v1/chat/completions", _chat(), window=W, est_prompt_tokens=W + 5_000
    )
    assert injected == lp._MAX_TOKENS_INJECT_FLOOR == 1024


def test_at_the_compaction_threshold_the_full_reserve_still_fits() -> None:
    """The invariant the 16,384 choice rests on: a prompt at the compaction trigger
    (min(0.9W, W - reserve)) still gets the full reserve — no crash window between
    'compaction has not fired' and 'provider rejects'. 32,768 would break this."""
    from swebench_eval.harnesses.compaction import compute_threshold

    _, injected = lp._inject_max_tokens(
        "/v1/chat/completions", _chat(), window=W, est_prompt_tokens=compute_threshold(W)
    )
    assert injected == OUTPUT_RESERVE
    assert compute_threshold(W) + OUTPUT_RESERVE <= W


def test_responses_api_uses_max_output_tokens() -> None:
    body, injected = lp._inject_max_tokens(
        "/v1/responses",
        json.dumps({"model": "m", "input": "hi"}).encode(),
        window=W,
        est_prompt_tokens=100,
    )
    assert injected == OUTPUT_RESERVE
    sent = json.loads(body)
    assert sent["max_output_tokens"] == OUTPUT_RESERVE
    assert "max_tokens" not in sent


def test_anthropic_messages_shape_uses_max_tokens() -> None:
    body, injected = lp._inject_max_tokens(
        "/v1/messages?beta=true",
        json.dumps({"model": "m", "messages": []}).encode(),
        window=W,
        est_prompt_tokens=100,
    )
    assert injected == OUTPUT_RESERVE
    assert json.loads(body)["max_tokens"] == OUTPUT_RESERVE


def test_non_completion_paths_and_non_json_are_untouched() -> None:
    for path, body in (
        ("/v1/models", b"{}"),
        ("/v1/messages/count_tokens", b'{"model":"m"}'),
        ("/v1/chat/completions", b"not json"),
        ("/v1/chat/completions", b"[1,2]"),
    ):
        out, injected = lp._inject_max_tokens(path, body, window=W, est_prompt_tokens=1)
        assert injected is None, path
        assert out == body, path


# ── through the shim: the wire body and the llm_calls row ─────────────────────


class _Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    captured: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        ln = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(ln) if ln else b""
        type(self).captured.append({"path": self.path, "body": body})
        payload = (
            b'{"id":"gen-1","model":"qwen/qwen3-coder-next","choices":[{"message":'
            b'{"content":"hi"},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":10,"completion_tokens":5,"cost":0.001}}'
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _through_shim(
    tmp_path: Any, request_body: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    _Upstream.captured = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        shim = LocalProxy(
            upstream_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            run_id="run-1",
            harness="mini_swe_agent",
            instance_id="sphinx-doc__sphinx-10614",
            attempt_number=1,
            usage=Usage(),
            output_dir=str(tmp_path),
            context_window_tokens=W,
        ).start()
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"http://127.0.0.1:{shim._bound_port}/v1/chat/completions",
                    json=request_body,
                )
                assert resp.status_code == 200, resp.text
        finally:
            shim.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5)
    lines = (tmp_path / "llm_calls.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    return json.loads(_Upstream.captured[0]["body"]), json.loads(lines[0])


def test_shim_puts_the_reserve_on_the_wire_and_records_it(tmp_path) -> None:
    sent, rec = _through_shim(
        tmp_path, {"model": "qwen3-coder-next-mini", "messages": [{"role": "user", "content": "x"}]}
    )
    assert sent["max_tokens"] == OUTPUT_RESERVE
    # The row tells the two sides apart: nothing requested, 16,384 injected.
    assert rec["max_tokens_requested"] is None
    assert rec["max_tokens_injected"] == OUTPUT_RESERVE


def test_shim_leaves_a_requested_cap_alone_and_records_none_injected(tmp_path) -> None:
    sent, rec = _through_shim(
        tmp_path,
        {"model": "qwen3-coder-next-mini", "messages": [], "max_tokens": 4096},
    )
    assert sent["max_tokens"] == 4096
    assert rec["max_tokens_requested"] == 4096
    assert rec["max_tokens_injected"] is None


# ── one number everywhere ─────────────────────────────────────────────────────


def test_run_config_reserve_is_the_shared_constant() -> None:
    """run_config.py carried a stale 32_768 copy; it must BE the shared constant."""
    assert DEFAULT_OUTPUT_RESERVE_TOKENS == OUTPUT_RESERVE == 16_384


def test_every_harness_alias_carries_the_reserve_as_its_deployment_default() -> None:
    """Belt-and-braces behind the shim: the db-model's litellm_params max_tokens
    (LiteLLM's router merges litellm_params UNDER the request kwargs — verified in
    the v1.99.1 image, router._acompletion — so it applies only when the caller
    sent none). rotate_model_key replaces litellm_params wholesale from this spec,
    so the next launch carries it. The judge alias is NOT a harness alias and
    keeps its real window."""
    harness_aliases = [a for a in ROTATABLE_MODELS if a != "judge-model"]
    # 5 families (laguna, qwen, deepseek, gpt-5-mini, minimax) x 5 harnesses
    assert len(harness_aliases) == 25
    for alias in harness_aliases:
        spec = ROTATABLE_MODELS[alias]
        assert spec.litellm_params["max_tokens"] == OUTPUT_RESERVE, alias
        assert spec.model_info["max_output_tokens"] == OUTPUT_RESERVE, alias
    judge = ROTATABLE_MODELS["judge-model"]
    assert "max_tokens" not in judge.litellm_params
    assert judge.model_info["max_output_tokens"] == 32_768
