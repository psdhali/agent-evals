"""Offline growth-curve / latency / period / survival refit over real ``llm_calls.jsonl``
artifacts.

Exact-design §10 step 4 (original spec §3): zero-risk, read-only analysis that turns recorded
calls into the per-(pool, harness) constants the L2 planner's DemandModel consumes — replacing
its pooled defaults (``curve_source=pooled_default``) with a fitted, provenance-carrying JSON
artifact. Re-run after every run so the curves self-correct; nothing here touches the network,
the gateway, or any live system.

Usage:
    python scripts/fit_growth_curves.py <dir-with-llm_calls.jsonl-files...> [-o curves.json]

Outputs one JSON document:
    {"fitted_at": ..., "n_calls": ..., "groups": {"<pool>|<harness>": {...}}, "pooled": {...}}

Fitting notes — revised 2026-09-03 (BUILDER4-DISPATCHER-FORECAST-REVIEW-2026-09-03.md §1)
after the first refit over 5,511 real calls exposed three modelling errors in the original:

- **Prompt size, not input+output.** The pacer meters the PROMPT (request bytes / κ). The
  Anthropic wire shape reports cache reads OUTSIDE ``input_tokens`` (claude_code: median input
  388, cached 57,088), the OpenAI shape inside it — so ``prompt = input + cached`` when cached
  exceeds input, else ``input``. Fitting input+output made claude_code look like a 2K-token
  harness with negative growth. Output tokens are ~1% of the prompt and drive LATENCY instead.
- **Latency is a function of OUTPUT tokens** (corr 0.97 on the real data; 0.12 vs input): the
  prompts are 90%+ cache hits, so prefill is not the cost — generation is. Fitted as
  ``latency = a + b x output_tokens``; the planner uses the constant at the group's median
  output. (The cold cache-MISS max-context latency discovery measures is a different regime and
  is seeded into ``pacer:cfg`` separately, not fitted here.)
- **Turn period is measured directly** as the median inter-call gap, never reconstructed as
  latency + tool time (that decomposition produced negative slopes and 0.0 tool times).
- Groups key on the shared POOL alias (``rotatable_models.pool_alias_for``): the per-harness
  rotatable aliases (``laguna-xs-2.1-codex``) and the older plain alias are the same provider
  pool and must pool their evidence.
- Least squares on (call_index, prompt); groups with < 30 calls are reported but flagged
  ``insufficient``; survival per 20-turn window is reported with the number of attempts it rests
  on (``survival_attempts``) — the consumer's fallback chain decides what is enough, and must
  record the level it used.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

MIN_CALLS_PER_GROUP = 30
SURVIVAL_WINDOW_TURNS = 20
MIN_ATTEMPTS_FOR_SURVIVAL_CLAIM = 5
MIN_GAPS_FOR_PERIOD = 10


def _lstsq(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Closed-form least squares (a, b) for y = a + b*x. No numpy — stdlib only."""
    n = len(xs)
    if n < 2:
        return (ys[0] if ys else 0.0), 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return my, 0.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return my - b * mx, b


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def prompt_tokens(rec: dict[str, Any]) -> float:
    """The prompt the pacer meters. Anthropic reports cache reads outside ``input_tokens``
    (cached > input); the OpenAI shape folds them in (cached <= input)."""
    inp = float(rec.get("input_tokens") or 0)
    cached = float(rec.get("cached_tokens") or 0)
    return inp + cached if cached > inp else inp


def pool_alias(model: str) -> str:
    """Collapse a per-harness rotatable alias onto its provider pool (the evidence pools)."""
    try:
        from swebench_eval.gateway.rotatable_models import pool_alias_for

        return pool_alias_for(model) or model
    except Exception:  # noqa: BLE001 — the script must also run outside the package env
        return model


