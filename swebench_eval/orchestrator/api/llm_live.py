"""Live LLM-call view — read-only queries against LiteLLM's spend-log table.

BUILDER4-LITELLM-SPEND-DB-LIVE-VIEW-2026-09-02.md, made real.  The gateway
writes one row per LLM call to ``"LiteLLM_SpendLogs"`` in the ``litellm_spend``
database (``store_prompts_in_spend_logs: true`` — batched by
``PROXY_BATCH_WRITE_AT=5``, so rows lag a call by seconds).  This module is the
API's read path over those rows so the UI can render a run's trajectory LIVE,
while the run is still going — the S3 trajectory artifacts only exist after an
attempt finishes.

The four verified facts this code is built on (all proven against live rows,
run ``01788315793731547650-d0488e54``):

1. The cheap run filter is ``metadata->>'user_api_key_alias' = run_id`` — the
   per-run virtual key's alias IS the run id by construction (run-launch's
   per-run-key wiring).  Never scan ``proxy_server_request`` to find a run.
2. The ``messages`` column is ALWAYS ``{}`` in this LiteLLM version.  The full
   request (messages / system / tools) lives in ``proxy_server_request``.
3. The shim's coordinates survive verbatim in
   ``proxy_server_request -> litellm_metadata -> headers``:
   ``x-eval-instance-id`` / ``x-eval-attempt`` / ``x-eval-harness``.
4. Per-call cache telemetry is in
   ``metadata -> usage_object -> prompt_tokens_details``
   (``text_tokens`` uncached / ``cached_tokens``).  LiteLLM 1.99 dropped
   ``text_tokens`` from that object (kept ``cached_tokens``), so the uncached
   figure is DERIVED as prompt_tokens - cached_tokens when absent — NULL when
   cached_tokens is missing too, never an invented number.  The ``spend`` column is
   always 0.0 (LiteLLM has no price for the custom models) — never sum it;
   real cost lives in the app DB ledger.

Connection: ``LITELLM_SPEND_DATABASE_URL`` (its own env — the app DB's
``DATABASE_URL`` points at ``app_control_plane``, a DIFFERENT database on the
same cluster).  Absent env or unreachable DB degrades to a 503 at the route,
never a crash — the view is a convenience atop the run, not part of it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

_TABLE = '"LiteLLM_SpendLogs"'

# JSONB path fragments (facts 1/3/4 above).
_ALIAS = "metadata->>'user_api_key_alias'"
_HDRS = "proxy_server_request->'litellm_metadata'->'headers'"
_USAGE_DETAILS = "metadata->'usage_object'->'prompt_tokens_details'"
# LiteLLM ≥1.99 stores only the request BODY in proxy_server_request (header
# capture went enterprise-only), so fact 3's path is empty on newer rows.  The
# shim now also sends x-litellm-spend-logs-metadata, whose JSON lands here on
# every row regardless of version — read BOTH shapes, old rows first.
_SLM = "metadata->'spend_logs_metadata'"


def _coord(header_field: str, slm_field: str) -> str:
    """One shim coordinate across both storage shapes (new slm first, old headers second).

    2026-09-09: this was ``COALESCE(headers-path, slm-path)`` and it was the whole reason
    the instance-scoped live view took 65 s on a 60-instance run.  COALESCE evaluates its
    first argument, and that argument lives in ``proxy_server_request`` — the FULL prompt
    body (100–250 KB per row with store_prompts on, TOASTed out of line).  Evaluating the
    WHERE clause de-TOASTed the prompt of every row the scan walked (thousands per page,
    hundreds of MB; CloudWatch ReadIOPS 800+ per page load, CPU flat).

    CASE is guaranteed to evaluate only the branch it needs (COALESCE / OR are not, the
    planner may reorder them), so every 1.99+ row — which carries ``spend_logs_metadata``,
    a few hundred bytes inline in ``metadata`` — never touches the prompt body.  Only
    pre-1.99 rows (headers shape) pay the old cost, and only those rows exist for old runs.
    """
    return (
        f"CASE WHEN {_SLM} ? '{slm_field}' THEN {_SLM}->>'{slm_field}' "
        f"ELSE {_HDRS}->>'{header_field}' END"
    )


_INDEX_SQL_PATH = Path(__file__).resolve().parents[3] / "infra" / "docker" / "spend_db_indexes.sql"


def ensure_spend_log_indexes(sql_path: Path | None = None) -> list[str]:
    """Create OUR indexes on LiteLLM's spend-log table (idempotent; never raises).

    ``infra/docker/spend_db_indexes.sql`` is the record; this applies it — one statement
    per execute on an autocommit, read-write connection, because ``CREATE INDEX
    CONCURRENTLY`` refuses to run inside a transaction block.  Called by the API at
    startup in a background thread (the API is the only consumer of these reads and the
    only service that holds ``LITELLM_SPEND_DATABASE_URL``) and by
    ``scripts/ensure_spend_db_indexes.py`` for a manual run.  Returns the statements that
    were executed; an unreachable DB or a failing statement is logged and skipped — the
    view degrades to slow, never to down.
    """
    path = sql_path or _INDEX_SQL_PATH
    if not path.exists():
        logger.warning("spend-log index file not found at %s — skipping", path)
        return []
    statements = [
        s.strip()
        for s in "\n".join(
            ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("--")
        ).split(";")
        if s.strip()
    ]
    try:
        from psycopg2 import connect

        conn = connect(**_connection_kwargs())
    except Exception as exc:  # noqa: BLE001 — degrade to slow, never crash the API
        logger.warning("spend-log indexes not applied (spend DB unavailable): %s", exc)
        return []
    done: list[str] = []
    try:
        conn.set_session(autocommit=True)
        for stmt in statements:
            try:
                with conn.cursor() as cur:
                    cur.execute(stmt)
                done.append(stmt)
                logger.info("spend-log index ensured: %s", stmt.split(" ON ")[0])
            except Exception:  # one bad statement must not stop the rest (logged)
                logger.warning("spend-log index statement failed: %s", stmt[:120], exc_info=True)
    finally:
        conn.close()
    return done


def ensure_spend_log_indexes_in_background() -> None:
    """Startup hook: apply the indexes without blocking the API's health check."""
    import threading

    threading.Thread(target=ensure_spend_log_indexes, name="spend-log-indexes", daemon=True).start()


