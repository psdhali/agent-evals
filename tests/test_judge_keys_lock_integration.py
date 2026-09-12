"""judge_pass_lock as a REAL constraint (offline-analysis-design.md §10.2
point 4) — needs a real UniqueViolation, not mockable-connection behavior.
Same rationale as test_run_launch_integration.py's claim-mutex coverage.
"""

from __future__ import annotations

import pytest

from swebench_eval.analysis import judge_keys

pytestmark = pytest.mark.integration


def _db():
    from swebench_eval.database.connection import get_connection

    return get_connection()


@pytest.fixture(autouse=True)
def _clean_lock():
    def _clean():
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM judge_pass_lock WHERE model_alias = %s",
                    (judge_keys.JUDGE_MODEL_ALIAS,),
                )
            conn.commit()
        finally:
            conn.close()

    _clean()
    yield
    _clean()


def test_second_concurrent_pass_is_refused_not_silently_overwritten() -> None:
    conn1 = _db()
    conn2 = _db()
    try:
        judge_keys.claim_pass_lock(conn1, "pass-a")
        with pytest.raises(judge_keys.JudgePassLockedError):
            judge_keys.claim_pass_lock(conn2, "pass-b")
    finally:
        conn1.close()
        conn2.close()


def test_release_then_reclaim_by_a_different_pass_succeeds() -> None:
    conn = _db()
    try:
        judge_keys.claim_pass_lock(conn, "pass-c")
        judge_keys.release_pass_lock(conn, "pass-c")
        judge_keys.claim_pass_lock(conn, "pass-d")  # must not raise
    finally:
        conn.close()


def test_release_is_idempotent_when_this_pass_never_held_it() -> None:
    conn = _db()
    try:
        judge_keys.release_pass_lock(conn, "pass-never-claimed")  # must not raise
    finally:
        conn.close()


def test_releasing_the_wrong_pass_id_does_not_release_someone_elses_lock() -> None:
    conn1 = _db()
    conn2 = _db()
    try:
        judge_keys.claim_pass_lock(conn1, "pass-e")
        judge_keys.release_pass_lock(conn2, "pass-f")  # a different pass_id — no-op
        with pytest.raises(judge_keys.JudgePassLockedError):
            judge_keys.claim_pass_lock(conn2, "pass-g")  # pass-e still holds it
    finally:
        conn1.close()
        conn2.close()
