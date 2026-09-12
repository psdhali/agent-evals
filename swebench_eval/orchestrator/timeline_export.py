"""The site-side timeline files — pure assembly over ``GET /runs/{id}/timeline`` + ``/export``.

dev/LIVE-RUN-TIMELINE-SITE-DATA-CONTRACT-AND-BUILD-PLAN-2026-09-04.md §2 is the contract;
this module builds it EXACTLY and ``scripts/export_run_timeline.py`` writes it. Everything
here is pure (dicts in, dicts out) so the shape is unit-testable without a database or a
tunnel, and the fixtures builder 2 builds against come from THIS code, not from hand.

The rules that shape every function:

  * ``t`` is integer seconds since ``window.start``; series are columnar arrays of equal
    length; an idle tick is absent from ``t`` (a gap, never a zero row).
  * Every number is nullable and ``None`` is preserved through decimation — a bucket whose
    values are all None stays None.
  * Scrubbing is allowlist-first (the persisted decision records are pruned to known keys
    BEFORE anything is serialised) and denylist-second (a regex pass over the final bytes
    that FAILS the export on any hit). ``manifest.scrub.hits`` is 0 or the files do not exist.
  * Idempotent: the same input produces byte-identical files (sorted keys, fixed separators,
    the exporter passes a fixed ``exported_at``).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, cast

from swebench_eval.orchestrator.export import _ABORT_STATES, _terminated_reason

SCHEMA_VERSION = 1
TARGET_POINTS = 720
LANE_SAMPLES_MAX = 40

BINDING_VOCAB = [
    "queue_empty",
    "arrival_budget",
    "inflight_budget",
    "qps_budget",
    "at_capacity",
    "ecs_quota",
    "paused",
    "stalled",
    "ramp_limited",
    "cooldown",
    "paced",
    "ceiling_override",
    "at_max_workers",
    "no_hosts",
    "asg_at_max",
    "scale_in_damped",
    "scale_in_blocked_busy",
    "none",
]
LANE_OUTCOMES = [
    "resolved",
    "unresolved",
    "grade_invalid",
    "empty_patch",
    "error",
    "aborted",
    "in_flight_at_export",
]
EVENT_KINDS = [
    "launch",
    "dispatched",
    "pause",
    "resume",
    "limit_edit",
    "abort_requested",
    "abort_settled",
    "finalised",
    "discovery_step",
    "ceiling_change",
    "binding_change",
]

# Keys the persisted planner decision records may carry that name infrastructure — dropped
# wholesale, at any depth, before anything else looks at the record.
DROPPED_KEYS = frozenset(
    {
        "cluster",
        "subnets",
        "security_groups",
        "tasks",
        "task_arns",
        "command",
        "environment",
        "network",
        "name",
        "notes",
        "hosts",
        "instances",
    }
)
# Keys kept from a decision record (planner + eval scaler). Anything not listed is dropped —
# a new field must be added here deliberately, never leaked by default.
ALLOWED_DECISION_KEYS = frozenset(
    {
        "decided_at",
        "mode",
        "desired_ceiling",
        "binding_constraint",
        "ceiling",
        "ceiling_override",
        "static_cap",
        "max_parallel",
        "operator_limits",
        "at_concurrency",
        "arrival_tok_s",
        "basis_tok_s",
        "inflight_tok",
        "qps",
        "r_tok",
        "r_tok_seed",
        "r_qps",
        "r_qps_seed",
        "k_inflight",
        "k_inflight_seed",
        "cached_share",
        "cached_weight",
        "curve_source",
        "curve_borrowed",
        "survival_discount",
        "survival_attempts",
        "budgets",
        "budgets_source",
        "source",
        "booting",
        "applied",
        "binding",
        "measured",
        "model_alias",
        "overloads",
        "paced_share",
        "paced_timeouts",
        "queue_len",
        "queue_head_wait_s",
        "seeded_at",
        "aliases",
        "per_alias",
        "pools",
        "hold_cap_timeouts",
        "in_flight",
        "running",
        "max_workers",
        "desired",
        "scale_in_ticks",
        "scale_in_ticks_needed",
        "tick_interval_s",
    }
)

_SCRUB_PATTERNS: dict[str, re.Pattern[str]] = {
    # A bare 12-digit run. The second lookbehind exempts the last group of a UUID
    # (`…-5555-666677778888`): a judge quoting Django's admin test fixtures refused the whole
    # mini-swe-agent export on one (2026-09-10). Real account ids do not follow `-xxxx-`.
    "aws_account_id": re.compile(r"(?<![\w.])(?<!-[0-9a-fA-F]{4}-)\d{12}(?![\w.])"),
    "arn": re.compile(r"arn:aws"),
    # A 32-hex ECS task id as it appears in the wild: after a slash — task ARNs
    # (`task/<cluster>/<id>`) and log stream names (`<prefix>/<name>/<id>`). Bare 32-hex runs are
    # MD5s: a judge quoting an HTTP Digest `response="c549dd…"` from a Django test refused the
    # OpenCode re-run export (2026-09-11). Task-id FIELDS are dropped by key, not by this pattern.
    "ecs_task_id": re.compile(r"(?<=/)[0-9a-f]{32}(?!\w)"),
    "subnet": re.compile(r"\b(subnet|vpc|sg|vpce|nat)-[0-9a-f]{8,}"),
    # VPC-internal names (gateway.eval.internal, ip-10-0-1-2.us-west-2.compute.internal,
    # *.ec2.internal) and AWS endpoints. Anchored on the DNS zone so that a code identifier
    # such as `UUIDField.internal` in a judge's reasoning is not a hit (2026-09-10).
    "hostname": re.compile(
        r"\b[\w-]+(?:\.[\w-]+)*\.(?:eval|compute|ec2)\.internal\b|\b[\w.-]+\.amazonaws\.com\b"
    ),
    "key_id": re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
}


class ScrubError(RuntimeError):
    """A denylist pattern matched the serialised output — the export must not be written."""


# ── time ──────────────────────────────────────────────────────────────────────────────────


def parse_iso(value: Any) -> float | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def to_t(value: Any, start: float) -> int | None:
    epoch = parse_iso(value)
    return round(epoch - start) if epoch is not None else None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── scrub ─────────────────────────────────────────────────────────────────────────────────


def prune_decision(obj: Any) -> Any:
    """Allowlist walk over a decision record: unknown keys are dropped, DROPPED_KEYS are
    dropped at any depth, scalars pass through."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if ks in DROPPED_KEYS:
                continue
            if ks in ALLOWED_DECISION_KEYS or _looks_like_alias(ks):
                out[ks] = prune_decision(v)
        return out
    if isinstance(obj, list):
        return [prune_decision(v) for v in obj]
    return obj


