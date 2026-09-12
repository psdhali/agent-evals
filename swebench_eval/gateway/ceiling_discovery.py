"""Ceiling discovery — Part 1 of BUILDER4-AUTOSCALER-TPM-CEILING-DISCOVERY-DESIGN-2026-08-31.md.

Synthetic load probe: fires N parallel chat-completion calls against the real gateway path, each
forcing a cache miss via a unique per-call prefix, classifies real provider overloads via
:func:`swebench_eval.harnesses.routing.is_real_provider_overload`, and converts the result to an
actual tok/min figure from the model's own **reported usage**, not an estimate, within a bounded
measurement **window** — never an assumed latency, and never dragged out by one straggler (design
doc §2, refined live).

**Top-down, not doubling-from-small (design doc §2, cost-driven revision).** The dataset this
system ever runs is bounded (~500 instances, max useful parallelism well under 200) — the true
extrapolated ceiling doesn't matter, only whether the concurrency we'll actually use is safe. So
:func:`find_ceiling` tests at the target concurrency first and only bisects *downward* if that
fails, instead of climbing up from a small number to find an abstract true limit we'll never use.

**Probe shape is the cheap near-max-context one — production-shape validation is separate, not
built here** (reviewer F1; design doc §2.1). This module answers "how much token *volume* before
overload"; whether that's the axis production actually hits is a follow-up calibration pass.

**tpm is computed from real ``usage`` data, windowed — not from the requested size, and not from
the full batch's wall-clock span.** Two things found by actually questioning the first version,
not assumed correct: (1) the original ``tpm()`` multiplied ``success_count`` by the *requested*
``target_tokens`` — a live check showed the model's real ``usage.prompt_tokens`` can differ from
the request's target by ~20% (a char-count-to-token estimate is inherently approximate); the real,
model-reported figure is what should drive a number this design trusts. (2) The original divisor
was the full batch's wall-clock span, end to end — one slow straggler (network jitter, a
provider momentarily queueing that one call) drags the whole rate estimate down, understating what
the batch mostly achieved. Fixed: sum REAL tokens only from calls that complete within a bounded
window (default 60s) from batch start, and divide by that window, not by whatever the last
straggler took.

The bisection procedure (:func:`find_ceiling`) is a pure function over an injected async prober
callback so it's testable without any network access — see ``tests/test_ceiling_discovery.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from swebench_eval.harnesses.routing import is_real_provider_overload

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_TOKENS = 300
DEFAULT_WINDOW_S = 60.0
# Rough English-text ratio for SIZING the request only — false precision isn't the goal there
# (design doc §2 explicitly rejects chasing single-digit accuracy on a constraint that moves with
# other tenants anyway). The OUTPUT tpm figure never uses this — it uses real reported usage.
_CHARS_PER_TOKEN = 4
_REQUEST_TIMEOUT_S = 120.0
# A probe call's timeout grows with its prompt: 120 s was sized for the 262K window (E17's
# median at 213K real tokens is ~13 s). The deepseek pool probes at ~1,046K (2026-09-04), and
# a timeout there would read as a network failure (status 0) and poison the burst count. The
# floor prefill rate is deliberately pessimistic; the gateway ALB's idle timeout is 600 s.
_MIN_PREFILL_TOK_PER_S = 2_500.0
_REQUEST_TIMEOUT_MAX_S = 540.0
_FILLER_PHRASE = "the quick brown fox jumps over the lazy dog "


def request_timeout_s(target_tokens: int) -> float:
    """Per-call HTTP timeout for a probe of *target_tokens*: 120 s, or longer for a prompt that
    would take longer than that to prefill at the pessimistic floor rate, capped under the ALB."""
    by_size = target_tokens / _MIN_PREFILL_TOK_PER_S
    return min(_REQUEST_TIMEOUT_MAX_S, max(_REQUEST_TIMEOUT_S, by_size))


def probe_content(target_tokens: int) -> str:
    """Near-max-context filler content with a unique nonce prefix — forces a cache miss (every
    call gets genuinely distinct content, not a shared prefix a provider could serve from cache),
    and a rotating phrase rather than one repeated character (some providers short-circuit
    degenerate repetition)."""
    nonce = uuid.uuid4().hex
    filler_chars = max(0, target_tokens * _CHARS_PER_TOKEN - len(nonce))
    filler = (_FILLER_PHRASE * (filler_chars // len(_FILLER_PHRASE) + 1))[:filler_chars]
    return f"probe-nonce:{nonce}\n{filler}"


def cached_prefix(prefix_tokens: int) -> str:
    """F3 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): the FIXED prefix Phase D reuses on every
    call so the provider serves it from its prompt cache — the opposite of :func:`probe_content`.
    Carries one nonce per probe RUN (at the front, so it is part of the cached span) so a
    previous run's cache entry can never make this run's first step look warm."""
    nonce = uuid.uuid4().hex
    head = f"probe-prefix:{nonce}\n"
    filler_chars = max(0, prefix_tokens * _CHARS_PER_TOKEN - len(head))
    filler = (_FILLER_PHRASE * (filler_chars // len(_FILLER_PHRASE) + 1))[:filler_chars]
    return head + filler


def cached_probe_content(prefix: str, suffix_tokens: int) -> str:
    """*prefix* verbatim (the cache hit) followed by a unique per-call suffix (the miss) — the
    agent-loop shape the pacer must price: a large shared context plus a small new turn."""
    nonce = uuid.uuid4().hex
    suffix_chars = max(0, suffix_tokens * _CHARS_PER_TOKEN - len(nonce))
    suffix = (_FILLER_PHRASE * (suffix_chars // len(_FILLER_PHRASE) + 1))[:suffix_chars]
    return f"{prefix}\nprobe-nonce:{nonce}\n{suffix}"


def _usage_cached_tokens(body: dict[str, object]) -> int | None:
    """``usage.prompt_tokens_details.cached_tokens`` (the OpenAI/OpenRouter shape) from a 200,
    or None when the provider reported nothing — never a fabricated 0 (a step that cannot
    prove its cache hits is INVALID, not clean)."""
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        return None
    cached = details.get("cached_tokens")
    if isinstance(cached, (int, float)):
        return int(cached)
    return None


def _usage_tokens(body: dict[str, object]) -> int | None:
    """Real total tokens from a 200 response's ``usage`` block, or None if absent/unparseable —
    never a guess. OpenAI-shaped (``usage.total_tokens``, falling back to prompt+completion)."""
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)):
        return int(total)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if isinstance(prompt, (int, float)) and isinstance(completion, (int, float)):
        return int(prompt) + int(completion)
    return None


@dataclass(frozen=True)
class BatchResult:
    """One probe batch's outcome — the full set of raw counts, not just a verdict, so the caller
    can build an audit trail (design doc's `model_tpm_observations` log wants exactly this).

    ``tokens_in_window``/``window_s`` carry the REAL, measured basis for :attr:`tpm` — real usage
    tokens from calls that completed within the window, divided by the window itself, not by
    whatever the slowest call in the batch happened to take.
    """

    concurrency: int
    elapsed_s: float  # full batch wall-clock — diagnostic only, NOT the tpm divisor
    window_s: float
    tokens_in_window: int  # real usage.total_tokens summed over calls completing within window_s
    success_count: int
    overload_count: int
    error_count: int  # neither a clean success nor a classified real overload
    # BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.1: per-call latency of each SUCCESSFUL
    # call (its own launch -> completion, stagger offset excluded) and the real token total of
    # all successes regardless of window. Both were previously measured and thrown away; the
    # ramp needs latency for its physically-right starting point (b_edge / L, the owner's
    # "one latency of service" model) and for soft-strain detection, and the un-windowed token
    # total for an HONEST offered rate (§1: window padding diluted the old figure by ~37%).
    latencies_s: tuple[float, ...] = ()
    real_tokens_total: int = 0
    # F3 (2026-09-04): per successful call, the provider-reported cached prompt tokens (None
    # when the response carried no prompt_tokens_details — recorded as -1 so the tuple stays
    # aligned with latencies_s and "not reported" is never mistaken for 0).
    cached_tokens_s: tuple[int, ...] = ()
    # 2026-09-05 (gpt-5-mini probe): the HTTP status of every call counted as an error (0 =
    # network failure). A step where errors dominate is refused, and the refusal must SAY what
    # the provider answered — 380 x 402 "would exceed your available credits" looked like a
    # clean ramp for three steps because the strain rule only ever looked at 429s.
    error_statuses: tuple[int, ...] = ()

    @property
    def overloaded(self) -> bool:
        return self.overload_count > 0

    @property
    def error_dominated(self) -> bool:
        """Errors (neither success nor classified overload) outnumber everything else — the
        step measured the provider REJECTING calls, not serving them. Distinct from
        :attr:`inconclusive` (nothing at all succeeded): a step with 2 successes out of 44 is
        not inconclusive by that rule, yet calling it "clean" would seed a ceiling from two
        calls' worth of evidence."""
        return self.error_count > 0 and self.error_count > self.success_count + self.overload_count

    def error_status_histogram(self) -> str:
        counts: dict[int, int] = {}
        for s in self.error_statuses:
            counts[s] = counts.get(s, 0) + 1
        return ", ".join(
            f"{n}x {s or 'network'}" for s, n in sorted(counts.items(), key=lambda kv: -kv[1])
        )

    def cache_hit_share(self, min_cached_tokens: int) -> float:
        """Share of the successful calls whose reported cached tokens reach
        *min_cached_tokens* — Phase D's validity test. 0.0 when nothing succeeded or nothing
        was reported (unknown must never render as a hit)."""
        if not self.cached_tokens_s:
            return 0.0
        hits = sum(1 for c in self.cached_tokens_s if c >= min_cached_tokens)
        return hits / len(self.cached_tokens_s)

    @property
    def median_latency_s(self) -> float | None:
        """Median per-call latency of the successful calls, or None if none succeeded."""
        if not self.latencies_s:
            return None
        ordered = sorted(self.latencies_s)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    def offered_rate_tok_s(self, stagger_s: float) -> float:
        """The rate this batch actually OFFERED the provider, tok/s, over the real offering
        span — ``(n-1) x stagger + median latency`` — never a padded window. This is the number
        a clean ramp step proves safe; §1 of the design doc records how dividing by
        ``window_s`` (= n x stagger + 60) under-reported it by ~37% and fed a seed 4-9x low."""
        med = self.median_latency_s
        if med is None or self.real_tokens_total <= 0:
            return 0.0
        span = max(0.001, (self.concurrency - 1) * max(0.0, stagger_s) + med)
        return self.real_tokens_total / span

    @property
    def inconclusive(self) -> bool:
        """True when nothing here can be trusted as a real signal — zero successes AND zero
        classified overloads, only errors (auth failures, network issues, a malformed request).

        Found by actually running this end to end, not assumed: a batch that 401'd on every call
        (a discovery alias whose key rotation hadn't propagated yet) briefly got reported as a
        confirmed-safe ceiling with a fabricated ~85M tok/min figure, because ``not overloaded``
        was the only check `find_ceiling` made — exactly the "unknown must never render as
        healthy" failure this whole design exists to prevent, caught in the design's own code.
        """
        return self.success_count == 0 and self.overload_count == 0 and self.error_count > 0

    @property
    def tpm(self) -> float:
        """Aggregate tok/min this batch sustained, from real reported usage within the bounded
        window — §5.1 of the original autoscaler spec: the control law's unit is tpm, not
        call/task count."""
        if self.window_s <= 0:
            return 0.0
        return self.tokens_in_window / self.window_s * 60.0


async def _one_call(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    target_tokens: int,
    output_tokens: int,
    batch_start: float,
    provider_pin: str | None = None,
    content: str | None = None,
) -> tuple[int, float | None, float, int | None, int | None]:
    """One probe call. Returns ``(status_code, retry_after_s, completed_at_s, real_tokens,
    cached_tokens)`` — ``completed_at_s`` is wall-clock elapsed since *batch_start*, and
    ``real_tokens`` is the model's own reported usage on a 200 (None otherwise, or if usage was
    absent/unparseable — a success with unreadable usage is never silently counted as zero,
    it's excluded from the windowed sum entirely, the same "never fabricate, never silently
    zero" discipline as elsewhere in this design). ``cached_tokens`` is the reported cache-hit
    count on a 200 (None when not reported). *content* overrides the default unique
    cache-missing body (Phase D's shared-prefix shape).

    Never raises on an HTTP error status — a 429 IS the signal this function exists to observe,
    not a failure of the call itself. A genuine network failure (timeout, connection refused) is
    reported as status 0 so the caller can count it separately from both success and overload.
    """
    body: dict[str, object] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": content if content is not None else probe_content(target_tokens),
            }
        ],
        "max_tokens": output_tokens,
    }
    if provider_pin:
        # Verified end-to-end 2026-09-01: the pin survives LiteLLM -> OpenRouter and is
        # enforced. Discovery MUST measure the same provider pool production rides (design §6:
        # constants are per (model, provider), not per model).
        body["provider"] = {"order": [provider_pin], "allow_fallbacks": False}
    try:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=request_timeout_s(target_tokens),
        )
    except httpx.HTTPError as exc:
        logger.warning("ceiling_discovery: call failed (network): %s", exc)
        return 0, None, time.monotonic() - batch_start, None, None
    completed_at_s = time.monotonic() - batch_start
    retry_after = resp.headers.get("retry-after")
    try:
        retry_after_s = float(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after_s = None
    real_tokens = None
    cached_tokens = None
    if resp.status_code == 200:
        try:
            parsed = resp.json()
            real_tokens = _usage_tokens(parsed)
            cached_tokens = _usage_cached_tokens(parsed)
        except ValueError:
            real_tokens = None
        if real_tokens is None:
            logger.warning(
                "ceiling_discovery: 200 response had no parseable usage — excluded from tpm"
            )
    return resp.status_code, retry_after_s, completed_at_s, real_tokens, cached_tokens


async def probe_batch(
    *,
    base_url: str,
    api_key: str,
    model: str,
    concurrency: int,
    target_tokens: int,
    output_tokens: int = DEFAULT_OUTPUT_TOKENS,
    window_s: float = DEFAULT_WINDOW_S,
    provider_pin: str | None = None,
    stagger_s: float = 0.0,
    content_factory: Callable[[], str] | None = None,
) -> BatchResult:
    """Fire *concurrency* parallel calls, real network I/O. *content_factory*, when given,
    builds each call's message content (Phase D's shared prefix + unique suffix); otherwise
    every call is a unique cache miss of *target_tokens*.

    Found by checking, not assumed: httpx's ``AsyncClient`` defaults to ``max_connections=100``
    internally (confirmed live — ``httpx.Limits()`` prints ``None``, but the transport falls back
    to 100). Left at the default, a concurrency=150 batch would only ever put 100 requests on the
    wire at once, queueing the rest at the CLIENT — not "150 concurrent load on the provider," a
    lower number silently substituted for it. The pool is sized to *concurrency* itself so every
    call in the batch can be genuinely in flight at once.
    """
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    start = time.monotonic()

    async def _staggered(
        i: int,
    ) -> tuple[int, float | None, float, int | None, float, int | None]:
        if stagger_s > 0:
            await asyncio.sleep(i * stagger_s)
        launched_at_s = time.monotonic() - start
        status_code, retry_after_s, completed_at_s, real_tokens, cached = await _one_call(
            client,
            base_url,
            api_key,
            model,
            target_tokens,
            output_tokens,
            start,
            provider_pin=provider_pin,
            content=content_factory() if content_factory is not None else None,
        )
        # Per-call latency is launch -> completion for THIS call — the stagger offset is
        # excluded, otherwise later calls in a paced batch would look slower than they were.
        return (
            status_code,
            retry_after_s,
            completed_at_s,
            real_tokens,
            completed_at_s - launched_at_s,
            cached,
        )

    async with httpx.AsyncClient(limits=limits) as client:
        results = await asyncio.gather(*(_staggered(i) for i in range(concurrency)))
    elapsed = time.monotonic() - start

    success_count = overload_count = error_count = 0
    tokens_in_window = 0
    real_tokens_total = 0
    latencies: list[float] = []
    cached_s: list[int] = []
    error_statuses: list[int] = []
    for status_code, retry_after_s, completed_at_s, real_tokens, latency_s, cached in results:
        if status_code == 200:
            success_count += 1
            latencies.append(latency_s)
            cached_s.append(cached if cached is not None else -1)
            if real_tokens is not None:
                real_tokens_total += real_tokens
                if completed_at_s <= window_s:
                    tokens_in_window += real_tokens
        elif is_real_provider_overload(status_code, retry_after_s):
            overload_count += 1
        else:
            error_count += 1
            error_statuses.append(int(status_code))
    return BatchResult(
        concurrency=concurrency,
        elapsed_s=elapsed,
        window_s=window_s,
        tokens_in_window=tokens_in_window,
        success_count=success_count,
        overload_count=overload_count,
        error_count=error_count,
        latencies_s=tuple(latencies),
        real_tokens_total=real_tokens_total,
        cached_tokens_s=tuple(cached_s),
        error_statuses=tuple(error_statuses),
    )


Prober = Callable[[int], Awaitable[BatchResult]]


class DiscoveryProbeError(RuntimeError):
    """A probe batch could not be interpreted as either safe or overloaded — refuse to guess."""


@dataclass(frozen=True)
class DiscoveryResult:
    """The final answer: a confirmed-safe concurrency and its measured tok/min, plus every batch
    fired along the way — a full audit trail, not just the final number."""

    safe_concurrency: int
    tpm: float
    probes: tuple[BatchResult, ...]


async def find_ceiling(
    prober: Prober,
    *,
    target_concurrency: int,
    bisect_floor: int = 5,
    bisect_tolerance: int = 10,
) -> DiscoveryResult:
    """Top-down-then-bisect (design doc §2, cost-driven revision of the original doubling ramp).

    Tests *target_concurrency* first — the only number that matters in practice, bounded by real
    dataset size / max useful parallelism, never the provider's abstract true ceiling. Only
    bisects DOWNWARD, and only if the target itself overloads.

    ``prober`` is injected so this is testable without any network access — see
    ``tests/test_ceiling_discovery.py`` for the pure-function coverage (mutation-checked per the
    project's standing discipline). tpm is read straight off each ``BatchResult`` — real, windowed
    usage data, nothing computed here from an assumed token size.
    """
    probes: list[BatchResult] = []

    top = await prober(target_concurrency)
    probes.append(top)
    _refuse_if_inconclusive(top)
    if not top.overloaded:
        return DiscoveryResult(
            safe_concurrency=target_concurrency, tpm=top.tpm, probes=tuple(probes)
        )

    # Target overloaded — bisect downward between a known-safe floor and the failed target.
    safe_lo = bisect_floor
    unsafe_hi = target_concurrency
    last_safe: BatchResult | None = None
    while unsafe_hi - safe_lo > bisect_tolerance:
        mid = (safe_lo + unsafe_hi) // 2
        result = await prober(mid)
        probes.append(result)
        _refuse_if_inconclusive(result)
        if result.overloaded:
            unsafe_hi = mid
        else:
            safe_lo = mid
            last_safe = result

    if last_safe is None:
        # Even the floor itself overloaded — confirm it directly so the caller gets a real,
        # measured (if disappointing) number rather than nothing, and never a fabricated one.
        floor_result = await prober(bisect_floor)
        probes.append(floor_result)
        _refuse_if_inconclusive(floor_result)
        last_safe = floor_result
        safe_lo = bisect_floor if not floor_result.overloaded else 0

    return DiscoveryResult(safe_concurrency=safe_lo, tpm=last_safe.tpm, probes=tuple(probes))


def _refuse_if_inconclusive(result: BatchResult) -> None:
    if result.inconclusive:
        raise DiscoveryProbeError(
            f"probe at concurrency={result.concurrency} was inconclusive "
            f"(0 success, 0 overload, {result.error_count} error) — refusing to report a "
            "ceiling from a batch that never actually exercised the provider"
        )
