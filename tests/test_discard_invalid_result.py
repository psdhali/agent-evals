"""A3/E1c — the discard script flips an invalid result row idempotently.

The eval row for a grade that is NOT a verdict (gold test patch failed to
apply → the run graded the agent's own test file) is flipped to
FAILED_EVAL / EVAL_GRADE_INVALID / verdict='invalid' with the reason in
error_detail.  Re-running on an already-discarded row is a no-op.
"""

from __future__ import annotations

from typing import Any, Self

import scripts.discard_invalid_result as mod


class _FakeCursor:
    def __init__(self, before: tuple[Any, ...] | None) -> None:
        self._before = before
        self._result: tuple[Any, ...] | None = before
        self._updates: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if sql.strip().startswith("UPDATE"):
            self._updates.append(params)
            self.rowcount = 1

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result

    def _refresh(self, state: tuple[Any, ...] | None) -> None:
        self._result = state


class _FakeConn:
    def __init__(self, before: tuple[Any, ...] | None) -> None:
        self._cur = _FakeCursor(before)

    def cursor(self) -> _FakeCursor:
        return self._cur

    def commit(self) -> None:
        self._committed = True

    def close(self) -> None:
        pass


def test_discard_flips_invalid_eval_row() -> None:
    conn = _FakeConn(("RESOLVED", "RESOLVED", "resolved"))
    mod._flip(conn, "run-x", "django__django-10924", 1, "because E1")
    assert conn._cur._updates == [
        (
            "because E1",
            "run-x",
            "django__django-10924",
            1,
        )
    ]
    assert conn._cur.rowcount == 1


def test_discard_noop_when_already_discarded() -> None:
    conn = _FakeConn(("FAILED_EVAL", "EVAL_GRADE_INVALID", "invalid"))
    mod._flip(conn, "run-x", "django__django-10924", 1, "because E1")
    assert conn._cur._updates == []


def test_discard_noop_when_no_eval_row() -> None:
    conn = _FakeConn(None)
    mod._flip(conn, "run-x", "nope", 1, "x")
    assert conn._cur._updates == []
