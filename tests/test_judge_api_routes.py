"""offline-analysis-design.md §9.5/§9.6 — the judge HTTP routes.

Hermetic (matches test_dashboard_api.py's convention): `_db` is stubbed with
a bare connection stand-in, and the `swebench_eval.analysis.judge` functions
themselves are mocked — they're already covered against a real Postgres in
test_judge_pass_integration.py. This file verifies only the wiring: routes
exist, request/response shapes round-trip, and the RunTask-vs-background-
fallback branch in the launch route behaves correctly.
"""

from __future__ import annotations

from unittest import mock

import pytest
from fastapi.testclient import TestClient

from swebench_eval.analysis import calibration as calibration_module
from swebench_eval.analysis import judge as judge_module
from swebench_eval.orchestrator.api.main import app


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return []  # no judge_results rows yet -> nothing "already judged"


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def close(self) -> None:
        pass


def _candidate(instance_id="i1", attempt=1, harness="mini", outcome="resolved", always=False):
    return judge_module.JudgeCandidate(
        instance_id=instance_id,
        attempt_number=attempt,
        harness=harness,
        outcome=outcome,
        patch_path="p",
        trajectory_path="t",
        leaked_node_ids=None,
        always_judge=always,
    )


@pytest.fixture(autouse=True)
def _wire_db():
    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=_FakeConn()):
        yield


def test_candidates_route_returns_eval_status_and_already_judged() -> None:
    with mock.patch.object(judge_module, "fetch_candidates", return_value=[_candidate()]):
        resp = TestClient(app).get("/runs/run-1/judge/candidates")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-1"
    assert body["candidates"][0]["instance_id"] == "i1"
    assert body["candidates"][0]["already_judged"] is False


def test_estimate_route_returns_a_number() -> None:
    with mock.patch.object(
        judge_module, "fetch_candidates", return_value=[_candidate(), _candidate("i2")]
    ):
        resp = TestClient(app).get("/runs/run-1/judge/estimate?prune_mode=pruned")
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidate_count"] == 2
    assert body["estimated_cost_usd"] > 0
    assert body["status"] == "estimate"


def test_estimate_route_parses_instance_ids_csv() -> None:
    captured = {}

    def _fake_fetch(conn, run_id, instance_ids=None):
        captured["instance_ids"] = instance_ids
        return []

    with mock.patch.object(judge_module, "fetch_candidates", side_effect=_fake_fetch):
        TestClient(app).get("/runs/run-1/judge/estimate?instance_ids=a,b,c")
    assert captured["instance_ids"] == ["a", "b", "c"]


def test_launch_route_rejects_no_eligible_candidates() -> None:
    with mock.patch.object(judge_module, "fetch_candidates", return_value=[]):
        resp = TestClient(app).post("/runs/run-1/judge", json={})
    assert resp.status_code == 400


