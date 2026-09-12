#!/usr/bin/env python3
"""Regenerate the publication site's TIMELINE fixtures from the real exporter.

dev/LIVE-RUN-TIMELINE-SITE-DATA-CONTRACT-AND-BUILD-PLAN-2026-09-04.md §2.9: builder 2 builds
the replay components against these before real data exists, so — same discipline as
``generate_site_fixtures.py`` — they are produced by ``timeline_export.build_site_files``
from synthetic API responses, never hand-written, and cannot disagree with the code.

Two fixtures under ``<repo>/../site-fixtures/timelines/``:

  * ``run-fixture-paused-aborted`` — a 40-minute run: ramp 3 -> 13, a ceiling override,
    a pause + resume, a pacer edit, then an abort; every event kind present; one lane
    per outcome.
  * ``run-fixture-uninstrumented`` — the null-rule proof: every token / cost series and
    every lane cost is None, and a 5-minute observer gap sits in the middle.

Usage:  .venv/bin/python scripts/generate_timeline_fixtures.py
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from swebench_eval.orchestrator import timeline_export

_FIXTURES_DIR = pathlib.Path(__file__).resolve().parents[1].parent / "site-fixtures" / "timelines"
_START = datetime(2026, 9, 6, 17, 0, 0, tzinfo=UTC)
_EXPORTED_AT = "2026-09-06T20:00:00Z"
_IDS = [
    "django__django-11000",
    "astropy__astropy-11037",
    "sympy__sympy-11074",
    "scikit-learn__scikit-learn-11111",
    "django__django-11148",
    "astropy__astropy-11185",
    "sympy__sympy-11222",
    "scikit-learn__scikit-learn-11259",
    "django__django-11296",
    "astropy__astropy-11333",
    "sympy__sympy-11370",
    "scikit-learn__scikit-learn-11407",
    "django__django-11444",
]


def _iso(offset_s: float) -> str:
    return (_START + timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")


def _provenance(run_id: str, harness: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "created_at": _iso(0),
        "framework_sha": "43bf763aa1c04e77bd2f9e0f1b3d5a7c9e11d204",
        "swebench_version": "2.1.0",
        "dataset_revision": "a1b2c3d4",
        "harness_image_digest": "sha256:9f1c…",
        "gateway_config_hash": "sha256:4d0a…",
        "model_alias": "deepseek-v4-flash-0731-mini",
        "model_resolved": "deepseek/deepseek-v4-flash-0731",
        "harness": harness,
        "harness_cli_version": "n/a",
        "network_posture": "isolated",
        "limits": {
            "max_tokens": None,
            "max_cost_usd_per_instance": 1.0,
            "attempts_per_instance": 1,
            "temperature": 1.0,
        },
    }


def _instrumented(run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ticks: list[dict[str, Any]] = []
    capacity: list[dict[str, Any]] = []
    total_s = 40 * 60
    cost = 0.0
    tok_in = 0
    for i in range(total_s // 30 + 1):
        s = i * 30
        paused = 900 <= s < 1080
        ceiling = min(13, 3 + s // 150) if s < 2100 else 0
        in_flight = 0 if paused or s >= 2100 else min(13, ceiling)
        if s < 2100 and not paused:
            tok_in += in_flight * 9000
            cost += in_flight * 0.00045
        resolved = min(5, s // 400)
        ticks.append(
            {
                "ts": _iso(s),
                "run_status": "aborting" if s >= 2100 else "running",
                "in_flight": in_flight,
                "stale": 0,
                "pending": max(0, 13 - in_flight - resolved - (2 if s > 1500 else 0)),
                "harness_running": in_flight,
                "eval_running": 1 if 400 < s < 2000 and s % 90 == 0 else 0,
                "resolved": resolved,
                "unresolved": 1 if s > 1500 else 0,
                "aborted": 6 if s >= 2100 else 0,
                "expected": 13,
                "denominator": resolved + (1 if s > 1500 else 0),
                "tok_in": tok_in,
                "tok_out": tok_in // 80,
                "tok_cached": int(tok_in * 0.65),
                "tok_reasoning": tok_in // 300,
                "cost_usd_live": round(cost, 6),
                "cost_usd_landed": round(cost * 0.6, 6),
                "harness_paused": paused,
                "eval_paused": False,
                "gateway_paused": False,
                "control_stale": False,
                "counts": [{"phase": "harness", "state": "HARNESS_RUNNING", "count": in_flight}],
                "pacer": {
                    "deepseek-v4-flash-0731-mini": {
                        "r_tok": 244608 if s < 1800 else 300000,
                        "r_qps": 8.0,
                        "k_inflight": 2025554,
                        "c_burst": 1066081,
                        "bucket_fill": round(1.0 - 0.6 * in_flight / 13, 4),
                        "req_fill": 0.9,
                        "inflight_calls": in_flight,
                        "inflight_tokens": in_flight * 150000,
                        "inflight_fill": round(in_flight * 150000 / 2025554, 4),
                        "queue_len": 2 if in_flight >= 12 else 0,
                        "head_waiting_s": 1.4 if in_flight >= 12 else None,
                        "admits_60s": in_flight * 6,
                        "over_2s_60s": 1 if in_flight >= 12 else 0,
                        "mean_wait_ms_60s": 120.0,
                        "overloads_60s": 0,
                    }
                },
            }
        )
        binding = (
            "paused"
            if paused
            else (
                "ceiling_override"
                if 1200 <= s < 1800
                else "paced" if in_flight >= 12 else "arrival_budget"
            )
        )
        capacity.append(
            {
                "ts": _iso(s + 0.4),
                "pool": "harness",
                "queue_depth": max(0, 13 - in_flight - resolved),
                "not_visible": in_flight,
                "current_workers": in_flight,
                "desired": 13 if 1200 <= s < 1800 else ceiling,
                "binding_constraint": binding,
                "ceiling_utilization": round(0.6 * in_flight / 13, 4),
                "decision_age_s": 4.0,
                "eta_low_s": 600,
                "eta_high_s": 1700,
                "constants_source": "pacer_cfg",
                "pacer_queue_len": 2 if in_flight >= 12 else 0,
                "paced_over_2s_share": 0.05,
                "decision": {
                    "decided_at": (_START + timedelta(seconds=s)).timestamp(),
                    "mode": "live",
                    "desired_ceiling": ceiling,
                    "binding_constraint": binding,
                    "static_cap": 145,
                    "ceiling_override": 13 if 1200 <= s < 1800 else None,
                    "cluster": "arn:aws:ecs:us-west-2:123456789012:cluster/eval-dev-cluster",
                    "subnets": ["subnet-0123456789abcdef0"],
                    "aliases": {
                        "deepseek-v4-flash-0731-mini": {
                            "ceiling": ceiling,
                            "binding": binding,
                            "curve_source": "fitted",
                            "arrival_tok_s": in_flight * 12000,
                            "cached_share": 0.65,
                        }
                    },
                },
            }
        )
        capacity.append(
            {
                "ts": _iso(s + 0.6),
                "pool": "eval",
                "queue_depth": 1 if 400 < s < 2000 else 0,
                "not_visible": 0,
                "current_workers": 1 if 400 < s < 2100 else 0,
                "desired": 1 if 400 < s < 2000 else 0,
                "binding_constraint": "queue_empty" if s <= 400 or s >= 2000 else "at_max_workers",
                "ceiling_utilization": None,
                "decision_age_s": 5.0,
                "eta_low_s": 240,
                "eta_high_s": 900,
                "constants_source": None,
                "pacer_queue_len": None,
                "paced_over_2s_share": None,
                "decision": {
                    "decided_at": 0,
                    "mode": "live",
                    "desired_ceiling": 1,
                    "binding_constraint": "at_max_workers",
                },
            }
        )

    outcomes = [
        ("RESOLVED", "resolved", None, None),
        ("RESOLVED", "resolved", None, None),
        ("RESOLVED", "resolved", None, None),
        ("RESOLVED", "resolved", None, None),
        ("RESOLVED", "resolved", None, None),
        ("UNRESOLVED", "unresolved", None, None),
        ("EMPTY_PATCH", None, "EMPTY_PATCH", None),
        ("HARNESS_MAX_TURNS_EXCEEDED", None, "HARNESS_MAX_TURNS_EXCEEDED", None),
        ("UNRESOLVED", "unresolved", None, True),
        ("ABORTED_IN_FLIGHT", None, None, None),
        ("ABORTED_IN_FLIGHT", None, None, None),
        ("NEVER_DISPATCHED", None, None, None),
        ("NEVER_DISPATCHED", None, None, None),
    ]
    lane_rows: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for i, (iid, (state, verdict, category, invalid)) in enumerate(
        zip(_IDS, outcomes, strict=True)
    ):
        landed = 400 + i * 130
        n_calls = 0 if state == "NEVER_DISPATCHED" else 12 + (i * 7) % 50
        lane_rows.append(
            {
                "instance_id": iid,
                "attempt_number": 1,
                "phase": "harness",
                "state": "PATCH_READY" if verdict else state,
                "verdict": None,
                "error_category": category,
                "retry_reason": None,
                "grade_invalid": None,
                "created_at": _iso(landed),
                "turns_used": n_calls or None,
                "input_tokens": n_calls * 9000 or None,
                "output_tokens": n_calls * 110 or None,
                "cached_tokens": int(n_calls * 9000 * 0.65) or None,
                "reasoning_tokens": n_calls * 30 or None,
                "cost_usd": round(n_calls * 0.00045, 6) if n_calls else None,
                "paced_wait_ms_total": n_calls * 40 if n_calls else None,
                "overload_retries_total": 1 if i == 3 else 0 if n_calls else None,
                "task_observed_s": 380.0 if n_calls else None,
                "agent_s": 300.0 if n_calls else None,
                "eval_test_s": None,
                "leak_detectable": True,
                "leaked_node_ids": [],
                "touches_test_files": False,
            }
        )
        if verdict or invalid:
            lane_rows.append(
                {
                    "instance_id": iid,
                    "attempt_number": 1,
                    "phase": "eval",
                    "state": state,
                    "verdict": verdict,
                    "error_category": None,
                    "retry_reason": None,
                    "grade_invalid": invalid,
                    "created_at": _iso(landed + 90),
                    "turns_used": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "cached_tokens": None,
                    "reasoning_tokens": None,
                    "cost_usd": None,
                    "paced_wait_ms_total": None,
                    "overload_retries_total": None,
                    "task_observed_s": None,
                    "agent_s": None,
                    "eval_test_s": 60.0,
                    "leak_detectable": True,
                    "leaked_node_ids": [],
                    "touches_test_files": False,
                }
            )
        first = landed - 300
        for k in range(n_calls):
            calls.append(
                {
                    "instance_id": iid,
                    "attempt_number": 1,
                    "call_index": k,
                    "started_at": _iso(first + k * (300 / max(1, n_calls))),
                    "latency_ms": 4200 + (k * 37) % 900,
                    "ttft_ms": 800,
                    "paced_wait_ms": 40 if k % 5 else 900,
                    "overload_retries": 1 if (i == 3 and k == 4) else 0,
                    "http_status": 200 if not (i == 3 and k == 4) else 429,
                    "error_type": None if not (i == 3 and k == 4) else "rate_limit",
                    "finish_reason": "stop",
                    "input_tokens": 9000,
                    "output_tokens": 110,
                    "cached_tokens": 5850,
                    "reasoning_tokens": 30,
                    "cost_usd": 0.00045,
                    "model_resolved": "deepseek/deepseek-v4-flash-0731",
                    "provider_name": "OpenInference",
                }
            )

    timeline = {
        "run_id": run_id,
        "status": "aborted",
        "window_start": _iso(0),
        "window_end": _iso(total_s + 300),
        "stamps": {
            "run_id": run_id,
            "status": "aborted",
            "created_at": _iso(-45),
            "dispatched_at": _iso(0),
            "stop_requested_at": _iso(2100),
            "stop_scope": "harness",
            "stop_reason": "fixture: operator abort",
            "stopped_at": _iso(total_s + 300),
            "finalised_at": _iso(total_s + 300),
        },
        "targets": [{"harness": "mini_swe_agent", "model_alias": "deepseek-v4-flash-0731-mini"}],
        "tick_interval_s": 30,
        "ticks": ticks,
        "capacity": capacity,
        "events": [
            {
                "id": 1,
                "ts": _iso(900),
                "run_id": None,
                "kind": "pause",
                "actor": "operator",
                "reason": "fixture: checking a stuck lane",
                "detail": {"pools": ["harness"]},
            },
            {
                "id": 2,
                "ts": _iso(1080),
                "run_id": None,
                "kind": "resume",
                "actor": "operator",
                "reason": "",
                "detail": {"pools": ["harness"]},
            },
        ],
        "limit_edits": [
            {
                "id": 1,
                "ts": _iso(1200),
                "scope": "run",
                "target": run_id,
                "field": "ceiling_override",
                "old_value": None,
                "new_value": "13",
                "actor": "operator",
                "reason": "fixture: all 13 in parallel",
            },
            {
                "id": 2,
                "ts": _iso(1800),
                "scope": "pacer",
                "target": "deepseek-v4-flash-0731-mini",
                "field": "r_tok",
                "old_value": "244608.0",
                "new_value": "300000.0",
                "actor": "operator",
                "reason": "fixture: raise the rate",
            },
            {
                "id": 3,
                "ts": _iso(1800),
                "scope": "run",
                "target": run_id,
                "field": "ceiling_override",
                "old_value": "13",
                "new_value": None,
                "actor": "operator",
                "reason": "",
            },
        ],
        "discovery": [
            {
                "id": 1,
                "ts": _iso(-5400),
                "model_alias": "deepseek-v4-flash-0731",
                "run_id": None,
                "event_type": "step",
                "tpm_value": 3195000,
                "at_concurrency": 15,
                "task_id": None,
                "notes": "ramp step 0 clean",
            },
            {
                "id": 2,
                "ts": _iso(-5300),
                "model_alias": "deepseek-v4-flash-0731",
                "run_id": None,
                "event_type": "step",
                "tpm_value": 4790000,
                "at_concurrency": 22,
                "task_id": None,
                "notes": "ramp step 1 clean",
            },
            {
                "id": 3,
                "ts": _iso(-5200),
                "model_alias": "deepseek-v4-flash-0731",
                "run_id": None,
                "event_type": "ramp_strain_rate_tok_per_s",
                "tpm_value": 80532,
                "at_concurrency": 33,
                "task_id": None,
                "notes": "soft strain",
            },
        ],
        "lane_rows": lane_rows,
        "calls": calls,
    }
    export = {"schema_version": 1, "provenance": _provenance(run_id, "mini_swe_agent")}
    return timeline, export


def _uninstrumented(run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    timeline, export = _instrumented(run_id)
    for t in timeline["ticks"]:
        for k in (
            "tok_in",
            "tok_out",
            "tok_cached",
            "tok_reasoning",
            "cost_usd_live",
            "cost_usd_landed",
        ):
            t[k] = None
        t["pacer"] = {"deepseek-v4-flash-0731-mini": None}

    # A five-minute observer gap in the middle: rows absent, never zero.
    def _in_gap(ts: str) -> bool:
        rel = timeline_export.to_t(ts, timeline_export.parse_iso(timeline["window_start"]) or 0)
        return rel is not None and 600 <= rel < 900

    timeline["ticks"] = [t for t in timeline["ticks"] if not _in_gap(t["ts"])]
    timeline["capacity"] = [c for c in timeline["capacity"] if not _in_gap(c["ts"])]
    for row in timeline["lane_rows"]:
        for k in (
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "cost_usd",
            "paced_wait_ms_total",
            "overload_retries_total",
        ):
            row[k] = None
    for c in timeline["calls"]:
        for k in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "cost_usd"):
            c[k] = None
    export["provenance"]["harness"] = "opencode"
    export["provenance"]["model_alias"] = "deepseek-v4-flash-0731-opencode"
    timeline["targets"] = [
        {"harness": "opencode", "model_alias": "deepseek-v4-flash-0731-opencode"}
    ]
    return timeline, export


def build_all() -> dict[str, dict[str, bytes]]:
    out: dict[str, dict[str, bytes]] = {}
    for name, builder in (
        ("run-fixture-paused-aborted", _instrumented),
        ("run-fixture-uninstrumented", _uninstrumented),
    ):
        run_id = f"01JFIXTURE{name.upper().replace('-', '')[:16]:<16}".replace(" ", "X")
        timeline, export = builder(run_id)
        out[name] = timeline_export.build_site_files(
            timeline, export, exporter_sha="fixture", exported_at=_EXPORTED_AT
        )
    return out


def main() -> int:
    for name, files in build_all().items():
        base = _FIXTURES_DIR / name
        for rel, data in files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        manifest = json.loads(files["manifest.json"])
        print(
            f"{name}: {len(files)} files, {manifest['files']['fleet.json']['points']} points, "
            f"{manifest['files']['fleet.json']['events']} events, "
            f"{manifest['files']['instances.json']['lanes']} lanes -> {base}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
