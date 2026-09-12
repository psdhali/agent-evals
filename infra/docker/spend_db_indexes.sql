-- Indexes WE add to LiteLLM's spend-log table (database litellm_spend), for the
-- live LLM-call view (swebench_eval/orchestrator/api/llm_live.py).
--
-- LiteLLM's own Prisma schema (v1.99.1) indexes "LiteLLM_SpendLogs" on
-- request_id (PK), "startTime", ("startTime", request_id), end_user and
-- session_id — nothing on the JSONB fields the live view filters by.  Without
-- this, a run-scoped read walks the "startTime" index backwards evaluating
-- metadata->>'user_api_key_alias' on every row of every run.
--
-- Applied by the API at startup (llm_live.ensure_spend_log_indexes — one
-- statement at a time, autocommit: CONCURRENTLY cannot run inside a
-- transaction) and by scripts/ensure_spend_db_indexes.py.  Idempotent.
-- Prisma migrations do not touch indexes they did not create.
--
-- Measured 2026-09-09 (run 39ff3720 live, 60 instances in flight):
-- instance-scoped page 65 s -> sub-second with this index plus the CASE
-- predicate in llm_live.py that stops de-TOASTing proxy_server_request.

CREATE INDEX CONCURRENTLY IF NOT EXISTS litellm_spendlogs_eval_alias_start
    ON "LiteLLM_SpendLogs" ((metadata->>'user_api_key_alias'), "startTime" DESC);
