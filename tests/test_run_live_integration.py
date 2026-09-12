"""GET /runs/{run_id}/live against the REAL compose Postgres + Redis.

BUILDER1-EXPORT-AND-LIVE-ENDPOINTS-2026-08-31.md §2 / §0: the endpoint must
not be trusted until it has run against real data — a fixture must not supply
what production creates.  The compose stack is up and carries both Postgres
and Redis, so this drives the ACTUAL route (TestClient over the real app,
which reads the real ``get_connection`` and the real Redis) with a seeded run
+ instance rows + a real ``write_progress`` key.

``-m integration`` only (needs the compose stack; deselected in CI).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from swebench_eval.harnesses.base import Usage

pytestmark = pytest.mark.integration

_PREFIX = "qi-live-"


def _db():
    from swebench_eval.database.connection import get_connection

    return get_connection()


@pytest.fixture(autouse=True)
def _clean_rows():
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM instance_results WHERE run_id LIKE %s", (f"{_PREFIX}%",))
            cur.execute("DELETE FROM runs WHERE run_id LIKE %s", (f"{_PREFIX}%",))
        conn.commit()
    finally:
        conn.close()
    yield
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM instance_results WHERE run_id LIKE %s", (f"{_PREFIX}%",))
            cur.execute("DELETE FROM runs WHERE run_id LIKE %s", (f"{_PREFIX}%",))
        conn.commit()
    finally:
        conn.close()
    from swebench_eval.database.redis_client import _get_client, progress_key

    for iid, attempt, _state in _ATTEMPTS:
        _get_client().delete(progress_key(_RUN_ID, iid, attempt))


_RUN_ID = f"{_PREFIX}run-1"
# (instance_id, attempt_number, ledger state): a started one (HARNESS_RUNNING),
# a not-yet-started one (PENDING), and a finished one (RESOLVED, excluded).
_ATTEMPTS = [
    ("scikit-learn__scikit-learn-25102", 1, "HARNESS_RUNNING"),
    ("django__django-10924", 1, "PENDING"),
    ("astropy__astropy-12907", 1, "RESOLVED"),
]


def _seed() -> None:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (run_id, config_snapshot, status) VALUES (%s, '{}'::jsonb, %s)",
                (_RUN_ID, "running"),
            )
            for iid, attempt, state in _ATTEMPTS:
                cur.execute(
                    """INSERT INTO instance_results
                       (run_id, instance_id, attempt_number, phase, state)
                       VALUES (%s, %s, %s, 'harness', %s)""",
                    (_RUN_ID, iid, attempt, state),
                )
        conn.commit()
    finally:
        conn.close()


def test_run_live_end_to_end_against_real_stack() -> None:
    """A real HARNESS_RUNNING attempt with a real Redis progress key reads as
    `running` with the actual turn/tokens/cost/age; a PENDING attempt with no
    key reads `pending` (never "0 turns"); a finished attempt is absent."""
    from swebench_eval.database.redis_client import _get_client, progress_key, write_progress

    _seed()
    # The one real key: the shim wrote it, exactly as production does.
    write_progress(
        _RUN_ID,
        "scikit-learn__scikit-learn-25102",
        1,
        turn_number=12,
        usage=Usage(input_tokens=2000, output_tokens=1500, cost_usd=0.11),
    )
    try:
        resp = TestClient(
            __import__("swebench_eval.orchestrator.api.main", fromlist=["app"]).app
        ).get(f"/runs/{_RUN_ID}/live")
    finally:
        _get_client().delete(progress_key(_RUN_ID, "scikit-learn__scikit-learn-25102", 1))

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == _RUN_ID
    assert body["state"] == "ok"
    assert body["status"] == "running"

    by_id = {i["instance_id"]: i for i in body["items"]}
    # The in-flight attempt with a real key.
    running = by_id["scikit-learn__scikit-learn-25102"]
    assert running["state"] == "running"
    assert running["turn_number"] == 12
    assert running["input_tokens"] == 2000
    assert running["output_tokens"] == 1500
    assert running["cost_usd"] == pytest.approx(0.11)
    assert isinstance(running["age_s"], float) and running["age_s"] >= 0
    # The not-yet-started attempt with no key -> explicit pending, no numbers.
    pending = by_id["django__django-10924"]
    assert pending["state"] == "pending"
    assert pending["turn_number"] is None
    assert pending["input_tokens"] is None
    assert pending["age_s"] is None
    # The finished attempt is NOT in the live set (only non-terminal attempts).
    assert "astropy__astropy-12907" not in by_id
