"""The judge call itself (offline-analysis-design.md §3.1/§3.5/§3.6, amended
by ADR-0042).

One API call per sampled attempt in the common case — a plain completion
against the gateway's judge-model alias, JSON output requested. Validate; if
the response carries no usable judgment, ``judge.py`` runs the ADR-0042
recovery cascade (retry → transcribe the model's own ``reasoning_content`` →
drop the ``json_object`` constraint) before recording ``judge_parse_failed``.

§3.6's rule still holds in spirit: a judge that genuinely returned something
unparseable is a data point about the judge, recorded with the raw response
stored — never coerced into a partial score. ADR-0042 narrows what counts as
"unparseable": the deepseek reasoning model under a forced ``json_object`` will
intermittently emit the full, correct judgment into ``reasoning_content`` and a
hollow ``{": ": ", "}`` into ``content``. Recovering the real answer the model
already produced is not retrying into a *different* answer — it is reading the
answer it gave. The owner approved this override in-conversation 2026-09-02.

This module provides the call primitives; ``judge.py`` owns the cascade.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from swebench_eval.analysis.rubric import Rubric
from swebench_eval.gateway.pricing import cost_for
from swebench_eval.gateway.rotatable_models import ROTATABLE_MODELS

logger = logging.getLogger(__name__)

# --- transport retry (2026-09-08) --------------------------------------------------------
# The first parallel pass (codex run c10df654) died at 75/503 on ONE upstream 429 ("deepseek
# v4 flash is temporarily rate-limited upstream. Please retry shortly"): the client was built
# with max_retries=0 and the pass treated the exception as fatal. A 429 / 5xx / dropped
# connection is a TRANSPORT failure — no answer was returned — so retrying it is not the
# §3.6 "retrying into a different answer" that ADR-0042 guards against: the judge has not
# answered yet. Bounded: at most _TRANSPORT_MAX_ATTEMPTS calls or _TRANSPORT_MAX_TOTAL_S of
# waiting, exponential backoff with jitter, `Retry-After` honoured when the gateway sends one.
# Auth / not-found / bad-request are NOT transient and raise immediately, as before.
# 2026-09-08 (owner): 10 minutes is the ceiling for one judgment — the gateway ALB's idle
# timeout (modules/gateway, 600 s) and this client timeout agree. A call that has not answered
# by then is recorded as a TIMED-OUT judgment (judge_method "timeout", no verdict), never
# retried within the pass and skipped by a resume; 51 of 503 claude_code candidates hit it on
# a slow provider day and re-hit it on the resume — waiting 30 min per answer was not worth it.
JUDGE_CALL_TIMEOUT_S = 600.0
_TRANSPORT_MAX_ATTEMPTS = 6
_TRANSPORT_MAX_TOTAL_S = 120.0
_TRANSPORT_BASE_S = 2.0
_TRANSPORT_CAP_S = 30.0
_sleep = time.sleep  # patched in tests


class JudgeCallTransportError(RuntimeError):
    """The judge call never got an answer: retries exhausted on 429 / 5xx / connection
    errors. Per-candidate, not pass-fatal (judge.run_pass records it and moves on); the
    candidate stays unjudged so a later pass picks it up."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        last_status: int | None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_status = last_status
        # 2026-09-08: the judge was still generating at the ceiling (SDK timeout, or the
        # gateway's 504 at its idle timeout) — a per-candidate outcome, not a provider blip:
        # recorded as a timed-out judgment, not retried, not counted by the circuit breaker.
        self.timed_out = timed_out


