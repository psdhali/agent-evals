"""Live LLM-call view — integration against the REAL local LiteLLM spend DB.

Runs against the compose stack's ``litellm-db`` (port 5433) — the actual
prisma-managed ``"LiteLLM_SpendLogs"`` table, not a hand-made lookalike, so a
schema drift between LiteLLM's table and llm_live.py's SQL fails HERE and not
at the first live click.  Marked ``integration`` like the other compose-stack
suites.

Seeded rows mirror the live shape verified on run 01788315793731547650
(BUILDER4-LITELLM-SPEND-DB-LIVE-VIEW-2026-09-02.md): run identity in
``metadata.user_api_key_alias``, coordinates in
``proxy_server_request.litellm_metadata.headers``, cache split in
``metadata.usage_object.prompt_tokens_details``, and the ``messages`` COLUMN
left ``{}`` exactly as this LiteLLM version writes it.

TWO row shapes are seeded and must both resolve: the 1.96 shape (coordinates in
``proxy_server_request.litellm_metadata.headers``) and the 1.99+ shape
(``proxy_server_request`` is only the request body; coordinates arrive via the
shim's ``x-litellm-spend-logs-metadata`` header → ``metadata.spend_logs_metadata``)
— main-stable floated between them on 2026-09-02 and the drift must fail HERE.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.integration

_DSN = "postgresql://litellm:litellm@localhost:5433/litellm"
_RUN = "llmlive-test-run-1"
_OTHER_RUN = "llmlive-test-run-2"


def _conn():
    import psycopg2

    return psycopg2.connect(
        host="localhost", port=5433, user="litellm", password="litellm", dbname="litellm"
    )


def _row(
    request_id: str,
    run_id: str,
    instance: str,
    attempt: int,
    ts: str,
    text_tok: int = 400,
    cached: int = 50_000,
    shape: str = "v196",
):
    details: dict[str, Any] = {"text_tokens": text_tok, "cached_tokens": cached}
    if shape != "v196":
        # 1.99 dropped text_tokens from prompt_tokens_details (kept cached) —
        # the API must DERIVE uncached as prompt_tokens - cached_tokens.
        details.pop("text_tokens")
    metadata: dict[str, Any] = {
        "user_api_key_alias": run_id,
        "usage_object": {
            "prompt_tokens": text_tok + cached,
            "completion_tokens": 80,
            "prompt_tokens_details": details,
        },
    }
    psr: dict[str, Any] = {
        "model": "laguna-xs-2.1-claude_code",
        "messages": [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "looking"},
        ],
        "system": "you are an engineer",
        "tools": [{"name": "Bash"}, {"name": "Edit"}],
    }
    if shape == "v196":
        # 1.96: the whole proxy request object, headers included.
        psr["litellm_metadata"] = {
            "headers": {
                "x-eval-run-id": run_id,
                "x-eval-instance-id": instance,
                "x-eval-attempt": str(attempt),
                "x-eval-harness": "claude_code",
            }
        }
    else:
        # 1.99+: proxy_server_request is ONLY the body (no headers anywhere);
        # the shim's x-litellm-spend-logs-metadata header lands here instead.
        metadata["spend_logs_metadata"] = {
            "eval_run_id": run_id,
            "eval_instance_id": instance,
            "eval_attempt": str(attempt),
            "eval_harness": "claude_code",
        }
    response = {"id": request_id, "usage": {"total_tokens": text_tok + cached + 80}}
    return (
        request_id,
        "anthropic_messages",
        "hash-abc",
        0.0,
        text_tok + cached + 80,
        text_tok + cached,
        80,
        ts,
        ts,
        "laguna-xs-2.1-claude_code",
        json.dumps(metadata),
        json.dumps({}),  # messages column: ALWAYS {} in this LiteLLM version
        json.dumps(response),
        json.dumps(psr),
        "session-1" if run_id == _RUN else "session-2",
    )


@pytest.fixture(autouse=True)
def _seed(monkeypatch):
    monkeypatch.setenv("LITELLM_SPEND_DATABASE_URL", _DSN)
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'DELETE FROM "LiteLLM_SpendLogs" WHERE request_id LIKE %s', ("llmlive-test-%",)
            )
            rows = [
                _row(
                    "llmlive-test-a1",
                    _RUN,
                    "sphinx-doc__sphinx-10614",
                    1,
                    "2026-09-02T02:25:00+00:00",
                ),
                _row(
                    "llmlive-test-a2",
                    _RUN,
                    "sphinx-doc__sphinx-10614",
                    1,
                    "2026-09-02T02:26:00+00:00",
                ),
                _row(
                    "llmlive-test-b1", _RUN, "django__django-10097", 1, "2026-09-02T02:27:00+00:00"
                ),
                _row(
                    "llmlive-test-x1",
                    _OTHER_RUN,
                    "sphinx-doc__sphinx-10614",
                    1,
                    "2026-09-02T02:28:00+00:00",
                ),
                _row(
                    "llmlive-test-c1",
                    _RUN,
                    "astropy__astropy-12907",
                    1,
                    "2026-09-02T02:29:00+00:00",
                    shape="v199",
                ),
                # 2026-09-09: the SAME instance as a1/a2 but in the 1.99 shape — the
                # CASE predicate must match both shapes in one query (mixed runs).
                _row(
                    "llmlive-test-a3",
                    _RUN,
                    "sphinx-doc__sphinx-10614",
                    1,
                    "2026-09-02T02:30:00+00:00",
                    shape="v199",
                ),
            ]
            cur.executemany(
                'INSERT INTO "LiteLLM_SpendLogs" (request_id, call_type, api_key, spend, total_tokens, '
                'prompt_tokens, completion_tokens, "startTime", "endTime", model, metadata, messages, '
                "response, proxy_server_request, session_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )
        conn.commit()
        yield
        with conn.cursor() as cur:
            cur.execute(
                'DELETE FROM "LiteLLM_SpendLogs" WHERE request_id LIKE %s', ("llmlive-test-%",)
            )
        conn.commit()
    finally:
        conn.close()


def test_list_scoped_to_run_newest_first():
    from swebench_eval.orchestrator.api.llm_live import list_llm_calls

    rows = list_llm_calls(_RUN)
    ids = [r["request_id"] for r in rows]
    assert ids == [
        "llmlive-test-a3",
        "llmlive-test-c1",
        "llmlive-test-b1",
        "llmlive-test-a2",
        "llmlive-test-a1",
    ]
    assert "llmlive-test-x1" not in ids, "another run's rows must never appear"
    # newest rows are the 1.99-shape ones: coordinates resolve from spend_logs_metadata
    assert rows[0]["instance_id"] == "sphinx-doc__sphinx-10614"
    r = rows[1]
    assert r["instance_id"] == "astropy__astropy-12907"
    assert r["attempt"] == 1
    assert r["harness"] == "claude_code"
    # 1.99 row carries no text_tokens — derived as prompt - cached
    assert r["text_tokens"] == 400 and r["cached_tokens"] == 50_000
    # and the 1.96-shape row still resolves from the old headers path
    r196 = rows[2]
    assert r196["instance_id"] == "django__django-10097"
    assert r196["text_tokens"] == 400 and r196["cached_tokens"] == 50_000
    assert r196["started_at"].startswith("2026-09-02T02:27")


def test_list_instance_filter_and_keyset_pagination():
    from swebench_eval.orchestrator.api.llm_live import list_llm_calls

    # one query, both storage shapes: a3 is the 1.99 shape, a2/a1 the 1.96 headers shape
    sphinx = list_llm_calls(_RUN, instance_id="sphinx-doc__sphinx-10614")
    assert [r["request_id"] for r in sphinx] == [
        "llmlive-test-a3",
        "llmlive-test-a2",
        "llmlive-test-a1",
    ]
    assert list_llm_calls(_RUN, instance_id="sphinx-doc__sphinx-10614", attempt=1) == sphinx
    assert list_llm_calls(_RUN, instance_id="sphinx-doc__sphinx-10614", attempt=2) == []
    # the instance filter must ALSO match the 1.99 shape on its own
    astropy = list_llm_calls(_RUN, instance_id="astropy__astropy-12907")
    assert [r["request_id"] for r in astropy] == ["llmlive-test-c1"]
    page2 = list_llm_calls(_RUN, before=sphinx[1]["started_at"])
    assert [r["request_id"] for r in page2] == ["llmlive-test-a1"]


def test_ensure_spend_log_indexes_is_idempotent_and_lands_in_pg_indexes():
    from swebench_eval.orchestrator.api.llm_live import ensure_spend_log_indexes

    first = ensure_spend_log_indexes()
    assert first and all("litellm_spendlogs_eval_alias_start" in s for s in first)
    second = ensure_spend_log_indexes()  # IF NOT EXISTS: same statements, no error
    assert second == first
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = %s AND indexname = %s",
                ("LiteLLM_SpendLogs", "litellm_spendlogs_eval_alias_start"),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, "index missing after ensure"
    assert "user_api_key_alias" in row[0] and "startTime" in row[0]


def test_detail_reads_conversation_from_proxy_server_request():
    from swebench_eval.orchestrator.api.llm_live import get_llm_call

    d = get_llm_call(_RUN, "llmlive-test-a1")
    assert d is not None
    assert [m["role"] for m in d["messages"]] == ["user", "assistant"]
    assert d["system"] == "you are an engineer"
    assert d["tools_count"] == 2
    assert d["response"]["usage"]["total_tokens"] == 50_480
    # 1.99-shape detail: body-only proxy_server_request, coords from slm
    d199 = get_llm_call(_RUN, "llmlive-test-c1")
    assert d199 is not None
    assert d199["instance_id"] == "astropy__astropy-12907"
    assert [m["role"] for m in d199["messages"]] == ["user", "assistant"]
    # run-scoping: the same request_id under the WRONG run must be None (404)
    assert get_llm_call(_OTHER_RUN, "llmlive-test-a1") is None


def test_routes_end_to_end_and_503_without_dsn(monkeypatch):
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    client = TestClient(app)
    listing = client.get(f"/runs/{_RUN}/llm-live", params={"limit": 2})
    assert listing.status_code == 200
    body = listing.json()
    assert body["run_id"] == _RUN
    assert len(body["items"]) == 2

    detail = client.get(f"/runs/{_RUN}/llm-live/llmlive-test-a1")
    assert detail.status_code == 200
    assert detail.json()["messages"][0]["role"] == "user"

    missing = client.get(f"/runs/{_OTHER_RUN}/llm-live/llmlive-test-a1")
    assert missing.status_code == 404

    monkeypatch.delenv("LITELLM_SPEND_DATABASE_URL")
    degraded = client.get(f"/runs/{_RUN}/llm-live")
    assert degraded.status_code == 503, "no DSN must degrade, never crash"