def _looks_like_alias(key: str) -> bool:
    """Per-alias sub-records are keyed by the alias itself (``deepseek-v4-flash-0731-mini``)."""
    return "-" in key and key.replace("-", "").replace("_", "").replace(".", "").isalnum()


def scrub_hits(text: str) -> list[str]:
    hits: list[str] = []
    for name, pattern in _SCRUB_PATTERNS.items():
        m = pattern.search(text)
        if m:
            hits.append(f"{name}:{m.group(0)[:24]}")
    return hits


# ── fleet series ──────────────────────────────────────────────────────────────────────────

_TICK_SERIES = (
    "in_flight",
    "stale",
    "pending",
    "harness_running",
    "eval_running",
    "resolved",
    "unresolved",
    "aborted",
    "expected",
    "denominator",
    "tok_in",
    "tok_out",
    "tok_cached",
    "tok_reasoning",
    "cost_usd_live",
    "harness_paused",
    "eval_paused",
    "gateway_paused",
    "run_status",
)


def _bucket(t: int, interval: int) -> int:
    return round(t / interval)


def _flatten_planner(record: dict[str, Any], prefix: str, out: dict[str, Any]) -> None:
    """Scalars of a pruned decision record, one level of per-alias nesting, as
    ``planner.<key>`` / ``planner.<alias>.<key>`` series."""
    for k, v in record.items():
        if isinstance(v, (int, float, str, bool)) or v is None:
            out[f"{prefix}.{k}"] = v
        elif isinstance(v, dict) and k in ("aliases", "per_alias", "pools", "budgets"):
            for ak, av in v.items():
                if isinstance(av, dict):
                    for sk, sv in av.items():
                        if isinstance(sv, (int, float, str, bool)) or sv is None:
                            out[f"{prefix}.{ak}.{sk}"] = sv
                elif isinstance(av, (int, float, str, bool)) or av is None:
                    out[f"{prefix}.{k}.{ak}"] = av