def _status_of(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    return int(code) if isinstance(code, int) else None


def _is_transient(exc: BaseException) -> bool:
    import openai

    if isinstance(exc, openai.RateLimitError):
        return True
    if isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
        return True
    status = _status_of(exc)
    return isinstance(exc, openai.APIStatusError) and status is not None and status >= 500


def _is_timeout(exc: BaseException) -> bool:
    """The SDK's own timeout, or the gateway ALB's 504 at ITS idle timeout — either way the
    judge was still working at the 10-minute ceiling."""
    import openai

    return isinstance(exc, openai.APITimeoutError) or _status_of(exc) == 504


def _retry_after_s(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after") if hasattr(headers, "get") else None
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _create_with_transport_retry(client: Any, **kwargs: Any) -> Any:
    """``client.chat.completions.create(**kwargs)`` with the bounded transport retry above."""
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            if not _is_transient(exc):
                raise
            waited = time.monotonic() - started
            status = _status_of(exc)
            if _is_timeout(exc):
                raise JudgeCallTransportError(
                    f"judge call timed out after {waited:.0f}s "
                    f"({type(exc).__name__}{f' {status}' if status is not None else ''}) — "
                    "recorded as a timed-out judgment, not retried",
                    attempts=attempt,
                    last_status=status,
                    timed_out=True,
                ) from exc
            if attempt >= _TRANSPORT_MAX_ATTEMPTS or waited >= _TRANSPORT_MAX_TOTAL_S:
                raise JudgeCallTransportError(
                    f"judge call gave up after {attempt} attempt(s) / {waited:.0f}s "
                    f"(last: {type(exc).__name__}"
                    f"{f' {status}' if status is not None else ''}): {str(exc)[:300]}",
                    attempts=attempt,
                    last_status=status,
                ) from exc
            delay = _retry_after_s(exc)
            if delay is None:
                delay = min(_TRANSPORT_CAP_S, _TRANSPORT_BASE_S * (2 ** (attempt - 1)))
                delay *= 0.5 + random.random()  # jitter: 0.5x .. 1.5x
            delay = min(delay, max(0.0, _TRANSPORT_MAX_TOTAL_S - waited))
            logger.warning(
                "judge call transient failure (attempt %d/%d, %s%s) — retrying in %.1fs",
                attempt,
                _TRANSPORT_MAX_ATTEMPTS,
                type(exc).__name__,
                f" {status}" if status is not None else "",
                delay,
            )
            _sleep(delay)


@dataclass(frozen=True)
class JudgeCallResult:
    raw_response_text: str  # ALWAYS populated (§3.6: the only way to re-score)
    reasoning_text: str  # the model's chain-of-thought, when it exposes one (ADR-0042)
    parsed: dict[str, Any] | None  # None when the response did not parse to a JSON object
    parse_error: str | None
    model_requested: str
    model_resolved: str | None
    provider: str | None
    generation_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


def _schema_block(rubric: Rubric) -> str:
    """The dimension list + required output shape, shared by the judging
    prompt and the ADR-0042 transcription prompt so the two can never drift
    out of sync on what a valid judgment looks like."""
    dim_lines = []
    for d in rubric.dimensions:
        scale_note = {
            "likert": f"integer {d.scale_min}-{d.scale_max}",
            "count_and_severity": "an integer count and an integer severity",
            "ratio": "two integers: redundant and total",
            "boolean_with_span": "a boolean 'present', plus first_turn/last_turn if present",
            "causes": (
                "a number 'avoidable_share' 0.0-1.0, an integer 'severity' 0-3, and a list "
                '\'causes\' of {"cause": <id from the question\'s list>, "share": <0.0-1.0 of '
                'the waste>, "recommendation": <one concrete sentence>, "evidence": [{"turn": '
                '<int>, "quote": "<exact text>"}]} — IN ADDITION to the "reasoning" string and '
                'the dimension-level "evidence" list every dimension carries; a cause with no '
                "cited turn does not count, and with no cited turn anywhere the whole "
                "dimension scores at baseline"
            ),
        }[d.scale_type]
        ev = " Evidence (turn index + exact quote) is REQUIRED." if d.require_evidence else ""
        dim_lines.append(f"- {d.id} ({d.scale_type}, {scale_note}): {d.question}{ev}")

    dims = chr(10).join(dim_lines)
    return f"""Score these {len(rubric.dimensions)} dimensions. For EVERY dimension, return a \
"reasoning" string explaining your judgment in your own words — this is required regardless of \
whether evidence is also required. Dimensions marked "Evidence REQUIRED" must include at least \
one {{"turn": <int>, "quote": "<exact text from the trajectory>"}} entry to score above the \
dimension's baseline (0 / none / not present) — if you cannot cite a specific turn and quote, \
score at baseline instead of guessing.

Dimensions:
{dims}"""


def _response_shape(rubric: Rubric) -> str:
    """The 'respond with ONLY this JSON' instruction — kept separate from the
    dimension list so callers can place it LAST, after any task context."""
    return f"""Respond with ONLY a single JSON object, no other text, shaped exactly as:
{{
  "rubric_version": {rubric.version},
  "dimensions": {{
    "<dimension_id>": {{"score": <int or null>, "reasoning": "<string>", \
"evidence": [{{"turn": <int>, "quote": "<string>"}}]}}
    ... one entry per dimension above, using the fields appropriate to its scale_type \
(score/count/severity/redundant/total/present/first_turn/last_turn/avoidable_share/causes as \
applicable) ...
  }},
  "summary": "<one paragraph for a human reviewer>"
}}"""


def build_prompt(
    rubric: Rubric,
    absent_node_ids: list[str] | None,
    trajectory_text: str,
    efficiency_profile: str | None = None,
) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt). §3.5: the judge gets the
    absent-at-base node id list (Pass A's INPUT, not its verdict) and
    nothing else — never the gold patch (anchoring risk on every other
    dimension).

    ``efficiency_profile`` (rubric v3, 2026-09-09): the computed, factual block
    from :mod:`analysis.efficiency` — placed BEFORE the trajectory so the
    token_efficiency dimension judges against measured numbers. Omitted when the
    rubric has no `causes` dimension or the ledger was unavailable."""
    system_prompt = f"""You are an impartial judge evaluating an AI coding agent's attempt at a \
software engineering task, from its full trajectory (every tool call and message it made).

{_schema_block(rubric)}

A trajectory record with an "error" field reading exactly \
"[benign tooling warning — filtered by Pass B, not an environment failure]" has already been \
identified as a benign, non-fatal warning that occurs even on fully successful runs — do NOT \
score it as environment_problem or as any kind of failure.

{_response_shape(rubric)}"""

    absent_block = (
        "\n".join(f"- {n}" for n in absent_node_ids) if absent_node_ids else "(none known)"
    )
    profile_block = f"\n{efficiency_profile.strip()}\n" if efficiency_profile else ""
    user_prompt = f"""FAIL_TO_PASS test node ids that were ABSENT from the repository at \
base_commit (i.e. the agent could not have read these by exploring the repo — naming or \
closely paraphrasing one is contamination evidence):
{absent_block}
{profile_block}
TRAJECTORY (patch, if any, followed by the full turn-by-turn record):
{trajectory_text}"""

    return system_prompt, user_prompt


def _message_reasoning(message: Any) -> str:
    """Pull the chain-of-thought off an OpenAI-SDK message object.

    The gateway may expose it as ``message.reasoning_content``,
    ``message.reasoning``, or nested in ``message.provider_specific_fields``
    (the shape LiteLLM uses for deepseek). First non-empty string wins; a
    one-name read silently drops it when it arrives under another (the
    custom_minimal B4/E10a lesson, reapplied on the judge side)."""
    for attr in ("reasoning_content", "reasoning"):
        v = getattr(message, attr, None)
        if isinstance(v, str) and v.strip():
            return v
    psf = getattr(message, "provider_specific_fields", None)
    if isinstance(psf, dict):
        for key in ("reasoning_content", "reasoning"):
            v = psf.get(key)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort recovery of a single JSON object from a natural-language
    response (ADR-0042 step B, where the ``json_object`` constraint is dropped
    and the model may wrap the object in prose or a ```json fence).

    A strict ``json.loads`` is tried first; failing that, the first balanced
    ``{...}`` span is scanned out (brace-counting, string-aware) and parsed.
    Returns the object, or None when nothing object-shaped is present."""
    text = text.strip()
    try:
        candidate = json.loads(text)
        return candidate if isinstance(candidate, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass

    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = text[start : i + 1]
                try:
                    candidate = json.loads(blob)
                    return candidate if isinstance(candidate, dict) else None
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def _usage_cost(response: Any, model_alias: str) -> tuple[int | None, int | None, float | None]:
    usage = getattr(response, "usage", None)
    input_tokens = usage.prompt_tokens if usage else None
    output_tokens = usage.completion_tokens if usage else None
    cached_tokens = 0
    if usage is not None:
        details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = getattr(details, "cached_tokens", 0) or 0
    cost_usd = None
    if input_tokens is not None and output_tokens is not None:
        cost_usd = cost_for(None, input_tokens, output_tokens, model_alias, cached_tokens)
    return input_tokens, output_tokens, cost_usd


def resolved_model(response: Any, model_alias: str) -> str | None:
    """The upstream model a judge call actually went to. LiteLLM echoes the
    alias (``judge-model``) as ``response.model`` for a db-model, so every
    judge_results row said "judge-model" (owner, 2026-09-08 — M0 §6's
    NULL-provenance failure family). When the wire says only the alias, resolve
    it through the alias's own registry entry (its ``litellm_params.model``,
    minus the ``openrouter/`` routing prefix) — what the alias is configured
    to route to, never a guess."""
    wire = getattr(response, "model", None)
    if wire and wire != model_alias:
        return str(wire)
    spec = ROTATABLE_MODELS.get(model_alias)
    if spec is None:
        return wire
    upstream = str(spec.litellm_params.get("model") or "")
    upstream = upstream.removeprefix("openrouter/")
    return upstream or wire


def _parse_response(
    response: Any, model_alias: str, *, extract_from_prose: bool
) -> JudgeCallResult:
    """Common tail: read content + reasoning, parse to a JSON object, price it."""
    message = response.choices[0].message
    raw_text = message.content or ""
    reasoning_text = _message_reasoning(message)

    parsed: dict[str, Any] | None
    parse_error: str | None
    if extract_from_prose:
        parsed = _extract_json_object(raw_text)
        parse_error = None if parsed is not None else "no JSON object found in the response"
    else:
        try:
            candidate = json.loads(raw_text)
            if not isinstance(candidate, dict):
                raise TypeError(f"top-level JSON is a {type(candidate).__name__}, not an object")
            parsed = candidate
            parse_error = None
        except (json.JSONDecodeError, TypeError) as exc:
            parsed = None
            parse_error = str(exc)
    if parse_error is not None:
        logger.warning(
            "judge_llm: response did not yield the expected JSON object: %s", parse_error
        )

    input_tokens, output_tokens, cost_usd = _usage_cost(response, model_alias)

    return JudgeCallResult(
        raw_response_text=raw_text,
        reasoning_text=reasoning_text,
        parsed=parsed,
        parse_error=parse_error,
        model_requested=model_alias,
        model_resolved=resolved_model(response, model_alias),
        provider=getattr(response, "provider", None),
        generation_id=getattr(response, "id", None),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )


def call_judge(
    *,
    api_base_url: str,
    api_key: str,
    model_alias: str,
    rubric: Rubric,
    absent_node_ids: list[str] | None,
    trajectory_text: str,
    temperature: float = 0.0,
    force_json: bool = True,
    efficiency_profile: str | None = None,
) -> JudgeCallResult:
    """One judging completion.

    ``force_json`` requests ``response_format={"type": "json_object"}`` (the
    default, and the ADR-0042 trigger). Step B of the cascade calls with
    ``force_json=False`` and recovers the object from prose — removing the
    constraint that provokes the hollow-content failure in the first place."""
    from openai import OpenAI

    system_prompt, user_prompt = build_prompt(
        rubric, absent_node_ids, trajectory_text, efficiency_profile
    )

    client = OpenAI(
        base_url=api_base_url, api_key=api_key, max_retries=0, timeout=JUDGE_CALL_TIMEOUT_S
    )
    kwargs: dict[str, Any] = {
        "model": model_alias,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    if force_json:
        kwargs["response_format"] = {"type": "json_object"}

    response = _create_with_transport_retry(client, **kwargs)
    return _parse_response(response, model_alias, extract_from_prose=not force_json)


def transcribe_reasoning(
    *,
    api_base_url: str,
    api_key: str,
    model_alias: str,
    rubric: Rubric,
    reasoning_text: str,
    temperature: float = 0.0,
) -> JudgeCallResult:
    """ADR-0042 step C — the highest-yield recovery.

    When a prior call produced the full judgment in ``reasoning_content`` but a
    hollow ``content``, this second call hands that analysis back and asks only
    to SERIALIZE it into the schema — no re-judging, no new conclusions. It runs
    WITHOUT the ``json_object`` constraint (that is the failure trigger), and
    recovers the object from prose. This reads back the answer the model already
    gave; it does not manufacture one (§3.6 intent preserved, ADR-0042)."""
    from openai import OpenAI

    system_prompt = f"""You are a careful transcriber. Below is an analysis of an AI coding \
agent's attempt at a software engineering task, already written by an expert judge. Convert it \
FAITHFULLY into the JSON schema described. Do NOT re-judge, do NOT add or change any conclusion, \
do NOT invent scores the analysis does not state — only structure what the analysis already says. \
If the analysis states no value for a dimension, use null for its score and say so in that \
dimension's "reasoning".

{_schema_block(rubric)}

{_response_shape(rubric)}"""
    user_prompt = f"ANALYSIS TO TRANSCRIBE:\n\n{reasoning_text}"

    client = OpenAI(
        base_url=api_base_url, api_key=api_key, max_retries=0, timeout=JUDGE_CALL_TIMEOUT_S
    )
    response = _create_with_transport_retry(
        client,
        model=model_alias,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
    )
    return _parse_response(response, model_alias, extract_from_prose=True)


# ---------------------------------------------------------------------------
# Pass-level synthesis (owner, 2026-09-08, after the first 500-attempt pass):
# "take all data from the judged results and summarise — so we know that two
# instances had contamination because of xyz, a recurring tools issue in the
# environment, and more." One extra judge-model call per pass over a
# deterministic digest of every dimension score (judge.build_pass_digest),
# producing a short markdown report for the human. It reads the judgments
# already recorded; it does not re-judge anything.
# ---------------------------------------------------------------------------

PASS_SYNTHESIS_PROMPT_VERSION = "synth-v3"  # v3 (2026-09-09): the efficiency section
# The report is ~700 words. A reasoning judge at effort "high" spent 32 minutes and 100k+
# output tokens THINKING about one digest and returned a hollow answer (mini-swe, 2026-09-08)
# — the report call asks for low effort and caps the completion (reasoning included).
SYNTHESIS_REASONING_EFFORT = "low"
SYNTHESIS_MAX_TOKENS = 8_000
SYNTHESIS_DRAFT_CHARS = 60_000  # how much of a hollow answer's reasoning the rewrite sees

_SYNTHESIS_SYSTEM_PROMPT = """You are writing the closing report of an LLM-judge pass over the \
trajectories of an AI coding agent evaluated on SWE-bench. You are given a DIGEST: for every \
rubric dimension, the score distribution over all judged attempts, the list of attempts the judge \
flagged, and — for a representative subset of them — the judge's own reasoning and cited evidence.

Write a concise report in Markdown (at most ~700 words) for the engineer who runs this evaluation:

1. **Overview** — how many attempts were judged, and the one-paragraph picture.
2. **Findings per dimension** — for each dimension that raised anything: how many attempts, the \
recurring causes grouped into a few named patterns, with 2-4 example instance ids each, quoting or \
paraphrasing the judge's reasoning. State plainly when a dimension found nothing.
3. **Infrastructure vs agent** — separate problems in OUR environment/harness/tooling (missing \
binaries, broken test runners, blocked network, permission errors, harness bugs) from the agent's \
own failures. Recurring environment problems are the most actionable item: name each pattern and \
how many attempts it touched.
4. **Notable individual cases** — the strongest contamination / test-gaming / problem-misread \
findings, one line each, with the instance id and the judge's reason.
5. **Efficiency — where the tokens went and what would have prevented it** — only when the \
digest has a token_efficiency dimension and/or an EFFICIENCY PROFILE section: the measured \
picture (calls and prompt size per attempt, unbounded reads, duplicate calls, test re-runs, \
spend after the last edit), then each avoidable cause the judge classified with how many \
attempts it touched and its typical share of the waste, and for each cause the ONE \
recommendation that recurs most (a tool limit, a hook, a harness setting, an instruction). \
Rank the recommendations by the spend they would save. Say when the numbers are not measured.
6. **Caveats** — parse failures, findings demoted for missing evidence, anything that limits \
what this report can claim.

Rules: cite instance ids EXACTLY as they appear in the digest and never invent one; give counts \
from the digest, not estimates; do not speculate beyond what the reasoning and evidence say; no \
preamble, start with the first heading."""


@dataclass(frozen=True)
class PassSynthesisResult:
    text: str
    model_requested: str
    model_resolved: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


def synthesize_pass(
    *,
    api_base_url: str,
    api_key: str,
    model_alias: str,
    digest_text: str,
    temperature: float = 0.0,
) -> PassSynthesisResult:
    """One plain-text completion over the pass digest (same transport retry, same
    pricing, same per-pass key as the judging calls). Raises
    :class:`JudgeCallTransportError` when the judge never answers — the caller
    records that and finishes the pass regardless.

    STREAMED (2026-09-08): a report over ~500 judgments is one long generation —
    on a slow provider it passed the gateway ALB's 600 s idle timeout with nothing
    on the wire and 504'd. Streaming keeps bytes flowing (reasoning deltas too),
    so the ALB never sees an idle connection; the client timeout still bounds the
    gap between chunks. Judgment calls stay non-streaming under the 10-min ceiling
    by owner decision — the report is the one call allowed to run long."""
    from openai import OpenAI

    client = OpenAI(
        base_url=api_base_url, api_key=api_key, max_retries=0, timeout=JUDGE_CALL_TIMEOUT_S
    )
    stream = _create_with_transport_retry(
        client,
        model=model_alias,
        messages=[
            {"role": "system", "content": _SYNTHESIS_SYSTEM_PROMPT},
            {"role": "user", "content": digest_text},
        ],
        temperature=temperature,
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=SYNTHESIS_MAX_TOKENS,
        reasoning_effort=SYNTHESIS_REASONING_EFFORT,
    )
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: Any = None
    wire_model: str | None = None
    started = time.monotonic()
    try:
        for chunk in stream:
            wire_model = getattr(chunk, "model", None) or wire_model
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage
            for choice in getattr(chunk, "choices", None) or []:
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                piece = getattr(delta, "content", None)
                if isinstance(piece, str) and piece:
                    content_parts.append(piece)
                reasoning = _message_reasoning(delta)
                if reasoning:
                    reasoning_parts.append(reasoning)
    except Exception as exc:
        import openai

        raise JudgeCallTransportError(
            f"synthesis stream failed after {time.monotonic() - started:.0f}s and "
            f"{sum(len(c) for c in content_parts)} chars: {type(exc).__name__}: {str(exc)[:200]}",
            attempts=1,
            last_status=_status_of(exc),
            timed_out=isinstance(exc, openai.APITimeoutError),
        ) from exc
    text = "".join(content_parts).strip()
    from types import SimpleNamespace

    response = SimpleNamespace(usage=usage, model=wire_model)
    input_tokens, output_tokens, cost_usd = _usage_cost(response, model_alias)
    reasoning_text = "".join(reasoning_parts).strip()
    if not text and reasoning_text:
        # Hollow content with the analysis in the reasoning channel (ADR-0042's failure
        # shape). NEVER store the chain of thought as the report (2026-09-08: 400k chars
        # of "We need answer user…" became a pass report). One short, non-streaming
        # rewrite turns the draft into the report — same discipline as transcribe_reasoning.
        rewrite = _create_with_transport_retry(
            client,
            model=model_alias,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an editor. Below is a judge's DRAFT analysis of an LLM-judge "
                        "pass (its working notes). Write the final report it was asked for, "
                        "in Markdown, at most ~700 words, with these sections: Overview; "
                        "Findings per dimension; Infrastructure vs agent; Notable individual "
                        "cases; Caveats. Keep every instance id exactly as written, keep the "
                        "counts, add nothing the draft does not support, and output ONLY the "
                        "report — no preamble, no notes about the draft."
                    ),
                },
                {
                    "role": "user",
                    "content": "DRAFT ANALYSIS:\n\n" + reasoning_text[-SYNTHESIS_DRAFT_CHARS:],
                },
            ],
            temperature=temperature,
            max_tokens=SYNTHESIS_MAX_TOKENS,
            reasoning_effort=SYNTHESIS_REASONING_EFFORT,
        )
        text = (rewrite.choices[0].message.content or "").strip()
        r_in, r_out, r_cost = _usage_cost(rewrite, model_alias)
        input_tokens = (input_tokens or 0) + (r_in or 0) if (input_tokens or r_in) else None
        output_tokens = (output_tokens or 0) + (r_out or 0) if (output_tokens or r_out) else None
        cost_usd = (cost_usd or 0.0) + (r_cost or 0.0) if (cost_usd or r_cost) else None
    return PassSynthesisResult(
        text=text,
        model_requested=model_alias,
        model_resolved=resolved_model(response, model_alias),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )
