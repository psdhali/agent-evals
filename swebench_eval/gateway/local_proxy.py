"""ADR-0019 — per-worker transparent streaming forwarder (Part-3 redesign).

Binds an async streaming proxy on ``127.0.0.1:0`` for the lifetime of one
``(run_id, instance_id, attempt_number)`` and forwards the subprocess harness's
base-URL calls upstream to the LiteLLM gateway.

Design (owner-agreed, mid-phase cost-plumbing review Part 3): the shim NEVER
reconstructs a stream.  It is a pure passthrough:

- Upstream: ``httpx.AsyncClient.stream()``; downstream: ``starlette.StreamingResponse``
  over the upstream raw chunks, forwarded immediately and unchanged.
- It keeps a bounded tail (~16 KB) of the raw response and, at END of stream,
  parses usage out of that tail only.  No SSE parser, no chunk reassembly.
- Exactly two additive request mutations (review P-4): §4 metadata, and
  ``stream_options: {include_usage: true}`` on OpenAI-shaped streaming requests
  (without it the stream carries no usage at all).
- ``Accept-Encoding: identity`` so the raw tail is never gzip-compressed.
- Cost is two-tier (F-2): API-reported ``cost`` wins; else priced locally from
  the shared ``pricing`` module (the Anthropic/Claude-Code path never carries
  gateway cost — F-1).

The shim observes; the wrapper (harness worker) kills on budget breach.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from swebench_eval.harnesses.base import Usage

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from swebench_eval.gateway.pacer import (
    KAPPA_INITIAL,
    Admission,
    Pacer,
    PacerTimeout,
    weighted_charge,
)
from swebench_eval.gateway.pricing import cost_for
from swebench_eval.harnesses.compaction import OUTPUT_RESERVE

logger = logging.getLogger(__name__)

# F1 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): no harness sends ``max_tokens``, and
# Parasail then reserves ITS default output allowance (131,072 for qwen) — so any prompt past
# 131K is rejected with ``400 "maximum context length is 262144 ... requested 131072 output
# tokens"`` long before the harness's compaction threshold (235,929) is reached. sphinx-10614
# crashed exactly there. The shim injects the shared compaction reserve when the field is
# absent: ``max_tokens = min(OUTPUT_RESERVE, W - est_prompt)`` keeps prompt + output <= W
# *exactly* where compaction hands over (threshold = W - OUTPUT_RESERVE), so there is no window
# in which compaction has not fired yet but the provider already rejects. 32,768 would leave one
# (229K-246K). Harness-agnostic, request-side only; a harness that sets its own cap is left
# verbatim (ADR-0019). The clamp never goes below this floor — a prompt so large that fewer
# tokens remain is past the threshold already, and the provider's own 400 is the right answer.
_MAX_TOKENS_INJECT_FLOOR = 1024
# Per-shape output-cap field names (the injection target when ALL of a path's names are absent).
_OUTPUT_CAP_FIELDS: dict[str, tuple[str, ...]] = {
    "/chat/completions": ("max_tokens", "max_completion_tokens"),
    "/v1/messages": ("max_tokens",),
    "/v1/responses": ("max_output_tokens",),
}

# §8b of BUILDER4-HARNESS-AUTOSCALER-EXACT-DESIGN-2026-09-01.md: shim-level pool-aware retry on
# a REAL provider overload — uniform across all five harnesses, safe because a 429 arrives as
# headers before any stream bytes reach the CLI. All waits share the pacer's total hold cap.
_MAX_OVERLOAD_RETRIES = 6
_OVERLOAD_BACKOFF_BASE_S = 30.0  # wait = min(base × attempt, 3 × base) × jitter
# 2026-09-04 (deepseek x mini run 01788550040741118596): OpenRouter answers an upstream
# provider failure with HTTP 200 — a choice whose message is a single space,
# ``finish_reason: "stop"`` but ``native_finish_reason: "error"`` and an embedded
# ``error {code: 502, "Upstream error from OpenInference: Service temporarily unavailable",
# metadata.error_type: "provider_unavailable"}`` (LiteLLM keeps it under the choice's
# ``provider_specific_fields``; the raw OpenRouter shape is ``choices[].error``). LiteLLM
# passed five instances three such "answers" in a row within ~2 s each; mini-swe-agent treats
# three consecutive no-action replies as a format failure and exits -> EMPTY_PATCH. Retried
# here like a provider 429, invisibly to the CLI, for NON-streaming completions only (a
# stream's bytes are already forwarded by the time its tail carries the marker — that case is
# recorded, never retried). Shorter backoff than the overload path: this is a blip, not
# capacity pressure, and it is NOT fed to the overload counter (the pool is not full).
_MAX_UPSTREAM_ERROR_RETRIES = 4
_UPSTREAM_ERROR_BACKOFF_BASE_S = 5.0  # wait = min(base × attempt, 6 × base) × jitter

# Bounded tail of the raw response kept for end-of-stream usage parsing.
# A bounded tail (not the whole body) because a streaming response can be
# unbounded; the usage block at the very end must survive while we keep memory
# use per-connection constant.
_TAIL_LIMIT = 16 * 1024

# Streaming tail limit (Finding 3, 2026-08-27): streaming NON-streaming delimiter.
# The original 16 KB tail was sized for typical completions and correctly widened
# for non-streaming bodies (4 MB below), but streaming calls stayed at 16 KB
# unconditionally. codex's Responses-API completions routinely run 500-611 KB —
# the terminal SSE event carries BOTH the full reasoning/output text AND the
# `usage` block, so the last 16 KB never reached the `usage` key and the call was
# recorded `usage_parse_failed` (~20% of codex's HTTP 200s) even though codex's
# own adapter parsed usage every turn (adapter counted 16% more input, 2x output).
# Widen streaming to a bound that reliably contains the terminal event (~611 KB
# worst case) at small, bounded, per-connection memory cost. Doesn't need to match
# the non-streaming 4 MB (that one holds a WHOLE body; this only needs the tail).
_STREAM_TAIL_LIMIT = 512 * 1024  # 512 KiB — covers the largest observed (611 KB) with headroom

# Bounded HEAD of the raw response kept for provider identity (M0-3 review fix):
# id/model/provider/finish_reason sit at the front of a non-streaming JSON body
# while usage is at the end.  On a long non-streaming call the 16 KB tail keeps
# tokens+cost but evicts the identity — scan identity on head + tail.
_HEAD_LIMIT = 8 * 1024

# Header fields the shim must not forward (hop-by-hop / re-framed by httpx).
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "accept-encoding",
        "content-length",
    }
)

_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


class LocalProxy:
    """A per-worker transparent forwarder to the gateway.

    Usage (in the harness worker, Commit 7):
        shim = LocalProxy(upstream_url, run_id, harness, instance_id, attempt)
        shim.start()   # binds 127.0.0.1:<port> (async server in a thread)
        env[...] = shim.local_base_url
        ... run the subprocess harness ...
        shim.stop()
        shim.usage.input_tokens / cost_usd  → feeds the budget watchdog
    """

    def __init__(
        self,
        upstream_url: str,
        run_id: str,
        harness: str,
        instance_id: str,
        attempt_number: int,
        usage: Any,  # a swebench_eval.harnesses.base.Usage to accumulate into
        output_dir: str | None = None,
        # R3 (builder1-REMAINING-WORK-single-handover): the per-instance budget
        # ceilings the shim enforces by refusing further calls.  None = no
        # ceiling for that dimension (the worker always passes the job's values).
        max_tokens_per_instance: int | None = None,
        max_cost_usd_per_instance: float | None = None,
        # master-handover 3.12: the per-instance TURN cap, enforced at the shim
        # so all five harnesses share the same limit.  None = unlimited (same
        # convention as max_tokens_per_instance).
        max_turns_per_instance: int | None = None,
        # master-handover 3.4: an optional per-turn callback for the worker to
        # write LIVE progress to Redis during the run (the ADR-0018 / ADR-0037 §5
        # channel has never carried live data — write_progress fired once at the
        # end with turn_number=0).  Fired on each serviced turn.  Best-effort:
        # a progress write must never take down the proxy.
        on_turn: Callable[[int, Usage], None] | None = None,
        # L1 pacer (exact-design §4): global call-start admission via the shared Redis ledger.
        # None (the default) resolves from EVAL_PACER_ENABLED — on unless explicitly "0"
        # (tests/conftest.py pins it off for unit tests; deployed workers leave it unset = on).
        pacer: Pacer | None = None,
        # F1 (2026-09-04): the resolved per-model context window (the job's
        # context_window_tokens — the dispatcher resolves it once per run).  Used
        # ONLY to clamp the injected max_tokens so prompt + output never exceeds
        # W; None = no clamp (the bare OUTPUT_RESERVE is injected).
        context_window_tokens: int | None = None,
    ) -> None:
        self._context_window = context_window_tokens
        # F3 (2026-09-04): the previous completed call's REAL prompt size — the agent-loop
        # prefix the next call is expected to hit in the provider's cache. The pacer charges
        # that part at cached_weight; the first call of a session charges in full.
        self._prior_prompt_tokens = 0
        if pacer is not None:
            self._pacer: Pacer | None = pacer
        elif os.environ.get("EVAL_PACER_ENABLED", "1") != "0":
            self._pacer = Pacer()
        else:
            self._pacer = None
        # F2 (exact-design review): the hold cap MUST sit below the CLI's own HTTP read timeout,
        # or the pacer out-waits its own client and the call dies as an unexplained client-side
        # timeout — an instance failure invisible to the telemetry built to explain failures.
        # Per-harness, from what is actually verified:
        #   custom_minimal 100s — MEASURED: its per-call read timeout clamps to [30,120]s
        #     (harness.py _per_call_read_timeout), 120s at the default run config.
        #   mini_swe_agent 240s — litellm SDK default read timeout is 600s (high confidence,
        #     not binary-verified).
        #   claude_code 100s — VERIFIED 2026-09-01 from the shipped binary (2.1.234, harness
        #     image 4.1.0-*-hw), then RE-CHARACTERISED after the owner's review finding (the
        #     first pass established the smallest bound's VALUE but not its KIND, and the
        #     "floor - 60s" rule only makes sense for idle bounds): the 300s bound's one call
        #     site is the NON-STREAMING fallback path (minified Hym, isNonStreamingRequest,
        #     telemetry cli_nonstreaming_fallback_error), where it is passed as the Anthropic
        #     SDK per-attempt `timeout` on messages.create — for a non-streaming create that
        #     is a TOTAL bound covering hold + the entire completion, and it does NOT reset on
        #     first byte. Each fallback attempt is a fresh request through this shim, so it
        #     takes its own hold. The two 300s idle timers (byte-stream watchdog, stream idle)
        #     reset on bytes and never bind during a hold; the streaming path's 600s SDK
        #     timeout is irrelevant at this cap either way. Cap 100s leaves 200s for a full
        #     non-streaming completion under the 300s total bound — the same headroom logic
        #     that gave custom_minimal (also a total-ish bound) its 100.
        #   codex 240s — VERIFIED 2026-09-01, sound by construction: the LLM path's only
        #     bound is stream_idle_timeout_ms, DEFAULT_STREAM_IDLE_TIMEOUT_MS = 300_000
        #     (codex-rs model-provider-info lib.rs; the shipped 0.147.0 binary carries the
        #     same config key, our adapter sets no override) — an IDLE bound by name and
        #     definition ("wait for activity on a streaming response"), so 300s must elapse
        #     with no bytes at all; the hold is the only silent stretch. 240s cap stands.
        #   opencode 240s — MEASURED 2026-09-01 (the stub-server experiment the wiring review
        #     specified): opencode 1.18.18, driven through the CLI itself with the adapter's
        #     exact config, held 716+ seconds of TOTAL SILENCE (no status line, no headers —
        #     the shim's hold modelled exactly) on both of its connections and never gave up;
        #     it died only to the experiment's own 720s wall-clock kill. No client-side bound
        #     exists, so no kind-test applies and the cap cannot out-wait the client. 240 is
        #     NOT "unbounded therefore anything": it is the bounded value chosen on the review's
        #     stated other grounds — parity with codex/mini so Run B's five-way comparison
        #     isn't skewed by our own constant (a 45s cap would cost opencode admissions the
        #     others get), while a hold still burns wall-clock against the harness timeout.
        # EVAL_PACER_HOLD_CAP_S, when set, CLAMPS further (min with the table) — it can tighten,
        # never loosen past a verified bound.
        _hold_caps = {
            "custom_minimal": 100.0,
            "mini_swe_agent": 240.0,
            "claude_code": 100.0,
            "codex": 240.0,
            "opencode": 240.0,
        }
        table_cap = _hold_caps.get(harness, 45.0)
        env_cap = os.environ.get("EVAL_PACER_HOLD_CAP_S", "")
        self._pacer_hold_cap_s = min(table_cap, float(env_cap)) if env_cap else table_cap
        self._upstream = upstream_url.rstrip("/")
        # Split into (origin host, port, base path).  The base path (e.g. /v1) is
        # exposed on local_base_url so the shim looks to harnesses exactly like
        # gateway_base_url(); forwarded paths are sent VERBATIM to the origin.
        self._host, self._port, self._base = _split_url(self._upstream)
        self._metadata = {
            "run_id": run_id,
            "harness": harness,
            "instance_id": instance_id,
            "attempt_number": attempt_number,
        }
        self._usage = usage
        # R3: per-instance budget ceilings the shim ENFORCES (refuses further
        # calls with 400 since 3.8), not just meters.  None = no ceiling for dim.
        self._max_tokens = max_tokens_per_instance
        self._max_cost = max_cost_usd_per_instance
        # master-handover 3.12: the turn cap.  A SEPARATE counter from
        # calls_made/_call_index — that one deliberately INCLUDES 429/400
        # refusals (the "only authoritative count of whether a run reached the
        # model"); a budget kill's refusals must not inflate the turn count.
        # Incremented ONLY on a serviced (non-refused) call.
        self._max_turns = max_turns_per_instance
        self._turns_used = 0
        self._on_turn = on_turn
        # True once any call has been refused (R3 / 3.12) — the worker reads it
        # to distinguish a budget/turn-killed run from a genuine crash.
        self.budget_refused = False

        # ADR-0034 M1.11: set when the upstream (gateway) answers a paused ALB
        # with the stable framework_paused 503 marker.  The harness must surface
        # PAUSED_BY_OPERATOR, never a data-corrupting MODEL_API_ERROR.
        # ``_paused_503_pending`` is True from the moment a 503 status arrives
        # until the body head has been inspected for the marker (see _proxy).
        self.paused = False
        self._paused_503_pending = False
        # BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §6: the second
        # recognized pause marker — a blocked LiteLLM key's stable 401.  Same
        # shape as the 503 case (a pending flag set on status, resolved by the
        # body-head scan in the stream's finally) and sets the SAME `self.paused`
        # field, so harness_worker.py:658's classification needs no change at
        # all — only the detection gains a second marker.
        self._blocked_401_pending = False

        # M0 §2.5: one JSON line per model call, appended to llm_calls.jsonl in
        # the job output dir.  ``output_dir`` is None only in tests / when a
        # caller doesn't want records (then recording is a no-op).  call_index is
        # monotonic per shim (== per (run, instance, attempt) — the shim lives
        # exactly one of those).  A lock because the recorder appends from the
        # async worker path (uvicorn's loop thread) while stop() may join.
        self._output_dir = output_dir
        self._call_index = 0
        self._recorder_lock = threading.Lock()

        self._app = self._build_app()
        self._server: uvicorn.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._bound_port: int | None = None

    # ------------------------------------------------------------------
    # M0 per-call recorder (ADR-0037 / M0 §2.5)
    # ------------------------------------------------------------------

    def _calls_path(self) -> Path:
        # _record_call guards on output_dir; the `or "."` only satisfies mypy's
        # inability to narrow an attribute across methods.
        return Path(self._output_dir or ".") / "llm_calls.jsonl"

    def _record_call(self, call: dict[str, Any]) -> None:
        """Append one JSON line for a completed call to llm_calls.jsonl.

        Best-effort: a write failure must never take down the proxy (this is an
        analytical artifact, not the live budget meter — Redis carries that).
        """
        if not self._output_dir:
            return
        try:
            with self._recorder_lock, self._calls_path().open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(call) + "\n")
        except OSError:
            logger.warning("shim: cannot append llm_calls record", exc_info=True)

    def _begin_call(self, path: str, model: str, body: bytes) -> dict[str, Any]:
        """Seed the per-call record with the request-shape fields.
        ``model`` is the REQUEST alias (the harness asked for it); model_resolved
        (what the provider served) is read from the response in _accumulate_tail.
        """
        self._call_index += 1
        call = dict(self._metadata)
        call.update(
            call_index=self._call_index,
            model_requested=model,
            path=path,
            started_at=_now_iso(),
            request_bytes=len(body),
        )
        # Request shape from the body — prove the Z3 max_tokens ceiling per call,
        # whether the run is deterministic (temperature), and the tools presence.
        o: dict[str, Any] = {}
        try:
            obj = json.loads(body) if body else {}
            if isinstance(obj, dict):
                o["stream"] = bool(obj.get("stream"))
                o["max_tokens_requested"] = obj.get("max_tokens")
                o["temperature"] = obj.get("temperature")
                msgs = obj.get("messages")
                o["n_messages"] = len(msgs) if isinstance(msgs, list) else None
                o["has_tools"] = bool(obj.get("tools"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        call.update(o)
        return call

    @property
    def calls_made(self) -> int:
        """R5.3: call count the shim has serviced (incl. 429 refusals).

        The ONLY authoritative count of whether a run ever reached the model —
        the adapter's stdout may be empty (opencode returned ~235 bytes with no
        llm_calls.jsonl at all) while the shim is the one component that sees
        every request.  Zero calls made == the harness never talked to the
        gateway == infrastructure failure, never a clean finish.
        """
        return self._call_index

    @property
    def _breached(self) -> bool:
        """R3: has the accumulated (sole-meter) usage crossed either ceiling?

        Checked BEFORE every forwarded call; ``None`` ceilings never trip.  Uses
        the same comparison the worker's post-hoc backstop performs so the two
        agree — a boundary the validation gate asserts directly.

        N3 (review): this check is per-CALL, so N concurrent in-flight calls all
        pass it before any of them accumulates — overshoot is bounded by
        CONCURRENCY, not one call.  Fine for the sequential CLIs this shim
        meters (codex/opencode/claude/mini run one call at a time); a concurrent
        harness would widen the bound and would need this revisited.
        """
        u = self._usage
        token_breached = (
            self._max_tokens is not None and (u.input_tokens + u.output_tokens) >= self._max_tokens
        )
        cost_breached = self._max_cost is not None and u.cost_usd >= self._max_cost
        turn_breached = self._max_turns is not None and self._turns_used >= self._max_turns
        return token_breached or cost_breached or turn_breached

    @property
    def turns_breached(self) -> bool:
        """R3/3.12: True only when the TURN cap (not budget) is the blocker."""
        return self._max_turns is not None and self._turns_used >= self._max_turns

    @property
    def turns_used(self) -> int:
        """M7 (review 2026-08-25): serviced completion turns so far.

        Public so the worker's end-of-run Redis progress write can record the
        LIVE turn count instead of erasing it with turn_number=0 — an instance
        that finished then read as one that never started.
        """
        return self._turns_used

    @property
    def local_base_url(self) -> str:
        """The base URL the harness should point its API calls at.

        Mirrors the upstream base path (/v1) so adapters written to consume
        ``gateway_base_url()`` behave identically when pointed at the shim.
        """
        assert self._bound_port is not None, "shim not started"
        return f"http://127.0.0.1:{self._bound_port}{self._base}"

    def start(self) -> LocalProxy:
        """Bind the proxy on 127.0.0.1:<random port> and serve in a thread."""
        self._thread = threading.Thread(target=self._serve, daemon=True, name="local-proxy")
        self._thread.start()
        # Wait for uvicorn to bind a port (server.started = True post-startup).
        while True:
            if not self._thread.is_alive():
                raise RuntimeError("shim server thread died before binding")
            if self._server is not None and self._server.started:
                break
            time.sleep(0.01)
        self._bound_port = self._server.servers[0].sockets[0].getsockname()[1]
        return self

    def stop(self) -> None:
        if self._server is not None:
            # uvicorn's serve loop checks should_exit (a plain bool) each pass.
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------
    # Server plumbing (starlette app served by uvicorn in a thread)
    # ------------------------------------------------------------------

    def _build_app(self) -> Starlette:

        async def handle(request: Request) -> Response:
            return await self._proxy(request)

        return Starlette(routes=[Route("/{path:path}", handle, methods=_ALL_METHODS)])

    def _serve(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        config = uvicorn.Config(
            self._app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
        )
        self._server = uvicorn.Server(config)
        self._loop.run_until_complete(self._server.serve())

    # ------------------------------------------------------------------
    # Request handler — pure passthrough with a tail
    # ------------------------------------------------------------------

    async def _proxy(self, request: Request) -> Response:
        # STEP 3 (review 2026-08-26): the shim's OWN pre-flight (body read +
        # budget/turn-cap check + stream_options mutation) is currently
        # UNMEASURED — `started` (the upstream clock) is set after it.  Capture
        # it so the head-to-head comparison can separate shim overhead from the
        # model.  shim_preflight_ms = started - preflight_at.
        preflight_at = time.monotonic()
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query

        body = await request.body()
        model = _extract_model(body)

        # R3 (builder1-REMAINING-WORK-single-handover): the shim is the KILLER,
        # not just the meter.  Once the accumulated usage has crossed either
        # ceiling, refuse every further call — the run stops near the ceiling
        # instead of burning 28x past it (the mini_swe_agent defect).
        # master-handover 3.8 (2026-08-24): the refusal status is 400, not 429.
        # Measured on the first graded run: 30 of 67 mini calls were refusals —
        # the SDK retries 429 (mini retried 30×), burning budget against a
        # refusal.  400 is terminal for every SDK/CLI.  Overshoot is bounded to
        # one call, the same shape as custom_minimal's in-loop check.  The
        # refusal is itself a recorded call (Trap 1 — every call, including
        # refusals).  The worker's post-hoc check stays as the backstop for a
        # CLI that ignores the error.  The 3.12 turn-cap refusal uses the same
        # status + separate marker.
        if self._breached:
            self.budget_refused = True
            refused = self._begin_call(path, model, body)
            # master-handover 3.8/3.12: 400 (terminal for every SDK/CLI).  The
            # turn-cap refusal carries its OWN marker + type (max_turns_exceeded,
            # already a TerminatedReason → HARNESS_MAX_TURNS_EXCEEDED) so a
            # turn-killed run isn't misread as a budget kill.
            if self.turns_breached:
                refused.update(
                    http_status=400,
                    error_type="max_turns_exceeded",
                    error_code="per-instance turn cap reached; refusing further calls",
                    latency_ms=0,
                    response_bytes=0,
                )
                self._record_call(refused)
                return Response(
                    status_code=400,
                    headers={"x-eval-turns-exceeded": "1"},
                    content=json.dumps(
                        {
                            "error": {
                                "type": "max_turns_exceeded",
                                "message": f"{self._max_turns}-turn cap reached",
                            }
                        }
                    ).encode(),
                )
            refused.update(
                http_status=400,
                error_type="budget_exceeded",
                error_code="per-instance budget ceiling crossed; refusing further calls",
                latency_ms=0,
                response_bytes=0,
            )
            self._record_call(refused)
            return Response(
                status_code=400,
                headers={"x-eval-budget-exceeded": "1"},
                content=json.dumps(
                    {
                        "error": {
                            "type": "budget_exceeded",
                            "message": (
                                f"per-instance budget ceiling crossed "
                                f"({self._usage.input_tokens + self._usage.output_tokens} tokens / "
                                f"${self._usage.cost_usd:.4f})"
                            ),
                        }
                    }
                ).encode(),
            )

        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        headers["accept-encoding"] = "identity"
        # §4 request metadata so attribution is gateway-side per call.
        headers["x-eval-run-id"] = str(self._metadata["run_id"])
        headers["x-eval-harness"] = str(self._metadata["harness"])
        headers["x-eval-instance-id"] = str(self._metadata["instance_id"])
        headers["x-eval-attempt"] = str(self._metadata["attempt_number"])
        # LiteLLM ≥1.99 stopped storing raw request headers in its spend rows
        # (proxy_server_request became just the request body — header capture
        # went enterprise-only), which silently severed the x-eval-* tie-back
        # the live LLM view reads.  This header is LiteLLM's sanctioned channel:
        # its JSON value lands verbatim in metadata->spend_logs_metadata on
        # every spend row.  Verified present in BOTH 1.96.0 and 1.99.1
        # (litellm_pre_call_utils._get_spend_logs_metadata_from_request_headers),
        # so it is safe regardless of which gateway image is deployed.
        headers["x-litellm-spend-logs-metadata"] = json.dumps(
            {
                "eval_run_id": str(self._metadata["run_id"]),
                "eval_harness": str(self._metadata["harness"]),
                "eval_instance_id": str(self._metadata["instance_id"]),
                "eval_attempt": str(self._metadata["attempt_number"]),
            }
        )

        # Additive request mutation (P-4): make OpenAI-shaped streaming requests
        # carry usage, or the stream has nothing to parse.
        content = _inject_stream_options(path, body)
        # Claude Code's mid-list `role: system` notices would be dropped by LiteLLM's
        # Anthropic transform together with the cache breakpoint they carry (2026-09-06:
        # 7% cache hit vs 98% on the OpenAI-shaped harnesses).  Re-role them as user text.
        content, rehomed = _rehome_system_role_messages(path, content)
        # F1: one prompt-size estimate per call, shared by the max_tokens clamp below and
        # the pacer's admission draw — the same chars/token κ so the two agree.
        est_prompt_tokens = 0
        max_tokens_injected: int | None = None
        if _is_completion_path(path):
            if self._pacer is not None:
                est_prompt_tokens = await self._pacer.estimate_tokens(model, len(body))
            else:
                est_prompt_tokens = max(1, int(len(body) / KAPPA_INITIAL))
            content, max_tokens_injected = _inject_max_tokens(
                path,
                content,
                window=self._context_window,
                est_prompt_tokens=est_prompt_tokens,
            )

        upstream_url = f"http://{self._host}:{self._port}{path}"
        tail: deque[bytes] = deque()
        usage = self._usage
        started = time.monotonic()
        # STEP 3: the shim's pre-flight (body read + budget/turn check + request
        # mutation) duration — set before the per-call record is finalised.
        shim_preflight_ms = _elapsed_ms(started, preflight_at)

        # M0 §2.5: one record per model call, EVERY call — including 429/5xx/
        # timeouts/connection failures (M0 §0.6 Trap 1).  The response must be
        # recorded on a handler-wide path, not only in the stream-iterator's
        # finally (which is skipped when `client.send` raises below, leaving the
        # shim blind to exactly the failures this design exists to record).  The
        # record is seeded here and finalized either in the upstream-error branch
        # or in the stream's finally — every path writes exactly once.
        call = self._begin_call(path, model, body)
        call["shim_preflight_ms"] = shim_preflight_ms
        # F1: beside max_tokens_requested (what the harness sent — None when absent) the
        # value the shim put on the wire, so a provider 400 can be tied to either side.
        call["max_tokens_injected"] = max_tokens_injected
        if rehomed:
            call["system_role_rehomed"] = rehomed
        # NICE (review 2026-08-25): only a COMPLETION request consumes a turn.
        # The old increment sat on the generic /{path:path} handler, so any
        # forwarded request counted — e.g. opencode's own reachability probe
        # (GET /v1/models against the shim) started every run at turn 1 with a
        # bogus row.  Gate on the completion paths; non-completions (probes,
        # model lists, health) still get a call record but never a turn.
        if _is_completion_path(path):
            # master-handover 3.12: a serviced (forwarded) completion request ==
            # ONE turn.  This line is reached only for forwarded calls — the
            # budget/turn refusal returns above — so refusals never count here
            # (they stay in _call_index, the "did it reach the model" counter).
            # The shim's turn cap therefore agrees with claude_code's --max-turns
            # and custom_minimal's max_turns (each = one model call).
            self._turns_used += 1
            # master-handover 3.4: fire the per-turn progress callback
            # best-effort.  The worker uses it to write LIVE Redis progress (turn
            # number + the running totals the shim accumulates).  A failure here
            # (or a slow callback) must never take down the proxy — guard +
            # fire-and-forget.
            if self._on_turn is not None:
                try:
                    self._on_turn(self._turns_used, self._usage)
                except Exception:
                    logger.warning("shim: on_turn progress callback failed", exc_info=True)
        response_bytes = 0
        first_chunk_at: float | None = None

        def _publish_pacing() -> None:
            """Re-fire the per-turn progress callback after a pacer event (admission,
            hold-cap timeout, §8b retry) so the LIVE view sees the hold as it happens —
            the regular on_turn above fires BEFORE admission, so without this a call
            stuck at the pacer is invisible until its next turn. Best-effort, same
            guard as the per-turn callback."""
            if self._on_turn is not None:
                try:
                    self._on_turn(self._turns_used, self._usage)
                except Exception:
                    logger.debug("shim: pacing progress callback failed", exc_info=True)

        # L1 pacer (exact-design §4): admission BEFORE any bytes go upstream. Only completion
        # paths — probes/model lists are not model calls and must not consume arrival budget.
        # A waiting call reserves nothing; budget is debited at the moment of admission. The
        # wait is recorded separately (paced_wait_ms) and excluded from upstream latency_ms.
        admission = None
        pacer_deadline = time.monotonic() + self._pacer_hold_cap_s
        if self._pacer is not None and _is_completion_path(path):
            est = est_prompt_tokens
            # F3: uncached at full price, the expected cached prefix at cached_weight.
            weight = 1.0
            _get_weight = getattr(self._pacer, "get_cached_weight", None)
            if callable(_get_weight):
                try:
                    weight = float(await _get_weight(model))
                except Exception:  # noqa: BLE001 — an unreadable weight is full price
                    weight = 1.0
            charge = weighted_charge(est, min(self._prior_prompt_tokens, est), weight)
            call["pacer_charge_tok"] = charge
            try:
                admission = await self._pacer.acquire(
                    model, est, hold_cap_s=self._pacer_hold_cap_s, charge_tokens=charge
                )
            except PacerTimeout as exc:
                # Surface the pressure honestly — a 429 the CLI's own retry machinery
                # understands — rather than out-waiting its HTTP timeout (§4 hold cap).
                call.update(
                    http_status=429,
                    error_type="pacer_hold_cap_exceeded",
                    error_code=str(exc),
                    latency_ms=0,
                    response_bytes=0,
                )
                call["paced_wait_ms"] = round(self._pacer_hold_cap_s * 1000)
                # Fold the timeout into the instance's live/durable pacer footprint.
                self._usage.pacer_timeouts += 1
                self._usage.paced_wait_ms_total += round(self._pacer_hold_cap_s * 1000)
                self._usage.paced_calls += 1
                _publish_pacing()
                self._record_call(call)
                return Response(
                    status_code=429,
                    headers={"retry-after": "30", "x-eval-paced": "1"},
                    content=json.dumps(
                        {"error": {"type": "pacer_hold_cap_exceeded", "message": str(exc)}}
                    ).encode(),
                )
            call["paced_wait_ms"] = admission.paced_wait_ms
            _apply_pacer_diagnostics(call, admission)
            if _fold_admission_into_usage(self._usage, admission):
                _publish_pacing()  # only when there was a wait worth showing
        # Review F7 (2026-09-03): has this call already been counted in Usage.paced_calls?
        # (_fold_admission_into_usage counts a queued admission; a later re-acquire timeout
        # must count the call once, not twice.)
        counted_paced = admission is not None and admission.was_queued
        # Upstream latency must not include pacer waiting — reset the clock after admission
        # (body_stream and the finalize paths close over this reassigned value).
        started = time.monotonic()

        client = httpx.AsyncClient(timeout=300.0)
        overload_retries = 0
        # BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.6 — the complete wall-clock
        # decomposition of a retried call. `started` is reset per attempt so latency_ms is the
        # FINAL attempt's upstream time; everything a retry costs on top is accumulated here:
        #   paced_wait_ms       Σ every admission wait (initial + each re-acquire)
        #   overload_backoff_ms Σ the §8b backoff sleeps
        #   retry_upstream_ms   Σ round-trip time of the retried (429) attempts
        # wall ≡ preflight + paced_wait + retry_upstream + backoff + latency.
        paced_wait_total_ms = admission.paced_wait_ms if admission is not None else 0
        overload_backoff_ms = 0
        retry_upstream_ms = 0
        upstream_error_retries = 0
        # A NON-streaming completion body read in full before any byte reaches the CLI, so an
        # embedded upstream error can be retried invisibly (see _MAX_UPSTREAM_ERROR_RETRIES);
        # None when the response is streamed straight through.
        prefetched: bytes | None = None

        async def _release_backoff_reacquire(
            backoff: float, exhausted_type: str
        ) -> Response | None:
            """The shared retry step: release the reservation (a rejected call is not in
            flight), sleep *backoff*, re-acquire admission (a re-send is a new arrival). Returns
            a 429 Response when the re-acquire times out — the caller returns it — else None."""
            nonlocal admission, paced_wait_total_ms, counted_paced, overload_backoff_ms
            if admission is not None and self._pacer is not None:
                await self._pacer.release(model, admission)
            await asyncio.sleep(backoff)
            overload_backoff_ms += round(backoff * 1000)
            if admission is None or self._pacer is None:
                return None
            reacquire_at = time.monotonic()
            try:
                admission = await self._pacer.acquire(
                    model,
                    admission.est_tokens,
                    hold_cap_s=max(0.1, pacer_deadline - time.monotonic()),
                    charge_tokens=admission.charge_tokens or None,
                )
            except PacerTimeout:
                reacquire_wait_ms = _elapsed_ms(time.monotonic(), reacquire_at)
                paced_wait_total_ms += reacquire_wait_ms
                # Review F7: this timeout was missing from the instance rollups
                # (pacer_timeouts stayed 0 for an instance whose calls died at the
                # pacer AFTER a 429 retry) and the live view never saw the terminal
                # state. Same three-step fold as the first-acquire timeout.
                self._usage.pacer_timeouts += 1
                self._usage.paced_wait_ms_total += reacquire_wait_ms
                if not counted_paced:
                    self._usage.paced_calls += 1
                    counted_paced = True
                _publish_pacing()
                call.update(
                    http_status=429,
                    error_type=exhausted_type,
                    error_code=(
                        f"still failing after {overload_retries + upstream_error_retries} "
                        f"retries within the {self._pacer_hold_cap_s:.0f}s hold cap"
                    ),
                    # No final upstream attempt happened after the failed
                    # re-acquire — 0, not a mixture of backoff + waits + a 429.
                    latency_ms=0,
                    response_bytes=0,
                    paced_wait_ms=paced_wait_total_ms,
                    overload_backoff_ms=overload_backoff_ms,
                    retry_upstream_ms=retry_upstream_ms,
                )
                self._record_call(call)
                await client.aclose()
                return Response(
                    status_code=429,
                    headers={"retry-after": "60", "x-eval-paced": "1"},
                    content=json.dumps(
                        {
                            "error": {
                                "type": exhausted_type,
                                "message": "provider overloaded; retries exhausted",
                            }
                        }
                    ).encode(),
                )
            paced_wait_total_ms += admission.paced_wait_ms
            _apply_pacer_diagnostics(call, admission, accumulate=True)
            _fold_admission_into_usage(self._usage, admission)
            counted_paced = counted_paced or admission.was_queued
            _publish_pacing()  # a retry is always worth showing live
            return None

        while True:
            try:
                upstream = await client.send(
                    httpx.Request(
                        request.method,
                        upstream_url,
                        headers=headers,
                        content=content,
                    ),
                    stream=True,
                )
            except httpx.HTTPError as exc:
                logger.warning("shim upstream error %s -> %s", path, exc)
                call.update(
                    http_status=502,
                    error_type=type(exc).__name__,
                    error_code=str(exc),
                    latency_ms=0,
                    response_bytes=0,
                )
                self._record_call(call)
                await client.aclose()
                if admission is not None and self._pacer is not None:
                    await self._pacer.release(model, admission)
                return Response(status_code=502)

            # §8b shim overload retry: a REAL provider 429 (no retry-after — the measured
            # discriminator, §2.1) is retried HERE, invisibly to the CLI (no stream bytes have
            # been forwarded yet), within the same total hold cap. The reservation is released
            # during the wait (a rejected call is not in flight) and re-acquired before the
            # re-send — a re-send is a new arrival and must re-pass admission.
            if admission is not None and self._pacer is not None and upstream.status_code == 429:
                from swebench_eval.harnesses.routing import is_real_provider_overload

                _ra = upstream.headers.get("retry-after")
                try:
                    _ra_s: float | None = float(_ra) if _ra is not None else None
                except (TypeError, ValueError):
                    _ra_s = None
                if is_real_provider_overload(429, _ra_s):
                    backoff = min(
                        _OVERLOAD_BACKOFF_BASE_S * (overload_retries + 1),
                        3 * _OVERLOAD_BACKOFF_BASE_S,
                    ) * random.uniform(0.75, 1.25)
                    if (
                        overload_retries < _MAX_OVERLOAD_RETRIES
                        and time.monotonic() + backoff < pacer_deadline
                    ):
                        # §2.3 counter for the retried observation; a final (non-retried) 429
                        # falls through to the stream path, which records it exactly once there.
                        await self._pacer.record_overload(model)
                        overload_retries += 1
                        call["overload_retries"] = overload_retries
                        self._usage.overload_retries_total += 1
                        # This attempt's round trip (to headers) — previously lost entirely.
                        retry_upstream_ms += _elapsed_ms(time.monotonic(), started)
                        await upstream.aclose()
                        timed_out = await _release_backoff_reacquire(
                            backoff, "engine_overloaded_retries_exhausted"
                        )
                        if timed_out is not None:
                            return timed_out
                        started = time.monotonic()
                        continue

            # Embedded upstream error in a 200 (see _MAX_UPSTREAM_ERROR_RETRIES): only a
            # NON-streaming completion can be retried invisibly — its body is read in full
            # here, before the CLI has seen a byte. The final attempt's body (retried or not)
            # is what the CLI receives, so a retry that never recovers still hands the harness
            # exactly what the gateway said, with error_type on the record.
            if upstream.status_code == 200 and _is_completion_path(path) and not call.get("stream"):
                prefetched = await upstream.aread()
                embedded = _embedded_upstream_error(prefetched)
                if embedded is not None:
                    backoff = min(
                        _UPSTREAM_ERROR_BACKOFF_BASE_S * (upstream_error_retries + 1),
                        6 * _UPSTREAM_ERROR_BACKOFF_BASE_S,
                    ) * random.uniform(0.75, 1.25)
                    if (
                        upstream_error_retries < _MAX_UPSTREAM_ERROR_RETRIES
                        and time.monotonic() + backoff < pacer_deadline
                    ):
                        upstream_error_retries += 1
                        call["upstream_error_retries"] = upstream_error_retries
                        retry_upstream_ms += _elapsed_ms(time.monotonic(), started)
                        logger.warning(
                            "shim: upstream error embedded in a 200 (%s) on %s — retry %d/%d "
                            "after %.1fs",
                            embedded,
                            model,
                            upstream_error_retries,
                            _MAX_UPSTREAM_ERROR_RETRIES,
                            backoff,
                        )
                        await upstream.aclose()
                        timed_out = await _release_backoff_reacquire(
                            backoff, "upstream_error_retries_exhausted"
                        )
                        if timed_out is not None:
                            return timed_out
                        prefetched = None
                        started = time.monotonic()
                        continue
                    call["error_type"] = "upstream_error_embedded"
                    call["error_code"] = embedded
                    logger.warning(
                        "shim: upstream error embedded in a 200 (%s) on %s — retries exhausted "
                        "(%d), forwarding the gateway's body as-is",
                        embedded,
                        model,
                        upstream_error_retries,
                    )
            break
        if overload_retries > 0 or upstream_error_retries > 0:
            # Same convention as `overload_retries` itself: present only on a retried call.
            call["paced_wait_ms"] = paced_wait_total_ms
            call["overload_backoff_ms"] = overload_backoff_ms
            call["retry_upstream_ms"] = retry_upstream_ms

        response_headers = [
            (k, v) for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP
        ]

        # ADR-0034 M1.11: a paused gateway answers with the stable
        # framework_paused marker.  The shim must pass it through UNMANAGLED so
        # the harness (which talks to the shim, not the ALB) can classify it —
        # a shim that wraps the 503 as a generic 5xx destroys the distinction
        # and converts healthy instances into recorded MODEL_API_ERROR failures.
        #
        # Key on the MARKER in the body, not on the status code alone: a
        # genuinely wedged LiteLLM answering a bare 503 is NOT an operator
        # pause, and recording it as PAUSED_BY_OPERATOR is the same data
        # corruption M1.11 exists to prevent (run backwards).  The body head is
        # scanned in the stream finalize, where the first chunks are available.
        self._paused_503_pending = upstream.status_code == 503
        self._blocked_401_pending = upstream.status_code == 401
        self.paused = False

        # Rate-limit headers the shim is the only component that can read
        # (M0 §2.3).  rate_limit_scope (gateway vs provider) is finalized after
        # the body arrives, in the stream's finally.
        call["http_status"] = upstream.status_code
        _apply_rate_limit_headers(call, upstream.headers)
        # STEP 3: the third latency tier (LiteLLM's own timing headers).
        _apply_gateway_timing_headers(call, upstream.headers)

        async def body_stream() -> AsyncIterator[bytes]:
            nonlocal response_bytes, first_chunk_at
            # M0-3 (review fix): identity fields (id/model/provider/finish_reason)
            # sit at the FRONT of a non-streaming JSON body, while usage is
            # at the END.  A 16 KB tail keeps usage but evicts the identity — so a
            # long non-streaming call (custom_minimal never streams) would look
            # complete but carry NULL model_resolved/provider_name.  Keep a small
            # head buffer alongside the tail and scan identity on head + tail.
            #
            # F4 (2026-08-24, harness-05 D2): for NON-streaming responses the body
            # is finite (usually a few tens of KB max) AND usage lives in one JSON
            # at its very end.  A 16 KB tail truncated a big non-stream body before
            # the usage block (the run's 4 `usage_parse_failed` calls: 25-82 KB,
            # model/usage both NULL) — and the budget watchdog then summed 4 calls
            # as ~0 tokens, under-counting the true total.  Buffer the WHOLE body
            # when stream:false (bounded by the request already being in memory);
            # keep the 16 KB bounded tail for streams (which can be unbounded).
            tail_limit = (
                _STREAM_TAIL_LIMIT if call.get("stream") else max(_TAIL_LIMIT, 4 * 1024 * 1024)
            )
            head: list[bytes] = []
            head_len = 0

            async def _prefetched_chunks() -> AsyncIterator[bytes]:
                if prefetched:
                    yield prefetched

            source = _prefetched_chunks() if prefetched is not None else upstream.aiter_raw()
            try:
                async for chunk in source:
                    if head_len < _HEAD_LIMIT:
                        head.append(chunk)
                        head_len += len(chunk)
                    tail.append(chunk)
                    while sum(len(c) for c in tail) > tail_limit:
                        tail.popleft()
                    response_bytes += len(chunk)
                    if first_chunk_at is None:
                        # M0 §2.3 ttft_ms: the first yielded chunk separates
                        # "provider was slow" from "model wrote a lot".
                        first_chunk_at = time.monotonic()
                    yield chunk
            finally:
                call["response_bytes"] = response_bytes
                if first_chunk_at is not None:
                    call["ttft_ms"] = _elapsed_ms(first_chunk_at, started)
                call["latency_ms"] = _elapsed_ms(time.monotonic(), started)
                # STEP 3: stream_ms = time spent receiving the body
                # (latency − ttft).  NULL when no body chunk ever arrived.
                if call.get("ttft_ms") is not None:
                    call["stream_ms"] = call["latency_ms"] - call["ttft_ms"]
                # Tokens/cost/identity read from the bounded tail (P-2).  Token
                # fields stay NULL when nothing was reported (M0 §0.6 Trap 3) —
                # the record only ever stores what the tail actually carried.
                _tail = b"".join(tail)
                parsed = _accumulate_tail(
                    _tail,
                    usage,
                    model,
                    call,
                    head=b"".join(head),
                    headers=upstream.headers,
                )
                # M0-3 (review): distinguish "the provider reported no usage"
                # from "we could not find what it reported" — otherwise a
                # successful 200 with an unparseable body is byte-identical to a
                # 429 in the data.
                # A STREAMED completion whose tail carries OpenRouter's embedded upstream
                # error (native_finish_reason "error"): the bytes are already with the CLI,
                # so this is recorded, never retried (the non-stream path above retries).
                if (
                    upstream.status_code == 200
                    and call.get("native_finish_reason") == "error"
                    and not call.get("error_type")
                ):
                    call["error_type"] = "upstream_error_embedded"
                    call["error_code"] = "native_finish_reason_error"
                if parsed is None and upstream.status_code < 400 and response_bytes > 0:
                    call["usage_parse_failed"] = True
                    # METERING-COMPLETENESS: roll up the per-call flag into the
                    # cumulative Usage so the worker can record an instance-level
                    # `usage_parse_failed_calls` on instance_results — a
                    # completeness marker that a shorted token total wasn't
                    # recorded as if it were complete.
                    self._usage.usage_parse_failed_calls += 1
                if upstream.status_code == 429 and call.get("rate_limit_scope") is None:
                    # §2.1 of the autoscaler spec (live-validated 2026-09-01): LiteLLM's own
                    # self-imposed 429 ALWAYS carries retry-after; a genuine upstream/provider
                    # overload never does (0 of 2,651 historical + every captured live 429,
                    # both the upstream flavor and OpenRouter's 'High demand' surge-shed).
                    # This replaces the body-string heuristic (_classify_rate_limit), whose
                    # own docstring admitted it was never live-validated — it mislabeled
                    # wrapped provider throttles as 'gateway'.
                    call["rate_limit_scope"] = (
                        "gateway" if call.get("retry_after_s") is not None else "provider"
                    )
                    # §2.3: a real provider overload observed on the FINAL (non-retried)
                    # attempt also feeds the congestion counter (fire-and-forget).
                    if call["rate_limit_scope"] == "provider" and self._pacer is not None:
                        try:
                            await self._pacer.record_overload(model)
                        except Exception:
                            logger.debug("overload counter publish failed", exc_info=True)
                if self._paused_503_pending:
                    head_bytes = b"".join(head)
                    # The marker is checked against the body HEAD via the shared
                    # canonicaliser (harnesses/routing.py) — a 503 that carries
                    # the ALB's fixed-response JSON sets paused; a bare 503
                    # (wedged LiteLLM) does not.
                    from swebench_eval.harnesses.routing import is_framework_paused_response

                    self.paused = is_framework_paused_response(503, head_bytes)
                elif self._blocked_401_pending:
                    head_bytes = b"".join(head)
                    # Same discipline, second marker (§6 of the gateway-pause
                    # design): a 401 carrying LiteLLM's stable "Key is blocked"
                    # body is an operator pause; a genuinely bad/expired key
                    # (different body) is not.
                    from swebench_eval.harnesses.routing import is_operator_block_response

                    self.paused = is_operator_block_response(401, head_bytes)
                self._record_call(call)
                # L1 pacer: free the in-flight reservation on EVERY terminal path (this finally
                # runs on completion, client disconnect, and stream errors alike), folding the
                # real reported usage back into the κ estimate when the body carried it.
                _real_prompt = call.get("input_tokens")
                _real_cached = call.get("cached_tokens")
                if _real_prompt:
                    # F3: the prompt the NEXT call is expected to resend (the fitter's rule:
                    # Anthropic reports cache reads outside input_tokens, OpenAI inside).
                    _p, _c = int(_real_prompt), int(_real_cached or 0)
                    self._prior_prompt_tokens = _p + _c if _c > _p else _p
                if admission is not None and self._pacer is not None:
                    await self._pacer.release(
                        model,
                        admission,
                        real_prompt_tokens=int(_real_prompt) if _real_prompt else None,
                        request_chars=len(body),
                        real_cached_tokens=int(_real_cached) if _real_cached else None,
                    )
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            body_stream(),
            status_code=upstream.status_code,
            headers=dict(response_headers),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _embedded_upstream_error(body: bytes) -> str | None:
    """The upstream error OpenRouter embedded in a 200 completion body, as a short code, or
    None for a genuine completion.

    Real shape (2026-09-04, deepseek x mini): LiteLLM-normalised
    ``choices[0].provider_specific_fields.error = {code, message, metadata.error_type}`` with
    ``choices[0].provider_specific_fields.native_finish_reason == "error"``; the raw OpenRouter
    shape carries ``choices[0].error`` and ``choices[0].native_finish_reason``. Either marker
    counts; a body that does not parse, or has no choices, is not an embedded error (that is
    ``usage_parse_failed`` territory, handled elsewhere)."""
    try:
        obj = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    choices = obj.get("choices")
    if not isinstance(choices, list):
        return None
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        psf_raw = choice.get("provider_specific_fields")
        psf: dict[str, Any] = psf_raw if isinstance(psf_raw, dict) else {}
        err = choice.get("error") if isinstance(choice.get("error"), dict) else psf.get("error")
        native = choice.get("native_finish_reason") or psf.get("native_finish_reason")
        if isinstance(err, dict):
            meta_raw = err.get("metadata")
            meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
            code = meta.get("error_type") or err.get("code") or "upstream_error"
            return str(code)
        if native == "error":
            return "native_finish_reason_error"
    return None


def _split_url(url: str) -> tuple[str, int, str]:
    """Split ``http://host:port[/base]`` into (host, port, base)."""
    rest = url.replace("http://", "", 1).replace("https://", "", 1)
    if "/" in rest:
        hostport, base = rest.split("/", 1)
        base = "/" + base.rstrip("/")
    else:
        hostport, base = rest, ""
    if ":" in hostport:
        host, port_s = hostport.rsplit(":", 1)
        port = int(port_s)
    else:
        host, port = hostport, 80
    return host, port, base


