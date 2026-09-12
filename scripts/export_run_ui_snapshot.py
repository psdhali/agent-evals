#!/usr/bin/env python3
"""Snapshot exported runs into the static data tree the demo-mode dashboard replays.

SITE-REDESIGN-AND-PUBLICATION-PLAN-2026-09-10.md §8.3. The public "Explorer" is the
operator UI running against ``ui/src/lib/demo/demoApi.ts`` instead of the API; this
script writes everything that adapter reads, from files already exported by
``GET /runs/{id}/...`` (``dev/exports/<run_id>/api/*.json``) and from the one-off global
endpoint captures. Nothing here talks to the API or to AWS.

Output tree (``--out``)::

    global/{runs,harnesses,models,presets,control,capacity,queues,model_ceilings,limits,
            autoscaler_harness,autoscaler_eval,calibration,dataset_instances,
            image_validation}.json
    runs/<run_id>/{run,instances,progress,export,judge_results,judge_passes}.json
    runs/<run_id>/replay.json.gz                 lanes (derived stamps), ticks (pacer
                                                 ledger + control flags), capacity
                                                 decisions, events, limit edits, calls
                                                 (started/finished/tokens/cost/status)
    runs/<run_id>/calls/<instance>__<attempt>.json.gz   the InstanceCalls API shape
    runs/<run_id>/artifacts/<instance>/<attempt>/<kind>.gz   (artifact runs only)
    manifest.json                                bytes + sha256 per file
    scrub_report.json                            every redaction, per file and pattern

Scrub rules (the same denylist as ``swebench_eval.orchestrator.timeline_export`` plus a
bearer-token / AWS-access-key pattern):

  * a hit inside any API/JSON file FAILS the run (nothing partial is left behind);
  * a hit inside an artifact text file is REDACTED to ``<redacted>`` and counted;
  * S3 URIs inside path fields are rewritten to the relative key before the scan.

Lane stamps: the API's lane rows carry no dispatch / start / finish stamps, only the
row's ``created_at`` and the measured phase durations (queue_wait_s, image_pull_s,
worker_boot_s, repo_prep_s, agent_s, eval_queue_wait_s, wall_clock_eval_s). The
per-attempt stamps are DERIVED from those plus the first/last llm call and the eval
row's created_at (= the eval enqueue). ``stamp_source`` on every lane says which
anchor was used; a lane with no anchor is ``"none"`` and replays as PENDING until its
final state lands at the run's end. The derivation is checked against the observer's
own per-tick state counts and the mean absolute error is printed per run.

Usage:
    uv run python scripts/export_run_ui_snapshot.py <run_id>... --out <dir> \
        [--exports DIR] [--captures DIR] [--artifacts-for id,id]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import pathlib
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from swebench_eval.orchestrator import timeline_export as te

SCHEMA_VERSION = 1

DEFAULT_EXPORTS = pathlib.Path(__file__).resolve().parents[3] / "exports"
# One-off captures of the global endpoints (GET /runs, /harnesses, /models, ...), taken while
# the API was up; pass --captures explicitly when they live elsewhere.
DEFAULT_CAPTURES = pathlib.Path(__file__).resolve().parents[1] / "data" / "explorer-captures"

ARTIFACT_KINDS = {
    # UI kind -> (artifact subdir kind, file name)
    "patch": ("harness", "patch.diff"),
    "trajectory": ("harness", "trajectory.jsonl"),
    "log": ("harness", "harness_stdout.log"),
    "native_trajectory": ("harness", "native_trajectory.json"),
    "report": ("eval", "eval_report.json"),
    "test_output": ("eval", "test_output.txt"),
    "run_log": ("eval", "run_instance.log"),
}

GLOBAL_FILES = {
    "runs": "runs.json",
    "harnesses": "harnesses.json",
    "models": "models.json",
    "presets": "presets.json",
    "control": "control.json",
    "capacity": "capacity.json",
    "queues": "queues.json",
    "model_ceilings": "model_ceilings.json",
    "limits": "limits.json",
    "autoscaler_harness": "autoscaler_harness.json",
    "autoscaler_eval": "autoscaler_eval.json",
    "calibration": "calibration.json",
}

PATH_FIELDS = (
    "patch_path",
    "trajectory_path",
    "raw_log_path",
    "report_path",
    "native_trajectory_s3_key",
    "test_output_s3_key",
    "run_log_s3_key",
    "patch_s3_key",
)

# The planner / eval-scaler decision keys the dashboard's PacerPanel + EvalScalerPanel
# read (AutoscalerDecision.record), on top of the site exporter's allowlist. Anything
# not listed is dropped before the denylist pass.
EXTRA_DECISION_KEYS = frozenset(
    {
        "in_flight_tasks",
        "binding_alias",
        "booting_tasks",
        "static_cap",
        "growth_clamped",
        "growth_applied",
        "recovery_set",
        "observed_tasks",
        "overloads_window",
        "projected_qps",
        "projected_arrival_tok_s",
        "projected_inflight_tok",
        "peak_at_s",
        "would_set",
        "desired_hosts",
        "visible",
        "not_visible",
        "feedforward",
        "running_tasks",
        "running_hosts",
        "idle_hosts",
        "asg_desired",
        "asg_max",
        "scale_in_pending_ticks",
        "tasks",  # a per-alias COUNT here (kept only when numeric, see prune)
        "latency_s_max_context",
        "age_s",
        "cached_share",
        "arrival_tok_s",
        "inflight_tok",
    }
)
ALLOWED_DECISION_KEYS = te.ALLOWED_DECISION_KEYS | EXTRA_DECISION_KEYS
DROPPED_KEYS = te.DROPPED_KEYS - {"tasks"}

SCRUB_PATTERNS: dict[str, re.Pattern[str]] = {
    **te._SCRUB_PATTERNS,
    "bearer_token": re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}"),
    "aws_access_key": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
}

TICK_FIELDS = (
    "run_status",
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
    "cost_usd_landed",
    "harness_paused",
    "eval_paused",
    "gateway_paused",
    "control_stale",
    "counts",
    "pacer",
)
CAPACITY_FIELDS = (
    "pool",
    "queue_depth",
    "not_visible",
    "current_workers",
    "desired",
    "binding_constraint",
    "ceiling_utilization",
    "decision_age_s",
    "eta_low_s",
    "eta_high_s",
    "constants_source",
    "pacer_queue_len",
    "paced_over_2s_share",
)
CALL_COLUMNS = (
    "lane",
    "t_start",
    "t_end",
    "tok_in",
    "tok_out",
    "tok_cached",
    "tok_reasoning",
    "cost_usd",
    "http_status",
)
# InstanceCall (the Calls table) — every column of the API shape; the seven the
# timeline export never carried stay null (the table renders them as '—').
INSTANCE_CALL_COLUMNS = (
    "call_index",
    "started_at",
    "http_status",
    "model_resolved",
    "error_type",
    "rate_limit_scope",
    "shim_preflight_ms",
    "paced_wait_ms",
    "overload_retries",
    "overload_backoff_ms",
    "retry_upstream_ms",
    "ttft_ms",
    "latency_ms",
    "pacer_was_queued",
    "pacer_queue_len",
    "pacer_deny_axis",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cost_usd",
)


class ScrubError(RuntimeError):
    pass


# ── helpers ──────────────────────────────────────────────────────────────────────────


def _load(path: pathlib.Path) -> Any:
    with path.open() as fh:
        return json.load(fh)


def _is_error_body(obj: Any) -> bool:
    return isinstance(obj, dict) and set(obj.keys()) == {"detail"}


def _dumps(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


def _rewrite_paths(obj: Any) -> Any:
    """Rewrite ``s3://bucket/key`` (and ``bucket/key`` with an account-shaped bucket) in
    the known path fields to the relative key, at any depth."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in PATH_FIELDS and isinstance(v, str):
                v = re.sub(r"^s3://[^/]+/", "", v)
                v = re.sub(r"^[a-z0-9.\-]*\d{12}[a-z0-9.\-]*/", "", v)
            out[k] = _rewrite_paths(v)
        return out
    if isinstance(obj, list):
        return [_rewrite_paths(v) for v in obj]
    return obj


