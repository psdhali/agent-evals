"""2026-09-09: the live LLM-call view must not de-TOAST the prompt body to filter rows.

The instance-scoped list took 65 s on a 60-instance run because the coordinate predicate
was ``COALESCE(proxy_server_request-path, spend_logs_metadata-path)`` — COALESCE reads
its first argument, and that is the full stored prompt.  These tests pin the shape that
fixes it (CASE, spend_logs_metadata first, headers only in the ELSE branch) and the index
file + startup wiring that make it durable.  No database needed here; the behaviour on
real rows is in test_llm_live_integration.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swebench_eval.orchestrator.api import llm_live

_HDR_PATH = "proxy_server_request->'litellm_metadata'->'headers'"
_SLM_PATH = "metadata->'spend_logs_metadata'"


def test_coordinate_predicate_reads_spend_logs_metadata_before_the_prompt_body() -> None:
    expr = llm_live._coord("x-eval-instance-id", "eval_instance_id")
    assert expr.startswith("CASE WHEN "), expr
    assert "COALESCE" not in expr
    # the existence test and the THEN branch use the small inline JSON …
    assert f"{_SLM_PATH} ? 'eval_instance_id'" in expr
    assert f"THEN {_SLM_PATH}->>'eval_instance_id'" in expr
    # … and the prompt body is only ever touched in the ELSE branch
    assert f"ELSE {_HDR_PATH}->>'x-eval-instance-id' END" in expr
    assert expr.index(_SLM_PATH) < expr.index(_HDR_PATH)


def test_index_file_is_the_record_and_is_idempotent_ddl() -> None:
    path = llm_live._INDEX_SQL_PATH
    assert path.exists(), path
    stmts = [
        s.strip()
        for s in "\n".join(
            ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("--")
        ).split(";")
        if s.strip()
    ]
    assert stmts, "no statements"
    for s in stmts:
        assert s.upper().startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS"), s
        assert '"LiteLLM_SpendLogs"' in s
    # the run-scoping predicate must be the leading index expression, ordered by startTime
    assert any("(metadata->>'user_api_key_alias')" in s and '"startTime" DESC' in s for s in stmts)


def test_ensure_indexes_never_raises_without_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LITELLM_SPEND_DATABASE_URL", raising=False)
    assert llm_live.ensure_spend_log_indexes() == []
    llm_live.ensure_spend_log_indexes_in_background()  # thread starts, logs, exits


def test_ensure_indexes_skips_a_missing_file(tmp_path: Path) -> None:
    assert llm_live.ensure_spend_log_indexes(sql_path=tmp_path / "nope.sql") == []


def test_api_startup_applies_the_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    calls: list[str] = []
    monkeypatch.setattr(
        llm_live, "ensure_spend_log_indexes_in_background", lambda: calls.append("started")
    )
    with TestClient(app):
        pass
    assert calls == ["started"]