def _extract_model(body: bytes) -> str:
    """Pull the ``model`` from a request body for local pricing; default alias."""
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            m = obj.get("model")
            if isinstance(m, str) and m:
                return m
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return "cheap-oss-model"


def _is_completion_path(path: str) -> bool:
    """True for a model-completion request path (a turn); false for anything else.

    Covers OpenAI chat/completions, Anthropic /v1/messages, and OpenAI
    /v1/responses. Probes / model lists / health (e.g. ``GET /v1/models`` the
    opencode adapter pings against the shim) are NOT model calls and must not
    count toward the turn cap (NICE, review 2026-08-25).

    ``/v1/messages/count_tokens`` must NOT count as a completion (Finding 2,
    2026-08-27): it is Anthropic's token-count utility — it contains
    ``/v1/messages`` as a substring but is not a model call. Counting it inflated
    the turn budget and recorded ~17 false ``usage_parse_failed`` rows. Exclude
    the ``count_tokens`` suffix explicitly.
    """
    return ("/chat/completions" in path or "/v1/messages" in path or "/v1/responses" in path) and (
        "count_tokens" not in path
    )


def _rehome_system_role_messages(path: str, body: bytes) -> tuple[bytes, int]:
    """Turn ``role: system`` entries INSIDE an Anthropic ``messages`` list into user text.

    Claude Code 2.1.234 appends a ``<total_tokens>N tokens left</total_tokens>``
    countdown as a ``role: system`` message after every tool result (plus one
    "Available agent types" notice at the start) and puts its cache_control
    breakpoint on the newest of them.  Anthropic's own API accepts that shape;
    LiteLLM's Anthropic transform (v1.99.1 ``anthropic_messages_pt``) only walks
    user/tool/function and assistant roles, so every mid-list system message is
    silently dropped — and with the last one goes the only message-level cache
    breakpoint.  Measured on run 01788665399451336589-d2bf217e (sphinx-10614):
    31 of 92 messages were such notices, the prompt grew to 49K tokens, and only
    the static system+tools prefix (2-4K) ever read from cache — 7% overall
    against 98% on the OpenAI-shaped harnesses, i.e. ~3.5x the cost.

    Re-roling them as user text blocks keeps their content, keeps their
    cache_control (the newest notice stays the last message), and keeps the
    prefix byte-stable across calls; the Messages API merges adjacent user
    messages, and LiteLLM folds consecutive user-type messages into one.  Only
    ``/v1/messages`` bodies are touched; anything unparseable, or with no such
    message, is returned verbatim (ADR-0019: additive mutation only).
    Returns ``(body, count_rehomed)``.
    """
    if "/v1/messages" not in path or not _is_completion_path(path):
        return body, 0
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body, 0
    if not isinstance(obj, dict):
        return body, 0
    msgs = obj.get("messages")
    if not isinstance(msgs, list):
        return body, 0
    rehomed = 0
    for msg in msgs:
        if not isinstance(msg, dict) or msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            block: dict[str, Any] = {"type": "text", "text": content}
            if isinstance(msg.get("cache_control"), dict):
                block["cache_control"] = msg["cache_control"]
            msg["content"] = [block]
        elif not isinstance(content, list):
            continue  # unknown shape: leave it to the gateway
        msg.pop("cache_control", None)
        msg["role"] = "user"
        rehomed += 1
    if not rehomed:
        return body, 0
    return json.dumps(obj).encode(), rehomed