def test_launch_route_local_dev_fallback_when_no_task_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No JUDGE_TASK_FAMILY configured -> in-process background task, never
    a RunTask call (local dev has no ECS)."""
    monkeypatch.delenv("JUDGE_TASK_FAMILY", raising=False)
    calls = {}

    def _fake_run(run_id, **kwargs):
        calls["run_id"] = run_id
        calls.update(kwargs)

    with (
        mock.patch.object(judge_module, "fetch_candidates", return_value=[_candidate()]),
        mock.patch(
            "swebench_eval.orchestrator.control_plane.llm_judge_task.run", side_effect=_fake_run
        ),
    ):
        resp = TestClient(app).post("/runs/run-1/judge", json={"prune_mode": "full"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-1"
    assert body["pass_id"].startswith("judge-")
    assert body["status"] == "started"
    # background_tasks run synchronously with TestClient before the response is returned
    assert calls["run_id"] == "run-1"
    assert calls["prune_mode"] == "full"


def test_launch_route_runtask_when_task_family_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUDGE_TASK_FAMILY", "eval-dev-llm-judge")
    monkeypatch.setenv("CLUSTER", "eval-dev-cluster")
    monkeypatch.setenv("JUDGE_SUBNET_IDS", '["subnet-1"]')
    monkeypatch.setenv("JUDGE_SECURITY_GROUP_IDS", '["sg-1"]')

    fake_ecs = mock.MagicMock()
    fake_boto3 = mock.MagicMock()
    fake_boto3.client.return_value = fake_ecs

    with (
        mock.patch.object(judge_module, "fetch_candidates", return_value=[_candidate()]),
        mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
    ):
        resp = TestClient(app).post(
            "/runs/run-1/judge", json={"instance_ids": ["i1"], "max_spend_usd": 3.5}
        )

    assert resp.status_code == 200
    fake_ecs.run_task.assert_called_once()
    kwargs = fake_ecs.run_task.call_args.kwargs
    assert kwargs["taskDefinition"] == "eval-dev-llm-judge"
    env = kwargs["overrides"]["containerOverrides"][0]["environment"]
    env_by_name = {e["name"]: e["value"] for e in env}
    assert env_by_name["JUDGE_RUN_ID"] == "run-1"
    assert env_by_name["JUDGE_MAX_SPEND_USD"] == "3.5"
    assert env_by_name["JUDGE_INSTANCE_IDS"] == '["i1"]'


def test_launch_route_runtask_failure_surfaces_as_502_never_looks_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_TASK_FAMILY", "eval-dev-llm-judge")
    fake_ecs = mock.MagicMock()
    fake_ecs.run_task.side_effect = RuntimeError("boto3 explosion")
    fake_boto3 = mock.MagicMock()
    fake_boto3.client.return_value = fake_ecs

    with (
        mock.patch.object(judge_module, "fetch_candidates", return_value=[_candidate()]),
        mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
    ):
        resp = TestClient(app).post("/runs/run-1/judge", json={})
    assert resp.status_code == 502


def test_results_route_shapes_dimensions() -> None:
    fake_row = {
        "instance_id": "i1",
        "attempt_number": 1,
        "judged_at": "2026-09-01T00:00:00+00:00",
        "judge_model_resolved": "deepseek/deepseek-v4-flash-0731",
        "rubric_version": "1",
        "judge_prune_mode": "pruned",
        "input_truncated": False,
        "tool_output_pruned": True,
        "judge_parse_failed": False,
        "summary": "clean",
        "judge_cost_usd": 0.002,
        "dimensions": [
            {
                "dimension_id": "contamination",
                "scale_type": "likert",
                "score_numeric": 0,
                "score_secondary": None,
                "flag": None,
                "span_start_turn": None,
                "span_end_turn": None,
                "reasoning": "nothing found",
                "evidence": [],
                "evidence_missing": False,
            }
        ],
    }
    with mock.patch.object(judge_module, "fetch_latest_results", return_value=[fake_row]):
        resp = TestClient(app).get("/runs/run-1/judge/results")
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["dimensions"][0]["dimension_id"] == "contamination"
    assert body["results"][0]["tool_output_pruned"] is True


def test_passes_route_surfaces_a_budget_truncated_pass() -> None:
    """review 2026-09-02 §3.2/§5.1: this is the launch-control banner's data
    source — a pass that hit the ceiling must be distinguishable from one
    that finished, not indistinguishable JSON."""
    fake_pass = {
        "pass_id": "judge-1",
        "requested_rate": 1.0,
        "seed": None,
        "total_eligible": 2500,
        "total_judged": 1750,
        "total_skipped_over_budget": 750,
        "total_parse_failed": 3,
        "created_at": "2026-09-02T00:00:00+00:00",
        # 2026-09-08: the pass-level synthesis rides on the same row
        "synthesis": "## Overview\n1750 attempts judged …",
        "synthesis_cost_usd": 0.0213,
        "synthesis_model_resolved": "deepseek/deepseek-v4-flash-0731",
        "synthesis_error": None,
    }
    with mock.patch.object(judge_module, "fetch_pass_summaries", return_value=[fake_pass]):
        resp = TestClient(app).get("/runs/run-1/judge/passes")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-1"
    assert body["passes"][0]["total_eligible"] == 2500
    assert body["passes"][0]["total_skipped_over_budget"] == 750
    assert body["passes"][0]["total_parse_failed"] == 3
    assert body["passes"][0]["synthesis"].startswith("## Overview")
    assert body["passes"][0]["synthesis_cost_usd"] == 0.0213
    assert body["passes"][0]["synthesis_error"] is None


def test_passes_route_tolerates_a_pass_from_before_the_synthesis_existed() -> None:
    fake_pass = {
        "pass_id": "judge-0",
        "requested_rate": 1.0,
        "seed": None,
        "total_eligible": 4,
        "total_judged": 4,
        "total_skipped_over_budget": 0,
        "total_parse_failed": 0,
        "created_at": "2026-09-02T00:00:00+00:00",
    }
    with mock.patch.object(judge_module, "fetch_pass_summaries", return_value=[fake_pass]):
        resp = TestClient(app).get("/runs/run-1/judge/passes")
    assert resp.status_code == 200
    assert resp.json()["passes"][0]["synthesis"] is None
    assert resp.json()["passes"][0]["synthesis_error"] is None


def test_calibration_review_route_records_and_echoes_back() -> None:
    with mock.patch.object(calibration_module, "record_review", return_value=None) as fake_record:
        resp = TestClient(app).post(
            "/runs/run-1/judge/results/inst-1/1/review",
            json={
                "judged_at": "2026-09-01T00:00:00+00:00",
                "dimension_id": "contamination",
                "decision": "deny",
                "reviewer_reasoning": "quote does not support this",
                "corrected_score_numeric": 0,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-1"
    assert body["instance_id"] == "inst-1"
    assert body["dimension_id"] == "contamination"
    assert body["status"] == "recorded"
    fake_record.assert_called_once()
    kwargs = fake_record.call_args.kwargs
    assert kwargs["decision"] == "deny"
    assert kwargs["judged_at"] == "2026-09-01T00:00:00+00:00"


def test_calibration_review_route_missing_target_is_404_not_500() -> None:
    with mock.patch.object(
        calibration_module,
        "record_review",
        side_effect=calibration_module.ReviewTargetNotFoundError("no such row"),
    ):
        resp = TestClient(app).post(
            "/runs/run-1/judge/results/inst-1/1/review",
            json={
                "judged_at": "2026-09-01T00:00:00+00:00",
                "dimension_id": "contamination",
                "decision": "approve",
                "reviewer_reasoning": "fine",
            },
        )
    assert resp.status_code == 404


def test_calibration_review_route_missing_reasoning_is_400() -> None:
    with mock.patch.object(
        calibration_module,
        "record_review",
        side_effect=ValueError("reviewer_reasoning is required"),
    ):
        resp = TestClient(app).post(
            "/runs/run-1/judge/results/inst-1/1/review",
            json={
                "judged_at": "2026-09-01T00:00:00+00:00",
                "dimension_id": "contamination",
                "decision": "approve",
                "reviewer_reasoning": "x",
            },
        )
    assert resp.status_code == 400


def test_calibration_summary_route_is_global_not_run_scoped() -> None:
    """No run_id in the path (§11) — this asserts the route exists at
    /judge/calibration, not under /runs/{run_id}/..."""
    fake_summary = [
        calibration_module.DimensionCalibration(
            dimension_id="contamination",
            reviewed_count=25,
            distinct_instances_reviewed=22,
            approve_count=20,
            deny_count=5,
            endorsement_rate=0.8,
            cleared_threshold=True,
        ),
        calibration_module.DimensionCalibration(
            dimension_id="hallucination",
            reviewed_count=0,
            distinct_instances_reviewed=0,
            approve_count=0,
            deny_count=0,
            endorsement_rate=None,
            cleared_threshold=False,
        ),
    ]
    with mock.patch.object(calibration_module, "calibration_summary", return_value=fake_summary):
        resp = TestClient(app).get("/judge/calibration")
    assert resp.status_code == 200
    body = resp.json()
    by_dim = {d["dimension_id"]: d for d in body["dimensions"]}
    assert by_dim["contamination"]["cleared_threshold"] is True
    assert by_dim["contamination"]["endorsement_rate"] == 0.8
    assert by_dim["hallucination"]["endorsement_rate"] is None  # never 0 for "not reviewed"
    assert body["min_distinct_reviewed_instances"] == 20


def test_review_history_route_requires_judged_at_and_returns_newest_first() -> None:
    fake_reviews = [
        {
            "dimension_id": "contamination",
            "decision": "approve",
            "reviewer_reasoning": "re-checked, fine",
            "reviewed_by": "operator",
            "reviewed_at": "2026-09-02T01:00:00+00:00",
            "corrected_score_numeric": None,
            "corrected_score_secondary": None,
            "corrected_flag": None,
        },
        {
            "dimension_id": "contamination",
            "decision": "deny",
            "reviewer_reasoning": "looked wrong at first",
            "reviewed_by": "operator",
            "reviewed_at": "2026-09-02T00:00:00+00:00",
            "corrected_score_numeric": 0.0,
            "corrected_score_secondary": None,
            "corrected_flag": None,
        },
    ]
    with mock.patch.object(
        calibration_module, "fetch_reviews_for_result", return_value=fake_reviews
    ) as fake_fetch:
        resp = TestClient(app).get(
            "/runs/run-1/judge/results/inst-1/1/reviews",
            params={"judged_at": "2026-09-01T00:00:00+00:00"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reviews"][0]["decision"] == "approve"
    assert body["reviews"][1]["corrected_score_numeric"] == 0.0
    fake_fetch.assert_called_once_with(mock.ANY, "run-1", "inst-1", 1, "2026-09-01T00:00:00+00:00")


def test_review_history_route_422s_without_judged_at() -> None:
    resp = TestClient(app).get("/runs/run-1/judge/results/inst-1/1/reviews")
    assert resp.status_code == 422  # FastAPI's required-query-param rejection


def test_launch_route_synthesis_only_regenerates_the_report_without_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-08 (owner): "regenerate report" — no eligible candidates needed (every
    attempt is already judged), the task gets JUDGE_SYNTHESIS_ONLY=1, the estimate is the
    synthesis preview, and a run with nothing judged is refused (nothing to report on)."""
    monkeypatch.setenv("JUDGE_TASK_FAMILY", "eval-dev-llm-judge")
    monkeypatch.setenv("CLUSTER", "eval-dev-cluster")
    monkeypatch.setenv("JUDGE_SUBNET_IDS", '["subnet-1"]')
    monkeypatch.setenv("JUDGE_SECURITY_GROUP_IDS", '["sg-1"]')
    fake_ecs = mock.MagicMock()
    fake_boto3 = mock.MagicMock()
    fake_boto3.client.return_value = fake_ecs

    with (
        mock.patch.object(judge_module, "fetch_candidates", return_value=[]),
        mock.patch.object(judge_module, "fetch_latest_results", return_value=[{"x": 1}]),
        mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
    ):
        resp = TestClient(app).post(
            "/runs/run-1/judge", json={"synthesis_only": True, "max_spend_usd": 1.0}
        )
    assert resp.status_code == 200
    assert resp.json()["estimated_cost_usd"] == judge_module.SYNTHESIS_ESTIMATE_USD
    env = fake_ecs.run_task.call_args.kwargs["overrides"]["containerOverrides"][0]["environment"]
    env_by_name = {e["name"]: e["value"] for e in env}
    assert env_by_name["JUDGE_SYNTHESIS_ONLY"] == "1"
    assert env_by_name["JUDGE_MAX_SPEND_USD"] == "1.0"

    with (
        mock.patch.object(judge_module, "fetch_latest_results", return_value=[]),
        mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
    ):
        resp = TestClient(app).post("/runs/run-1/judge", json={"synthesis_only": True})
    assert resp.status_code == 400
    assert "no judged attempts" in resp.json()["detail"]


