"""Real-Postgres coverage for offline-analysis-design.md §2.3 / §9 DoD #3:
``leak_scan_at`` must distinguish "never scanned" from "scanned" (whether the
outcome was UNKNOWN, clean, or a real hit) — the bug this replaces
(`leaked or None`) collapsed "scanned, nothing found" into the same NULL as
"never scanned", corrupting the Pass A vs Pass B confusion matrix (§10.6).

Requires the compose stack (``-m integration``), same rationale as
``test_restart_close_integration.py`` — the WHERE-clause candidate selection
(``leak_scan_at IS NULL``) and idempotency are real-DB behavior, not
mockable-connection behavior.
"""

from __future__ import annotations

import pytest

from scripts import backfill_leak_detection
from swebench_eval.evaluation import leak_detection

pytestmark = pytest.mark.integration

_PREFIX = "bld-test-"


def _db():
    from swebench_eval.database.connection import get_connection

    return get_connection()


@pytest.fixture(autouse=True)
def _clean_rows():
    def _clean():
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM instance_results WHERE run_id LIKE %s", (f"{_PREFIX}%",))
                cur.execute("DELETE FROM runs WHERE run_id LIKE %s", (f"{_PREFIX}%",))
            conn.commit()
        finally:
            conn.close()

    _clean()
    yield
    _clean()


@pytest.fixture
def fake_map(monkeypatch: pytest.MonkeyPatch):
    """A small, deterministic map: one leak-detectable instance, one
    checked-and-clean instance, one instance absent from the map entirely
    (UNKNOWN). Never touches the real committed artifact."""
    m = {
        "leaky-instance": ["tests/test_foo.py::test_absent_thing"],
        "clean-instance": [],
    }
    monkeypatch.setattr(leak_detection, "load_leak_detectable", lambda: dict(m))
    monkeypatch.setattr(leak_detection, "leak_map_version", lambda: "sha256:deadbeefcafef00d")
    return m


def _insert_run_and_row(
    conn, run_id: str, instance_id: str, attempt: int = 1, patch_path: str | None = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (run_id, config_snapshot, status) "
            "VALUES (%s, '{\"harness\": \"custom_minimal\"}'::jsonb, 'running') "
            "ON CONFLICT (run_id) DO NOTHING",
            (run_id,),
        )
        cur.execute(
            """INSERT INTO instance_results
                   (run_id, instance_id, attempt_number, phase, state, patch_path)
               VALUES (%s, %s, %s, 'harness', 'RESOLVED', %s)""",
            (run_id, instance_id, attempt, patch_path),
        )
    conn.commit()