def _inject_stream_options(path: str, body: bytes) -> bytes:
    """Add ``stream_options: {include_usage: true}`` to a streaming OpenAI request (P-4).

    Without it an OpenAI-shaped streaming response carries no usage chunk, so
    there is nothing to parse at end of stream.  Anything parseable-only is left
    verbatim (ADR-0019).  Only the OpenAI chat-completions shape gets this; the
    Anthropic path has no stream_options.
    """
    if "/chat/completions" not in path:
        return body
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(obj, dict) or not obj.get("stream"):
        return body
    if "stream_options" not in obj:
        obj["stream_options"] = {"include_usage": True}
        return json.dumps(obj).encode()
    return body


def _inject_max_tokens(
    path: str,
    body: bytes,
    *,
    window: int | None,
    est_prompt_tokens: int,
) -> tuple[bytes, int | None]:
    """F1: put an output cap on a completion request that carries none.

    Returns ``(body, injected)`` — ``injected`` is the value written, or None
    when the body already had a cap (left verbatim), is not a completion shape,
    or is not JSON. The value is ``min(OUTPUT_RESERVE, window - est_prompt)``,
    floored at ``_MAX_TOKENS_INJECT_FLOOR``; with no window it is the bare
    reserve. The field name follows the shape (``max_tokens`` for chat and
    Anthropic messages, ``max_output_tokens`` for the Responses API); a chat
    body naming ``max_completion_tokens`` instead counts as capped.
    """
    if not _is_completion_path(path):
        return body, None  # count_tokens / models / health: never a model call
    fields: tuple[str, ...] | None = None
    for marker, names in _OUTPUT_CAP_FIELDS.items():
        if marker in path:
            fields = names
            break
    if fields is None:
        return body, None
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body, None
    if not isinstance(obj, dict):
        return body, None
    if any(obj.get(name) is not None for name in fields):
        return body, None
    cap = OUTPUT_RESERVE
    if window is not None:
        cap = min(cap, window - est_prompt_tokens)
    cap = max(_MAX_TOKENS_INJECT_FLOOR, cap)
    obj[fields[0]] = cap
    return json.dumps(obj).encode(), cap


