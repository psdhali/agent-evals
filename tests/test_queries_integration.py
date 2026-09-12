"""queries.py against a real Postgres — dev/BUILDER4-RUNS-STATUS-FILTER-500-ISSUE-2026-08-28.md.

``GET /runs?status=<value>`` 500'd in production: ``list_runs``'s ROWS query
bound params POSITIONALLY (``params + (_ACTIVE_STATES, limit, offset)``), but
the query's `ir.state IN %s` placeholder sits in the SELECT list — textually
BEFORE `{where}`'s `r.status = %s` gets substituted in after FROM/JOIN.
psycopg2 binds `%s` by left-to-right position in the FINAL rendered SQL, not
by the order params happen to be listed in code, so `status` landed on the
`IN %s` slot: `ir.state IN 'running'`, a bare string where IN needs a
list/tuple — `psycopg2.errors.SyntaxError: syntax error at or near 'running'`,
exactly the live symptom.

The existing unit test for this (``test_list_runs_status_filter_hits_the_where_
clause``, ``test_dashboard_api.py``) uses a mock connection — it can pin the
WHERE clause and param shape, but it never executes real SQL, so it could not
have caught the actual bug (a positional/textual ORDER mismatch) and would not
catch a regression of it either. This file requires the compose stack
(``-m integration``) specifically so a query change that reintroduces a
parameter-order mismatch fails here, for real, the way it failed live.
"""

from __future__ import annotations

import pytest

from swebench_eval.orchestrator.api import queries

pytestmark = pytest.mark.integration

_PREFIX = "qi-status-filter-"


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


def _insert_run(conn, run_id: str, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (run_id, config_snapshot, status) VALUES (%s, '{}'::jsonb, %s)",
            (run_id, status),
        )
    conn.commit()


def test_list_runs_status_filter_against_real_postgres() -> None:
    """The exact live reproduction: filter by a real status against a real
    connection, no mocks anywhere in the call path.

    Mutation-check: revert the fix (bind positionally as
    ``params + (_ACTIVE_STATES, limit, offset)`` with a plain WHERE r.status
    = %s) — this test then fails with a genuine
    ``psycopg2.errors.SyntaxError`` raised out of ``list_runs``, not an
    assertion failure, matching the live 500 exactly. Verified by hand and
    reverted.
    """
    conn = _db()
    try:
        _insert_run(conn, f"{_PREFIX}running-1", "running")
        _insert_run(conn, f"{_PREFIX}completed-1", "completed")
        _insert_run(conn, f"{_PREFIX}completed-2", "completed")

        running_rows, running_total = queries.list_runs(conn, limit=50, offset=0, status="running")
        completed_rows, completed_total = queries.list_runs(
            conn, limit=50, offset=0, status="completed"
        )
    finally:
        conn.close()

    running_ids = {r["run_id"] for r in running_rows if r["run_id"].startswith(_PREFIX)}
    completed_ids = {r["run_id"] for r in completed_rows if r["run_id"].startswith(_PREFIX)}

    assert running_ids == {f"{_PREFIX}running-1"}
    assert completed_ids == {f"{_PREFIX}completed-1", f"{_PREFIX}completed-2"}
    # totals are global counts (no LIMIT), so only assert they're at least
    # what this test itself inserted — other runs may exist in a shared DB.
    assert running_total >= 1
    assert completed_total >= 2


def test_list_runs_unfiltered_still_works_against_real_postgres() -> None:
    """The unfiltered path never had this bug (no WHERE clause to
    mis-order against) — pinned here so a future change can't silently
    break it while fixing the filtered path."""
    conn = _db()
    try:
        _insert_run(conn, f"{_PREFIX}unfiltered-1", "running")
        rows, total = queries.list_runs(conn, limit=50, offset=0)
    finally:
        conn.close()

    assert any(r["run_id"] == f"{_PREFIX}unfiltered-1" for r in rows)
    assert total >= 1
