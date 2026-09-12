"""5a-ii: connection env reconciliation.

The deployed ECS tasks inject ``DATABASE_URL`` (the Aurora DSN from Secrets
Manager); the local stack uses separate ``PG*`` vars. ``_connection_kwargs``
must prefer ``DATABASE_URL`` when present and otherwise keep the PG*/default
behaviour — without the seam a deployed task silently falls back to
localhost:5432 eval/eval and fails every query (the empty-secret failure mode
DoD 2 names).
"""

from __future__ import annotations

from unittest import mock

from swebench_eval.database.connection import _connection_kwargs


def test_database_url_wins_when_present() -> None:
    with mock.patch.dict(
        "swebench_eval.database.connection.os.environ",
        {"DATABASE_URL": "postgresql://eval:oops%40pass@db.example.com:5432/app_control_plane"},
        clear=False,
    ):
        kwargs = _connection_kwargs()
    assert kwargs["host"] == "db.example.com"
    assert kwargs["port"] == "5432"
    assert kwargs["user"] == "eval"
    assert kwargs["password"] == "oops@pass"  # URL-encoded char round-trips
    assert kwargs["dbname"] == "app_control_plane"


def test_pg_env_fallback_without_database_url() -> None:
    with mock.patch.dict(
        "swebench_eval.database.connection.os.environ",
        {"PGHOST": "pg.local", "PGUSER": "u", "PGPASSWORD": "p", "PGDATABASE": "d"},
        clear=True,
    ):
        kwargs = _connection_kwargs()
    assert kwargs == {
        "host": "pg.local",
        "port": "5432",
        "user": "u",
        "password": "p",
        "dbname": "d",
    }


def test_defaults_with_nothing_set() -> None:
    with mock.patch.dict("swebench_eval.database.connection.os.environ", {}, clear=True):
        kwargs = _connection_kwargs()
    assert kwargs["host"] == "localhost"
    assert kwargs["user"] == "eval"
    assert kwargs["dbname"] == "eval_framework"