def _accumulate_tail(
    data: bytes,
    usage: Any,
    model: str,
    call: dict[str, Any] | None = None,
    head: bytes = b"",
    headers: Any = None,
) -> dict[str, Any] | None:
    """Accumulate the response's usage after the stream ends.

    Two-tier cost (F-2): use the API-reported ``cost`` from the usage block when
    present; otherwise attempt this call's tokens locally via the shared pricing
    module (required for the Anthropic/Claude-Code path, F-1).

    When ``call`` is given (M0 §2.5), the per-call record's token/cost/identity
    fields are populated from the SAME block already parsed.  Token fields are
    written NULL (key absent) when the response reported nothing — a zeros-based
    record would rank the un-instrumented harness best by infinite margin
    (M0 §0.6 Trap 3).

    ``head`` is the first bytes of a non-streaming response (M0-3 review fix):
    identity fields sit at the front, usage at the end — past a 16 KB tail the
    identity would be evicted while tokens/cost survive, yielding a record that
    looks complete and is not.  Identity is scanned over head + tail.

    ``headers`` — the upstream response headers (if available) — let the shim
    prefer the provider-authoritative values the body cannot carry: G-6 reads
    ``model_resolved`` from ``x-litellm-model-name`` (the body echo is the
    *alias*), and 1.6 reads ``generation_id`` from ``x-generation-id`` (more
    reliable and shape-independent than body-scanning).  Headers are optional —
    a parser without them falls back to the body scan.

    Returns the parsed usage dict, or None when the response carried no parseable
    usage (the caller then records ``usage_parse_failed`` on 2xx/3xx so "no usage
    reported" and "usage not found" are distinguishable in the data).
    """
    u = _usage_from_tail(data)
    if not u:
        return None
    try:
        input_tokens = int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0)
        output_tokens = int(u.get("output_tokens", u.get("completion_tokens", 0)) or 0)
        cost = u.get("cost")
        # The *_details sub-objects can be present-but-null in odd provider JSON;
        # coerce defensively so the sole meter never raises on a null (M0-1).
        pd = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
        cd = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
        if not isinstance(pd, dict):
            pd = {}
        if not isinstance(cd, dict):
            cd = {}
        cached_tokens = int(pd.get("cached_tokens", 0) or 0)
        cache_write_tokens = int(pd.get("cache_write_tokens", 0) or 0)
        # G-1 (review 2026-08-26): Anthropic reports cache as TOP-LEVEL usage
        # keys (cache_read_input_tokens / cache_creation_input_tokens), NOT
        # nested in prompt_tokens_details like the OpenAI shape.  The mapping
        # never fired, so claude_code recorded 0 cached tokens and priced cache
        # reads at full input rate (defeating the B8 cache-read dimension).
        # When the OpenAI nesting is absent, map the Anthropic top level.
        if cached_tokens == 0:
            cached_tokens = int(u.get("cache_read_input_tokens", 0) or 0)
        if cache_write_tokens == 0:
            cache_write_tokens = int(u.get("cache_creation_input_tokens", 0) or 0)
        # Anthropic reports thinking tokens as output_tokens_details.thinking_tokens
        # (e.g. "thinking_tokens": 13), NOT reasoning_tokens — so claude_code always
        # read 0 here despite a thinking-enabled setup (review 2026-08-27 §3). Read
        # the Anthropic key first, fall back to the OpenAI key name.
        reasoning_tokens = int(cd.get("thinking_tokens", cd.get("reasoning_tokens", 0)) or 0)
        call_cost = cost_for(
            float(cost) if cost not in (None, 0) else None,
            input_tokens,
            output_tokens,
            model,
            # B8: the cache-read dimension — cached input prices at 20% of the
            # input rate; without this the shim counted cache reads at full price.
            cached_input_tokens=cached_tokens,
        )

        usage.input_tokens += input_tokens
        usage.output_tokens += output_tokens
        usage.cached_tokens += cached_tokens
        usage.cache_write_tokens += cache_write_tokens
        usage.reasoning_tokens += reasoning_tokens
        usage.cost_usd += call_cost
        if cost not in (None, 0):
            usage.source = "gateway"

        if call is not None:
            # Provider-side identity (M0 §2.5 / R2-2 review): generation_id is
            # the TOP-LEVEL response `id`; model_resolved the provider served;
            # provider_name the provider.  These are object keys at the FRONT of
            # a non-streaming body, while every entry in choices[].message.
            # tool_calls[] ALSO carries an `id` in the TAIL — so identity must be
            # FIRST-WINS over the head, never last-wins over head+tail (a
            # last-wins scan silently picks the last tool-call id and writes a
            # ledger join key that matches nothing).  finish_reason sits in the
            # last choice and legitimately wants the tail + last-wins.
            head_text = head.decode("utf-8", "replace")
            text = head_text + data.decode("utf-8", "replace")
            # G-3 (review 2026-08-26): `provider` sits in SSE chunk 0 (the head
            # for an opencode/codex stream) but LiteLLM drops it entirely on the
            # streaming path — scanning head alone never rescues it.  Scan the
            # FULL head+tail first-wins so an occurrence anywhere in the body is
            # found; a shape that still has none is disclosed, not invented
            # (claude is fixed outright in STEP 2 by the Anthropic adapter).
            call.update(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                cache_write_tokens=cache_write_tokens,
                reasoning_tokens=reasoning_tokens,
                cost_usd=call_cost,
                cost_source="provider" if cost not in (None, 0) else "local_pricing",
                # G-6 (review 2026-08-26): `model_resolved` read from the
                # LiteLLM response header FIRST — the alias echo lives in the
                # chat-shape body `model` field and says `cheap-oss-model`, not
                # the literal upstream.  The header is the provider's answer.
                generation_id=(
                    _header_scalar(headers, "x-generation-id")
                    or _first_json_scalar(head_text, "id")
                ),
                model_resolved=(
                    _header_scalar(headers, "x-litellm-model-name")
                    or _first_json_scalar(text, "model")
                ),
                provider_name=_first_json_scalar(text, "provider"),
                finish_reason=_scan_json_scalar(text, "finish_reason"),
                # 1.6 (review 2026-08-26): the provider's raw stop reason before
                # OpenRouter normalises it — separates a real length-stop from a
                # provider quirk.
                native_finish_reason=_scan_json_scalar(text, "native_finish_reason"),
                # 1.6: provider/model build identity — a change mid-run means the
                # backend moved under us.
                system_fingerprint=_scan_json_scalar(text, "system_fingerprint"),
                # 1.6: marginal, stored because it is free.
                service_tier=_scan_json_scalar(text, "service_tier"),
                # G-4 (review 2026-08-26): the OpenAI Responses API names its
                # completion signal `status` (+ `incomplete_details.reason`), not
                # `finish_reason`/`stop_reason`, so codex recorded neither.  Both
                # are last-wins: the terminal `response.completed` /
                # `response.incomplete` event is the final one in the stream.
                responses_status=_scan_json_scalar(text, "status"),
                responses_incomplete_reason=_responses_incomplete_reason(text),
                # R5.2 (builder1-REMAINING-WORK-single-handover): the OpenAI
                # chat-completions field is `finish_reason`; Anthropic
                # /v1/messages AND OpenAI /v1/responses carry `stop_reason`.
                # Without scanning stop_reason, claude_code (51 calls) and codex
                # (12) recorded None on every call and a truncation was
                # indistinguishable from a clean stop.
                stop_reason=_scan_json_scalar(text, "stop_reason"),
            )
            # cost_details.upstream_inference_cost — the upstream's own figure,
            # distinct from what we priced/paid at the gateway.  M0-1 (review):
            # ``cost_details`` can be present-but-null in the JSON; guard with
            # isinstance so the sole meter cannot raise AttributeError mid-parse
            # (the old bare ``u.get("cost_details", {}).get(...)`` returned None
            # for a null value and raised in the stream's finally — truncating
            # a billed response and losing the llm_calls row).
            cost_details = u.get("cost_details")
            up_cost = (
                cost_details.get("upstream_inference_cost")
                if isinstance(cost_details, dict)
                else None
            )
            if up_cost is not None:
                call["upstream_inference_cost_usd"] = float(up_cost)
            # 1.6 (review 2026-08-26): the prompt/completion cost SPLIT, straight
            # from the provider's cost_details — no re-derivation from a price
            # table.  Absent keys stay NULL (Trap 3).
            if isinstance(cost_details, dict):
                for k, dst in (
                    ("upstream_inference_prompt_cost", "upstream_inference_prompt_cost_usd"),
                    (
                        "upstream_inference_completions_cost",
                        "upstream_inference_completions_cost_usd",
                    ),
                ):
                    v = cost_details.get(k)
                    if v is not None:
                        call[dst] = float(v)
    except (AttributeError, KeyError, TypeError, ValueError):
        # The sole meter must never lose a call to a parse edge: log and keep
        # what we already have.  This is the component's fail-safe.
        logger.warning("shim: _accumulate_tail dropped a field on an odd body", exc_info=True)
    return u


