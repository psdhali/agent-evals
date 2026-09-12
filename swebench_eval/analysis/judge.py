"""Pass B orchestrator (offline-analysis-design.md §3, extended §9/§10).

Candidate selection (incl. eval status, §10.1) -> stratified sampling (§3.2)
-> per-candidate: assemble input (§3.4/§9.4) -> call the judge (§3.1/§3.6) ->
parse into judge_dimension_scores (§10.7) -> write judge_results +
judge_dimension_scores -> judge_sampling summary row (§3.2/§10.2).

Key lifecycle (mint -> use -> finalise, §10.2) and the budget ceiling
(§3.10) wrap the whole pass, not each call — one pass, one set of keys, one
running total checked BEFORE every call (§3.10: "a ceiling that is checked
after the spend is not a ceiling", the same H4 admission-gate shape).
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from swebench_eval.analysis import efficiency, judge_keys, judge_live, trajectory_assembly
from swebench_eval.analysis.judge_llm import (
    JUDGE_CALL_TIMEOUT_S,
    PASS_SYNTHESIS_PROMPT_VERSION,
    JudgeCallTransportError,
    call_judge,
    resolved_model,
    synthesize_pass,
    transcribe_reasoning,
)
from swebench_eval.analysis.rubric import Rubric
from swebench_eval.analysis.score_parsing import (
    DimensionScore,
    count_scored_dimensions,
    parse_all_dimensions,
)
from swebench_eval.gateway.rotatable_models import ROTATABLE_MODELS
from swebench_eval.harnesses.routing import gateway_base_url

logger = logging.getLogger(__name__)

# §3.2 defaults.
DEFAULT_SAMPLE_RATE = 1.0  # §9.3: 100% is affordable at judge-model's real price
DEFAULT_MIN_PER_STRATUM = 5

# ADR-0042: the empty-judgment recovery cascade. At most this many judge-model
# calls for one candidate — primary + retry (A) + transcribe (C) + plain (B).
# Anything still empty after that is a real parse failure (D).
JUDGE_MAX_ATTEMPTS = 4

# 2026-09-07: concurrent judging. The default is a launch-time choice (JudgeLaunchRequest /
# JUDGE_WORKERS, 24); this is the hard cap the deepseek discovery seeds justify with margin
# (c_req 180, k_inflight 18.7M tokens, r_tok 245k tok/s — see the launch schema's note).
JUDGE_DEFAULT_WORKERS = 24
JUDGE_MAX_WORKERS = 100

# 2026-09-08: a candidate whose judge call never got an answer (judge_llm's transport retry
# exhausted on 429 / 5xx / connection errors) is recorded as call_failed and the pass GOES ON
# — the candidate stays unjudged, so a relaunch (resume) picks it up. The first parallel pass
# died at 75/503 on a single upstream 429 because every worker exception was pass-fatal.
# Pass-fatal is reserved for what a retry cannot fix (auth / config / a bug) plus this circuit
# breaker: the provider is down, not blipping, when this many candidates fail back to back.
JUDGE_MAX_CONSECUTIVE_CALL_FAILURES = 8

# §9.1/§9.3: measured against real S3 trajectories (9 files, 5 harnesses, one
# instance — the only real laguna data available as of 2026-09-01). Used for
# the UI's pre-launch cost estimate; the ACTUAL spend is what judge_sampling
# records after the fact, this is a preview, not a bill.
_MEASURED_MEAN_RAW_TOKENS = 116_244
_MEASURED_MEAN_PRUNED_TOKENS = 79_295
_ESTIMATE_OUTPUT_TOKENS = 1_000


def estimate_pass_cost_usd(
    n_candidates: int, prune_mode: str, model_alias: str = "judge-model"
) -> float:
    """A pre-launch estimate (§9.3's own math), not a bill — mirrors
    ceiling_discovery.estimate_cost's role: the number the UI's confirm gate
    shows BEFORE real spend, per §10.5's launch-estimate/confirm pattern."""
    from swebench_eval.gateway.pricing import cost_for

    mean_input = _MEASURED_MEAN_RAW_TOKENS if prune_mode == "full" else _MEASURED_MEAN_PRUNED_TOKENS
    per_call = cost_for(None, mean_input, _ESTIMATE_OUTPUT_TOKENS, model_alias)
    return per_call * n_candidates


@dataclass(frozen=True)
class JudgeCandidate:
    instance_id: str
    attempt_number: int
    harness: str
    outcome: str
    patch_path: str | None
    trajectory_path: str | None
    leaked_node_ids: list[str] | None
    always_judge: bool


@dataclass
class JudgePassSummary:
    pass_id: str
    total_eligible: int
    total_judged: int
    total_skipped_over_budget: int
    total_parse_failed: int
    strata: dict[str, dict[str, int]]
    spend_usd: float = 0.0
    total_call_failed: int = 0  # judge call never answered (transport retries exhausted)
    total_already_judged: int = 0  # skipped: a judge_results row already existed (resume)
    # 2026-09-08 (owner): judge still generating at the 10-min ceiling — recorded as a
    # judgment with no verdict (judge_method "timeout"), counted here, not retried.
    total_timed_out: int = 0
    # 2026-09-08 (owner): "regenerate report" — a pass that judged nothing by design and
    # only re-ran the synthesis over the run's recorded judgments.
    synthesis_only: bool = False
    # 2026-09-08 (owner): the pass-level synthesis — one judge-model call over the digest
    # of every recorded judgment for the run (not only this pass's), stored on the
    # judge_sampling row. None + synthesis_error when it was skipped or failed; the pass
    # is still "done" — the per-attempt judgments are the record, this is the gloss.
    synthesis: str | None = None
    synthesis_cost_usd: float = 0.0
    synthesis_model_resolved: str | None = None
    synthesis_error: str | None = None


JUDGE_TIMEOUT_METHOD = "timeout"


def _timeout_row(
    rubric: Rubric,
    candidate: JudgeCandidate,
    exc: JudgeCallTransportError,
    *,
    prune_mode: str,
    model_alias: str,
) -> tuple[dict[str, Any], list[DimensionScore]]:
    """The judge_results row for a candidate whose judgment never arrived within the
    ceiling: every dimension null with an honest reasoning line, judge_method "timeout",
    no summary, zero cost (the provider bills nothing for an aborted stream). A resume
    treats it as judged; only rejudge=True tries it again."""
    note = (
        f"not judged: the judge call timed out after {JUDGE_CALL_TIMEOUT_S:.0f}s "
        f"(no verdict; {exc})"
    )[:500]
    row: dict[str, Any] = {
        "judge_model_requested": model_alias,
        "judge_model_resolved": resolved_model(object(), model_alias),
        "judge_provider": None,
        "judge_generation_id": None,
        "rubric_version": str(rubric.version),
        "rubric_sha256": rubric.sha256,
        "judge_prompt_version": "1",
        "judge_context_mode": "absent_ids" if candidate.leaked_node_ids is not None else "none",
        "temperature": 0.0,
        "judge_prune_mode": prune_mode,
        "input_tokens": None,
        "output_tokens": None,
        "input_truncated": False,
        "tool_output_pruned": False,
        "events_elided": 0,
        "judge_parse_failed": False,
        "raw_response_s3_key": None,
        "judge_method": JUDGE_TIMEOUT_METHOD,
        "judge_attempts": exc.attempts,
        "scores": {},
        "summary": None,
        "judge_cost_usd": 0.0,
    }
    dims = [
        DimensionScore(
            dimension_id=d.id,
            scale_type=d.scale_type,
            score_numeric=None,
            score_secondary=None,
            flag=None,
            span_start_turn=None,
            span_end_turn=None,
            reasoning=note,
            evidence=[],
            evidence_missing=False,
            missing_reasoning=True,
        )
        for d in rubric.dimensions
    ]
    return row, dims


# The synthesis digest quotes reasoning + one evidence line for EVERY flagged attempt (owner,
# 2026-09-08: the first report's "reasoning was sampled (40/398)" caveat was a bound I chose
# before measuring — a 500-attempt run fully quoted is ~100k tokens, ~$0.01 at the judge
# model's rate, inside its 1M context). The cap stays as a parameter for a run that is
# pathologically large; when it does bite, the digest says so and picks evenly across the
# flagged list, strongest first (deterministic, no RNG).
DIGEST_MAX_EXAMPLES_PER_DIMENSION = 100_000
# Pre-launch estimate shown by the UI's "regenerate report" confirm: the run-6 report over
# 501 attempts cost $0.0034 with sampling; unsampled is ~3x the input. A preview, not a bill.
SYNTHESIS_ESTIMATE_USD = 0.02
DIGEST_REASONING_CHARS = 320
DIGEST_EVIDENCE_CHARS = 160


def _dimension_flagged(d: dict[str, Any]) -> bool:
    """The dimension raised something, by the rubric's own baseline (mirrors the UI's
    lib/judge.ts judgeIssues): likert/count > 0, boolean flag true, ratio >= 1/4 redundant,
    causes at an avoidable share >= 1/4 or severity >= 2."""
    if d.get("evidence_missing"):
        return False
    n = d.get("score_numeric")
    scale = d.get("scale_type")
    if scale == "boolean_with_span":
        return bool(d.get("flag"))
    if scale == "ratio":
        total = d.get("score_secondary")
        return bool(n and total and float(n) / float(total) >= 0.25)
    if scale == "causes":
        sev = d.get("score_secondary")
        return bool((n is not None and float(n) >= 0.25) or (sev is not None and float(sev) >= 2))
    return n is not None and float(n) > 0


def _score_label(d: dict[str, Any]) -> str:
    if d.get("evidence_missing"):
        return "evidence_missing"
    scale = d.get("scale_type")
    if scale == "boolean_with_span":
        return f"flag={d.get('flag')}"
    if scale == "count_and_severity":
        return f"count={_num(d.get('score_numeric'))} severity={_num(d.get('score_secondary'))}"
    if scale == "ratio":
        return f"redundant={_num(d.get('score_numeric'))}/{_num(d.get('score_secondary'))}"
    if scale == "causes":
        causes = ",".join(
            str(c.get("cause")) for c in (d.get("causes") or []) if isinstance(c, dict)
        )
        return (
            f"avoidable={_num(d.get('score_numeric'))} severity={_num(d.get('score_secondary'))}"
            f" causes=[{causes}]"
        )
    return f"score={_num(d.get('score_numeric'))}"


def _causes_bucket(d: dict[str, Any]) -> str:
    """Distribution label for a causes dimension — bucketed by avoidable share."""
    if d.get("evidence_missing"):
        return "evidence_missing"
    n = d.get("score_numeric")
    if n is None:
        return "null"
    share = float(n)
    if share == 0:
        return "avoidable 0%"
    if share < 0.25:
        return "avoidable <25%"
    if share < 0.50:
        return "avoidable 25-50%"
    return "avoidable >=50%"


def _causes_summary(rows: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[str]:
    """Per-cause aggregate over a causes dimension: attempts touched, mean share of the
    waste, and the recommendations the judge gave (deduplicated, most frequent first)."""
    touched: dict[str, int] = {}
    shares: dict[str, list[float]] = {}
    recs: dict[str, dict[str, int]] = {}
    for _r, d in rows:
        if d.get("evidence_missing"):
            continue
        for c in d.get("causes") or []:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("cause"))
            if c.get("label"):
                cid = f"other ({c['label']})"
            touched[cid] = touched.get(cid, 0) + 1
            if isinstance(c.get("share"), (int, float)):
                shares.setdefault(cid, []).append(float(c["share"]))
            rec = c.get("recommendation")
            if isinstance(rec, str) and rec.strip():
                key = " ".join(rec.split())
                recs.setdefault(cid, {})[key] = recs.setdefault(cid, {}).get(key, 0) + 1
    out = []
    for cid, n in sorted(touched.items(), key=lambda kv: -kv[1]):
        s = shares.get(cid) or []
        mean = f", mean share of the waste {100 * sum(s) / len(s):.0f}%" if s else ""
        out.append(f"cause {cid}: {n} attempt(s){mean}")
        for rec, count in sorted(recs.get(cid, {}).items(), key=lambda kv: -kv[1])[:3]:
            out.append(f"  recommendation (x{count}): {rec[:240]}")
    return out


def _wants_profile(rubric: Any) -> bool:
    """True when the rubric has a `causes` dimension (v3+): only then is the efficiency
    profile computed and shown to the judge. Tolerates the bare stand-in rubrics the
    unit tests pass."""
    return any(
        getattr(d, "scale_type", None) == "causes" for d in getattr(rubric, "dimensions", ())
    )


def _profile_summary(results: list[dict[str, Any]]) -> list[str]:
    """The computed efficiency profiles aggregated over the run (medians and totals) — the
    measured picture the report's efficiency section opens with."""
    profiles: list[dict[str, Any]] = [
        p for p in (r.get("efficiency_profile") for r in results) if isinstance(p, dict)
    ]
    if not profiles:
        return []

    def _med(key: str, sub: str | None = None) -> str:
        vals = []
        for p in profiles:
            v = p.get(key)
            if sub is not None and isinstance(v, dict):
                v = v.get(sub)
            if isinstance(v, (int, float)):
                vals.append(float(v))
        if not vals:
            return "n/a"
        vals.sort()
        m = vals[len(vals) // 2]
        return str(int(m)) if m.is_integer() or m > 100 else f"{m:.2f}"

    def _tot(key: str) -> float:
        return sum(float(p[key]) for p in profiles if isinstance(p.get(key), (int, float)))

    n = len(profiles)
    summary_line = (
        f"EFFICIENCY PROFILE SUMMARY (computed, {n} attempt(s) with a profile): per attempt "
        f"median calls {_med('calls')}, median max prompt tokens {_med('prompt_tokens', 'max')}, "
        f"median prompt tokens total {_med('prompt_tokens_total')}, median cost "
        f"${_med('cost_usd')}; unbounded file reads {int(_tot('reads_unbounded'))} across the run "
        f"(median {_med('reads_unbounded')}/attempt), re-reads {int(_tot('repeated_reads'))}, "
        f"duplicate tool calls {int(_tot('duplicate_calls'))}, test re-runs "
        f"{int(_tot('test_reruns'))}, outputs above 20kB {int(_tot('large_outputs'))}; "
        f"median calls after the last edit {_med('calls_after_last_edit')}; total cost after "
        f"the last edit ${_tot('cost_after_last_edit_usd'):.2f} of ${_tot('cost_usd'):.2f}."
    )
    lines = [summary_line]
    hint_counts: dict[str, int] = {}
    for p in profiles:
        for h in p.get("hints") or []:
            hint_counts[str(h)] = hint_counts.get(str(h), 0) + 1
    if hint_counts:
        lines.append(
            "computed hints (attempts whose numbers alone suggest the cause): "
            + ", ".join(f"{k}: {v}" for k, v in sorted(hint_counts.items(), key=lambda kv: -kv[1]))
        )
    return lines


def _ratio_bucket(d: dict[str, Any]) -> str:
    """Distribution label for a ratio dimension — bucketed, else a 500-attempt run lists
    hundreds of distinct a/b fractions."""
    if d.get("evidence_missing"):
        return "evidence_missing"
    n, total = d.get("score_numeric"), d.get("score_secondary")
    if n is None or not total:
        return "null"
    ratio = float(n) / float(total)
    if ratio == 0:
        return "redundant 0%"
    if ratio < 0.10:
        return "redundant <10%"
    if ratio < 0.25:
        return "redundant 10-25%"
    if ratio < 0.50:
        return "redundant 25-50%"
    return "redundant >=50%"


def _num(v: Any) -> str:
    if v is None:
        return "null"
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:.2f}"


def _severity(d: dict[str, Any]) -> float:
    """Sort key: the more severe first, so the quoted examples are the strongest ones."""
    scale = d.get("scale_type")
    if scale == "ratio":
        n, total = d.get("score_numeric"), d.get("score_secondary")
        return float(n) / float(total) if n and total else 0.0
    if scale == "boolean_with_span":
        return 1.0
    if scale == "causes":
        return float(d.get("score_numeric") or 0) + float(d.get("score_secondary") or 0)
    return float(d.get("score_numeric") or 0)


def _evenly(items: list[Any], k: int) -> list[Any]:
    if len(items) <= k:
        return items
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def build_pass_digest(
    results: list[dict[str, Any]],
    rubric: Rubric,
    *,
    run_id: str,
    pass_id: str,
    harness: str | None = None,
    max_examples_per_dimension: int = DIGEST_MAX_EXAMPLES_PER_DIMENSION,
) -> str:
    """The synthesis call's input: a deterministic, bounded text digest of every
    recorded judgment for the run — per dimension the score distribution, every
    flagged attempt's id, and reasoning + one evidence quote for a representative
    subset. Same rows the UI's findings filter reads (fetch_latest_results)."""
    lines: list[str] = []
    timed_out = [r for r in results if r.get("judge_method") == JUDGE_TIMEOUT_METHOD]
    results = [r for r in results if r.get("judge_method") != JUDGE_TIMEOUT_METHOD]
    n = len(results)
    parse_failed = sum(1 for r in results if r.get("judge_parse_failed"))
    pruned = sum(1 for r in results if r.get("tool_output_pruned"))
    truncated = sum(1 for r in results if r.get("input_truncated"))
    lines.append(
        f"JUDGE PASS DIGEST — run {run_id}, pass {pass_id}, harness {harness or 'unknown'}"
    )
    lines.append(
        f"Attempts judged: {n}. Judge responses that failed to parse: {parse_failed}. "
        f"Inputs with tool output pruned: {pruned}; with events elided (truncated): {truncated}."
    )
    if timed_out:
        lines.append(
            f"Attempts the judge could NOT score because its call timed out at the "
            f"{JUDGE_CALL_TIMEOUT_S:.0f}s ceiling ({len(timed_out)}; excluded from every "
            "distribution below, no verdict of any kind for them): "
            + ", ".join(f"{r['instance_id']}/{r['attempt_number']}" for r in timed_out)
        )
    lines.append(
        "Each attempt is identified as <instance_id>/<attempt_number>. Scores follow the rubric "
        "scales: likert 0-3 (0 = nothing found), count_and_severity (count, severity 0-3), "
        "ratio (redundant tool calls / total), boolean_with_span (flag + turn span), causes "
        "(avoidable share 0-1, severity 0-3, classified causes with recommendations)."
    )
    profile_lines = _profile_summary(results)
    if profile_lines:
        lines.append("")
        lines.extend(profile_lines)
    by_dim: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for r in results:
        for d in r.get("dimensions", []):
            by_dim.setdefault(str(d.get("dimension_id")), []).append((r, d))

    for dim in rubric.dimensions:
        rows = by_dim.get(dim.id, [])
        lines.append("")
        lines.append(f"## {dim.id} ({dim.scale_type}) — {' '.join(dim.question.split())}")
        if not rows:
            lines.append("no scores recorded for this dimension")
            continue
        dist: dict[str, int] = {}
        for _, d in rows:
            if dim.scale_type == "ratio":
                label = _ratio_bucket(d)
            elif dim.scale_type == "causes":
                label = _causes_bucket(d)
            else:
                label = _score_label(d)
            dist[label] = dist.get(label, 0) + 1
        lines.append(
            "distribution: "
            + ", ".join(f"{label}: {count}" for label, count in sorted(dist.items()))
        )
        if dim.scale_type == "causes":
            lines.extend(_causes_summary(rows))
        flagged = [(r, d) for r, d in rows if _dimension_flagged(d)]
        demoted = [r for r, d in rows if d.get("evidence_missing")]
        if demoted:
            lines.append(
                f"flagged above baseline but demoted for missing evidence ({len(demoted)}): "
                + ", ".join(f"{r['instance_id']}/{r['attempt_number']}" for r in demoted[:60])
                + (" …" if len(demoted) > 60 else "")
            )
        if not flagged:
            lines.append("flagged attempts: none")
            continue
        flagged.sort(
            key=lambda rd: (-_severity(rd[1]), rd[0]["instance_id"], rd[0]["attempt_number"])
        )
        lines.append(
            f"flagged attempts ({len(flagged)}): "
            + ", ".join(
                f"{r['instance_id']}/{r['attempt_number']} [{_score_label(d)}]" for r, d in flagged
            )
        )
        examples = _evenly(flagged, max_examples_per_dimension)
        lines.append(
            f"judge reasoning for {len(examples)} of them"
            + (
                " (evenly sampled across the list above, strongest first)"
                if len(examples) < len(flagged)
                else ""
            )
            + ":"
        )
        for r, d in examples:
            reasoning = " ".join(str(d.get("reasoning") or "").split())
            if len(reasoning) > DIGEST_REASONING_CHARS:
                reasoning = reasoning[: DIGEST_REASONING_CHARS - 1] + "…"
            quote = ""
            for e in d.get("evidence") or []:
                if isinstance(e, dict) and e.get("quote"):
                    q = " ".join(str(e["quote"]).split())
                    if len(q) > DIGEST_EVIDENCE_CHARS:
                        q = q[: DIGEST_EVIDENCE_CHARS - 1] + "…"
                    quote = f' | evidence turn {e.get("turn", "?")}: "{q}"'
                    break
            lines.append(
                f"- {r['instance_id']}/{r['attempt_number']} [{_score_label(d)}]: "
                f"{reasoning or '(no reasoning recorded)'}{quote}"
            )
    return "\n".join(lines)


SYNTHESIS_HEARTBEAT_S = 10.0
# 2026-09-08: the report call streams (so the gateway never idles it) — which also means
# nothing else bounds it. OpenRouter keeps a stalled upstream "alive" with keep-alive
# comments, so a stuck provider could hold the pass open indefinitely. 20 minutes is the
# wall-clock cap: past it the report is recorded as not written and the pass finishes.
SYNTHESIS_MAX_WALL_S = 1200.0


def _synthesize_pass_findings(
    conn: Any,
    summary: JudgePassSummary,
    live: judge_live.JudgeLiveState,
    *,
    run_id: str,
    rubric: Rubric,
    api_base_url: str,
    raw_api_key: str,
    model_alias: str,
    max_spend_usd: float,
    stop_event: threading.Event | None = None,
) -> None:
    """Fill summary.synthesis* — never raises: the judgments are already written and the
    pass finishes "done" with or without its gloss. Skipped (recorded as such) when the
    ceiling is already reached or there is nothing judged to summarise."""
    try:
        if summary.spend_usd >= max_spend_usd:
            summary.synthesis_error = "skipped: budget ceiling reached before the synthesis call"
            return
        results = fetch_latest_results(conn, run_id)
        if not results:
            summary.synthesis_error = "skipped: no judged attempts to summarise"
            return
        with conn.cursor() as cur:
            cur.execute("SELECT harness FROM run_targets WHERE run_id = %s LIMIT 1", (run_id,))
            row = cur.fetchone()
        harness = row[0] if row else None
        digest = build_pass_digest(
            results, rubric, run_id=run_id, pass_id=summary.pass_id, harness=harness
        )
        live.status = "synthesizing"
        judge_live.write_judge_live(live)
        logger.info(
            "judge pass %s: synthesising %d judged attempt(s) (%d-char digest, %s)",
            summary.pass_id,
            len(results),
            len(digest),
            PASS_SYNTHESIS_PROMPT_VERSION,
        )
        # One call can take minutes; the live snapshot's elapsed/updated_at are computed
        # at WRITE time, so republish it while the call is in flight (owner, 2026-09-08:
        # "the live snapshot froze at 542 s for five minutes") — same TTL'd, best-effort key.
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="judge-synth")
        started = time.monotonic()
        abandoned: str | None = None
        try:
            fut = pool.submit(
                synthesize_pass,
                api_base_url=api_base_url,
                api_key=raw_api_key,
                model_alias=model_alias,
                digest_text=digest,
            )
            while True:
                done, _ = wait([fut], timeout=SYNTHESIS_HEARTBEAT_S)
                if done:
                    break
                if stop_event is not None and stop_event.is_set():
                    abandoned = "skipped: pass stopped by the operator during the report"
                    break
                if time.monotonic() - started > SYNTHESIS_MAX_WALL_S:
                    abandoned = (
                        f"gave up: the report call exceeded the {SYNTHESIS_MAX_WALL_S:.0f}s "
                        "wall-clock cap (stream still open — abandoned)"
                    )
                    break
                judge_live.write_judge_live(live)
        finally:
            # an abandoned stream keeps its thread until the process exits — never wait for it
            pool.shutdown(wait=abandoned is None, cancel_futures=abandoned is not None)
        if abandoned is not None:
            summary.synthesis_error = abandoned
            logger.warning("judge pass %s: %s", summary.pass_id, abandoned)
            return
        result = fut.result()
        if not result.text:
            summary.synthesis_error = "judge returned an empty synthesis"
        else:
            summary.synthesis = result.text
        summary.synthesis_model_resolved = result.model_resolved
        summary.synthesis_cost_usd = float(result.cost_usd or 0.0)
        summary.spend_usd += summary.synthesis_cost_usd
        live.spend_usd = summary.spend_usd
    except Exception as exc:  # noqa: BLE001 — the gloss must never fail the pass
        summary.synthesis_error = f"{type(exc).__name__}: {exc}"[:500]
        logger.warning(
            "judge pass %s: synthesis failed, pass still complete: %s", summary.pass_id, exc
        )