def _fetch(conn, run_id: str, instance_id: str) -> dict[str, object]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT leak_scan_at, leak_map_version, leaked_node_ids, leak_detectable
                 FROM instance_results
                WHERE run_id = %s AND instance_id = %s AND phase = 'harness'""",
            (run_id, instance_id),
        )
        row = cur.fetchone()
    return {
        "leak_scan_at": row[0],
        "leak_map_version": row[1],
        "leaked_node_ids": row[2],
        "leak_detectable": row[3],
    }


def test_unknown_instance_gets_scanned_but_stays_null(fake_map, monkeypatch):
    """An instance absent from the map is UNKNOWN — leaked_node_ids/leak_detectable
    stay NULL — but leak_scan_at MUST be set: we looked, this is the answer."""
    run_id = f"{_PREFIX}unknown"
    conn = _db()
    try:
        _insert_run_and_row(conn, run_id, "not-in-map-instance")
        n = backfill_leak_detection.backfill_run(run_id)
        assert n == 1
        row = _fetch(conn, run_id, "not-in-map-instance")
        assert row["leak_scan_at"] is not None, "UNKNOWN must still be marked scanned"
        assert row["leak_map_version"] == "sha256:deadbeefcafef00d"
        assert row["leaked_node_ids"] is None
        assert row["leak_detectable"] is None
    finally:
        conn.close()


def test_clean_instance_gets_empty_array_not_null(fake_map, monkeypatch):
    """The bug this replaces: `leaked or None` wrote NULL for a scanned-and-clean
    result, indistinguishable from never-scanned. Must now be an empty array."""
    monkeypatch.setattr(
        backfill_leak_detection, "get_artifact", lambda bucket, key: b"nothing relevant here"
    )
    run_id = f"{_PREFIX}clean"
    conn = _db()
    try:
        _insert_run_and_row(conn, run_id, "clean-instance", patch_path="fake/patch.diff")
        backfill_leak_detection.backfill_run(run_id)
        row = _fetch(conn, run_id, "clean-instance")
        assert row["leak_scan_at"] is not None
        assert row["leaked_node_ids"] == [], "scanned-clean must be [] , not NULL"
        assert row["leak_detectable"] is False
    finally:
        conn.close()


def test_leaky_instance_records_the_hit(fake_map, monkeypatch):
    monkeypatch.setattr(
        backfill_leak_detection,
        "get_artifact",
        lambda bucket, key: b"the patch calls tests/test_foo.py::test_absent_thing directly",
    )
    run_id = f"{_PREFIX}leaky"
    conn = _db()
    try:
        _insert_run_and_row(conn, run_id, "leaky-instance", patch_path="fake/patch.diff")
        backfill_leak_detection.backfill_run(run_id)
        row = _fetch(conn, run_id, "leaky-instance")
        assert row["leaked_node_ids"] == ["tests/test_foo.py::test_absent_thing"]
        assert row["leak_detectable"] is True
        assert row["leak_scan_at"] is not None
    finally:
        conn.close()


def test_second_run_is_a_no_op_on_already_scanned_rows(fake_map, monkeypatch):
    """DoD #2 (§6): re-running Pass A must not re-process rows it already
    scanned — the WHERE clause is leak_scan_at IS NULL, not leaked_node_ids
    IS NULL (the old condition would forever re-select a correctly-UNKNOWN row)."""
    monkeypatch.setattr(backfill_leak_detection, "get_artifact", lambda bucket, key: b"")
    run_id = f"{_PREFIX}idempotent"
    conn = _db()
    try:
        _insert_run_and_row(conn, run_id, "not-in-map-instance")
        first = backfill_leak_detection.backfill_run(run_id)
        assert first == 1
        second = backfill_leak_detection.backfill_run(run_id)
        assert second == 0, "already-scanned rows must not be re-selected"
    finally:
        conn.close()


def test_empty_map_still_marks_rows_scanned(monkeypatch):
    """Even with no map at all (B6 not populated), leak_scan_at must be set —
    'we looked, found no map' is a completed, honest scan outcome."""
    monkeypatch.setattr(leak_detection, "load_leak_detectable", dict)
    monkeypatch.setattr(leak_detection, "leak_map_version", lambda: "absent")
    run_id = f"{_PREFIX}emptymap"
    conn = _db()
    try:
        _insert_run_and_row(conn, run_id, "whatever-instance")
        n = backfill_leak_detection.backfill_run(run_id)
        assert n == 1
        row = _fetch(conn, run_id, "whatever-instance")
        assert row["leak_scan_at"] is not None
        assert row["leak_map_version"] == "absent"
        assert row["leaked_node_ids"] is None
    finally:
        conn.close()


def test_instance_ids_filter_scopes_the_scan(fake_map, monkeypatch):
    """offline-analysis-design.md §10.1: a judge launch scoped to a subset of
    instances must not force-scan the rest of the run."""
    monkeypatch.setattr(backfill_leak_detection, "get_artifact", lambda bucket, key: b"")
    run_id = f"{_PREFIX}scoped"
    conn = _db()
    try:
        _insert_run_and_row(conn, run_id, "not-in-map-instance")
        _insert_run_and_row(conn, run_id, "clean-instance", attempt=1)
        n = backfill_leak_detection.backfill_run(run_id, instance_ids=["clean-instance"])
        assert n == 1
        scoped = _fetch(conn, run_id, "clean-instance")
        untouched = _fetch(conn, run_id, "not-in-map-instance")
        assert scoped["leak_scan_at"] is not None
        assert untouched["leak_scan_at"] is None
    finally:
        conn.close()