def load_calls(paths: list[Path]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for root in paths:
        files = [root] if root.is_file() else sorted(root.rglob("llm_calls.jsonl"))
        for f in files:
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Only completed model calls carry the fit inputs; refusals/429s have no usage
                # (Trap 3: absent, never zero) and drop out here naturally.
                if rec.get("input_tokens") is not None and rec.get("call_index") is not None:
                    calls.append(rec)
    return calls


def fit_group(calls: list[dict[str, Any]]) -> dict[str, Any]:
    xs = [float(c["call_index"]) for c in calls]
    ys = [prompt_tokens(c) for c in calls]
    a, b = _lstsq(xs, ys)

    # F3 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): the share of the group's prompt tokens
    # the provider served from its cache — the planner discounts this share of every projected
    # prompt by the pool's measured cached_weight. Token-weighted (Σcached / Σprompt), so a
    # long-context tail dominates the way it dominates the pacer's ledger.
    prompt_total = sum(ys)
    cached_total = sum(float(c.get("cached_tokens") or 0) for c in calls)
    cached_share = min(1.0, cached_total / prompt_total) if prompt_total > 0 else 0.0

    # Latency vs OUTPUT tokens (the measured driver); the planner's constant is the line at
    # the group's median output. Slope clamped at 0 — a negative slope is noise, never a model.
    lat_pairs = [
        (float(c.get("output_tokens") or 0), float(c["latency_ms"]) / 1000.0)
        for c in calls
        if c.get("latency_ms")
    ]
    lat_a, lat_b = _lstsq([p[0] for p in lat_pairs], [p[1] for p in lat_pairs])
    lat_b = max(0.0, lat_b)
    median_out = _median([p[0] for p in lat_pairs])
    latency_s = (lat_a + lat_b * median_out) if median_out is not None else None
    if latency_s is not None:
        latency_s = max(0.1, latency_s)

    # Survival per 20-turn window, per instance attempt: max call_index per attempt.
    max_turn: dict[tuple[str, str, Any], int] = {}
    for c in calls:
        key = (str(c.get("run_id")), str(c.get("instance_id")), c.get("attempt_number"))
        max_turn[key] = max(max_turn.get(key, 0), int(c["call_index"]))
    survival: dict[str, float] = {}
    survival_attempts: dict[str, int] = {}
    maxes = list(max_turn.values())
    for start in range(0, 200, SURVIVAL_WINDOW_TURNS * 2):
        reached = sum(1 for m in maxes if m >= start)
        survived = sum(1 for m in maxes if m >= start + SURVIVAL_WINDOW_TURNS)
        if reached >= MIN_ATTEMPTS_FOR_SURVIVAL_CLAIM:  # too few = no claim, never a fabricated 1.0
            survival[str(start)] = round(survived / reached, 3)
            survival_attempts[str(start)] = reached

    # Turn period: the median inter-call wall-clock gap, measured directly.
    gaps: list[float] = []
    by_attempt: dict[tuple[str, str, Any], list[dict[str, Any]]] = defaultdict(list)
    for c in calls:
        by_attempt[
            (str(c.get("run_id")), str(c.get("instance_id")), c.get("attempt_number"))
        ].append(c)
    for group in by_attempt.values():
        group.sort(key=lambda c: c["call_index"])
        for prev, cur in itertools.pairwise(group):
            try:
                gap = (
                    datetime.fromisoformat(cur["started_at"])
                    - datetime.fromisoformat(prev["started_at"])
                ).total_seconds()
            except (KeyError, ValueError, TypeError):
                continue
            if 0 < gap < 600:
                gaps.append(gap)
    turn_period_s = _median(gaps) if len(gaps) >= MIN_GAPS_FOR_PERIOD else None
    tool_time = (
        max(0.0, turn_period_s - latency_s)
        if turn_period_s is not None and latency_s is not None
        else None
    )

    return {
        "a": round(a, 1),
        "b": round(b, 3),
        "n": len(calls),
        "insufficient": len(calls) < MIN_CALLS_PER_GROUP,
        "latency_intercept_s": round(lat_a, 3),
        "latency_per_output_token_s": round(lat_b, 6),
        "median_output_tokens": round(median_out, 1) if median_out is not None else None,
        "latency_s": round(latency_s, 3) if latency_s is not None else None,
        "turn_period_s": round(turn_period_s, 2) if turn_period_s is not None else None,
        "n_gaps": len(gaps),
        "median_tool_time_s": round(tool_time, 2) if tool_time is not None else None,
        "survival": survival,
        "survival_attempts": survival_attempts,
        "cached_share": round(cached_share, 3),
    }


def group_key(rec: dict[str, Any]) -> str:
    return f"{pool_alias(str(rec.get('model_requested', '?')))}|{rec.get('harness', '?')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("-o", "--out", type=Path, default=Path("growth_curves.json"))
    args = parser.parse_args(argv)

    calls = load_calls(args.paths)
    if not calls:
        print("no usable calls found", file=sys.stderr)
        return 1

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in calls:
        groups[group_key(c)].append(c)

    fitted = {k: fit_group(v) for k, v in sorted(groups.items())}
    doc = {
        "fitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_calls": len(calls),
        "prompt_field": "input_tokens + cached_tokens when cached > input (Anthropic), else input",
        "groups": fitted,
        "pooled": fit_group(calls),
    }
    args.out.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {args.out} — {len(calls)} calls, {len(groups)} groups")
    for k, g in fitted.items():
        flag = " (insufficient)" if g["insufficient"] else ""
        print(
            f"  {k}: prompt ≈ {g['a']:.0f} + {g['b']:.1f}·turn, latency {g['latency_s']}s, "
            f"period {g['turn_period_s']}s, n={g['n']}{flag}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