def scrub_hits(text: str) -> list[str]:
    hits = []
    for name, pat in SCRUB_PATTERNS.items():
        m = pat.search(text)
        if m:
            hits.append(f"{name}:{m.group(0)[:24]}")
    return hits


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for name, pat in SCRUB_PATTERNS.items():
        text, n = pat.subn("<redacted>", text)
        if n:
            counts[name] = n
    return text, counts


def prune_decision(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if ks in DROPPED_KEYS:
                continue
            if ks == "tasks" and not isinstance(v, (int, float)):
                continue
            if ks in ALLOWED_DECISION_KEYS or te._looks_like_alias(ks):
                out[ks] = prune_decision(v)
        return out
    if isinstance(obj, list):
        return [prune_decision(v) for v in obj]
    return obj


def _t(value: Any, start: float) -> float | None:
    epoch = te.parse_iso(value)
    return None if epoch is None else round(epoch - start, 1)


def _first(*vals: Any) -> Any:
    for v in vals:
        if v is not None:
            return v
    return None


# ── per-run assembly ─────────────────────────────────────────────────────────────────


def load_instances(api_dir: pathlib.Path) -> dict[str, Any]:
    """Merge every captured page of GET /runs/{id}/instances into one list."""
    candidates = sorted(
        p
        for p in api_dir.glob("instances*.json")
        if re.fullmatch(r"instances(?:[-_]p\d+|_limit_\d+_offset_\d+)?\.json", p.name)
    )
    items: dict[tuple[str, int, str], dict[str, Any]] = {}
    total = 0
    for p in candidates:
        body = _load(p)
        if _is_error_body(body):
            continue
        total = max(total, int(body.get("total") or 0))
        for row in body.get("items") or []:
            items[(row["instance_id"], int(row["attempt_number"]), row["phase"])] = row
    if not items:
        raise FileNotFoundError(f"no usable instances page under {api_dir}")
    rows = sorted(items.values(), key=lambda r: (r["instance_id"], r["attempt_number"], r["phase"]))
    return {"items": rows, "total": max(total, len(rows)), "limit": len(rows), "offset": 0}


def derive_lanes(
    rows: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    start: float,
    stamps: dict[str, Any],
    ticks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[tuple[str, int], int]]:
    by_key: dict[tuple[str, int], dict[str, Any]] = defaultdict(dict)
    for row in rows:
        by_key[(row["instance_id"], int(row["attempt_number"]))][row["phase"]] = row
    by_lane_calls: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for c in calls:
        by_lane_calls[(c["instance_id"], int(c["attempt_number"]))].append(c)

    t_stop_req = _t(stamps.get("stop_requested_at"), start)
    t_stopped = _t(stamps.get("stopped_at"), start)

    lanes: list[dict[str, Any]] = []
    index: dict[tuple[str, int], int] = {}
    for key in sorted(by_key):
        h = by_key[key].get("harness")
        e = by_key[key].get("eval")
        lane_calls = sorted(
            by_lane_calls.get(key, []), key=lambda c: (te.parse_iso(c.get("started_at")) or 0)
        )
        h_state = (h or {}).get("state")
        e_state = (e or {}).get("state")

        hs: float | None = None
        hf: float | None = None
        ee: float | None = None
        es: float | None = None
        ef: float | None = None
        r: float | None = None
        d: float | None = None
        source = "none"
        if lane_calls:
            t0 = _t(lane_calls[0].get("started_at"), start)
            last = lane_calls[-1]
            t_last = _t(last.get("started_at"), start)
            hs = t0
            if t_last is not None:
                hf = round(
                    t_last
                    + (last.get("latency_ms") or 0) / 1000.0
                    + ((h or {}).get("patch_extract_s") or 0)
                    + ((h or {}).get("artifact_upload_s") or 0),
                    1,
                )
            source = "calls"
        if e is not None:
            ee = _t(e.get("created_at"), start)
            if ee is not None:
                # the eval row is inserted at enqueue, right after the harness upload
                hf = ee if hf is None or abs(ee - hf) < 120 else hf
                if hs is None:
                    agent = _first((h or {}).get("wall_clock_harness_s"), (h or {}).get("agent_s"))
                    if agent is not None:
                        hs = round(hf - float(agent), 1)
                        source = "eval_row"
                es = round(ee + (e.get("eval_queue_wait_s") or 0), 1)
                wall = _first(e.get("wall_clock_eval_s"), e.get("eval_test_s"))
                ef = round(es + float(wall or 0), 1)
        if hs is not None and h is not None:
            r = round(
                max(
                    0.0,
                    hs
                    - (h.get("repo_prep_s") or 0)
                    - (h.get("worker_boot_s") or 0)
                    - (h.get("image_pull_s") or 0),
                ),
                1,
            )
        if h_state in te_abort_states():
            # the abort sweep starts when the stop is requested; NEVER_DISPATCHED rows
            # are spread over the sweep's real duration below (calibrate_sweep)
            hf = t_stop_req if t_stop_req is not None else (t_stopped or hf)
            if h_state == "NEVER_DISPATCHED":
                d = r = hs = None
        if e is not None and e_state in ("ABANDONED",) and t_stop_req is not None:
            ef = t_stop_req

        index[key] = len(lanes)
        lanes.append(
            {
                "instance_id": key[0],
                "attempt": key[1],
                "t_dispatched": d,
                "t_running": r,
                "t_first_call": hs,
                "t_harness_finished": hf,
                "t_eval_enqueued": ee,
                "t_eval_started": es,
                "t_eval_finished": ef,
                "stamp_source": source,
                "harness_state": h_state,
                "eval_state": e_state,
                "verdict": (e or {}).get("verdict"),
                "error_category": _first(
                    (h or {}).get("error_category"), (e or {}).get("error_category")
                ),
                "retry_reason": _first(
                    (h or {}).get("retry_reason"), (e or {}).get("retry_reason")
                ),
                "grade_invalid": (e or {}).get("grade_invalid"),
                "turns": (h or {}).get("turns_used"),
                "tok_in": (h or {}).get("input_tokens"),
                "tok_out": (h or {}).get("output_tokens"),
                "cost_usd": (h or {}).get("cost_usd"),
                "calls": len(lane_calls) or None,
                "t_stop_requested": t_stop_req if h_state in te_abort_states() else None,
            }
        )
    calibrate_dispatch(lanes, ticks)
    calibrate_sweep(lanes, ticks, t_stop_req)
    return lanes, index


def calibrate_sweep(
    lanes: list[dict[str, Any]], ticks: list[dict[str, Any]], t_stop_req: float | None
) -> None:
    """Spread the NEVER_DISPATCHED rows over the abort sweep's real duration (the
    observer shows them landing over ~2 min, not on one tick)."""
    if t_stop_req is None:
        return
    never = [ln for ln in lanes if ln["harness_state"] == "NEVER_DISPATCHED"]
    if not never:
        return
    total = len(never)
    sweep_end = None
    for tk in ticks:
        n = sum(
            c.get("count") or 0
            for c in tk.get("counts") or []
            if c.get("phase") == "harness" and c.get("state") == "NEVER_DISPATCHED"
        )
        if n >= total:
            sweep_end = tk["t"]
            break
    if sweep_end is None or sweep_end <= t_stop_req:
        sweep_end = t_stop_req + 120.0
    span = sweep_end - t_stop_req
    never.sort(key=lambda ln: ln["instance_id"])
    for i, ln in enumerate(never):
        ln["t_harness_finished"] = round(t_stop_req + span * (i + 1) / total, 1)


def calibrate_dispatch(lanes: list[dict[str, Any]], ticks: list[dict[str, Any]]) -> None:
    """Place each lane's DISPATCHED stamp from the observer's own per-tick PENDING count.

    ``queue_wait_s`` on the harness row is NOT "time since dispatch" (the dispatcher
    enqueues in small planner-sized batches and a worker picks a job up within seconds
    — the observed DISPATCHED count is single digits), so the cumulative dispatched
    curve ``lanes − PENDING(t)`` from the ticks is the honest source: the k-th lane by
    worker pickup order was dispatched by the first tick whose curve reaches k.
    """
    n = len(lanes)
    curve: list[tuple[float, int]] = []
    for tk in ticks:
        pending = sum(
            c.get("count") or 0
            for c in tk.get("counts") or []
            if c.get("phase") == "harness" and c.get("state") == "PENDING"
        )
        seen = sum(
            c.get("count") or 0 for c in tk.get("counts") or [] if c.get("phase") == "harness"
        )
        if seen == 0:
            continue
        curve.append((tk["t"], max(0, seen - pending)))
    if not curve:
        return
    interval = 30.0
    if len(curve) > 1:
        interval = max(1.0, (curve[-1][0] - curve[0][0]) / (len(curve) - 1))

    def first_tick_at_least(k: int) -> float | None:
        for t, c in curve:
            if c >= k:
                return t
        return None

    dispatched = [
        ln
        for ln in lanes
        if ln["harness_state"] != "NEVER_DISPATCHED"
        and (ln["t_running"] is not None or ln["harness_state"] is not None)
    ]
    dispatched.sort(
        key=lambda ln: (
            ln["t_running"] if ln["t_running"] is not None else float("inf"),
            ln["instance_id"],
        )
    )
    for k, ln in enumerate(dispatched, start=1):
        t_tick = first_tick_at_least(k)
        r = ln["t_running"]
        if t_tick is None:
            d = r
        else:
            d = max(0.0, t_tick - interval / 2)
            if r is not None:
                d = min(d, r)
        ln["t_dispatched"] = None if d is None else round(d, 1)
        if ln["stamp_source"] == "none" and d is not None:
            ln["stamp_source"] = "tick_curve"
    del n


def te_abort_states() -> frozenset[str]:
    from swebench_eval.orchestrator.export import _ABORT_STATES

    return _ABORT_STATES


def lane_state_at(lane: dict[str, Any], t: float, t_end: float) -> tuple[str | None, str | None]:
    """The (harness_state, eval_state) the derived stamps imply at replay time t — the
    same ladder ``ui/src/lib/demo/replay.ts`` implements; used here only to check the
    derivation against the observer's tick counts."""
    d, r, hf = lane["t_dispatched"], lane["t_running"], lane["t_harness_finished"]
    ee, es, ef = lane["t_eval_enqueued"], lane["t_eval_started"], lane["t_eval_finished"]
    h_final, e_final = lane["harness_state"], lane["eval_state"]
    if h_final == "NEVER_DISPATCHED":
        h = "PENDING" if (hf is None or t < hf) else h_final
        return h, None
    if d is None:
        h = h_final if (hf is not None and t >= hf) or t >= t_end else "PENDING"
    elif t < d:
        h = "PENDING"
    elif r is None or t < r:
        h = "DISPATCHED"
    elif hf is None or t < hf:
        h = "HARNESS_RUNNING"
    else:
        h = h_final
    e = None
    if ee is not None and t >= ee:
        if es is None or t < es:
            e = "PENDING"
        elif ef is None or t < ef:
            e = "EVAL_RUNNING"
        else:
            e = e_final
    return h, e


def check_against_ticks(
    lanes: list[dict[str, Any]], ticks: list[dict[str, Any]], t_end: float
) -> str:
    """Mean absolute error of the derived per-state counts vs the observer's counts."""
    errs: dict[str, list[float]] = defaultdict(list)
    n = 0
    for tk in ticks:
        t = tk["t"]
        if t < 0:
            continue
        observed: Counter[tuple[str, str]] = Counter()
        for c in tk.get("counts") or []:
            observed[(c["phase"], c["state"])] = c["count"]
        derived: Counter[tuple[str, str]] = Counter()
        for lane in lanes:
            h, e = lane_state_at(lane, t, t_end)
            if h:
                derived[("harness", h)] += 1
            if e:
                derived[("eval", e)] += 1
        for k in set(observed) | set(derived):
            errs[k[1]].append(abs(observed[k] - derived[k]))
        n += 1
    if not n:
        return "no ticks to check against"
    parts = [f"{k}={sum(v) / len(v):.1f}" for k, v in sorted(errs.items())]
    return f"{n} ticks, mean |observed−derived| per state: " + ", ".join(parts)


def build_replay(
    run_id: str,
    run: dict[str, Any],
    instances: dict[str, Any],
    timeline: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    stamps = timeline.get("stamps") or {}
    start_iso = _first(
        timeline.get("window_start"), run.get("dispatched_at"), run.get("created_at")
    )
    start = te.parse_iso(start_iso)
    if start is None:
        raise ValueError(f"{run_id}: no window start")

    ticks = []
    for tk in timeline.get("ticks") or []:
        t = _t(tk.get("ts"), start)
        if t is None:
            continue
        ticks.append({"t": t, **{k: tk.get(k) for k in TICK_FIELDS}})
    ticks.sort(key=lambda x: x["t"])

    rows = instances["items"]
    calls = timeline.get("calls") or []
    lanes, index = derive_lanes(rows, calls, start, stamps, ticks)

    capacity = []
    for row in timeline.get("capacity") or []:
        t = _t(row.get("ts"), start)
        if t is None:
            continue
        capacity.append(
            {
                "t": t,
                **{k: row.get(k) for k in CAPACITY_FIELDS},
                "decision": prune_decision(row.get("decision") or {}),
            }
        )
    capacity.sort(key=lambda x: x["t"])

    limit_edits = []
    for le in timeline.get("limit_edits") or []:
        t = _t(le.get("ts"), start)
        limit_edits.append(
            {
                "t": t,
                **{
                    k: le.get(k)
                    for k in (
                        "scope",
                        "target",
                        "field",
                        "old_value",
                        "new_value",
                        "actor",
                        "reason",
                    )
                },
            }
        )

    compact_calls = []
    for c in calls:
        key = (c["instance_id"], int(c["attempt_number"]))
        if key not in index:
            continue
        ts = _t(c.get("started_at"), start)
        if ts is None:
            continue
        te_ = round(ts + (c.get("latency_ms") or 0) / 1000.0, 1)
        compact_calls.append(
            [
                index[key],
                ts,
                te_,
                c.get("input_tokens"),
                c.get("output_tokens"),
                c.get("cached_tokens"),
                c.get("reasoning_tokens"),
                c.get("cost_usd"),
                c.get("http_status"),
            ]
        )
    compact_calls.sort(key=lambda x: x[1])

    lane_ends = [
        v
        for lane in lanes
        for v in (lane["t_harness_finished"], lane["t_eval_finished"])
        if v is not None
    ]
    end_explicit = _t(
        _first(stamps.get("finalised_at"), stamps.get("stopped_at"), timeline.get("window_end")),
        start,
    )
    last_tick = ticks[-1]["t"] if ticks else None
    # the run is over when the last lane lands; an aborted run keeps draining past
    # stopped_at (the observer keeps ticking), so its last tick wins
    candidates = [max(lane_ends, default=None), end_explicit]
    if stamps.get("stop_requested_at"):
        candidates.append(last_tick)
    t_end = max([v for v in candidates if v is not None], default=None)
    if t_end is None:
        t_end = last_tick or 0.0

    final_status = run.get("status")
    if stamps.get("finalised_at") and final_status == "running":
        final_status = "completed"

    replay = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "harness": run.get("harness"),
        "model_alias": run.get("model_alias"),
        "window": {
            "start": datetime.fromtimestamp(start, tz=UTC).isoformat(),
            "end_t": t_end,
            "seconds": t_end,
        },
        "final_status": final_status,
        "stamps": {
            "created_at": stamps.get("created_at") or run.get("created_at"),
            "dispatched_at": stamps.get("dispatched_at") or run.get("dispatched_at"),
            "stop_requested_at": stamps.get("stop_requested_at"),
            "stop_scope": stamps.get("stop_scope"),
            "stop_reason": stamps.get("stop_reason"),
            "stopped_at": stamps.get("stopped_at"),
            "finalised_at": stamps.get("finalised_at") or run.get("finalised_at"),
            "t_stop_requested": _t(stamps.get("stop_requested_at"), start),
            "t_stopped": _t(stamps.get("stopped_at"), start),
            "t_finalised": _t(stamps.get("finalised_at"), start),
        },
        "tick_interval_s": timeline.get("tick_interval_s"),
        "targets": timeline.get("targets") or [],
        "lanes": lanes,
        "ticks": ticks,
        "capacity": capacity,
        "events": timeline.get("events") or [],
        "limit_edits": limit_edits,
        "discovery": timeline.get("discovery") or [],
        "calls_columns": list(CALL_COLUMNS),
        "calls": compact_calls,
    }
    check = check_against_ticks(lanes, ticks, t_end)
    return replay, check


def build_calls_files(run_id: str, calls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_lane: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for c in calls:
        by_lane[(c["instance_id"], int(c["attempt_number"]))].append(c)
    out = {}
    for (iid, attempt), rows in by_lane.items():
        rows = sorted(rows, key=lambda c: int(c.get("call_index") or 0))
        out[te.lane_filename(iid, attempt)] = {
            "run_id": run_id,
            "instance_id": iid,
            "attempt_number": attempt,
            "items": [{k: c.get(k) for k in INSTANCE_CALL_COLUMNS} for c in rows],
        }
    return out


# ── the writer ───────────────────────────────────────────────────────────────────────


class Writer:
    def __init__(self, out: pathlib.Path) -> None:
        self.out = out
        self.manifest: dict[str, dict[str, Any]] = {}
        self.scrub: dict[str, dict[str, int]] = {}
        self.total_bytes = 0

    def _record(self, rel: str, data: bytes) -> None:
        self.manifest[rel] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        self.total_bytes += len(data)

    def write_json(self, rel: str, obj: Any) -> None:
        obj = _rewrite_paths(obj)
        data = _dumps(obj)
        hits = scrub_hits(data.decode("utf-8", "replace"))
        if hits:
            raise ScrubError(f"{rel}: denylist hit {hits} — nothing written for this run")
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self._record(rel, data)

    def write_json_gz(self, rel: str, obj: Any) -> None:
        obj = _rewrite_paths(obj)
        data = _dumps(obj)
        hits = scrub_hits(data.decode("utf-8", "replace"))
        if hits:
            raise ScrubError(f"{rel}: denylist hit {hits} — nothing written for this run")
        gz = gzip.compress(data, compresslevel=6, mtime=0)
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gz)
        self._record(rel, gz)

    def write_text_gz(self, rel: str, text: str) -> None:
        text, counts = redact(text)
        if counts:
            self.scrub[rel] = counts
        data = text.encode("utf-8")
        gz = gzip.compress(data, compresslevel=6, mtime=0)
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gz)
        self._record(rel, gz)