_JSON_SCALAR = re.compile(
    r'"([A-Za-z_]\w*)"\s*:\s*("(?:\\.|[^"\\])*"|[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|true|false|null)'
)


def _scan_json_scalar(text: str, key: str) -> Any:
    """Return the LAST value of top-level JSON scalar *key* in *text*.

    Used on a raw (possibly SSE) response tail to pull fields that legitimately
    live at the end (``finish_reason`` in the last choice, M0 §2.5).  Last-wins
    so a long stream never evicts the answer.
    """
    if not text:
        return None
    result: Any = None
    for m in _JSON_SCALAR.finditer(text):
        if m.group(1) != key:
            continue
        v = m.group(2)
        if v == "null":
            result = None
        elif v in ("true", "false"):
            result = v == "true"
        else:
            result = json.loads(v)
    return result


def _first_json_scalar(text: str, key: str) -> Any:
    """Return the FIRST value of top-level JSON scalar *key* in *text*.

    Used for the top-level identity fields (``id`` / ``model`` / ``provider``)
    on the HEAD prefix of a response.  R2-2 (review): these are object keys at
    the front of a non-streaming body, but every entry in ``choices[].
    message.tool_calls[]`` ALSO carries an ``id`` in the tail — a last-wins scan
    of head+tail would silently pick the last tool-call id (a ledger join key
    that matches nothing).  First-wins over the head keeps the top-level value.
    ``finish_reason`` is NOT identity and must stay last-wins (see
    :func:`_scan_json_scalar`).
    """
    if not text:
        return None
    for m in _JSON_SCALAR.finditer(text):
        if m.group(1) != key:
            continue
        v = m.group(2)
        if v == "null":
            return None
        if v in ("true", "false"):
            return v == "true"
        return json.loads(v)
    return None


