"""Dashboard read endpoints (architecture §10 / ADR-0009) + their queries/artifacts.

Hermetic by construction: every DB/S3 boundary is stubbed the way
``test_abort.py`` stubs Aurora — a ``_FakeConn`` serving scripted rows.  CI has
no Postgres/Redis/MinIO, so a test here must never reach the real stack
(``conftest.py`` raises if one tries).  The endpoint-level tests use FastAPI's
TestClient over the in-process ASGI transport with ``_db`` swapped for the fake.

Coverage is the design's named concerns:

  - the null path first (the status file's intended first test): a run with no
    ``run_summary`` row serialises with ``summary=None`` — never a 500;
  - Decimal columns normalise to JSON-safe floats through the whole stack;
  - ``terminal`` is computed (running rows ⇒ not terminal), never read off a
    ``runs.status`` that only records the abort pair;
  - the S3 artifact proxy resolves each kind from the phase row that produced
    it (patch/trajectory/log on the harness row, report on the eval row) —
    regression-locked after a real bug where eval-first row order made
    ``/artifacts/.../patch`` always 404;
  - the dashboard reads + control surface stay registered (M1.10).  ``POST
    /runs`` is now also registered — owner-assigned to builder 4 this round
    (BUILDER4-RUN-LAUNCH-ORCHESTRATOR-2026-08-26 §0), superseding this
    file's original "read-only by construction, no POST /runs" framing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Self
from unittest import mock

import pytest

from swebench_eval.orchestrator.api import artifacts, queries

# ── scripted Postgres boundary (matches tests/test_abort.py::_FakeConn) ─────────


class _FakeCursor:
    """One cursor open: serves one scripted result set, records the SQL."""

    def __init__(self, conn: _FakeConn, results: list[dict[str, Any]]) -> None:
        self.conn = conn
        self._results = results
        self._i = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        self._i = len(self._results)
        return self._results

    def fetchone(self) -> dict[str, Any] | None:
        if self._i < len(self._results):
            row = self._results[self._i]
            self._i += 1
            return row
        return None

    def close(self) -> None:
        return None


class _FakeConn:
    """Serves one scripted result set per ``cursor()`` open, oldest first.

    ``queries._rows/_one/_count`` each open their own cursor, so the caller
    passes the result sets in the same order the function touches them (e.g.
    ``list_runs`` = count, then rows).
    """

    def __init__(self, *result_sets: list[dict[str, Any]]) -> None:
        self._queue: list[list[dict[str, Any]]] = list(result_sets)
        self.executed: list[tuple[str, Any]] = []

    def cursor(self, cursor_factory: Any = None) -> _FakeCursor:
        results = self._queue.pop(0) if self._queue else []
        return _FakeCursor(self, results)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


# ── fixture rows ───────────────────────────────────────────────────────────────


def _run_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "run_id": "run-1",
        "status": "running",
        "created_at": datetime(2026, 8, 21, 10, 0, tzinfo=UTC),
        "estimated_cost_usd": None,
        "cost_confidence_tier": None,
        "compute_cost_estimated_usd": None,
        "compute_cost_reconciled_usd": None,
        "budget_cap_usd": None,
        "stop_requested_at": None,
        "stop_scope": None,
        "stop_reason": None,
        "stopped_at": None,
        "config_snapshot": None,
        "summary": None,
    }
    row.update(overrides)
    return row


def _instance_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "run_id": "run-1",
        "instance_id": "repo/owner-1",
        "attempt_number": 1,
        "phase": "harness",
        "state": "PATCH_READY",
        "error_category": None,
        "error_detail": None,
        "verdict": None,
        "wall_clock_harness_s": None,
        "wall_clock_eval_s": None,
        "touches_test_files": None,
        "patch_path": None,
        "trajectory_path": None,
        "raw_log_path": None,
        "report_path": None,
        "report_json": None,
        "native_trajectory_s3_key": None,
        "test_output_s3_key": None,
        "run_log_s3_key": None,
        "created_at": datetime(2026, 8, 21, 10, 1, tzinfo=UTC),
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "adapter_input_tokens": None,
        "adapter_output_tokens": None,
        "adapter_cost_usd": None,
        "agent_s": None,
        "task_observed_s": None,
        "task_billed_s": None,
        "repo_prep_s": None,
        "eval_test_s": None,
        "queue_wait_s": None,
        "provision_s": None,
        "image_pull_s": None,
        "worker_boot_s": None,
        "patch_extract_s": None,
        "artifact_upload_s": None,
        "repo_prep_cache_hit": None,
        "image_pull_cold": None,
        "eval_queue_wait_s": None,
        "eval_patch_fetch_s": None,
        "eval_image_pull_s": None,
        "eval_log_upload_s": None,
        "eval_image_pull_cold": None,
        "cost_source": None,
        "stripped_test_paths": None,
        "grade_invalid": None,
        "leaked_node_ids": None,
        "gold_patch_similarity": None,
        "leak_detectable": None,
    }
    row.update(overrides)
    return row


# ── queries: the null path + normalisation ────────────────────────────────────


def test_list_runs_null_summary_is_the_null_path() -> None:
    """A run with no run_summary row must render (summary=None), never 500."""
    conn = _FakeConn(
        [{"count": 1}],  # the count query
        [_run_row()],  # the list query — no summary join row
    )
    rows, total = queries.list_runs(conn, limit=50, offset=0)
    assert total == 1
    assert rows[0]["summary"] is None
    assert rows[0]["created_at"] == "2026-08-21T10:00:00+00:00"


def test_list_runs_status_filter_hits_the_where_clause() -> None:
    """dev/BUILDER4-RUNS-STATUS-FILTER-500-ISSUE-2026-08-28.md: named
    parameters now (not positional %s) — this mock-conn test only pins the
    WHERE clause + param dict shape; it does NOT execute real SQL, so it
    could not have caught (and would not catch a regression of) the actual
    live bug, a placeholder/param ORDER mismatch. See
    test_list_runs_status_filter_against_real_postgres (integration) for
    that — this test's job is narrower: prove status reaches the query at
    all."""
    conn = _FakeConn([{"count": 0}], [])
    queries.list_runs(conn, limit=50, offset=0, status="aborted")
    count_sql, count_params = conn.executed[0]
    assert count_params == {"status": "aborted"}
    assert "WHERE r.status = %(status)s" in count_sql


def test_list_runs_normalizes_decimals_and_passes_strings() -> None:
    row = _run_row(estimated_cost_usd=Decimal("12.50"), status="running")
    conn = _FakeConn([{"count": 1}], [row])
    rows, _ = queries.list_runs(conn, limit=50, offset=0)
    assert rows[0]["estimated_cost_usd"] == 12.5
    assert isinstance(rows[0]["estimated_cost_usd"], float)


def test_get_run_terminal_is_computed_not_read() -> None:
    """Running instance rows ⇒ not terminal; all-terminal ⇒ terminal."""
    live = _FakeConn(
        [_run_row(status="running")],
        [
            {"state": "HARNESS_RUNNING", "count": 2},
            {"state": "RESOLVED", "count": 1},
        ],
    )
    live_run = queries.get_run(live, "run-1")
    assert live_run is not None
    assert live_run["terminal"] is False

    done = _FakeConn(
        [_run_row(status="running")],
        [{"state": "RESOLVED", "count": 3}],
    )
    done_run = queries.get_run(done, "run-1")
    assert done_run is not None
    assert done_run["terminal"] is True

    aborted = _FakeConn(
        [_run_row(status="aborted")],
        [{"state": "HARNESS_RUNNING", "count": 1}],
    )
    aborted_run = queries.get_run(aborted, "run-1")
    assert aborted_run is not None
    assert aborted_run["terminal"] is True


def test_get_run_state_buckets_surfaces_db_ordering() -> None:
    """The SQL orders buckets by count DESC; the fake serves rows the same way."""
    conn = _FakeConn(
        [_run_row()],
        [
            {"state": "PENDING", "count": 9},
            {"state": "RESOLVED", "count": 3},
            {"state": "HARNESS_RUNNING", "count": 1},
        ],
    )
    run = queries.get_run(conn, "run-1")
    assert run is not None
    states = run["states"]
    assert [s["count"] for s in states] == [9, 3, 1]
    assert states[0]["state"] == "PENDING"


def test_list_instances_filters_and_paginates() -> None:
    conn = _FakeConn([{"count": 1}], [_instance_row(state="RESOLVED")])
    rows, total = queries.list_instances(
        conn, "run-1", state="RESOLVED", error_category=None, limit=10, offset=0
    )
    assert total == 1
    assert rows[0]["state"] == "RESOLVED"
    list_sql, list_params = conn.executed[1]
    assert list_params == ("run-1", "RESOLVED", 10, 0)
    assert "state = %s" in list_sql


def test_get_instance_returns_phase_rows() -> None:
    conn = _FakeConn(
        [
            _instance_row(phase="eval", state="RESOLVED", verdict="resolved"),
            _instance_row(phase="harness", state="PATCH_READY"),
        ]
    )
    rows = queries.get_instance(conn, "run-1", "repo/owner-1", 1)
    assert [r["phase"] for r in rows] == ["eval", "harness"]


def test_list_capacity_pool_and_since_and_iso_ts() -> None:
    conn = _FakeConn(
        [
            {
                "ts": datetime(2026, 8, 21, 11, 0, tzinfo=UTC),
                "pool": "harness",
                "queue_depth": 5,
                "gateway_headroom": 10,
                "current_workers": 2,
                "desired": 3,
            }
        ]
    )
    rows = queries.list_capacity(conn, pool="harness", since="2026-08-21T00:00:00Z", limit=500)
    assert len(rows) == 1
    assert rows[0]["ts"] == "2026-08-21T11:00:00+00:00"
    assert rows[0]["queue_depth"] == 5
    _sql, params = conn.executed[0]
    assert params == ("harness", "2026-08-21T00:00:00Z", 500)


def test_normalize_leaves_non_decimal_untouched() -> None:
    row = {"cost_usd": 3, "verdict": "resolved", "leak_detectable": None}
    queries._normalize(row)
    assert row["cost_usd"] == 3
    assert row["verdict"] == "resolved"
    assert row["leak_detectable"] is None


# ── artifacts: kind → path column mapping is the whole contract ───────────────


def test_artifact_path_column_for_each_kind() -> None:
    assert artifacts.path_column_for("patch") == "patch_path"
    assert artifacts.path_column_for("trajectory") == "trajectory_path"
    assert artifacts.path_column_for("log") == "raw_log_path"
    assert artifacts.path_column_for("report") == "report_path"


def test_artifact_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="unknown artifact kind"):
        artifacts.path_column_for("bogus")
    with pytest.raises(ValueError, match="unknown artifact kind"):
        artifacts.artifact_key_for_row("bogus", {})


def test_artifact_key_for_row_none_when_absent() -> None:
    assert artifacts.artifact_key_for_row("patch", _instance_row()) is None
    assert (
        artifacts.artifact_key_for_row("patch", _instance_row(patch_path="runs/x/1/patch.diff"))
        == "runs/x/1/patch.diff"
    )


# ── endpoints: HTTP layer, read-only by construction, the artifact regression ──


def test_dashboard_routes_are_registered_and_post_runs_exists() -> None:
    """run-launch (BUILDER4-RUN-LAUNCH-ORCHESTRATOR-2026-08-26 §0): the owner
    deliberately assigned ``POST /runs`` to builder 4 this round — "their
    brief says 'no POST /runs' precisely because it was reserved... the
    endpoint is assigned to you" — superseding this file's original "no
    POST /runs (spends money)" assertion.  It still spends money; the owner's
    sign-off is what changed, not the risk."""
    from swebench_eval.orchestrator.api.main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    for expected in (
        "/runs",
        "/runs/{run_id}",
        "/runs/{run_id}/instances",
        "/runs/{run_id}/progress",
        "/instances/{run_id}/{instance_id}/{attempt}",
        "/capacity",
        "/queues",
        "/artifacts/{run_id}/{instance_id}/{attempt}/{kind}",
        "/control",
        "/control/pause",
        "/control/resume",
        "/runs/{run_id}/abort",
        "/dataset/instances",
        "/harnesses",
        "/models",
    ):
        assert expected in paths, f"route {expected} missing"
    methods: set[str] = set()
    for r in app.routes:
        for method in getattr(r, "methods", []) or []:
            if getattr(r, "path", None) == "/runs":
                methods.add(method)
    assert "POST" in methods, "POST /runs must exist (run-launch, owner-assigned to builder4)"


def test_get_runs_http_round_trip() -> None:
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    conn = _FakeConn([{"count": 1}], [_run_row(estimated_cost_usd=Decimal("4.25"))])
    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn):
        resp = TestClient(app).get("/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["run_id"] == "run-1"
    assert item["estimated_cost_usd"] == 4.25, "Decimal must serialise as a float"


def test_get_run_404_when_missing() -> None:
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    conn = _FakeConn([])  # `_one` finds nothing → None → 404
    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn):
        resp = TestClient(app).get("/runs/nope")
    assert resp.status_code == 404


def test_capacity_invalid_pool_422() -> None:
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=_FakeConn()):
        resp = TestClient(app).get("/capacity?pool=bogus")
    assert resp.status_code == 422


def test_instance_not_found_404() -> None:
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    conn = _FakeConn([])  # get_instance finds no phase rows
    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn):
        resp = TestClient(app).get("/instances/run-1/astropy__astropy-12907/1")
    assert resp.status_code == 404


def test_artifact_patch_resolves_from_the_harness_row() -> None:
    """Regression: phase rows come back eval-first, but patch/trajectory/log
    live on the harness row.  Resolving from the first row made every patch
    fetch a confused 404."""
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    conn = _FakeConn(
        [
            _instance_row(phase="eval", state="RESOLVED"),
            _instance_row(phase="harness", state="PATCH_READY", patch_path="runs/x/1/patch.diff"),
        ]
    )
    patch = b"diff --git a/x b/x\n"
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        mock.patch("swebench_eval.orchestrator.api.artifacts.fetch", return_value=patch),
    ):
        resp = TestClient(app).get("/artifacts/run-1/astropy__astropy-12907/1/patch")
    assert resp.status_code == 200
    assert resp.content == patch
    assert 'filename="patch.diff"' in resp.headers.get("content-disposition", "")


def test_artifact_report_resolves_from_the_eval_row() -> None:
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    conn = _FakeConn(
        [
            _instance_row(phase="eval", state="RESOLVED", report_path="runs/x/1/report.json"),
            _instance_row(phase="harness", state="PATCH_READY"),
        ]
    )
    report = b'{"resolved": true}'
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        mock.patch("swebench_eval.orchestrator.api.artifacts.fetch", return_value=report),
    ):
        resp = TestClient(app).get("/artifacts/run-1/astropy__astropy-12907/1/report")
    assert resp.status_code == 200
    assert resp.content == report


def test_artifact_unknown_kind_is_400() -> None:
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    conn = _FakeConn([_instance_row(phase="harness")])
    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn):
        resp = TestClient(app).get("/artifacts/run-1/astropy__astropy-12907/1/bogus")
    assert resp.status_code == 400


def test_artifact_missing_s3_key_is_honest_404() -> None:
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    conn = _FakeConn([_instance_row(phase="harness")])  # no patch_path on either row
    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn):
        resp = TestClient(app).get("/artifacts/run-1/astropy__astropy-12907/1/patch")
    assert resp.status_code == 404
    assert "no patch" in resp.json()["detail"]


# ── progress: per-phase/per-state counts + the two denominators (M2.4) ─────────


def _progress_conn() -> _FakeConn:
    """A run with a summary row seeded with per-run expected/denominator."""
    return _FakeConn(
        [{"status": "running", "summary": {"expected": 900, "denominator": 60}}],  # run row
        [  # per-phase/per-state count rows (second result set)
            {"phase": "harness", "state": "PATCH_READY", "count": 50},
            {"phase": "harness", "state": "HARNESS_RUNNING", "count": 10},
            {"phase": "eval", "state": "RESOLVED", "count": 22},
        ],
    )


def test_get_run_progress_counts_and_denominators() -> None:
    conn = _progress_conn()
    prog = queries.get_run_progress(conn, "run-1")
    assert prog is not None
    assert prog["expected"] == 900  # the dispatch plan — 3-attempt × 300 Lite
    assert prog["denominator"] == 60  # the honesty denominator (M1.8), ≠ expected
    harness = [p for p in prog["phases"] if p["phase"] == "harness"]
    assert harness == [
        {"phase": "harness", "state": "PATCH_READY", "count": 50},
        {"phase": "harness", "state": "HARNESS_RUNNING", "count": 10},
    ]
    assert any(p["state"] == "RESOLVED" for p in prog["phases"] if p["phase"] == "eval")


def test_get_run_progress_terminal_computed_over_phases() -> None:
    conn = _FakeConn(
        [{"status": "running", "summary": None}],
        [{"phase": "harness", "state": "RESOLVED", "count": 3}],
    )
    prog = queries.get_run_progress(conn, "run-1")
    assert prog is not None
    assert prog["terminal"] is True  # all instance rows terminal → run done
    assert prog["expected"] is None
    assert prog["denominator"] is None


def test_get_run_progress_missing_run_is_none() -> None:
    conn = _FakeConn([])  # `_one` finds nothing
    assert queries.get_run_progress(conn, "nope") is None


def test_get_run_progress_jsonb_string_summary_is_parsed() -> None:
    """psycopg2 without a jsonb typecaster hands the blob back as text."""
    conn = _FakeConn(
        [{"status": "running", "summary": '{"expected": 900, "denominator": 60}'}],
        [{"phase": "eval", "state": "RESOLVED", "count": 22}],
    )
    prog = queries.get_run_progress(conn, "run-1")
    assert prog is not None
    assert prog["expected"] == 900
    assert prog["denominator"] == 60


def test_progress_http_round_trip_and_404() -> None:
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    conn = _progress_conn()
    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn):
        resp = TestClient(app).get("/runs/run-1/progress")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-1"
    assert body["expected"] == 900
    assert body["denominator"] == 60
    assert body["status"] == "running"

    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=_FakeConn([])):
        resp = TestClient(app).get("/runs/nope/progress")
    assert resp.status_code == 404


# ── queues: the incident view (M2.1/M2.4), DLQ as alarm ───────────────────────


def test_queues_http_round_trip_with_dlq_alarm() -> None:
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app
    from swebench_eval.queue import client as queue_client

    with (
        mock.patch.object(
            queue_client,
            "get_queue_depth",
            side_effect=[
                queue_client.QueueDepth(visible=42, not_visible=3, oldest_age_s=120),
                queue_client.QueueDepth(visible=0, not_visible=0, oldest_age_s=None),
                queue_client.QueueDepth(visible=7, not_visible=1, oldest_age_s=None),
            ],
        ),
        mock.patch.object(queue_client, "get_dlq_depth", side_effect=[0, 0, 4]),
    ):
        resp = TestClient(app).get("/queues")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["queue"] for i in items] == ["harness-jobs", "eval-jobs", "results"]
    assert items[0]["visible"] == 42
    assert items[0]["not_visible"] == 3
    assert items[0]["oldest_age_s"] == 120
    assert items[1]["oldest_age_s"] is None, "unavailable must be None, never 0"
    assert items[2]["dlq_depth"] == 4, "the DLQ alarm value must surface through"


def test_get_queue_depth_visible_vs_not_visible_not_collapsed() -> None:
    from swebench_eval.queue import client as queue_client

    fake = mock.Mock()
    fake.get_queue_attributes.return_value = {
        "Attributes": {
            "ApproximateNumberOfMessages": "13",
            "ApproximateNumberOfMessagesNotVisible": "5",
        }
    }
    with (
        mock.patch.object(queue_client, "get_sqs_client", return_value=fake),
        mock.patch.object(queue_client, "get_queue_url", return_value="q://eval-jobs"),
        mock.patch.object(queue_client, "get_cloudwatch_client", return_value=None),
    ):
        depth = queue_client.get_queue_depth("eval-jobs")
    assert (depth.visible, depth.not_visible) == (13, 5)
    assert depth.oldest_age_s is None, "no CloudWatch locally → None, never 0"
    fake.get_queue_attributes.assert_called_once_with(
        QueueUrl="q://eval-jobs",
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )


def test_get_dlq_depth_reads_the_dlq_queue() -> None:
    from swebench_eval.queue import client as queue_client

    fake = mock.Mock()
    fake.get_queue_attributes.return_value = {"Attributes": {"ApproximateNumberOfMessages": "6"}}
    with (
        mock.patch.object(queue_client, "get_sqs_client", return_value=fake),
        mock.patch.object(queue_client, "get_queue_url", return_value="q://eval-jobs-dlq"),
    ):
        assert queue_client.get_dlq_depth("eval-jobs") == 6
    fake.get_queue_attributes.assert_called_once_with(
        QueueUrl="q://eval-jobs-dlq", AttributeNames=["ApproximateNumberOfMessages"]
    )


# ── 2026-09-08: the run endpoint echoes the launch-time limits ─────────────────


def test_launch_limits_come_from_config_snapshot_or_are_none() -> None:
    import json

    snap = {
        "harness": "opencode",
        "model_alias": "minimax-m2.5-opencode",
        "timeout_seconds": 3600,
        "max_cost_usd_per_instance": 2.5,
        "max_turns_per_instance": 200,
        "context_window_tokens": None,
        "max_parallel_harness_tasks": 60,
        "initial_budget_override": {"r_qps": 18.0},
    }
    limits = queries._launch_limits(snap)
    assert limits == {
        "timeout_seconds": 3600,
        "max_tokens_per_instance": None,
        "max_cost_usd_per_instance": 2.5,
        "max_turns_per_instance": 200,
        "context_window_tokens": None,
        "max_parallel_harness_tasks": 60,
        "ramp_step_pct": None,
        "ramp_cooldown_seconds": None,
        "autoscaler_enabled": None,
        "initial_budget_override": {"r_qps": 18.0},
        "harness_instructions": None,  # 2026-09-09: a pre-arm snapshot has none
    }
    # a JSONB handed back as text parses; garbage or no snapshot is None, never defaults
    from_text = queries._launch_limits(json.dumps(snap))
    assert from_text is not None
    assert from_text["timeout_seconds"] == 3600
    assert queries._launch_limits("{not json") is None
    assert queries._launch_limits(None) is None