def _derive_outcome(
    verdict: str | None,
    grade_invalid: bool | None,
    harness_error_category: str | None,
    harness_state: str | None,
) -> str:
    if grade_invalid:
        return "invalid"
    if verdict:
        return verdict  # 'resolved' | 'unresolved'
    if harness_error_category:
        return harness_error_category
    return harness_state or "unknown"


_ALWAYS_JUDGE_ERROR_CATEGORIES = frozenset({"HARNESS_STUCK", "HARNESS_PATCH_EXTRACT_TIMEOUT"})


def fetch_candidates(
    conn: Any, run_id: str, instance_ids: list[str] | None = None
) -> list[JudgeCandidate]:
    """§10.1: every candidate carries its eval status (state/verdict/
    grade_invalid) so the UI's instance selector can filter on it — and, per
    §2's Pass A/B link, whether the deterministic detector already fired
    (leaked_node_ids), which feeds `always_judge` below.

    2026-09-09: only attempts WITH a stored trajectory are candidates. An aborted
    run leaves NEVER_DISPATCHED / ABORTED_IN_FLIGHT harness rows with no artifacts
    (the Laguna pilot: 416 of 500); judging those would send an empty trajectory
    to the model — spend for a verdict about nothing. A crash that still uploaded
    its trajectory stays a candidate (patch may be absent — that's judgeable)."""
    with conn.cursor() as cur:
        cur.execute("SELECT harness FROM run_targets WHERE run_id = %s LIMIT 1", (run_id,))
        row = cur.fetchone()
        harness = row[0] if row else "unknown"

        params: list[object] = [run_id]
        instance_filter = ""
        if instance_ids:
            instance_filter = " AND h.instance_id = ANY(%s)"
            params.append(list(instance_ids))

        cur.execute(
            f"""SELECT h.instance_id, h.attempt_number, h.patch_path, h.trajectory_path,
                       h.leaked_node_ids, h.error_category, h.state,
                       e.verdict, e.grade_invalid
                  FROM instance_results h
                  LEFT JOIN instance_results e
                    ON e.run_id = h.run_id AND e.instance_id = h.instance_id
                   AND e.attempt_number = h.attempt_number AND e.phase = 'eval'
                 WHERE h.run_id = %s AND h.phase = 'harness'
                   AND h.trajectory_path IS NOT NULL{instance_filter}
                 ORDER BY h.instance_id, h.attempt_number""",
            params,
        )
        rows = cur.fetchall()

    candidates = []
    for (
        instance_id,
        attempt_number,
        patch_path,
        trajectory_path,
        leaked_node_ids,
        h_error_category,
        h_state,
        verdict,
        grade_invalid,
    ) in rows:
        outcome = _derive_outcome(verdict, grade_invalid, h_error_category, h_state)
        always = (
            bool(leaked_node_ids)
            or bool(grade_invalid)
            or h_error_category in _ALWAYS_JUDGE_ERROR_CATEGORIES
        )
        candidates.append(
            JudgeCandidate(
                instance_id=instance_id,
                attempt_number=attempt_number,
                harness=harness,
                outcome=outcome,
                patch_path=patch_path,
                trajectory_path=trajectory_path,
                leaked_node_ids=leaked_node_ids,
                always_judge=always,
            )
        )
    return candidates


def already_judged_keys(conn: Any, run_id: str) -> set[tuple[str, int]]:
    """(instance_id, attempt_number) pairs that already have a judge_results row for
    *run_id* — the candidates route's "already reviewed" marker and, since 2026-09-08,
    the resume rule's skip set."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT instance_id, attempt_number FROM judge_results WHERE run_id = %s",
            (run_id,),
        )
        return {(r[0], r[1]) for r in cur.fetchall()}


NO_VERDICT_TIMEOUT = "timeout"
NO_VERDICT_PARSE_FAILED = "parse_failed"


def latest_judgment_by_key(conn: Any, run_id: str) -> dict[tuple[str, int], str]:
    """(instance_id, attempt_number) -> how the LATEST judge_results row ended:
    ``"timeout"`` (the call hit the ceiling, no verdict), ``"parse_failed"`` (answered,
    unparseable, no verdict) or ``"judged"``. The resume rule and the candidates route
    read this so the operator can retry the no-verdict rows from the Judge card
    (owner, 2026-09-08: "add the ability to include failed / timed out in the panel")."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (instance_id, attempt_number)
                      instance_id, attempt_number, judge_method, judge_parse_failed
                 FROM judge_results
                WHERE run_id = %s
                ORDER BY instance_id, attempt_number, judged_at DESC""",
            (run_id,),
        )
        out: dict[tuple[str, int], str] = {}
        for instance_id, attempt_number, method, parse_failed in cur.fetchall():
            if method == JUDGE_TIMEOUT_METHOD:
                out[(instance_id, attempt_number)] = NO_VERDICT_TIMEOUT
            elif parse_failed:
                out[(instance_id, attempt_number)] = NO_VERDICT_PARSE_FAILED
            else:
                out[(instance_id, attempt_number)] = "judged"
        return out


def drop_already_judged(
    conn: Any, run_id: str, candidates: list[JudgeCandidate], *, retry_no_verdict: bool = False
) -> list[JudgeCandidate]:
    """Resume semantics (2026-09-08): the candidates without a judge_results row yet. A
    failed / budget-truncated / killed pass is relaunched for the cost of what is left; a
    deliberate re-judge passes ``rejudge=True`` and skips this.

    ``retry_no_verdict``: also keep the candidates whose LATEST judgment has no verdict —
    the call timed out at the ceiling, or the answer failed to parse — so they can be
    tried again (typically at lower concurrency) without re-judging everything."""
    latest = latest_judgment_by_key(conn, run_id)
    kept: list[JudgeCandidate] = []
    for c in candidates:
        status = latest.get((c.instance_id, c.attempt_number))
        retryable = status in (NO_VERDICT_TIMEOUT, NO_VERDICT_PARSE_FAILED)
        if status is None or (retry_no_verdict and retryable):
            kept.append(c)
    return kept


def stratify(
    candidates: list[JudgeCandidate],
    *,
    sample_rate: float = DEFAULT_SAMPLE_RATE,
    min_per_stratum: int = DEFAULT_MIN_PER_STRATUM,
    seed: int | None = None,
) -> tuple[list[JudgeCandidate], dict[str, dict[str, int]]]:
    """§3.2: stratify by (harness, outcome), a floor per stratum, and 100%
    coverage of always_judge candidates regardless of rate. Uniform sampling
    is deliberately NOT what this does — see the design's own Trap 3."""
    rng = random.Random(seed)
    by_stratum: dict[tuple[str, str], list[JudgeCandidate]] = {}
    for c in candidates:
        by_stratum.setdefault((c.harness, c.outcome), []).append(c)

    def key_of(c: JudgeCandidate) -> tuple[str, int]:
        return (c.instance_id, c.attempt_number)

    selected_keys: set[tuple[str, int]] = set()
    selected: list[JudgeCandidate] = []

    for c in candidates:
        if c.always_judge:
            selected.append(c)
            selected_keys.add(key_of(c))

    strata: dict[str, dict[str, int]] = {}
    for (harness, outcome), members in by_stratum.items():
        already = sum(1 for c in members if key_of(c) in selected_keys)
        remaining = [c for c in members if key_of(c) not in selected_keys]
        rng.shuffle(remaining)

        floor = min(min_per_stratum, len(members))
        by_rate = round(len(members) * sample_rate)
        target = max(floor, by_rate) - already
        target = max(0, min(target, len(remaining)))

        picked = remaining[:target]
        selected.extend(picked)
        for c in picked:
            selected_keys.add(key_of(c))

        strata[f"{harness}|{outcome}"] = {
            "eligible": len(members),
            "sampled": already + len(picked),
        }

    return selected, strata


@dataclass
class _CascadeOutcome:
    method: str  # 'primary' | 'retry' | 'transcribe' | 'plain' | 'failed'
    attempts: int  # judge-model calls actually made for this candidate
    winner: Any  # the JudgeCallResult that produced the scores (or the last, on failure)
    dim_scores: list[Any]
    parse_failed: bool
    total_cost: float
    raw_payload: str  # JSON of every attempt's raw response — the §3.6 re-score record


def _judge_with_cascade(
    rubric: Rubric,
    candidate: JudgeCandidate,
    *,
    assembled_text: str,
    api_base_url: str,
    raw_api_key: str,
    model_alias: str,
    spend_remaining: float,
    efficiency_profile_text: str | None = None,
) -> _CascadeOutcome:
    """ADR-0042 recovery cascade for ONE candidate.

    primary → A(retry) → C(transcribe the model's own reasoning) → B(drop the
    json_object constraint) → D(record a real parse failure). Stops at the
    first attempt that yields at least one addressed dimension. Each escalation
    is gated on the remaining pass budget so the §3.10 "checked before every
    call" ceiling still holds across sub-calls, not only across candidates."""
    attempts: list[tuple[str, Any]] = []
    total_cost = 0.0

    def _record(method: str, result: Any) -> list[Any]:
        nonlocal total_cost
        attempts.append((method, result))
        total_cost += result.cost_usd or 0.0
        if result.parsed is None:
            return []
        return parse_all_dimensions(rubric, result.parsed)

    def _budget_left_for_another() -> bool:
        # Refuse to start a further sub-call once this candidate has already
        # consumed the pass's remaining headroom (ceiling checked BEFORE spend).
        return len(attempts) < JUDGE_MAX_ATTEMPTS and total_cost < spend_remaining

    def _outcome(method: str, winner: Any, dims: list[Any], parse_failed: bool) -> _CascadeOutcome:
        payload = json.dumps(
            [
                {
                    "method": m,
                    "raw_response_text": r.raw_response_text,
                    "reasoning_text": r.reasoning_text,
                    "parse_error": r.parse_error,
                    "generation_id": r.generation_id,
                }
                for (m, r) in attempts
            ]
        )
        return _CascadeOutcome(
            method=method,
            attempts=len(attempts),
            winner=winner,
            dim_scores=dims,
            parse_failed=parse_failed,
            total_cost=total_cost,
            raw_payload=payload,
        )

    def _call(force_json: bool) -> Any:
        return call_judge(
            api_base_url=api_base_url,
            api_key=raw_api_key,
            model_alias=model_alias,
            rubric=rubric,
            absent_node_ids=candidate.leaked_node_ids,
            trajectory_text=assembled_text,
            force_json=force_json,
            efficiency_profile=efficiency_profile_text,
        )

    # 1. Primary — the ordinary forced-json_object call.
    primary = _call(force_json=True)
    dims = _record("primary", primary)
    if count_scored_dimensions(dims) > 0:
        return _outcome("primary", primary, dims, parse_failed=False)

    # 2. A — retry the same call. Each independently succeeds ~2/3 of the time.
    if _budget_left_for_another():
        retry = _call(force_json=True)
        dims = _record("retry", retry)
        if count_scored_dimensions(dims) > 0:
            return _outcome("retry", retry, dims, parse_failed=False)

    # 3. C — transcribe the model's own reasoning (already the correct analysis)
    #    into the schema. Highest yield when the failure is serialization, not
    #    judgment. Skipped when no attempt exposed any reasoning to transcribe.
    reasoning = next(
        (r.reasoning_text for (_m, r) in reversed(attempts) if r.reasoning_text.strip()), ""
    )
    if reasoning and _budget_left_for_another():
        trans = transcribe_reasoning(
            api_base_url=api_base_url,
            api_key=raw_api_key,
            model_alias=model_alias,
            rubric=rubric,
            reasoning_text=reasoning,
        )
        dims = _record("transcribe", trans)
        if count_scored_dimensions(dims) > 0:
            return _outcome("transcribe", trans, dims, parse_failed=False)

    # 4. B — drop the json_object constraint (the trigger) and recover the
    #    object from a natural response.
    if _budget_left_for_another():
        plain = _call(force_json=False)
        dims = _record("plain", plain)
        if count_scored_dimensions(dims) > 0:
            return _outcome("plain", plain, dims, parse_failed=False)

    # D — every attempt was empty. A real parse failure, recorded with the raw
    #    responses stored, never coerced into a silent zero-score judgment.
    last = attempts[-1][1]
    return _outcome("failed", last, [], parse_failed=True)


def _run_one(
    rubric: Rubric,
    candidate: JudgeCandidate,
    *,
    run_id: str,
    pass_id: str,
    patch_text: str,
    trajectory_text: str,
    prune_mode: str,
    api_base_url: str,
    raw_api_key: str,
    model_alias: str,
    spend_remaining: float,
    store_artifact: Any = None,
    calls: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[Any], float, bool]:
    """Runs one candidate end to end through the ADR-0042 cascade. Returns
    (judge_results row dict, dimension score rows, cost_usd, parse_failed).
    cost_usd is the TOTAL across every cascade sub-call (real money spent);
    provenance/token columns attribute to the call that produced the scores.

    ``calls`` (rubric v3): the attempt's llm_calls rows in call order — with the
    trajectory they make the computed efficiency profile the judge is shown and
    the row stores. None (ledger unavailable) still yields a profile from the
    trajectory alone, with the token/cost fields null."""
    raw_max_input_tokens = ROTATABLE_MODELS[model_alias].model_info.get("max_input_tokens", 100_000)
    max_input_tokens = (
        int(raw_max_input_tokens) if isinstance(raw_max_input_tokens, (int, float)) else 100_000
    )
    assembled = trajectory_assembly.assemble(
        patch_text,
        trajectory_text,
        prune_mode=prune_mode,
        max_input_tokens=max_input_tokens,
    )

    # rubric v3: the profile is computed only when the rubric asks for it (a `causes`
    # dimension) — an older rubric judges exactly as before, prompt included.
    profile: dict[str, Any] | None = None
    profile_text: str | None = None
    if _wants_profile(rubric):
        try:
            profile = efficiency.build_efficiency_profile(
                efficiency.parse_trajectory_jsonl(trajectory_text), calls
            )
            profile_text = efficiency.render_profile_for_judge(profile)
        except Exception as exc:  # noqa: BLE001 — a profiling bug must not lose the judgment
            logger.warning(
                "judge pass %s: efficiency profile failed for %s/%s: %s",
                pass_id,
                candidate.instance_id,
                candidate.attempt_number,
                exc,
            )
            profile, profile_text = None, None

    outcome = _judge_with_cascade(
        rubric,
        candidate,
        assembled_text=assembled.text,
        api_base_url=api_base_url,
        raw_api_key=raw_api_key,
        model_alias=model_alias,
        spend_remaining=spend_remaining,
        efficiency_profile_text=profile_text,
    )
    winner = outcome.winner

    # §3.6: the raw responses are ALWAYS stored — the only way to re-score a
    # failure or audit a recovered judgment. A storage hiccup degrades to a
    # null key, never a lost pass.
    raw_response_s3_key: str | None = None
    if store_artifact is not None:
        key = f"judge/{run_id}/{pass_id}/{candidate.instance_id}__{candidate.attempt_number}.json"
        try:
            raw_response_s3_key = store_artifact(key, outcome.raw_payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "judge pass %s: raw-response upload failed for %s/%s: %s",
                pass_id,
                candidate.instance_id,
                candidate.attempt_number,
                exc,
            )

    parsed = winner.parsed or {}
    judge_row = {
        "judge_model_requested": winner.model_requested,
        "judge_model_resolved": winner.model_resolved,
        "judge_provider": winner.provider,
        "judge_generation_id": winner.generation_id,
        "rubric_version": str(rubric.version),
        "rubric_sha256": rubric.sha256,
        "judge_prompt_version": "1",
        "judge_context_mode": "absent_ids" if candidate.leaked_node_ids is not None else "none",
        "temperature": 0.0,
        "judge_prune_mode": prune_mode,
        "input_tokens": winner.input_tokens,
        "output_tokens": winner.output_tokens,
        "input_truncated": assembled.input_truncated,
        "tool_output_pruned": assembled.tool_output_pruned,
        "events_elided": assembled.events_elided,
        "judge_parse_failed": outcome.parse_failed,
        "raw_response_s3_key": raw_response_s3_key,
        "judge_method": outcome.method,
        "judge_attempts": outcome.attempts,
        "scores": {} if outcome.parse_failed else parsed.get("dimensions", {}),
        "summary": None if outcome.parse_failed else parsed.get("summary"),
        "judge_cost_usd": outcome.total_cost,
        "efficiency_profile": profile,
    }
    return judge_row, outcome.dim_scores, outcome.total_cost, outcome.parse_failed


def _write_result(
    conn: Any,
    run_id: str,
    candidate: JudgeCandidate,
    judge_row: dict[str, Any],
    dim_scores: list[Any],
) -> None:

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO judge_results
                   (run_id, instance_id, attempt_number,
                    judge_model_requested, judge_model_resolved, judge_provider,
                    judge_generation_id, rubric_version, rubric_sha256, judge_prompt_version,
                    judge_context_mode, temperature, judge_prune_mode,
                    input_tokens, output_tokens, input_truncated, tool_output_pruned,
                    events_elided, judge_parse_failed, raw_response_s3_key,
                    judge_method, judge_attempts, scores, summary, judge_cost_usd,
                    efficiency_profile)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING judged_at""",
            (
                run_id,
                candidate.instance_id,
                candidate.attempt_number,
                judge_row["judge_model_requested"],
                judge_row["judge_model_resolved"],
                judge_row["judge_provider"],
                judge_row["judge_generation_id"],
                judge_row["rubric_version"],
                judge_row["rubric_sha256"],
                judge_row["judge_prompt_version"],
                judge_row["judge_context_mode"],
                judge_row["temperature"],
                judge_row["judge_prune_mode"],
                judge_row["input_tokens"],
                judge_row["output_tokens"],
                judge_row["input_truncated"],
                judge_row["tool_output_pruned"],
                judge_row["events_elided"],
                judge_row["judge_parse_failed"],
                judge_row["raw_response_s3_key"],
                judge_row["judge_method"],
                judge_row["judge_attempts"],
                json.dumps(judge_row["scores"]),
                judge_row["summary"],
                judge_row["judge_cost_usd"],
                (
                    json.dumps(judge_row["efficiency_profile"])
                    if judge_row.get("efficiency_profile") is not None
                    else None
                ),
            ),
        )
        (judged_at,) = cur.fetchone()

        for d in dim_scores:
            causes = getattr(d, "causes", None) or []
            cur.execute(
                """INSERT INTO judge_dimension_scores
                       (run_id, instance_id, attempt_number, judged_at,
                        dimension_id, scale_type, score_numeric, score_secondary,
                        flag, span_start_turn, span_end_turn, reasoning, evidence,
                        evidence_missing, causes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    run_id,
                    candidate.instance_id,
                    candidate.attempt_number,
                    judged_at,
                    d.dimension_id,
                    d.scale_type,
                    d.score_numeric,
                    d.score_secondary,
                    d.flag,
                    d.span_start_turn,
                    d.span_end_turn,
                    d.reasoning,
                    json.dumps(d.evidence),
                    d.evidence_missing,
                    json.dumps(causes) if causes else None,
                ),
            )
    conn.commit()


def fetch_calls_for_attempts(
    conn: Any, run_id: str, keys: list[tuple[str, int]]
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """The llm_calls ledger for the given (instance_id, attempt_number) keys, in call
    order — one query for the whole pass (rubric v3's efficiency profile). Only the
    fields the profile reads; an attempt with no rows is simply absent (profile
    without token figures)."""
    if not keys:
        return {}
    instance_ids = sorted({k[0] for k in keys})
    wanted = set(keys)
    out: dict[tuple[str, int], list[dict[str, Any]]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT instance_id, attempt_number, call_index, input_tokens, cached_tokens,
                      output_tokens, cost_usd
                 FROM llm_calls
                WHERE run_id = %s AND instance_id = ANY(%s)
                ORDER BY instance_id, attempt_number, call_index""",
            (run_id, instance_ids),
        )
        for iid, att, idx, inp, cached, outp, cost in cur.fetchall():
            key = (iid, int(att))
            if key not in wanted:
                continue
            out.setdefault(key, []).append(
                {
                    "call_index": idx,
                    "input_tokens": inp,
                    "cached_tokens": cached,
                    "output_tokens": outp,
                    "cost_usd": float(cost) if cost is not None else None,
                }
            )
    return out


def run_pass(
    conn_factory: Any,
    run_id: str,
    *,
    pass_id: str,
    instance_ids: list[str] | None = None,
    sample_rate: float = DEFAULT_SAMPLE_RATE,
    min_per_stratum: int = DEFAULT_MIN_PER_STRATUM,
    seed: int | None = None,
    prune_mode: str = "pruned",
    model_alias: str = "judge-model",
    max_spend_usd: float,
    rubric: Rubric,
    fetch_artifact: Any,
    store_artifact: Any = None,
    workers: int = 1,
    rejudge: bool = False,
    synthesis_only: bool = False,
    stop_event: threading.Event | None = None,
    retry_no_verdict: bool = False,
) -> JudgePassSummary:
    """The whole Pass B lifecycle for one launch: claim the key mutex, mint
    keys, select+stratify candidates, judge each up to the budget ceiling,
    record the sample, finalise the keys — success, failure, or
    budget-exhaustion all reach finalisation (§10.2 point 5).

    ``workers`` (2026-09-07): candidates judged concurrently (1 = the old
    sequential loop, bit for bit). The gateway cascade runs on worker threads;
    every DB write and every dollar of accounting stays on this thread. The
    pass's live progress is published to Redis as it goes (:mod:`judge_live`).

    ``stop_event`` (2026-09-08, owner: "kill the judge and relaunch with more
    workers"): when set (the task's SIGTERM handler), the pass stops
    submitting, records what has already come back, abandons the in-flight
    calls WITHOUT waiting for them (ECS SIGKILLs the container 30 s after
    SIGTERM — a drain could not finish), writes its ledger row with
    status ``stopped``, and finalises keys + releases the lock. Everything
    judged stays; a relaunch resumes. Before this, a stopped task left the
    per-model lock row behind and the next launch was refused."""
    conn = conn_factory()
    litellm_key_id: str | None = None
    or_hash: str | None = None
    try:
        judge_keys.claim_pass_lock(conn, pass_id)
        try:
            raw_key, litellm_key_id, or_hash = judge_keys.provision_pass_keys(
                pass_id, max_spend_usd
            )

            # 2026-09-08 (owner): "regenerate report" — judge nothing, only re-run the
            # synthesis over what is already recorded. Same key/lock/ledger lifecycle, so
            # its (tiny) spend and provenance are accounted exactly like a judging pass.
            candidates = [] if synthesis_only else fetch_candidates(conn, run_id, instance_ids)
            n_already_judged = 0
            if not rejudge and not synthesis_only:
                fresh = drop_already_judged(
                    conn, run_id, candidates, retry_no_verdict=retry_no_verdict
                )
                n_already_judged = len(candidates) - len(fresh)
                if n_already_judged:
                    logger.info(
                        "judge pass %s: resuming — %d of %d candidate(s) already judged, skipped "
                        "(rejudge=True judges them again)",
                        pass_id,
                        n_already_judged,
                        len(candidates),
                    )
                candidates = fresh
            selected, strata = stratify(
                candidates, sample_rate=sample_rate, min_per_stratum=min_per_stratum, seed=seed
            )

            summary = JudgePassSummary(
                pass_id=pass_id,
                total_eligible=len(candidates),
                total_judged=0,
                total_skipped_over_budget=0,
                total_parse_failed=0,
                strata=strata,
                total_already_judged=n_already_judged,
                synthesis_only=synthesis_only,
            )

            api_base_url = gateway_base_url()
            workers = max(1, min(int(workers), JUDGE_MAX_WORKERS))

            # rubric v3: the call ledger for every selected attempt, fetched once on the
            # main thread (the only DB user) and handed to the workers with the candidate.
            calls_by_key: dict[tuple[str, int], list[dict[str, Any]]] = {}
            if selected and _wants_profile(rubric):
                try:
                    calls_by_key = fetch_calls_for_attempts(
                        conn,
                        run_id,
                        [(c.instance_id, c.attempt_number) for c in selected],
                    )
                except Exception as exc:  # noqa: BLE001 — profile without tokens, not no pass
                    logger.warning(
                        "judge pass %s: llm_calls ledger unavailable (%s) — efficiency "
                        "profiles will carry no token figures",
                        pass_id,
                        exc,
                    )
                    rollback = getattr(conn, "rollback", None)
                    if callable(rollback):
                        rollback()
            live = judge_live.JudgeLiveState(
                run_id=run_id,
                pass_id=pass_id,
                status="running",
                workers=workers,
                selected=len(selected),
                max_spend_usd=max_spend_usd,
                already_judged=n_already_judged,
            )
            judge_live.write_judge_live(live)

            def _judge_candidate(candidate: JudgeCandidate, spend_remaining: float) -> Any:
                """Worker-side: artifacts + assembly + the gateway cascade. NO DB access —
                the pass's one connection is used by the main thread only (2026-09-07:
                single DB writer by design). Returns None when the artifacts could not be
                fetched (skipped, as before), else _run_one's tuple."""
                try:
                    patch_text = (
                        fetch_artifact(candidate.patch_path).decode("utf-8", "replace")
                        if candidate.patch_path
                        else ""
                    )
                    trajectory_text = (
                        fetch_artifact(candidate.trajectory_path).decode("utf-8", "replace")
                        if candidate.trajectory_path
                        else ""
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "judge pass %s: skipping %s/%s (artifact fetch failed): %s",
                        pass_id,
                        candidate.instance_id,
                        candidate.attempt_number,
                        exc,
                    )
                    return None
                return _run_one(
                    rubric,
                    candidate,
                    run_id=run_id,
                    pass_id=pass_id,
                    patch_text=patch_text,
                    trajectory_text=trajectory_text,
                    prune_mode=prune_mode,
                    api_base_url=api_base_url,
                    raw_api_key=raw_key,
                    model_alias=model_alias,
                    spend_remaining=spend_remaining,
                    store_artifact=store_artifact,
                    calls=calls_by_key.get((candidate.instance_id, candidate.attempt_number)),
                )

            # Up to `workers` candidates in flight; the main thread is the ONLY DB writer and
            # the only place spend is accounted. The §3.10 ceiling is checked BEFORE each
            # submission against the spend recorded so far — with N in flight the overshoot
            # is bounded by N calls' cost (cents), and a candidate never starts once the
            # ceiling is reached. A call that never got an answer (JudgeCallTransportError)
            # is a per-candidate failure — counted, the pass goes on, the candidate stays
            # unjudged for a resume; JUDGE_MAX_CONSECUTIVE_CALL_FAILURES of them in a row is
            # the provider being down and aborts. Any OTHER exception (auth, config, a bug)
            # aborts the pass exactly as the sequential loop did (nothing new is submitted,
            # in-flight results are still recorded, the exception propagates to key
            # finalisation).
            queue = list(selected)
            next_idx = 0
            pending: dict[Future[Any], JudgeCandidate] = {}
            failure: BaseException | None = None
            consecutive_call_failures = 0
            stopped = False

            def _stop_requested() -> bool:
                return stop_event is not None and stop_event.is_set()

            def _submit_more(pool: ThreadPoolExecutor) -> None:
                nonlocal next_idx
                while (
                    next_idx < len(queue)
                    and len(pending) < workers
                    and failure is None
                    and not _stop_requested()
                ):
                    candidate = queue[next_idx]
                    next_idx += 1
                    if summary.spend_usd >= max_spend_usd:
                        summary.total_skipped_over_budget += 1
                        live.skipped_over_budget = summary.total_skipped_over_budget
                        continue
                    fut = pool.submit(
                        _judge_candidate, candidate, max_spend_usd - summary.spend_usd
                    )
                    pending[fut] = candidate
                    live.in_flight.append(
                        judge_live.InFlight(
                            candidate.instance_id, candidate.attempt_number, time.time()
                        )
                    )

            def _drop_in_flight(candidate: JudgeCandidate) -> None:
                live.in_flight = [
                    f
                    for f in live.in_flight
                    if (f.instance_id, f.attempt_number)
                    != (candidate.instance_id, candidate.attempt_number)
                ]

            pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="judge")
            try:
                _submit_more(pool)
                judge_live.write_judge_live(live)
                while pending:
                    if _stop_requested():
                        # Abandon the in-flight calls: their threads keep running until the
                        # process dies, but nothing they return is recorded past this point.
                        stopped = True
                        logger.warning(
                            "judge pass %s: stop requested — %d in flight abandoned, %d judged kept",
                            pass_id,
                            len(pending),
                            summary.total_judged,
                        )
                        pending.clear()
                        live.in_flight = []
                        break
                    done, _ = wait(list(pending), timeout=1.0, return_when=FIRST_COMPLETED)
                    for fut in done:
                        candidate = pending.pop(fut)
                        _drop_in_flight(candidate)
                        try:
                            result = fut.result()
                        except JudgeCallTransportError as exc:
                            if exc.timed_out:
                                # 2026-09-08 (owner): still generating at the 10-min ceiling —
                                # a judgment with no verdict is WRITTEN (resume skips it),
                                # the breaker is not touched (slowness is not an outage).
                                row, dims = _timeout_row(
                                    rubric,
                                    candidate,
                                    exc,
                                    prune_mode=prune_mode,
                                    model_alias=model_alias,
                                )
                                _write_result(conn, run_id, candidate, row, dims)
                                summary.total_timed_out += 1
                                live.timed_out = summary.total_timed_out
                                live.last_error = f"{type(exc).__name__}: {exc}"[:300]
                                logger.warning(
                                    "judge pass %s: %s/%s timed out at the %.0fs ceiling — "
                                    "recorded with no verdict",
                                    pass_id,
                                    candidate.instance_id,
                                    candidate.attempt_number,
                                    JUDGE_CALL_TIMEOUT_S,
                                )
                                continue
                            # 2026-09-08: the judge never answered (retries exhausted). Recorded,
                            # the candidate stays unjudged (resume picks it up), the pass goes
                            # on — unless the provider is plainly down (circuit breaker).
                            summary.total_call_failed += 1
                            live.call_failed = summary.total_call_failed
                            live.last_error = f"{type(exc).__name__}: {exc}"[:300]
                            consecutive_call_failures += 1
                            logger.warning(
                                "judge pass %s: %s/%s call failed (%d consecutive): %s",
                                pass_id,
                                candidate.instance_id,
                                candidate.attempt_number,
                                consecutive_call_failures,
                                exc,
                            )
                            if (
                                consecutive_call_failures >= JUDGE_MAX_CONSECUTIVE_CALL_FAILURES
                                and failure is None
                            ):
                                failure = RuntimeError(
                                    f"judge pass {pass_id}: {consecutive_call_failures} "
                                    "consecutive judge calls failed — provider down, aborting "
                                    f"(last: {exc})"
                                )
                                live.last_error = str(failure)[:300]
                                logger.error("%s; draining %d in flight", failure, len(pending))
                            continue
                        except BaseException as exc:  # noqa: BLE001 — recorded, then re-raised
                            if failure is None:
                                failure = exc
                                live.last_error = f"{type(exc).__name__}: {exc}"[:300]
                                logger.error(
                                    "judge pass %s: %s/%s raised — no new candidates will "
                                    "start; draining %d in flight",
                                    pass_id,
                                    candidate.instance_id,
                                    candidate.attempt_number,
                                    len(pending),
                                )
                            continue
                        consecutive_call_failures = 0
                        if result is None:
                            live.skipped_artifacts += 1
                            continue
                        judge_row, dim_scores, cost, parse_failed = result
                        _write_result(conn, run_id, candidate, judge_row, dim_scores)
                        summary.total_judged += 1
                        summary.spend_usd += cost
                        if parse_failed:
                            summary.total_parse_failed += 1
                        live.judged = summary.total_judged
                        live.spend_usd = summary.spend_usd
                        live.parse_failed = summary.total_parse_failed
                    _submit_more(pool)
                    judge_live.write_judge_live(live)
                # A stop that landed while the last in-flight batch was being recorded
                # (nothing left pending, queue not exhausted) is still a stop.
                if _stop_requested() and next_idx < len(queue):
                    stopped = True
            finally:
                # A stop must not wait for the abandoned calls (they can take minutes);
                # a normal end drains as before.
                pool.shutdown(wait=not stopped, cancel_futures=stopped)

            if failure is not None:
                live.status = "failed"
                live.finished_at = time.time()
                judge_live.write_judge_live(live)
                raise failure
            if stopped:
                summary.synthesis_error = "skipped: pass stopped by the operator"
                live.status = "stopped"
                live.finished_at = time.time()
                judge_live.write_judge_live(live)
            # Candidates never submitted because the ceiling was reached are skipped-over-
            # budget too (the sequential loop counted every remaining candidate that way).
            while next_idx < len(queue):
                next_idx += 1
                summary.total_skipped_over_budget += 1
            live.skipped_over_budget = summary.total_skipped_over_budget

            # 2026-09-08 (owner): the pass-level report — one more call under the same
            # per-pass key (still minted here), over every recorded judgment for the run.
            # Never fatal; recorded on the judge_sampling row with the counts.
            if not stopped:
                _synthesize_pass_findings(
                    conn,
                    summary,
                    live,
                    run_id=run_id,
                    rubric=rubric,
                    api_base_url=api_base_url,
                    raw_api_key=raw_key,
                    model_alias=model_alias,
                    max_spend_usd=max_spend_usd,
                    stop_event=stop_event,
                )

            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO judge_sampling
                           (run_id, pass_id, requested_rate, seed, strata_json,
                            total_eligible, total_judged, total_skipped_over_budget,
                            total_parse_failed, litellm_key_id, openrouter_key_hash,
                            synthesis, synthesis_cost_usd, synthesis_model_resolved,
                            synthesis_error, synthesis_prompt_version, synthesis_only)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s)""",
                    (
                        run_id,
                        pass_id,
                        sample_rate,
                        seed,
                        json.dumps(summary.strata),
                        summary.total_eligible,
                        summary.total_judged,
                        summary.total_skipped_over_budget,
                        summary.total_parse_failed,
                        litellm_key_id,
                        or_hash,
                        summary.synthesis,
                        summary.synthesis_cost_usd,
                        summary.synthesis_model_resolved,
                        summary.synthesis_error,
                        PASS_SYNTHESIS_PROMPT_VERSION,
                        summary.synthesis_only,
                    ),
                )
            conn.commit()
            live.status = "stopped" if stopped else "done"
            live.finished_at = time.time()
            judge_live.write_judge_live(live)
            return summary
        finally:
            judge_keys.finalize_pass_keys(pass_id, litellm_key_id, or_hash)
            judge_keys.release_pass_lock(conn, pass_id)
    finally:
        conn.close()


def fetch_latest_results(conn: Any, run_id: str) -> list[dict[str, Any]]:
    """Reads the LATEST judge_results row per (instance_id, attempt_number)
    — judged_at is part of the PK precisely so a re-judge is a new row, not
    an overwrite (§4/§10.6); the UI shows the latest by default. Each row
    carries its judge_dimension_scores (§10.7), fetched in ONE query for the run and
    grouped here — not one round trip per instance."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (instance_id, attempt_number)
                      instance_id, attempt_number, judged_at, judge_model_resolved,
                      rubric_version, judge_prune_mode, input_truncated, tool_output_pruned,
                      judge_parse_failed, judge_method, judge_attempts, summary, judge_cost_usd,
                      efficiency_profile
                 FROM judge_results
                WHERE run_id = %s
                ORDER BY instance_id, attempt_number, judged_at DESC""",
            (run_id,),
        )
        latest_rows = cur.fetchall()

        # 2026-09-09 query audit: this used to issue one judge_dimension_scores query PER latest
        # row (501 statements per request on a 500-attempt pass, polled every 15 s). One query
        # for the run, grouped in Python by the (instance, attempt, judged_at) key.
        cur.execute(
            """SELECT instance_id, attempt_number, judged_at,
                      dimension_id, scale_type, score_numeric, score_secondary, flag,
                      span_start_turn, span_end_turn, reasoning, evidence, evidence_missing,
                      causes
                 FROM judge_dimension_scores
                WHERE run_id = %s
                ORDER BY instance_id, attempt_number, judged_at, dimension_id""",
            (run_id,),
        )
        dims_by_key: dict[tuple[Any, Any, Any], list[tuple[Any, ...]]] = {}
        for row in cur.fetchall():
            dims_by_key.setdefault((row[0], row[1], row[2]), []).append(row[3:])

        results = []
        for (
            instance_id,
            attempt_number,
            judged_at,
            model_resolved,
            rubric_version,
            prune_mode,
            input_truncated,
            tool_output_pruned,
            parse_failed,
            judge_method,
            judge_attempts,
            summary,
            cost_usd,
            efficiency_profile,
        ) in latest_rows:
            dims = []
            for d in dims_by_key.get((instance_id, attempt_number, judged_at), []):
                evidence = _jsonb(d[8]) or []
                causes = _jsonb(d[10]) or []
                dims.append(
                    {
                        "dimension_id": d[0],
                        "scale_type": d[1],
                        "score_numeric": d[2],
                        "score_secondary": d[3],
                        "flag": d[4],
                        "span_start_turn": d[5],
                        "span_end_turn": d[6],
                        "reasoning": d[7],
                        "evidence": evidence if isinstance(evidence, list) else [],
                        "evidence_missing": d[9],
                        "causes": causes if isinstance(causes, list) else [],
                    }
                )
            results.append(
                {
                    "instance_id": instance_id,
                    "attempt_number": attempt_number,
                    "judged_at": judged_at.isoformat(),
                    "judge_model_resolved": model_resolved,
                    "rubric_version": rubric_version,
                    "judge_prune_mode": prune_mode,
                    "input_truncated": input_truncated,
                    "tool_output_pruned": tool_output_pruned,
                    "judge_parse_failed": parse_failed,
                    "judge_method": judge_method,
                    "judge_attempts": judge_attempts,
                    "summary": summary,
                    "judge_cost_usd": cost_usd,
                    "dimensions": dims,
                    "efficiency_profile": (
                        _jsonb(efficiency_profile) if efficiency_profile is not None else None
                    ),
                }
            )
    return results


def _jsonb(value: Any) -> Any:
    """psycopg2 without a jsonb typecaster hands JSONB back as text — decode either way."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def fetch_pass_summaries(conn: Any, run_id: str) -> list[dict[str, Any]]:
    """Every judge_sampling row for a run, newest first (BUILDER3-JUDGE-
    VERIFIED-AND-UI-BRIEF-2026-09-02.md §3.2): `run_pass` already records
    total_eligible/total_judged/total_skipped_over_budget/total_parse_failed
    per pass, but until this function existed nothing ever read them back —
    a pass truncated by the budget ceiling rendered identically to a
    complete one. The UI's job is `judged N of M eligible — K skipped at
    the $X ceiling`, per §5 point 1 of that review; this is its data source."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT pass_id, requested_rate, seed, total_eligible, total_judged,
                      total_skipped_over_budget, total_parse_failed, created_at,
                      synthesis, synthesis_cost_usd, synthesis_model_resolved,
                      synthesis_error, synthesis_only
                 FROM judge_sampling
                WHERE run_id = %s
                ORDER BY created_at DESC""",
            (run_id,),
        )
        return [
            {
                "pass_id": pass_id,
                "requested_rate": float(requested_rate) if requested_rate is not None else None,
                "seed": seed,
                "total_eligible": total_eligible,
                "total_judged": total_judged,
                "total_skipped_over_budget": total_skipped_over_budget,
                "total_parse_failed": total_parse_failed,
                "created_at": created_at.isoformat(),
                "synthesis": synthesis,
                "synthesis_cost_usd": (
                    float(synthesis_cost_usd) if synthesis_cost_usd is not None else None
                ),
                "synthesis_model_resolved": synthesis_model_resolved,
                "synthesis_error": synthesis_error,
                "synthesis_only": bool(synthesis_only),
            }
            for (
                pass_id,
                requested_rate,
                seed,
                total_eligible,
                total_judged,
                total_skipped_over_budget,
                total_parse_failed,
                created_at,
                synthesis,
                synthesis_cost_usd,
                synthesis_model_resolved,
                synthesis_error,
                synthesis_only,
            ) in cur.fetchall()
        ]