def _header_scalar(headers: Any, name: str) -> str | None:
    """Return a single response header value as a str, else None.

    Case-insensitive via the header map's ``.get``; tolerant of a ``None``
    header map (tests / parsers called without one).  Used for G-6
    (``x-litellm-model-name``) and 1.6 (``x-generation-id``).
    """
    if headers is None:
        return None
    try:
        v = headers.get(name)
    except AttributeError:
        return None
    if isinstance(v, str) and v:
        return v
    return None


def _responses_incomplete_reason(text: str) -> Any:
    """Read the LAST ``incomplete_details`` object's ``reason`` (OpenAI Responses).

    G-4 (review 2026-08-26): codex (the Responses API) signals a length stop as
    ``status: incomplete`` + ``incomplete_details: {reason: ...}`` in its
    terminal ``response.incomplete`` event.  The value is an OBJECT, so the
    scalar scanner cannot reach it — brace-match each ``incomplete_details`` and
    read ``reason`` from the last one.  Returns None when absent or malformed.
    """
    if not text:
        return None
    result: Any = None
    i = text.find('"incomplete_details"')
    while i != -1:
        j = text.find(":", i + len('"incomplete_details"'))
        if j != -1:
            k = j + 1
            while k < len(text) and text[k] in " \t\r\n":
                k += 1
            if k < len(text) and text[k] == "{":
                depth = 0
                l = k
                while l < len(text):
                    if text[l] == "{":
                        depth += 1
                    elif text[l] == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                obj = json.loads(text[k : l + 1])
                            except json.JSONDecodeError:
                                obj = None
                            # LAST-wins on the OBJECT itself: the terminal
                            # event's incomplete_details is authoritative.  A
                            # transient "incomplete" event followed by a final
                            # empty "completed" one means the call FINISHED —
                            # the empty (None) result of the later event wins,
                            # not the earlier max_output_tokens.
                            if isinstance(obj, dict):
                                result = obj.get("reason")  # None when absent
                            break
                    l += 1
        i = text.find('"incomplete_details"', i + 1)
    return result


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _fold_admission_into_usage(usage: Any, admission: Admission) -> bool:
    """Fold one admission into the instance's cumulative pacer footprint (Usage — the
    live-progress + instance_results source, design doc §2.5). Returns True when the
    admission carried a wait or a deny worth publishing live (so a clean, instant admit
    does not cost an extra Redis write per call)."""
    usage.paced_wait_ms_total += admission.paced_wait_ms
    if admission.was_queued:
        usage.paced_calls += 1
        usage.pacer_last_deny_axis = admission.deny_axis_last
    usage.pacer_last_queue_len = admission.queue_len_at_admit
    return admission.was_queued or admission.paced_wait_ms >= 1_000


