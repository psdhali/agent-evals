"""GET /runs/{run_id}/export against the REAL compose Postgres.

BUILDER1-EXPORT-AND-LIVE-ENDPOINTS-2026-08-31.md §0: build offline, but do
not trust the endpoint until it has run against real data — a fixture must
not supply what production creates.  This drives the ACTUAL route (TestClient
over the real app reading the real get_connection) with a seeded run +
instance rows, then checks the export shape against the §5.1 schema.

``-m integration`` only (needs the compose stack; deselected in CI).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

_PREFIX = "qi-export-"


def _db():
    from swebench_eval.database.connection import get_connection

    return get_connection()


@pytest.fixture(autouse=True)
def _clean_rows():
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM instance_results WHERE run_id LIKE %s", (f"{_PREFIX}%",))
            cur.execute("DELETE FROM run_summary WHERE run_id LIKE %s", (f"{_PREFIX}%",))
            cur.execute("DELETE FROM runs WHERE run_id LIKE %s", (f"{_PREFIX}%",))
        conn.commit()
    finally:
        conn.close()
    yield
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM instance_results WHERE run_id LIKE %s", (f"{_PREFIX}%",))
            cur.execute("DELETE FROM run_summary WHERE run_id LIKE %s", (f"{_PREFIX}%",))
            cur.execute("DELETE FROM runs WHERE run_id LIKE %s", (f"{_PREFIX}%",))
        conn.commit()
    finally:
        conn.close()


_RUN_ID = f"{_PREFIX}run-1"
_SNAPSHOT = {
    "harness": "custom_minimal",
    "model_alias": "cheap-oss-model",
    "attempts_per_instance": 1,
    "max_tokens_per_instance": None,
    "max_cost_usd_per_instance": 0.75,
    "framework_sha": "abc123",
    "swebench_version": "4.1.0",
    "dataset_revision": "rev1",
    "harness_image_digest": "sha256:xxx",
    "gateway_config_hash": "sha256:yyy",
    "harness_cli_versions": {"custom_minimal": "1.0.0"},
    "resolved_models": {
        "cheap-oss-model": {"model": "deepseek/deepseek-v4-flash-0731", "temperature": 0.0}
    },
}


def _seed() -> None:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (run_id, config_snapshot, status, compute_cost_estimated_usd) "
                "VALUES (%s, %s::jsonb, %s, %s)",
                (_RUN_ID, json.dumps(_SNAPSHOT), "running", 0.1),
            )
            # (phase, state, verdict, error_category, input_tokens, output_tokens,
            #  cost_usd, agent_s).  Three instances: A resolved, B unresolved
            # (max_turns), C aborted.
            instances: dict[str, list[tuple[Any, ...]]] = {
                "a__a-1": [
                    ("harness", "PATCH_READY", None, None, 1000, 500, 0.01, 100.0),
                    ("eval", "RESOLVED", "resolved", None, None, None, None, None),
                ],
                "b__b-1": [
                    (
                        "harness",
                        "HARNESS_MAX_TURNS_EXCEEDED",
                        None,
                        "HARNESS_MAX_TURNS_EXCEEDED",
                        None,
                        None,
                        None,
                        50.0,
                    ),
                    ("eval", "UNRESOLVED", "unresolved", None, None, None, None, None),
                ],
                "c__c-1": [
                    ("harness", "NEVER_DISPATCHED", None, None, None, None, None, None),
                ],
            }
            for iid, inst_rows in instances.items():
                for phase, state, verdict, ec, inp, outp, cost, agent in inst_rows:
                    cur.execute(
                        """INSERT INTO instance_results
                           (run_id, instance_id, attempt_number, phase, state, verdict,
                            error_category, input_tokens, output_tokens, cost_usd, agent_s)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (_RUN_ID, iid, 1, phase, state, verdict, ec, inp, outp, cost, agent),
                    )
        conn.commit()
    finally:
        conn.close()


def test_run_export_end_to_end_against_real_postgres() -> None:
    """The route reads the REAL DB: an uninstrumented B (no tokens/cost) and
    an aborted C come out null / excluded — never 0 / never in attempted.
    A is resolved with measured tokens.  The export reconciles: sum of the
    instances' cost equals cost_usd_total."""
    _seed()
    resp = TestClient(__import__("swebench_eval.orchestrator.api.main", fromlist=["app"]).app).get(
        f"/runs/{_RUN_ID}/export"
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["schema_version"] == 1
    assert body["provenance"]["model_resolved"] == "deepseek/deepseek-v4-flash-0731"
    assert body["provenance"]["harness"] == "custom_minimal"
    assert body["provenance"]["network_posture"] == "isolated"
    assert body["provenance"]["limits"]["max_tokens"] is None  # no token ceiling

    # A resolved + B unresolved = gradeable 2; C aborted excluded from
    # attempted.  tokens/cost are null for the uninstrumented B but summed
    # for A.
    assert body["totals"]["attempted"] == 2
    assert body["totals"]["gradeable"] == 2
    assert body["totals"]["resolved"] == 1
    assert body["totals"]["resolve_rate_attempted"] == 0.5
    assert body["totals"]["tokens"]["input"] == 1000
    assert body["totals"]["cost_usd_total"] == 0.01

    # The instances list: A and B present, C absent; B's cost/tokens null.
    by_id = {i["instance_id"]: i for i in body["instances"]}
    assert set(by_id) == {"a__a-1", "b__b-1"}
    assert by_id["a__a-1"]["verdict"] == "resolved"
    assert by_id["a__a-1"]["cost_usd"] == 0.01
    assert by_id["b__b-1"]["verdict"] == "unresolved"
    assert by_id["b__b-1"]["cost_usd"] is None
    assert by_id["b__b-1"]["terminated_reason"] == "max_turns_exceeded"

    # terminated_reasons: A completed, B max_turns, C excluded.
    assert body["terminated_reasons"] == {"completed": 1, "max_turns_exceeded": 1}
