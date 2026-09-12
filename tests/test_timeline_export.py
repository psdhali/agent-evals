"""The site-side timeline files (timeline plan §2, 2026-09-04) — built by the real exporter.

Pins the contract: integer ``t`` since the window start, columnar series aligned across the
run ticks and both capacity pools, derived rates that are None across a gap, decimation
that preserves None, every event source folded in plus the derived ceiling / binding
changes, lanes classified like the results export, per-lane call files, allowlist pruning
of the decision records, a denylist that FAILS the export, and byte-identical re-runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from swebench_eval.orchestrator import timeline_export as te

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_timeline_fixtures as fixtures

# (timeline, export) — the two JSON documents the exporter consumes
_Synthetic = tuple[dict[str, Any], dict[str, Any]]


@pytest.fixture(scope="module")
def synthetic() -> _Synthetic:
    pair: _Synthetic = fixtures._instrumented("01JTESTRUN0000000000000000")
    return pair


@pytest.fixture(scope="module")
def files(synthetic: _Synthetic) -> dict[str, bytes]:
    timeline, export = synthetic
    return te.build_site_files(
        timeline, export, exporter_sha="abc", exported_at="2026-09-06T20:00:00Z"
    )


def _load(files: dict[str, bytes], name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(files[name])
    return doc


def test_fleet_series_are_columnar_on_one_integer_t_grid(files: dict[str, bytes]) -> None:
    fleet = _load(files, "fleet.json")
    s = fleet["series"]
    n = len(s["t"])
    assert n == 81  # 40 min at 30 s + 1
    assert s["t"][:3] == [0, 30, 60]
    assert all(len(v) == n for v in s.values())
    # run ticks and BOTH capacity pools landed on the same rows
    assert s["in_flight"][10] == s["harness_workers"][10]
    assert s["desired_ceiling"][0] == 3 and s["eval_workers"][20] == 1
    assert "pacer.deepseek-v4-flash-0731-mini.bucket_fill" in s
    assert "planner.deepseek-v4-flash-0731-mini.curve_source" in s
    assert s["ceiling_override"][45] == 13 and s["ceiling_override"][0] is None
    assert s["static_cap"][0] == 145


def test_derived_rates_exist_between_ticks_and_are_none_across_a_gap() -> None:
    timeline, export = fixtures._uninstrumented("01JTESTRUN0000000000000001")
    f = te.build_site_files(timeline, export, exporter_sha="abc", exported_at="x")
    s = _load(f, "fleet.json")["series"]
    assert 600 not in s["t"] and 870 not in s["t"]  # the gap is ABSENT, not zeros
    assert all(v is None for v in s["tok_in"])  # uninstrumented: never 0
    assert all(v is None for v in s["offered_tok_s"])
    inst, _ = fixtures._instrumented("01JTESTRUN0000000000000002")
    s2 = _load(te.build_site_files(inst, export, exported_at="x"), "fleet.json")["series"]
    assert s2["offered_tok_s"][0] is None  # nothing before the first tick
    assert s2["offered_tok_s"][5] is not None and s2["offered_tok_s"][5] > 0
    assert s2["usd_per_min"][5] is not None


def test_decimation_preserves_none_and_categoricals() -> None:
    cols: dict[str, list[Any]] = {
        "t": list(range(0, 3000, 30)),  # 100 raw points
        "in_flight": [None if i % 2 else i for i in range(100)],
        "flag": [i % 3 == 0 for i in range(100)],
        "binding": ["a"] * 50 + ["b"] * 50,
        "all_null": [None] * 100,
    }
    out, info = te.decimate(cols, 25)
    assert info["bucket"] == 4 and len(out["t"]) == 25
    assert out["t"][0] == 0 and out["t"][1] == 120
    assert out["all_null"] == [None] * 25  # never coerced to 0
    assert out["in_flight"][0] == pytest.approx(1.0)  # mean of the measured (0, 2)
    assert out["in_flight_n_null"][0] == 2
    assert out["binding"][11] == "a" and out["binding"][13] == "b"  # bucket 12 straddles
    assert out["flag"][0] is True
    same, info2 = te.decimate(cols, 500)
    assert same is cols and info2["bucket"] == 1


def test_every_event_source_is_folded_in_and_changes_are_derived(files: dict[str, bytes]) -> None:
    kinds = {e["kind"] for e in _load(files, "fleet.json")["events"]}
    assert {
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
    } <= kinds
    events = _load(files, "fleet.json")["events"]
    pause = next(e for e in events if e["kind"] == "pause")
    assert pause["t"] == 900 and pause["pools"] == ["harness"] and pause["global"] is True
    edit = next(e for e in events if e["kind"] == "limit_edit" and e["field"] == "ceiling_override")
    assert edit["t"] == 1200 and edit["new"] == "13" and edit["target"] is None
    assert any(e["kind"] == "ceiling_change" and e["from"] == 3 and e["to"] == 4 for e in events)
    assert any(e["kind"] == "binding_change" and e["to"] == "paused" for e in events)
    assert events == sorted(events, key=lambda e: (e["t"], e["kind"]))


def test_lanes_are_classified_like_the_results_export(files: dict[str, bytes]) -> None:
    lanes = _load(files, "instances.json")["lanes"]
    by_outcome: dict[str, int] = {}
    for lane in lanes:
        by_outcome[lane["outcome"]] = by_outcome.get(lane["outcome"], 0) + 1
    assert by_outcome == {
        "resolved": 5,
        "unresolved": 1,
        "empty_patch": 1,
        "error": 1,
        "grade_invalid": 1,
        "aborted": 4,
    }
    lane = next(lane for lane in lanes if lane["instance_id"] == "django__django-11000")
    # The harness row's created_at is the launch SEED (not a landing); the eval row's is the
    # verdict landing; the lane ends at the verdict when there is one, else at the last call.
    assert lane["t_seeded"] == 400 and lane["t_landed"] == 490 and lane["t_end"] == 490
    empty = next(lane for lane in lanes if lane["outcome"] == "empty_patch")
    assert empty["t_landed"] is None and empty["t_end"] == empty["t_last_call"]
    assert lane["t_first_call"] == 100 and lane["calls"] == 12
    assert lane["samples"][0][1] == 0 and lane["samples"][-1][2] == pytest.approx(12 * 0.00045)
    never = next(lane for lane in lanes if lane["harness_state"] == "NEVER_DISPATCHED")
    assert never["calls"] is None and never["samples"] == [] and never["cost_usd"] is None
    assert all(len(lane["samples"]) <= te.LANE_SAMPLES_MAX + 1 for lane in lanes)


def test_call_files_are_columnar_metadata_only_one_per_lane(files: dict[str, bytes]) -> None:
    names = [k for k in files if k.startswith("calls/")]
    assert len(names) == 11  # 13 lanes minus the two never dispatched
    one = json.loads(files["calls/django__django-11000__1.json"])
    cols = one["columns"]
    assert cols["t"][0] == 100 and len(cols["t"]) == len(cols["latency_ms"]) == 12
    assert "tok_in" in cols and "cost_usd" in cols
    assert not any(k in cols for k in ("prompt", "response", "messages", "content"))


def test_manifest_copies_provenance_and_hashes_every_file(
    files: dict[str, bytes], synthetic: _Synthetic
) -> None:
    import hashlib

    manifest = _load(files, "manifest.json")
    assert manifest["provenance"] == synthetic[1]["provenance"]
    assert manifest["window"]["seconds"] == 40 * 60 + 300
    for name in ("fleet.json", "instances.json", "discovery.json"):
        assert manifest["files"][name]["sha256"] == hashlib.sha256(files[name]).hexdigest()
        assert manifest["files"][name]["bytes"] == len(files[name])
    assert manifest["files"]["calls"]["count"] == 11
    assert manifest["scrub"]["hits"] == 0
    assert "ceiling_override" in manifest["vocab"]["binding_constraint"]
    assert manifest["files"]["fleet.json"]["points"] == 81


def test_decision_records_are_pruned_by_allowlist_and_the_denylist_fails_the_export(
    synthetic: _Synthetic,
) -> None:
    timeline, export = synthetic
    text = b"".join(te.build_site_files(timeline, export, exported_at="x").values()).decode()
    # The fixture's decision records carry an ARN cluster and a subnet id: both pruned away.
    assert "arn:aws" not in text and "subnet-" not in text and "eval-dev-cluster" not in text
    # ...but a value that survives (a pause reason typed by an operator) is caught.
    poisoned = json.loads(json.dumps(timeline))
    poisoned["events"][0]["reason"] = "see arn:aws:ecs:us-west-2:123456789012:task/x"
    with pytest.raises(te.ScrubError, match="arn"):
        te.build_site_files(poisoned, export, exported_at="x")
    poisoned = json.loads(json.dumps(timeline))
    poisoned["stamps"]["stop_reason"] = "gateway.eval.internal timed out"
    with pytest.raises(te.ScrubError, match="hostname"):
        te.build_site_files(poisoned, export, exported_at="x")


def test_rerun_is_byte_identical_and_fixtures_match_the_generator() -> None:
    a = fixtures.build_all()
    b = fixtures.build_all()
    assert a.keys() == b.keys()
    for name in a:
        assert a[name] == b[name], name
    assert set(a) == {"run-fixture-paused-aborted", "run-fixture-uninstrumented"}
    null_fleet = json.loads(a["run-fixture-uninstrumented"]["fleet.json"])["series"]
    assert all(v is None for v in null_fleet["cost_usd_live"])
    null_lanes = json.loads(a["run-fixture-uninstrumented"]["instances.json"])["lanes"]
    assert all(lane["cost_usd"] is None for lane in null_lanes)
    assert all(s[2] is None for lane in null_lanes for s in lane["samples"])


def test_fixture_run_ids_are_obviously_synthetic() -> None:
    for files in fixtures.build_all().values():
        assert json.loads(files["manifest.json"])["run_id"].startswith("01JFIXTURE")


# ── 2026-09-08: scripts/export_run_timeline.py --site path resolution ──────────


def test_resolve_site_root_never_nests_site_inside_site(tmp_path) -> None:
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "export_run_timeline",
        pathlib.Path(__file__).resolve().parents[1] / "scripts" / "export_run_timeline.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    repo = tmp_path / "site-repo"
    (repo / "site").mkdir(parents=True)
    assert mod.resolve_site_root(repo) == repo / "site"  # the repo root: unchanged
    assert mod.resolve_site_root(repo / "site") == repo / "site"  # site/ itself: no nesting
    adhoc = tmp_path / "exports" / "run-1" / "site"
    adhoc.mkdir(parents=True)
    assert mod.resolve_site_root(adhoc) == adhoc
    holds_data = tmp_path / "bundle"
    (holds_data / "data").mkdir(parents=True)
    assert mod.resolve_site_root(holds_data) == holds_data
    fresh = tmp_path / "fresh"
    assert mod.resolve_site_root(fresh) == fresh / "site"


# ── judge (2026-09-08) ────────────────────────────────────────────────────────────────────


def _judge_payload() -> dict[str, Any]:
    def dim(dim_id, scale, **kw):
        base = {
            "dimension_id": dim_id,
            "scale_type": scale,
            "score_numeric": 0 if scale != "boolean_with_span" else None,
            "score_secondary": None,
            "flag": False if scale == "boolean_with_span" else None,
            "span_start_turn": None,
            "span_end_turn": None,
            "reasoning": "nothing found",
            "evidence": [],
            "evidence_missing": False,
        }
        base.update(kw)
        return base

    def result(instance_id, dims, **kw):
        row = {
            "instance_id": instance_id,
            "attempt_number": 1,
            "judged_at": "2026-09-08T00:00:00+00:00",
            "judge_model_resolved": "deepseek/deepseek-v4-flash-0731",
            "rubric_version": "1",
            "judge_prune_mode": "pruned",
            "input_truncated": False,
            "tool_output_pruned": False,
            "judge_parse_failed": False,
            "summary": "one paragraph",
            "judge_cost_usd": 0.002,
            "dimensions": dims,
        }
        row.update(kw)
        return row

    results = [
        result(
            "django__django-2",
            [
                dim(
                    "contamination",
                    "likert",
                    score_numeric=2,
                    reasoning="names the gold test",
                    evidence=[{"turn": 9, "quote": "test_structured"}],
                ),
                dim("environment_problem", "boolean_with_span", flag=True, span_start_turn=3),
                dim("tool_efficiency", "ratio", score_numeric=5, score_secondary=16),
            ],
        ),
        result("django__django-1", [dim("contamination", "likert", score_numeric=0)]),
        result(
            "django__django-3",
            [dim("hallucination", "count_and_severity", score_numeric=None, evidence_missing=True)],
            judge_parse_failed=True,
        ),
    ]
    passes = [
        {
            "pass_id": "judge-2",
            "created_at": "2026-09-08T02:00:00+00:00",
            "total_eligible": 0,
            "total_judged": 0,
            "total_skipped_over_budget": 0,
            "total_parse_failed": 0,
            "synthesis": "## Overview\n3 attempts judged. **django__django-2** showed contamination.",
            "synthesis_cost_usd": 0.02,
            "synthesis_model_resolved": "deepseek/deepseek-v4-flash-0731",
            "synthesis_error": None,
            "synthesis_only": True,
        },
        {
            "pass_id": "judge-1",
            "created_at": "2026-09-08T01:00:00+00:00",
            "total_eligible": 3,
            "total_judged": 3,
            "total_skipped_over_budget": 0,
            "total_parse_failed": 1,
            "synthesis": None,
            "synthesis_cost_usd": 0.0,
            "synthesis_model_resolved": None,
            "synthesis_error": "JudgeCallTransportError: 429",
            "synthesis_only": False,
        },
    ]
    return {"results": results, "passes": passes}


def test_judge_files_carry_every_verdict_the_finding_counts_and_the_latest_report(
    synthetic: _Synthetic,
) -> None:
    timeline, export = synthetic
    files = te.build_site_files(
        timeline,
        export,
        exporter_sha="abc",
        exported_at="2026-09-06T20:00:00Z",
        judge=_judge_payload(),
    )
    judge = _load(files, "judge.json")
    assert judge["judged_attempts"] == 3
    # sorted by instance id, every dimension with reasoning + evidence + honesty flags
    assert [a["instance_id"] for a in judge["attempts"]] == [
        "django__django-1",
        "django__django-2",
        "django__django-3",
    ]
    a2 = judge["attempts"][1]
    assert a2["findings"] == ["contamination", "environment_problem", "tool_efficiency"]
    assert a2["dimensions"][0]["evidence"] == [{"turn": 9, "quote": "test_structured"}]
    assert a2["dimensions"][1]["span"] == [3, None]
    a3 = judge["attempts"][2]
    assert a3["findings"] == ["parse_failed", "no_evidence"]
    assert judge["attempts"][0]["findings"] == []
    # counts mirror the UI's findings filter
    assert judge["finding_counts"]["contamination"] == 1
    assert judge["finding_counts"]["environment_problem"] == 1
    assert judge["finding_counts"]["tool_efficiency"] == 1  # 5/16 >= 25 %
    assert judge["finding_counts"]["no_evidence"] == 1
    assert judge["finding_counts"]["parse_failed"] == 1
    assert judge["finding_counts"]["any"] == 2 and judge["finding_counts"]["none"] == 1
    # report = the latest pass that has one (the report-only pass), ledger has both
    assert judge["report"]["pass_id"] == "judge-2"
    assert judge["report"]["file"] == "judge_report.md"
    assert files["judge_report.md"].decode().startswith("## Overview")
    assert [p["pass_id"] for p in judge["passes"]] == ["judge-2", "judge-1"]
    assert judge["passes"][1]["report_error"] == "JudgeCallTransportError: 429"
    manifest = _load(files, "manifest.json")
    assert manifest["files"]["judge.json"]["judged_attempts"] == 3
    assert manifest["files"]["judge.json"]["finding_counts"]["any"] == 2
    assert manifest["files"]["judge_report.md"]["pass_id"] == "judge-2"
    assert manifest["scrub"]["hits"] == 0

    # byte-identical re-run
    again = te.build_site_files(
        timeline,
        export,
        exporter_sha="abc",
        exported_at="2026-09-06T20:00:00Z",
        judge=_judge_payload(),
    )
    assert again["judge.json"] == files["judge.json"]
    assert again["judge_report.md"] == files["judge_report.md"]


def test_without_judge_the_output_is_unchanged_and_no_judge_files_exist(
    files: dict[str, bytes], synthetic: _Synthetic
) -> None:
    assert "judge.json" not in files and "judge_report.md" not in files
    assert "judge.json" not in _load(files, "manifest.json")["files"]
    timeline, export = synthetic
    empty = te.build_site_files(
        timeline,
        export,
        exporter_sha="abc",
        exported_at="2026-09-06T20:00:00Z",
        judge={"results": [], "passes": []},
    )
    assert empty == files


def test_a_denylist_hit_inside_judge_reasoning_fails_the_export(
    synthetic: _Synthetic,
) -> None:
    timeline, export = synthetic
    payload = _judge_payload()
    payload["results"][0]["dimensions"][0][
        "reasoning"
    ] = "cites arn:aws:ecs:us-west-2:123456789012:task/x in the trajectory"
    with pytest.raises(te.ScrubError):
        te.build_site_files(
            timeline, export, exporter_sha="abc", exported_at="2026-09-06T20:00:00Z", judge=payload
        )


def test_judge_export_marks_a_timed_out_judgment_as_its_own_finding(
    synthetic: _Synthetic,
) -> None:
    timeline, export = synthetic
    payload = _judge_payload()
    payload["results"][0]["judge_method"] = "timeout"
    for d in payload["results"][0]["dimensions"]:
        d.update({"score_numeric": None, "flag": None, "evidence_missing": False})
    files = te.build_site_files(
        timeline, export, exporter_sha="abc", exported_at="2026-09-06T20:00:00Z", judge=payload
    )
    judge = _load(files, "judge.json")
    by_id = {a["instance_id"]: a for a in judge["attempts"]}
    assert by_id["django__django-2"]["findings"] == ["timeout"]
    assert by_id["django__django-2"]["judge_method"] == "timeout"
    assert judge["finding_counts"]["timeout"] == 1
    assert judge["finding_counts"]["contamination"] == 0
    assert "timeout" in judge["finding_kinds"]