def _apply_pacer_diagnostics(
    call: dict[str, Any], admission: Admission, *, accumulate: bool = False
) -> None:
    """Design doc §2.5: make a pacer wait explainable after the fact. ``pacer_was_queued`` is
    OR-ed across re-acquires (any deny on any attempt), ``pacer_queue_len`` is the depth seen
    at the LAST admission, ``pacer_deny_axis`` the last axis that denied this call (absent if
    it was never denied)."""
    was_queued = admission.was_queued or (accumulate and bool(call.get("pacer_was_queued")))
    call["pacer_was_queued"] = was_queued
    call["pacer_queue_len"] = admission.queue_len_at_admit
    if admission.deny_axis_last is not None:
        call["pacer_deny_axis"] = admission.deny_axis_last


def _elapsed_ms(now: float, start: float) -> int:
    return round((now - start) * 1000)


def _apply_rate_limit_headers(call: dict[str, Any], headers: Any) -> None:
    """Read the rate-limit fields only the shim can see, into *call* (M0 §2.3)."""
    try:
        ra = headers.get("retry-after")
        if ra is not None:
            call["retry_after_s"] = float(ra)
    except (TypeError, ValueError):
        pass

    def _num(field: str, dst: str) -> None:
        try:
            v = headers.get(field)
            if v is not None:
                call[dst] = int(v)
        except (TypeError, ValueError):
            pass

    _num("x-ratelimit-remaining-requests", "ratelimit_remaining_requests")
    _num("x-ratelimit-remaining-tokens", "ratelimit_remaining_tokens")