def test_launch_and_estimate_forward_retry_no_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-08 (owner): the Judge card's "retry attempts with no verdict" — the
    estimate counts them and the task gets JUDGE_RETRY_NO_VERDICT=1."""
    seen: list[bool] = []

    def _drop(conn, run_id, cands, retry_no_verdict=False):
        seen.append(retry_no_verdict)
        return cands if retry_no_verdict else []

    monkeypatch.setenv("JUDGE_TASK_FAMILY", "eval-dev-llm-judge")
    monkeypatch.setenv("CLUSTER", "eval-dev-cluster")
    monkeypatch.setenv("JUDGE_SUBNET_IDS", '["subnet-1"]')
    monkeypatch.setenv("JUDGE_SECURITY_GROUP_IDS", '["sg-1"]')
    fake_ecs = mock.MagicMock()
    fake_boto3 = mock.MagicMock()
    fake_boto3.client.return_value = fake_ecs
    with (
        mock.patch.object(judge_module, "fetch_candidates", return_value=[_candidate()]),
        mock.patch.object(judge_module, "drop_already_judged", side_effect=_drop),
        mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
    ):
        est = TestClient(app).get("/runs/run-1/judge/estimate?retry_no_verdict=true")
        assert est.json()["candidate_count"] == 1
        est0 = TestClient(app).get("/runs/run-1/judge/estimate")
        assert est0.json()["candidate_count"] == 0
        resp = TestClient(app).post("/runs/run-1/judge", json={"retry_no_verdict": True})
    assert resp.status_code == 200
    env = fake_ecs.run_task.call_args.kwargs["overrides"]["containerOverrides"][0]["environment"]
    assert {e["name"]: e["value"] for e in env}["JUDGE_RETRY_NO_VERDICT"] == "1"
    assert seen == [True, False, True]


def test_candidates_route_reports_how_the_latest_judgment_ended() -> None:
    latest = {("i1", 1): "timeout"}
    with (
        mock.patch.object(judge_module, "fetch_candidates", return_value=[_candidate()]),
        mock.patch.object(judge_module, "latest_judgment_by_key", return_value=latest),
    ):
        resp = TestClient(app).get("/runs/run-1/judge/candidates")
    row = resp.json()["candidates"][0]
    assert row["already_judged"] is True and row["last_judgment"] == "timeout"