def snapshot_run(
    run_id: str,
    exports: pathlib.Path,
    w: Writer,
    with_artifacts: bool,
) -> dict[str, Any]:
    api = exports / run_id / "api"
    notes: list[str] = []
    run = _load(api / "run.json")
    instances = load_instances(api)
    timeline = _load(api / "timeline.json")
    rel = f"runs/{run_id}"

    w.write_json(f"{rel}/run.json", run)
    w.write_json(f"{rel}/instances.json", instances)
    for name in ("progress", "export", "judge_results", "judge_passes"):
        p = api / f"{name}.json"
        if not p.exists():
            notes.append(f"{name}.json not captured")
            continue
        body = _load(p)
        if _is_error_body(body):
            notes.append(f"{name}.json is an error body ({body['detail']}) — skipped")
            continue
        w.write_json(f"{rel}/{name}.json", body)

    replay, check = build_replay(run_id, run, instances, timeline)
    w.write_json_gz(f"{rel}/replay.json.gz", replay)
    notes.append(f"stamp check: {check}")

    calls_files = build_calls_files(run_id, timeline.get("calls") or [])
    for name, body in calls_files.items():
        w.write_json_gz(f"{rel}/calls/{name}.gz", body)

    artifacts_written = 0
    if with_artifacts:
        art_root = exports / run_id / "artifacts"
        if not art_root.is_dir():
            notes.append("artifacts requested but dev/exports has none")
        else:
            harness_dirs = [
                p for p in art_root.iterdir() if p.is_dir() and p.name not in ("eval", "aborted")
            ]
            for kind, (sub, fname) in ARTIFACT_KINDS.items():
                if sub == "eval":
                    bases = [art_root / "eval"]
                else:
                    bases = [m for h in harness_dirs for m in h.iterdir() if m.is_dir()]
                for base in bases:
                    for src in sorted(base.glob(f"*/*/{fname}")):
                        attempt = src.parent.name
                        inst = src.parent.parent.name
                        text = src.read_text(encoding="utf-8", errors="replace")
                        w.write_text_gz(f"{rel}/artifacts/{inst}/{attempt}/{kind}.gz", text)
                        artifacts_written += 1
    return {
        "harness": run.get("harness"),
        "model_alias": run.get("model_alias"),
        "instances": run.get("instance_count"),
        "lanes": len(replay["lanes"]),
        "ticks": len(replay["ticks"]),
        "calls": len(replay["calls"]),
        "calls_files": len(calls_files),
        "artifacts": artifacts_written,
        "window_seconds": replay["window"]["seconds"],
        "notes": notes,
    }