def _apply_gateway_timing_headers(call: dict[str, Any], headers: Any) -> None:
    """STEP 3 (review 2026-08-26): the three LiteLLM timing headers we already
    receive and discard, recorded verbatim as NUMERIC columns.  Their exact
    meaning on a STREAMING call (time-to-headers vs whole body) is verified in
    Phase 3/4 against a long stream — recorded here as the header value, never
    relabelled.
    """
    for header, field in (
        ("x-litellm-response-duration-ms", "gateway_response_ms"),
        ("x-litellm-overhead-duration-ms", "gateway_overhead_ms"),
        ("x-litellm-callback-duration-ms", "gateway_callback_ms"),
    ):
        try:
            v = headers.get(header)
            if v is not None and str(v).strip() != "":
                call[field] = float(v)
        except (TypeError, ValueError):
            pass


# _classify_rate_limit (the body-string heuristic) was REMOVED 2026-09-01: its own docstring
# admitted it was never live-validated, and the autoscaler spec §2.1 proved it wrong — LiteLLM
# wraps upstream throttles in its own ``litellm.RateLimitError`` text, so the string match
# labeled real provider overloads as 'gateway'. The live-validated discriminator (retry-after
# header presence) is applied directly at the rate_limit_scope call site in ``body_stream``.


def _usage_from_tail(data: bytes) -> dict[str, Any] | None:
    """Locate and parse the JSON object holding ``usage`` from a raw response tail.

    The tail may be plain JSON or SSE ``data: {...}`` lines (OpenAI and Anthropic
    alike).  Scans for each ``"usage"`` key and brace-matches its value.

    G-7 (review 2026-08-26): some streams report usage TWICE.  Anthropic emits an
    empty/zero block in ``message_start`` and the real one in ``message_delta``;
    returning the FIRST block (as the naive scan did) recorded 47% of
    claude_code's calls as zero input tokens.  Collect every usage object and
    return the MOST COMPLETE one — the first carrying non-zero
    ``input_tokens``/``prompt_tokens`` — or the LAST when none is non-zero (a
    genuinely empty response).  "Most complete" is unambiguous for Anthropic
    because ``message_start`` is all zeros/nulls by construction; it also absorbs
    the same family for OpenAI Responses (G-5), whose terminal event can
    re-emit/replace the usage object.
    """
    text = data.decode("utf-8", "replace")
    candidates: list[dict[str, Any]] = []
    i = text.find('"usage"')
    while i != -1:
        j = text.find(":", i + 7)
        if j != -1:
            k = j + 1
            while k < len(text) and text[k] in " \t\r\n":
                k += 1
            if k < len(text) and text[k] == "{":
                depth = 0
                l = k
                while l < len(text):
                    if text[l] == "{":
                        depth += 1
                    elif text[l] == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                value = json.loads(text[k : l + 1])
                            except json.JSONDecodeError:
                                break
                            if isinstance(value, dict):
                                candidates.append(value)
                            break
                    l += 1
        i = text.find('"usage"', i + 1)
    if not candidates:
        return None
    for c in candidates:
        try:
            if int(c.get("input_tokens", c.get("prompt_tokens", 0)) or 0) > 0:
                return c
        except (TypeError, ValueError):
            continue
    return candidates[-1]