class LlmLiveUnavailable(Exception):
    """The spend DB is not configured or not reachable — surface as 503."""


def _connection_kwargs() -> dict[str, Any]:
    url = os.environ.get("LITELLM_SPEND_DATABASE_URL")
    if not url:
        raise LlmLiveUnavailable(
            "LITELLM_SPEND_DATABASE_URL is not set — the live LLM-call view "
            "needs the litellm_spend DSN (wired via the ui tier's task "
            "definition; local dev: point it at the compose Postgres)"
        )
    try:
        parsed = urlparse(url)
        return {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 5432,
            "user": parsed.username or "",
            "password": unquote(parsed.password or ""),
            "dbname": (parsed.path or "/").lstrip("/"),
        }
    except Exception as exc:
        raise LlmLiveUnavailable(f"could not parse LITELLM_SPEND_DATABASE_URL: {exc}") from exc


def _connect() -> Any:
    from psycopg2 import connect

    try:
        conn = connect(**_connection_kwargs())
    except LlmLiveUnavailable:
        raise
    except Exception as exc:
        raise LlmLiveUnavailable(f"litellm_spend DB unreachable: {exc}") from exc
    conn.set_session(readonly=True, autocommit=True)  # SELECT-only surface, enforced
    return conn


def list_llm_calls(
    run_id: str,
    *,
    instance_id: str | None = None,
    attempt: int | None = None,
    limit: int = 50,
    before: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-first light rows for one run (optionally one instance / attempt).

    ``attempt`` (2026-09-06) narrows an instance to ONE attempt — the instance
    page is per attempt, and a restarted instance's page showed the previous
    attempt's calls without it.  ``status`` rides along on every row: LiteLLM
    writes a ``failure`` row for a call the provider rejected (the shim's
    metadata never reached it, so such a row carries no coordinates) — the UI
    labels it a failed call instead of "unlabelled".

    ``before`` is the previous page's oldest ``started_at`` (ISO string) —
    keyset pagination on ``"startTime"``; LIMIT alone would skate as new rows
    arrive mid-scroll, which on a LIVE view is the common case not the edge.
    """
    limit = max(1, min(int(limit), 200))
    where = [f"{_ALIAS} = %s"]
    params: list[Any] = [run_id]
    if instance_id:
        where.append(f"{_coord('x-eval-instance-id', 'eval_instance_id')} = %s")
        params.append(instance_id)
    if attempt is not None:
        # the coordinate is stored as text (a header / JSON string), so compare as text
        where.append(f"{_coord('x-eval-attempt', 'eval_attempt')} = %s")
        params.append(str(int(attempt)))
    if before:
        where.append('"startTime" < %s')
        params.append(before)
    sql = f"""
        SELECT request_id,
               "startTime",
               model,
               prompt_tokens,
               completion_tokens,
               session_id,
               status,
               {_coord("x-eval-instance-id", "eval_instance_id")} AS instance_id,
               {_coord("x-eval-attempt", "eval_attempt")}         AS attempt,
               {_coord("x-eval-harness", "eval_harness")}         AS harness,
               COALESCE(
                   {_USAGE_DETAILS}->>'text_tokens',
                   (prompt_tokens - ({_USAGE_DETAILS}->>'cached_tokens')::bigint)::text
               ) AS text_tokens,
               {_USAGE_DETAILS}->>'cached_tokens' AS cached_tokens
        FROM {_TABLE}
        WHERE {" AND ".join(where)}
        ORDER BY "startTime" DESC
        LIMIT %s
    """
    params.append(limit)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        st = r.pop("startTime", None)
        r["started_at"] = st.isoformat() if hasattr(st, "isoformat") else (str(st) if st else "")
        for k in ("text_tokens", "cached_tokens", "attempt"):
            r[k] = int(r[k]) if r.get(k) not in (None, "") else None
    return rows


def _parts_text(parts: Any) -> str:
    """The text of a Responses-API content list (input_text / output_text /
    summary_text / text parts), or the string itself."""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict) and isinstance(p.get("text"), str):
            out.append(p["text"])
    return "\n".join(s for s in out if s)


def responses_input_to_messages(items: Any) -> list[dict[str, Any]]:
    """Normalise an OpenAI Responses-API ``input`` into chat-shaped messages.

    ``message`` items keep their role and flatten their content parts;
    ``function_call`` becomes an assistant message carrying an OpenAI-style
    ``tool_calls`` entry (the panel's OpenAIToolCall renders it);
    ``function_call_output`` becomes a ``tool`` message; ``reasoning`` becomes
    an assistant message with ``reasoning`` text.  Unknown item types are kept
    as a labelled placeholder rather than dropped — the record must not lie by
    omission.  A bare string input is a single user message.
    """
    if isinstance(items, str):
        return [{"role": "user", "content": items}]
    if not isinstance(items, list):
        return []
    msgs: list[dict[str, Any]] = []
    for it in items:
        if isinstance(it, str):
            msgs.append({"role": "user", "content": it})
            continue
        if not isinstance(it, dict):
            continue
        kind = it.get("type") or ("message" if "role" in it else "")
        if kind == "message":
            msgs.append(
                {"role": str(it.get("role") or "user"), "content": _parts_text(it.get("content"))}
            )
        elif kind == "function_call":
            msgs.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": it.get("call_id") or it.get("id"),
                            "type": "function",
                            "function": {"name": it.get("name"), "arguments": it.get("arguments")},
                        }
                    ],
                }
            )
        elif kind == "function_call_output":
            out = it.get("output")
            msgs.append(
                {
                    "role": "tool",
                    "content": out if isinstance(out, str) else _parts_text(out),
                    "tool_call_id": it.get("call_id"),
                }
            )
        elif kind == "reasoning":
            text = _parts_text(it.get("summary")) or _parts_text(it.get("content"))
            msgs.append(
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning": text or "(encrypted / not returned)",
                }
            )
        else:
            msgs.append({"role": "tool", "content": f"[{kind or 'item'}]"})
    return msgs


def get_llm_call(run_id: str, request_id: str) -> dict[str, Any] | None:
    """One call's full detail — conversation from ``proxy_server_request``.

    The run_id predicate is part of the WHERE, not a courtesy: a request_id is
    globally unique but the route is run-scoped, and a row from another run
    must 404 rather than leak across runs.
    """
    sql = f"""
        SELECT request_id,
               "startTime",
               model,
               prompt_tokens,
               completion_tokens,
               session_id,
               status,
               {_coord("x-eval-instance-id", "eval_instance_id")} AS instance_id,
               {_coord("x-eval-attempt", "eval_attempt")}         AS attempt,
               {_coord("x-eval-harness", "eval_harness")}         AS harness,
               proxy_server_request,
               response
        FROM {_TABLE}
        WHERE {_ALIAS} = %s AND request_id = %s
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (run_id, request_id))
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            r = dict(zip(cols, row, strict=True))
    finally:
        conn.close()
    st = r.pop("startTime", None)
    r["started_at"] = st.isoformat() if hasattr(st, "isoformat") else (str(st) if st else "")
    r["attempt"] = int(r["attempt"]) if r.get("attempt") not in (None, "") else None
    psr = r.pop("proxy_server_request", None) or {}
    if not isinstance(psr, dict):
        psr = {}
    # Fact 2: the request body is HERE; the messages COLUMN is always {}.
    messages = psr.get("messages") or []
    # 2026-09-07 (owner, codex run c10df654): a Responses-API body (codex) has
    # no `messages` — its conversation is the `input` item list and its system
    # prompt is `instructions`.  The detail used to show only the response's
    # own output ("one step"); normalise the input into the chat shape the
    # panel already renders so the whole history is visible.
    if not messages and psr.get("input") is not None:
        messages = responses_input_to_messages(psr.get("input"))
    r["messages"] = messages
    r["system"] = psr.get("system") or psr.get("instructions")
    r["tools_count"] = len(psr.get("tools") or [])
    resp = r.get("response")
    r["response"] = resp if isinstance(resp, dict) else None
    return r