def build_fleet_rows(timeline: dict[str, Any], start: float, interval: int) -> list[dict[str, Any]]:
    """Merge the run's ticks and both pools' capacity rows onto one ``t`` grid (bucketed to
    the tick interval — the two are written by the same thread seconds apart). One dict per
    bucket, every series key present, None where not measured."""
    rows: dict[int, dict[str, Any]] = {}

    def row_for(t: int) -> dict[str, Any]:
        b = _bucket(t, interval)
        return rows.setdefault(b, {"t": b * interval})

    for tick in timeline.get("ticks") or []:
        t = to_t(tick.get("ts"), start)
        if t is None:
            continue
        r = row_for(t)
        for k in _TICK_SERIES:
            r[k] = tick.get(k)
        pacer = tick.get("pacer") or {}
        if isinstance(pacer, dict):
            for alias, block in pacer.items():
                if isinstance(block, dict):
                    for f, v in block.items():
                        r[f"pacer.{alias}.{f}"] = v
    for cap in timeline.get("capacity") or []:
        t = to_t(cap.get("ts"), start)
        if t is None:
            continue
        r = row_for(t)
        pool = cap.get("pool")
        if pool == "harness":
            r["desired_ceiling"] = cap.get("desired")
            r["binding_constraint"] = cap.get("binding_constraint")
            r["harness_workers"] = cap.get("current_workers")
            r["ceiling_utilization"] = cap.get("ceiling_utilization")
            r["queue_harness_visible"] = cap.get("queue_depth")
            r["queue_harness_not_visible"] = cap.get("not_visible")
            r["pacer_queue_len_fleet"] = cap.get("pacer_queue_len")
            r["paced_over_2s_share"] = cap.get("paced_over_2s_share")
            r["constants_source"] = cap.get("constants_source")
            r["decision_age_s"] = cap.get("decision_age_s")
            dec = cap.get("decision")
            if isinstance(dec, dict):
                pruned = prune_decision(dec)
                _flatten_planner(pruned, "planner", r)
                r["ceiling_override"] = pruned.get("ceiling_override")
                r["static_cap"] = pruned.get("static_cap", pruned.get("max_parallel"))
        elif pool == "eval":
            r["eval_workers"] = cap.get("current_workers")
            r["eval_desired"] = cap.get("desired")
            r["eval_binding"] = cap.get("binding_constraint")
            r["queue_eval_visible"] = cap.get("queue_depth")
            r["queue_eval_not_visible"] = cap.get("not_visible")
    ordered = [rows[b] for b in sorted(rows)]
    # Derived rates between consecutive rows; None across a gap (> 2 intervals) or when a
    # side is unmeasured.
    prev: dict[str, Any] | None = None
    for r in ordered:
        r["offered_tok_s"] = None
        r["usd_per_min"] = None
        if prev is not None and r["t"] - prev["t"] <= 2 * interval:
            dt = r["t"] - prev["t"]
            a, b = prev.get("tok_in"), r.get("tok_in")
            c, d = prev.get("tok_out"), r.get("tok_out")
            if (
                dt > 0
                and isinstance(a, (int, float))
                and isinstance(b, (int, float))
                and isinstance(c, (int, float))
                and isinstance(d, (int, float))
            ):
                r["offered_tok_s"] = round(((b - a) + (d - c)) / dt, 1)
            e, f = prev.get("cost_usd_live"), r.get("cost_usd_live")
            if dt > 0 and isinstance(e, (int, float)) and isinstance(f, (int, float)):
                r["usd_per_min"] = round((float(f) - float(e)) / dt * 60.0, 5)
        prev = r
    return ordered


