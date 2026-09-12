"""Tests for ADR-0022 second-database creation (review N-2).

The Aurora cluster's second database (litellm_spend) is not created by
Terraform (local-exec psql had no path to a private Aurora — review M-2/N-2).
The control-plane/pipeline creates it at startup via
``ensure_additional_databases``. These tests pin the SQL + idempotency with a
mocked psycopg2 connection; the real end-to-end proof (spend rows landing in
litellm_spend) is DoD 1.
"""

from __future__ import annotations

from typing import Self
from unittest import mock

from swebench_eval.database.connection import ensure_additional_databases


class _FakeCursor:
    """A cursor that records executes and returns a configured row for fetches."""

    def __init__(self, exists: bool) -> None:
        self.exists = exists
        self.executed: list[str] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append(sql)

    def fetchone(self) -> tuple[int, ...] | None:
        # the existence check (SELECT 1 FROM pg_database) -> (1,) if it exists
        return (1,) if self.exists else None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    """Mimics the psycopg2 connection surface we touch: autocommit + cursor."""

    def __init__(self, exists: bool) -> None:
        self.autocommit = False
        self.cur = _FakeCursor(exists)

    def cursor(self) -> _FakeCursor:
        return self.cur

    def close(self) -> None:
        pass


def _run(exists: bool) -> tuple[_FakeConn, list[str]]:
    conn = _FakeConn(exists)
    # ensure_additional_databases does `from psycopg2 import connect` inside the
    # function, so the connect symbol is rebound from the psycopg2 module each
    # call — patch at the source, not the connection module's attribute.
    with mock.patch("psycopg2.connect", return_value=conn) as m:
        created = ensure_additional_databases()
    assert m.call_count == 1
    return conn, created


def test_creates_missing_litellm_spend() -> None:
    conn, created = _run(exists=False)
    assert created == ["litellm_spend"]
    # it must have issued a CREATE DATABASE and set autocommit (CREATE DATABASE
    # is not allowed inside a transaction)
    assert any("CREATE DATABASE" in s for s in conn.cur.executed)
    assert conn.autocommit is True


def test_skips_existing_litellm_spend() -> None:
    conn, created = _run(exists=True)
    assert created == []
    # no CREATE DATABASE when the db already exists (idempotent)
    assert not any("CREATE DATABASE" in s for s in conn.cur.executed)
    assert conn.autocommit is True


def test_always_connects_to_an_existing_database() -> None:
    """The connection must target the cluster's guaranteed database, not the
    one being created (you cannot connect to a database that doesn't exist yet)."""
    with mock.patch("psycopg2.connect", return_value=_FakeConn(False)) as m:
        ensure_additional_databases()
    kwargs = m.call_args.kwargs
    # PGDATABASE default is eval_framework (the cluster's app_control_plane home);
    # we must NOT have pointed dbname at litellm_spend
    assert kwargs.get("dbname") != "litellm_spend"