def snapshot_global(
    captures: pathlib.Path, published: list[str], w: Writer, exports: pathlib.Path
) -> list[str]:
    notes = []
    for key, fname in GLOBAL_FILES.items():
        src = captures / fname
        if not src.exists():
            notes.append(f"{fname} not captured")
            continue
        body = _load(src)
        if key == "runs":
            items = [r for r in body.get("items") or [] if r.get("run_id") in published]
            order = {rid: i for i, rid in enumerate(published)}
            items.sort(key=lambda r: order[r["run_id"]])
            missing = [rid for rid in published if rid not in {r["run_id"] for r in items}]
            if missing:
                notes.append(
                    f"runs.json lacks {missing} — the run list is built from run.json for those"
                )
                for rid in missing:
                    run = _load(exports / rid / "api" / "run.json")
                    items.append(
                        {
                            k: v
                            for k, v in run.items()
                            if k
                            not in (
                                "states",
                                "instance_states",
                                "resolve_rate_denominator",
                                "ready_to_close",
                                "gateway_key_blocked_by",
                            )
                        }
                    )
            body = {"items": items, "total": len(items), "limit": len(items), "offset": 0}
        if key == "limits":
            body = {**body, "run": None}  # the demo builds the run block from the replayed run
        w.write_json(f"global/{fname}", body)

    p1 = captures / "dataset_instances_p1.json"
    p2 = captures / "dataset_instances_p2.json"
    if p1.exists():
        body = _load(p1)
        if p2.exists() and p2.read_bytes() == p1.read_bytes():
            notes.append(
                "dataset_instances p1 == p2 (offset ignored by the capture) — using p1 only"
            )
        w.write_json("global/dataset_instances.json", body)

    # Gold-validation results (the launch screen's "validate images" step): every
    # captured image-validation run, latest verdict per instance.
    validation: dict[str, dict[str, Any]] = {}
    for vdir in sorted(exports.glob("image-validation-*")):
        try:
            inst = load_instances(vdir / "api")
        except FileNotFoundError:
            continue
        for row in inst["items"]:
            if row.get("phase") != "eval" or not row.get("verdict"):
                continue
            validation[row["instance_id"]] = {
                "run_id": vdir.name,
                "attempt_number": row["attempt_number"],
                "verdict": row["verdict"],
                "state": row["state"],
                "created_at": row.get("created_at"),
            }
    w.write_json(
        "global/image_validation.json",
        {"items": validation, "runs": [p.name for p in sorted(exports.glob("image-validation-*"))]},
    )
    return notes


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("run_ids", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--exports", default=str(DEFAULT_EXPORTS))
    ap.add_argument("--captures", default=str(DEFAULT_CAPTURES))
    ap.add_argument(
        "--artifacts-for",
        default="",
        help="comma-separated run ids (or suffixes) whose artifacts are published",
    )
    ap.add_argument("--max-file-mb", type=float, default=25.0)
    ap.add_argument("--max-files", type=int, default=20000)
    args = ap.parse_args()

    exports = pathlib.Path(args.exports)
    captures = pathlib.Path(args.captures)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    w = Writer(out)
    art_ids = [s.strip() for s in args.artifacts_for.split(",") if s.strip()]

    included: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    for rid in args.run_ids:
        if not (exports / rid / "api" / "timeline.json").exists():
            skipped[rid] = "api/timeline.json missing in dev/exports"
            continue
        with_art = any(rid == a or rid.endswith(a) for a in art_ids)
        try:
            included[rid] = snapshot_run(rid, exports, w, with_art)
        except ScrubError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        print(f"{rid}: {json.dumps(included[rid])}")

    global_notes = snapshot_global(captures, list(included), w, exports)

    too_big = {
        k: v["bytes"] for k, v in w.manifest.items() if v["bytes"] > args.max_file_mb * 1024 * 1024
    }
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs": included,
        "skipped": skipped,
        "global_notes": global_notes,
        "scrub": {
            "json_hits": 0,
            "artifact_redactions": sum(sum(c.values()) for c in w.scrub.values()),
            "files_with_redactions": len(w.scrub),
        },
        "totals": {
            "files": len(w.manifest) + 2,
            "bytes": w.total_bytes,
            "max_file_bytes": max((v["bytes"] for v in w.manifest.values()), default=0),
        },
        "files": w.manifest,
    }
    scrub_report = {
        "patterns": sorted(SCRUB_PATTERNS),
        "json_files_scanned": sum(1 for k in w.manifest if k.endswith((".json", ".json.gz"))),
        "json_hits": 0,
        "artifact_files_scanned": sum(1 for k in w.manifest if "/artifacts/" in k),
        "artifact_redactions_total": manifest["scrub"]["artifact_redactions"],
        "artifacts": w.scrub,
    }
    (out / "scrub_report.json").write_text(
        json.dumps(scrub_report, indent=2, sort_keys=True) + "\n"
    )
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")

    print(
        f"wrote {manifest['totals']['files']} files, {w.total_bytes / 1e6:.1f} MB, "
        f"largest {manifest['totals']['max_file_bytes'] / 1e6:.1f} MB; "
        f"artifact redactions {manifest['scrub']['artifact_redactions']} in {len(w.scrub)} files; "
        f"skipped {skipped or 'none'}"
    )
    for n in global_notes:
        print(f"global: {n}")
    if too_big:
        print(f"FAIL: files over {args.max_file_mb} MB: {too_big}", file=sys.stderr)
        return 3
    if manifest["totals"]["files"] > args.max_files:
        print(f"FAIL: {manifest['totals']['files']} files > {args.max_files}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