def derive_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ceiling_change / binding_change from the UNDECIMATED rows — a one-tick excursion
    must survive decimation as a marker."""
    events: list[dict[str, Any]] = []
    last_ceiling: Any = None
    last_binding: Any = None
    seen_ceiling = seen_binding = False
    for r in rows:
        c = r.get("desired_ceiling")
        if c is not None:
            if seen_ceiling and c != last_ceiling:
                events.append(
                    {"t": r["t"], "kind": "ceiling_change", "from": last_ceiling, "to": c}
                )
            last_ceiling, seen_ceiling = c, True
        b = r.get("binding_constraint")
        if b is not None:
            if seen_binding and b != last_binding:
                events.append(
                    {"t": r["t"], "kind": "binding_change", "from": last_binding, "to": b}
                )
            last_binding, seen_binding = b, True
    return events


def to_columnar(rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
    keys: list[str] = ["t"]
    seen = {"t"}
    for r in rows:
        for k in r:
            if k not in seen:
                keys.append(k)
                seen.add(k)
    return {k: [r.get(k) for r in rows] for k in keys}


def decimate(
    columns: dict[str, list[Any]], target: int
) -> tuple[dict[str, list[Any]], dict[str, Any]]:
    """Bucket to at most *target* points: numeric = mean of non-null (None when all null),
    bool = any, str = last non-null, ``t`` = the bucket's first t. ``in_flight_n_null``
    records how many raw ticks in the bucket were unmeasured."""
    n = len(columns.get("t", []))
    info = {"target_points": target, "raw_ticks": n, "method": "bucket-mean|last-categorical"}
    if n <= target:
        return columns, {**info, "bucket": 1}
    size = math.ceil(n / target)
    out: dict[str, list[Any]] = {k: [] for k in columns}
    out["in_flight_n_null"] = []
    for i in range(0, n, size):
        for k, vals in columns.items():
            chunk = vals[i : i + size]
            if k == "t":
                out[k].append(chunk[0])
                continue
            present = [v for v in chunk if v is not None]
            if not present:
                out[k].append(None)
            elif all(isinstance(v, bool) for v in present):
                out[k].append(any(present))
            elif all(isinstance(v, (int, float)) for v in present):
                mean = sum(present) / len(present)
                out[k].append(round(mean, 4) if isinstance(mean, float) else mean)
            else:
                out[k].append(present[-1])
        out["in_flight_n_null"].append(
            sum(1 for v in columns.get("in_flight", [None] * n)[i : i + size] if v is None)
        )
    return out, {**info, "bucket": size}


# ── events ────────────────────────────────────────────────────────────────────────────────


def collect_events(timeline: dict[str, Any], start: float) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    stamps = timeline.get("stamps") or {}

    def stamp(kind: str, key: str, **extra: Any) -> None:
        t = to_t(stamps.get(key), start)
        if t is not None:
            events.append({"t": t, "kind": kind, **extra})

    stamp("launch", "created_at")
    stamp("dispatched", "dispatched_at")
    stamp(
        "abort_requested",
        "stop_requested_at",
        scope=stamps.get("stop_scope"),
        reason=stamps.get("stop_reason") or "",
    )
    stamp("abort_settled", "stopped_at")
    stamp("finalised", "finalised_at")
    for ev in timeline.get("events") or []:
        t = to_t(ev.get("ts"), start)
        if t is None:
            continue
        detail = ev.get("detail") or {}
        events.append(
            {
                "t": t,
                "kind": str(ev.get("kind")),
                "actor": ev.get("actor"),
                "reason": ev.get("reason") or "",
                "pools": detail.get("pools") if isinstance(detail, dict) else None,
                "global": ev.get("run_id") is None,
            }
        )
    for ed in timeline.get("limit_edits") or []:
        t = to_t(ed.get("ts"), start)
        if t is None:
            continue
        events.append(
            {
                "t": t,
                "kind": "limit_edit",
                "scope": ed.get("scope"),
                "target": None if ed.get("scope") == "run" else ed.get("target"),
                "field": ed.get("field"),
                "old": ed.get("old_value"),
                "new": ed.get("new_value"),
                "actor": ed.get("actor"),
                "reason": ed.get("reason") or "",
            }
        )
    for d in timeline.get("discovery") or []:
        t = to_t(d.get("ts"), start)
        if t is None:
            continue
        events.append(
            {
                "t": t,
                "kind": "discovery_step",
                "model_alias": d.get("model_alias"),
                "event_type": d.get("event_type"),
                "tpm_value": d.get("tpm_value"),
                "at_concurrency": d.get("at_concurrency"),
            }
        )
    events.sort(key=lambda e: (e["t"], e["kind"]))
    return events


# ── lanes + calls ─────────────────────────────────────────────────────────────────────────

_ACTIVE = frozenset({"PENDING", "DISPATCHED", "HARNESS_RUNNING", "EVAL_RUNNING"})


def _outcome(pair: dict[str, Any]) -> str:
    if pair.get("aborted"):
        return "aborted"
    if pair.get("grade_invalid"):
        return "grade_invalid"
    if pair.get("verdict") in ("resolved", "unresolved"):
        return str(pair["verdict"])
    if pair.get("harness_state") in _ACTIVE or pair.get("eval_state") in _ACTIVE:
        return "in_flight_at_export"
    if pair.get("harness_state") == "EMPTY_PATCH" or pair.get("error_category") == "EMPTY_PATCH":
        return "empty_patch"
    return "error"


def build_lanes(
    lane_rows: list[dict[str, Any]], calls: list[dict[str, Any]], start: float
) -> list[dict[str, Any]]:
    pairs: dict[tuple[str, int], dict[str, Any]] = {}
    for row in lane_rows:
        key = (str(row["instance_id"]), int(row["attempt_number"]))
        p = pairs.setdefault(
            key,
            {
                "instance_id": key[0],
                "attempt": key[1],
                "harness_state": None,
                "eval_state": None,
                "verdict": None,
                "error_category": None,
                "retry_reason": None,
                "grade_invalid": False,
                "aborted": False,
                "turns": None,
                "tok_in": None,
                "tok_out": None,
                "tok_cached": None,
                "cost_usd": None,
                "paced_wait_ms_total": None,
                "overload_retries_total": None,
                "task_observed_s": None,
                "t_landed": None,
                "t_seeded": None,
            },
        )
        if row.get("state") in _ABORT_STATES:
            p["aborted"] = True
        # instance_results rows are UPDATEd in place: a harness row's created_at is the
        # PENDING seed at launch (before the window starts), NOT when its result landed. Only
        # the eval row is inserted when its verdict lands, so only it gives a landing time.
        t_row = to_t(row.get("created_at"), start)
        if row.get("phase") == "eval":
            if t_row is not None:
                p["t_landed"] = t_row
            p["eval_state"] = row.get("state")
            if row.get("verdict"):
                p["verdict"] = str(row["verdict"])
            if row.get("grade_invalid") is True:
                p["grade_invalid"] = True
        else:
            p["harness_state"] = row.get("state")
            if row.get("error_category"):
                p["error_category"] = str(row["error_category"])
            if row.get("retry_reason"):
                p["retry_reason"] = str(row["retry_reason"])
            for src, dst in (
                ("turns_used", "turns"),
                ("input_tokens", "tok_in"),
                ("output_tokens", "tok_out"),
                ("cached_tokens", "tok_cached"),
                ("cost_usd", "cost_usd"),
                ("paced_wait_ms_total", "paced_wait_ms_total"),
                ("overload_retries_total", "overload_retries_total"),
                ("task_observed_s", "task_observed_s"),
            ):
                if row.get(src) is not None:
                    p[dst] = row[src]
            if t_row is not None:
                p["t_seeded"] = t_row

    by_lane: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for c in calls:
        by_lane[(str(c["instance_id"]), int(c["attempt_number"]))].append(c)

    lanes: list[dict[str, Any]] = []
    for key in sorted(pairs):
        p = pairs[key]
        lane_calls = sorted(by_lane.get(key, []), key=lambda c: int(c.get("call_index") or 0))
        ts = [to_t(c.get("started_at"), start) for c in lane_calls]
        ts_known = [t for t in ts if t is not None]
        samples: list[list[Any]] = []
        cum = 0.0
        measured_cost = False
        for c, t in zip(lane_calls, ts, strict=True):
            if c.get("cost_usd") is not None:
                cum += float(c["cost_usd"])
                measured_cost = True
            if t is not None:
                samples.append(
                    [t, int(c.get("call_index") or 0), round(cum, 6) if measured_cost else None]
                )
        if len(samples) > LANE_SAMPLES_MAX:
            stride = math.ceil(len(samples) / LANE_SAMPLES_MAX)
            kept = samples[::stride]
            if kept[-1] is not samples[-1]:
                kept.append(samples[-1])
            samples = kept
        lanes.append(
            {
                "instance_id": p["instance_id"],
                "attempt": p["attempt"],
                "t_seeded": p["t_seeded"],  # the PENDING row at launch (often < 0)
                "t_first_call": min(ts_known) if ts_known else None,
                "t_last_call": max(ts_known) if ts_known else None,
                "t_landed": p["t_landed"],  # the eval verdict landing; None without one
                # The lane's visible end: the verdict when there is one, else the last call.
                "t_end": max(
                    [
                        v
                        for v in (p["t_landed"], max(ts_known) if ts_known else None)
                        if v is not None
                    ],
                    default=None,
                ),
                "outcome": _outcome(p),
                "harness_state": p["harness_state"],
                "verdict": p["verdict"],
                "error_category": p["error_category"],
                "terminated_reason": _terminated_reason(p),
                "retry_reason": p["retry_reason"],
                "turns": p["turns"],
                "calls": len(lane_calls) if lane_calls else None,
                "tok_in": p["tok_in"],
                "tok_out": p["tok_out"],
                "tok_cached": p["tok_cached"],
                "cost_usd": p["cost_usd"],
                "paced_wait_ms_total": p["paced_wait_ms_total"],
                "overload_retries_total": p["overload_retries_total"],
                "samples": samples,
            }
        )
    return lanes


_CALL_COLUMNS = (
    "latency_ms",
    "ttft_ms",
    "paced_wait_ms",
    "overload_retries",
    "http_status",
    "error_type",
    "finish_reason",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "cost_usd",
    "model_resolved",
    "provider_name",
)
_CALL_RENAME = {
    "input_tokens": "tok_in",
    "output_tokens": "tok_out",
    "cached_tokens": "tok_cached",
    "reasoning_tokens": "tok_reasoning",
}


def lane_filename(instance_id: str, attempt: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "%", instance_id)
    return f"{safe}__{attempt}.json"


def build_calls_files(calls: list[dict[str, Any]], start: float) -> dict[str, dict[str, Any]]:
    by_lane: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for c in calls:
        by_lane[(str(c["instance_id"]), int(c["attempt_number"]))].append(c)
    files: dict[str, dict[str, Any]] = {}
    for (iid, attempt), rows in by_lane.items():
        rows = sorted(rows, key=lambda c: int(c.get("call_index") or 0))
        cols: dict[str, list[Any]] = {"t": [], "call_index": []}
        for k in _CALL_COLUMNS:
            cols[_CALL_RENAME.get(k, k)] = []
        for c in rows:
            cols["t"].append(to_t(c.get("started_at"), start))
            cols["call_index"].append(c.get("call_index"))
            for k in _CALL_COLUMNS:
                cols[_CALL_RENAME.get(k, k)].append(c.get(k))
        files[lane_filename(iid, attempt)] = {
            "schema_version": SCHEMA_VERSION,
            "instance_id": iid,
            "attempt": attempt,
            "columns": cols,
        }
    return files


def build_discovery(timeline: dict[str, Any], start: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in timeline.get("discovery") or []:
        t = to_t(d.get("ts"), start)
        if t is None:
            continue
        out.append(
            {
                "t": t,
                "model_alias": d.get("model_alias"),
                "event_type": d.get("event_type"),
                "tpm_value": d.get("tpm_value"),
                "at_concurrency": d.get("at_concurrency"),
                "notes": d.get("notes"),
            }
        )
    return out


# ── the whole set ─────────────────────────────────────────────────────────────────────────


def _dumps(obj: Any, *, pretty: bool = False) -> bytes:
    if pretty:
        return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode()
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


# ── judge (Pass B) ────────────────────────────────────────────────────────────────────────
# 2026-09-08 (owner): the site also carries the LLM judge's per-attempt verdicts and the
# pass-level report, so the article's "the judge found …" claims are backed by the same
# files a visitor can open. Optional — a run without a judge pass exports exactly as before.

JUDGE_FINDING_KINDS = (
    "contamination",
    "hallucination",
    "tool_efficiency",
    "loop",
    "environment_problem",
    "gave_up_early",
    "test_gaming",
    "problem_misread",
    "token_efficiency",
)
# Same thresholds as the UI's findings filter (ui/src/lib/judge.ts judgeIssues) and the
# synthesis digest (analysis/judge._dimension_flagged): rubric baseline, ratio at >= 25 %,
# the v3 `causes` scale at an avoidable share >= 25 % (or severity >= 2).
JUDGE_TOOL_EFFICIENCY_REDUNDANT_RATIO = 0.25
JUDGE_AVOIDABLE_SHARE_FLAG = 0.25


def judge_dimension_flagged(d: dict[str, Any]) -> bool:
    if d.get("evidence_missing"):
        return False
    n = d.get("score_numeric")
    scale = d.get("scale_type")
    if scale == "boolean_with_span":
        return bool(d.get("flag"))
    if scale == "ratio":
        total = d.get("score_secondary")
        return bool(
            n and total and float(n) / float(total) >= JUDGE_TOOL_EFFICIENCY_REDUNDANT_RATIO
        )
    if scale == "causes":
        sev = d.get("score_secondary")
        return bool(
            (n is not None and float(n) >= JUDGE_AVOIDABLE_SHARE_FLAG)
            or (sev is not None and float(sev) >= 2)
        )
    return n is not None and float(n) > 0


def build_judge(
    results: list[dict[str, Any]], passes: list[dict[str, Any]], *, run_id: str
) -> tuple[dict[str, Any], str | None]:
    """``judge.json`` (every judged attempt with all dimensions, plus per-kind finding
    counts and the pass ledger) and the latest pass report's Markdown (``None`` when no
    pass wrote one). Attempts are sorted so a re-run is byte-identical."""
    attempts: list[dict[str, Any]] = []
    counts: dict[str, int] = {k: 0 for k in JUDGE_FINDING_KINDS}
    counts.update({"no_evidence": 0, "parse_failed": 0, "timeout": 0, "any": 0, "none": 0})
    for r in sorted(results, key=lambda r: (str(r["instance_id"]), int(r["attempt_number"]))):
        dims_out: list[dict[str, Any]] = []
        flagged: list[str] = []
        no_evidence = False
        for d in r.get("dimensions") or []:
            dim_id = str(d.get("dimension_id"))
            if d.get("evidence_missing"):
                no_evidence = True
            elif judge_dimension_flagged(d) and dim_id in counts:
                flagged.append(dim_id)
            evidence = [
                {"turn": e.get("turn"), "quote": e.get("quote")}
                for e in (d.get("evidence") or [])
                if isinstance(e, dict)
            ]
            dims_out.append(
                {
                    "id": dim_id,
                    "scale": d.get("scale_type"),
                    "score": d.get("score_numeric"),
                    "secondary": d.get("score_secondary"),
                    "flag": d.get("flag"),
                    "span": [d.get("span_start_turn"), d.get("span_end_turn")],
                    "reasoning": d.get("reasoning"),
                    "evidence": evidence,
                    "evidence_missing": bool(d.get("evidence_missing")),
                    "causes": list(d.get("causes") or []),
                }
            )
        findings = list(flagged)
        if no_evidence:
            findings.append("no_evidence")
        if r.get("judge_parse_failed"):
            findings.insert(0, "parse_failed")
        if r.get("judge_method") == "timeout":
            # the judge never answered within the ceiling — no verdict of any kind
            findings = ["timeout"]
        for k in findings:
            counts[k] += 1
        counts["any" if findings else "none"] += 1
        attempts.append(
            {
                "instance_id": r["instance_id"],
                "attempt": int(r["attempt_number"]),
                "judged_at": r.get("judged_at"),
                "model": r.get("judge_model_resolved"),
                "rubric_version": r.get("rubric_version"),
                "prune_mode": r.get("judge_prune_mode"),
                "judge_method": r.get("judge_method"),
                "parse_failed": bool(r.get("judge_parse_failed")),
                "tool_output_pruned": bool(r.get("tool_output_pruned")),
                "input_truncated": bool(r.get("input_truncated")),
                "cost_usd": r.get("judge_cost_usd"),
                "summary": r.get("summary"),
                "findings": findings,
                "dimensions": dims_out,
                "efficiency_profile": r.get("efficiency_profile"),
            }
        )
    ledger = [
        {
            "pass_id": p.get("pass_id"),
            "created_at": p.get("created_at"),
            "total_eligible": p.get("total_eligible"),
            "total_judged": p.get("total_judged"),
            "total_skipped_over_budget": p.get("total_skipped_over_budget"),
            "total_parse_failed": p.get("total_parse_failed"),
            "synthesis_only": bool(p.get("synthesis_only")),
            "has_report": bool(p.get("synthesis")),
            "report_error": p.get("synthesis_error"),
        }
        for p in passes
    ]
    report_pass = next((p for p in passes if p.get("synthesis")), None)
    report_meta = (
        {
            "pass_id": report_pass.get("pass_id"),
            "created_at": report_pass.get("created_at"),
            "model": report_pass.get("synthesis_model_resolved"),
            "cost_usd": report_pass.get("synthesis_cost_usd"),
            "file": "judge_report.md",
        }
        if report_pass
        else None
    )
    doc = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "judged_attempts": len(attempts),
        "finding_counts": counts,
        "finding_kinds": list(JUDGE_FINDING_KINDS) + ["no_evidence", "parse_failed", "timeout"],
        "tool_efficiency_redundant_ratio": JUDGE_TOOL_EFFICIENCY_REDUNDANT_RATIO,
        "report": report_meta,
        "passes": ledger,
        "attempts": attempts,
    }
    return doc, (str(report_pass["synthesis"]) if report_pass else None)


def build_site_files(
    timeline: dict[str, Any],
    export: dict[str, Any],
    *,
    target_points: int = TARGET_POINTS,
    exporter_sha: str = "",
    exported_at: str = "",
    judge: dict[str, Any] | None = None,
) -> dict[str, bytes]:
    """Every file of ``site/data/timelines/<run_id>/`` (+ ``calls/`` for ``static/``), as
    bytes, keyed by relative path. Raises :class:`ScrubError` on any denylist hit.

    ``judge`` (optional, 2026-09-08): ``{"results": [...], "passes": [...]}`` — the two
    judge endpoints' payloads; adds ``judge.json`` (+ ``judge_report.md`` when a pass wrote
    a report) and a ``files.judge`` manifest entry. Absent, the output is byte-identical
    to the pre-judge exporter."""
    run_id = str(timeline["run_id"])
    start = parse_iso(timeline.get("window_start"))
    if start is None:
        raise ValueError("timeline has no window_start (created_at / dispatched_at)")
    interval = int(timeline.get("tick_interval_s") or 30)
    end = parse_iso(timeline.get("window_end"))
    last_tick = max(
        (parse_iso(t.get("ts")) or 0.0 for t in timeline.get("ticks") or []), default=0.0
    )
    last_call = max(
        (parse_iso(c.get("started_at")) or 0.0 for c in timeline.get("calls") or []),
        default=0.0,
    )
    if end is None:
        end = max(start, last_tick, last_call)

    raw_rows = build_fleet_rows(timeline, start, interval)
    events = collect_events(timeline, start) + derive_events(raw_rows)
    events.sort(key=lambda e: (e["t"], e["kind"]))
    columns, decimation = decimate(to_columnar(raw_rows), target_points)
    lanes = build_lanes(timeline.get("lane_rows") or [], timeline.get("calls") or [], start)
    calls_files = build_calls_files(timeline.get("calls") or [], start)
    discovery = build_discovery(timeline, start)

    provenance = dict(export.get("provenance") or {})
    fleet = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "tick_interval_s": interval,
        "series": columns,
        "events": events,
    }
    instances = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "lanes": lanes}
    disc = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "steps": discovery}

    files: dict[str, bytes] = {
        "fleet.json": _dumps(fleet),
        "instances.json": _dumps(instances),
        "discovery.json": _dumps(disc),
    }
    for name, obj in calls_files.items():
        files[f"calls/{name}"] = _dumps(obj)
    judge_doc: dict[str, Any] | None = None
    if judge is not None and (judge.get("results") or judge.get("passes")):
        judge_doc, report_md = build_judge(
            list(judge.get("results") or []), list(judge.get("passes") or []), run_id=run_id
        )
        files["judge.json"] = _dumps(judge_doc)
        if report_md is not None:
            files["judge_report.md"] = report_md.encode()

    hits: list[str] = []
    for name, data in files.items():
        for h in scrub_hits(data.decode()):
            hits.append(f"{name}:{h}")
    if hits:
        raise ScrubError("scrub hits: " + "; ".join(hits[:10]))

    calls_bytes = sum(len(v) for k, v in files.items() if k.startswith("calls/"))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "provenance": provenance,
        "window": {"start": _iso(start), "end": _iso(end), "seconds": round(end - start)},
        "tick_interval_s": interval,
        "decimation": decimation,
        "files": {
            "fleet.json": {
                "bytes": len(files["fleet.json"]),
                "sha256": hashlib.sha256(files["fleet.json"]).hexdigest(),
                "points": len(columns.get("t", [])),
                "events": len(events),
            },
            "instances.json": {
                "bytes": len(files["instances.json"]),
                "sha256": hashlib.sha256(files["instances.json"]).hexdigest(),
                "lanes": len(lanes),
            },
            "discovery.json": {
                "bytes": len(files["discovery.json"]),
                "sha256": hashlib.sha256(files["discovery.json"]).hexdigest(),
                "steps": len(discovery),
            },
            "calls": {
                "count": len(calls_files),
                "bytes_total": calls_bytes,
                "path": f"/timelines/{run_id}/calls/",
            },
        },
        "vocab": {
            "binding_constraint": BINDING_VOCAB,
            "lane_outcome": LANE_OUTCOMES,
            "event_kind": EVENT_KINDS,
        },
        "scrub": {
            "patterns_applied": sorted(_SCRUB_PATTERNS),
            "dropped_keys": sorted(DROPPED_KEYS),
            "hits": 0,
        },
        "exported_at": exported_at,
        "exporter_sha": exporter_sha,
    }
    if judge_doc is not None:
        files_manifest = cast(dict[str, Any], manifest["files"])
        files_manifest["judge.json"] = {
            "bytes": len(files["judge.json"]),
            "sha256": hashlib.sha256(files["judge.json"]).hexdigest(),
            "judged_attempts": judge_doc["judged_attempts"],
            "finding_counts": judge_doc["finding_counts"],
        }
        if "judge_report.md" in files:
            files_manifest["judge_report.md"] = {
                "bytes": len(files["judge_report.md"]),
                "sha256": hashlib.sha256(files["judge_report.md"]).hexdigest(),
                "pass_id": (judge_doc.get("report") or {}).get("pass_id"),
                "model": (judge_doc.get("report") or {}).get("model"),
            }
    manifest_bytes = _dumps(manifest, pretty=True)
    if scrub_hits(manifest_bytes.decode().replace(exporter_sha, "")):
        raise ScrubError("scrub hits in manifest")
    files["manifest.json"] = manifest_bytes
    return files
