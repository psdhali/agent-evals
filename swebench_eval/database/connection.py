"""Postgres connection management.

Uses ``psycopg2`` (synchronous) for Phase 2.  Sensible defaults for the
Docker Compose setup in ``infra/docker/docker-compose.yml`` — override via
the standard ``PGHOST``, ``PGPORT``, ``PGUSER``, ``PGPASSWORD``, ``PGDATABASE``
environment variables.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg2

logger = logging.getLogger(__name__)

# Psycopg2 kwargs — populated from PG* env vars with Docker Compose defaults.
# Env var → psycopg2 kwarg mapping.
_PSYCOPG2_KWARGS: dict[str, str] = {
    "PGHOST": "host",
    "PGPORT": "port",
    "PGUSER": "user",
    "PGPASSWORD": "password",
    "PGDATABASE": "dbname",
}

# Defaults matching the Docker Compose postgres service.
_DEFAULTS: dict[str, str] = {
    "PGHOST": "localhost",
    "PGPORT": "5432",
    "PGUSER": "eval",
    "PGPASSWORD": "eval",
    "PGDATABASE": "eval_framework",
}


def _connection_kwargs() -> dict[str, str]:
    """Return psycopg2 kwargs from ``DATABASE_URL`` if set, else PG* env/defaults.

    Phase 5 swap (5a-ii, recorded in the plan): the deployed ECS task definitions
    inject the Aurora DSN via the ``DATABASE_URL`` secret — but the local dev
    stack (and any non-AWS run) uses separate ``PG*`` vars. The seam is exactly
    that: prefer ``DATABASE_URL`` when present (the AWS shape), then ``PG*``
    overrides, then the Compose defaults. Without this, a deployed task would
    silently fall back to ``localhost:5432 eval/eval`` and fail every query while
    looking misconfigured (the empty-secret failure DoD 2 names).
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        try:
            from urllib.parse import unquote, urlparse

            parsed = urlparse(url)
            host = parsed.hostname or ""
            port = str(parsed.port or 5432)
            user = parsed.username or ""
            password = unquote(parsed.password or "")
            dbname = (parsed.path or "/").lstrip("/")
            return {
                "host": host,
                "port": port,
                "user": user,
                "password": password,
                "dbname": dbname,
            }
        except Exception:  # noqa: BLE001
            logger.warning("Could not parse DATABASE_URL=%r; falling back to PG* env", url)
    return {
        _PSYCOPG2_KWARGS[env_key]: os.environ.get(env_key, _DEFAULTS[env_key])
        for env_key in _DEFAULTS
    }


def get_connection() -> psycopg2.extensions.connection:
    """Return a ``psycopg2`` connection (``DATABASE_URL``, else PG* env, else defaults)."""
    from psycopg2 import connect  # deferred import so the module is importable without psycopg2

    return connect(**_connection_kwargs())


def run_migrations() -> None:
    """Read and execute ``infra/docker/init.sql`` (idempotent via ``IF NOT EXISTS``).

    The SQL file path is resolved relative to the project root (parent of the
    ``swebench_eval`` package).
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    sql_path = project_root / "infra" / "docker" / "init.sql"
    if not sql_path.exists():
        logger.warning("Migration file not found at %s — skipping", sql_path)
        return

    sql = sql_path.read_text()
    conn = get_connection()
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            logger.info("Migrations applied from %s", sql_path)
        except Exception:
            conn.rollback()
            # Run drift detection even on failure — a migration error against
            # a legacy database is the exact scenario the drift check exists for.
            # An actionable RuntimeError is more useful than a raw SQL error.
            _check_schema_drift(conn)
            raise

        # Drift detection — verify the live schema matches the expected columns.
        # CREATE TABLE IF NOT EXISTS is idempotent-by-skip: it can create a
        # schema but can never evolve one.  If a column was renamed or added
        # in init.sql but the ALTER was skipped, this catches it.
        _check_schema_drift(conn)
    finally:
        conn.close()


def ensure_additional_databases() -> list[str]:
    """Ensure the non-default databases ADR-0022 requires exist on the shared cluster.

    Terraform provisions the Aurora cluster with ``database_name =
    app_control_plane`` (the control-plane's DB). ADR-0022's second database,
    ``litellm_spend``, is **not** created by Terraform — review M-2/N-2 removed
    the ``local-exec psql`` provisioner (a laptop psql into a private Aurora has
    no path). It is instead created here, by the application, at startup inside
    the VPC with credentials from Secrets Manager — the same place migrations run.

    Implements the N-2 requirement concretely (grep-satisfiable, not a comment):
    connect as the master user, ``CREATE DATABASE`` any of
    ``names`` that do not already exist, then return the created list. ``CREATE
    DATABASE`` cannot run inside a transaction in Postgres, so each name is
    autocommitted individually.

    Returns the names created (empty on subsequent boots — idempotent).
    """
    names = ("litellm_spend",)  # ADR-0022: the second DB, owned by the app
    from psycopg2 import connect  # lazy, consistent with get_connection

    # Always create against the control-plane DB (a guaranteed-valid database on
    # the cluster); connect as the master user (the Aurora master). The DATABASE
    # URL (or PG* env) must point at app_control_plane, NOT litellm_spend —
    # we're the thing that makes litellm_spend exist. When DATABASE_URL carries
    # its own dbname (the AWS shape) it is already app_control_plane; only fall
    # back to PGDATABASE env/default when there is no DATABASE_URL to lose.
    kwargs = _connection_kwargs()
    if not os.environ.get("DATABASE_URL"):
        kwargs["dbname"] = os.environ.get("PGDATABASE", _DEFAULTS["PGDATABASE"])

    created: list[str] = []
    conn = connect(**kwargs)
    try:
        conn.autocommit = True  # CREATE DATABASE must run outside a transaction
        with conn.cursor() as cur:
            for name in names:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE DATABASE {name}")  # internal, task-controlled constant
                    created.append(name)
                    logger.info("Created database %s (ADR-0022)", name)
                else:
                    logger.info("Database %s already exists", name)
    finally:
        conn.close()

    return created


def _check_schema_drift(conn: psycopg2.extensions.connection) -> None:
    """Verify the live database schema matches the expected shape.

    Raises ``RuntimeError`` if a known column or foreign-key constraint is
    missing — this means the schema file and the live database disagree.
    """
    expected: dict[tuple[str, str], str] = {
        # (table, column) pairs that must exist — add here when schema changes.
        ("capacity_snapshot", "current_workers"): "round-1: renamed from current",
        ("runs", "cost_confidence_tier"): "round-1: added cost confidence tier",
        ("instance_results", "report_path"): "round-3: added eval-report S3 key column",
        ("runs", "stop_requested_at"): "ADR-0034 M1: per-run abort intent",
        ("runs", "stop_scope"): "ADR-0034 M1: per-run abort intent scope",
        ("runs", "stopped_at"): "ADR-0034 M1: per-run abort completion",
        # ADR-0037 / M0 §2.2 — the llm_calls per-model-call table.  A
        # representative column per group plus the PK constraint below is the
        # drift check; enumerating all ~35 would duplicate init.sql.
        ("llm_calls", "call_index"): "ADR-0037 M0: llm_calls PK column (shim call_index)",
        ("llm_calls", "provider_name"): "ADR-0037 M0: llm_calls provider identity",
        ("llm_calls", "started_at"): "ADR-0037 M0: llm_calls started_at (NOT NULL)",
        ("llm_calls", "rate_limit_scope"): "ADR-0037 M0: llm_calls rate-limit scope",
        # R5.2 (review M2): stop_reason for the Anthropic messages / OpenAI
        # responses wire formats — the curated allowlist is why the drift guard
        # could NOT catch the missing column, so it gets an explicit entry.
        ("llm_calls", "stop_reason"): "R5.2: claude_code/codex truncation marker",
        # 2026-08-26 (to-the-verified-run, STEP 1.6/STEP 3): the metering +
        # latency capture columns.  Representative of each new group — a missing
        # one must fail loudly at startup (run_migrations) rather than silently
        # dropping a field the shim already captured.
        (
            "llm_calls",
            "native_finish_reason",
        ): "1.6+G-4: provider-native + Responses-API completion stop signal",
        ("llm_calls", "system_fingerprint"): "1.6: provider/model build identity",
        ("llm_calls", "stream_ms"): "STEP 3: latency breakdown (stream_ms)",
        (
            "llm_calls",
            "gateway_response_ms",
        ): "STEP 3: LiteLLM timing header (gateway response ms)",
        (
            "llm_calls",
            "upstream_inference_prompt_cost_usd",
        ): "1.6: provider prompt/completion cost split",
        # BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5/§2.6 — the L1 pacer's footprint.
        # Representative of the per-call decomposition + diagnostics (llm_calls), the
        # per-instance rollup (instance_results), and the per-tick pressure
        # (capacity_snapshot); the full sets are the init.sql ALTER blocks.
        ("llm_calls", "paced_wait_ms"): "pacer §2.6: per-call admission wait (Σ re-acquires)",
        ("llm_calls", "pacer_deny_axis"): "pacer §2.5: last axis that denied this call",
        ("instance_results", "paced_wait_ms_total"): "pacer §2.5: per-instance hold rollup",
        ("capacity_snapshot", "pacer_queue_len"): "pacer §2.5: live wait-queue depth per tick",
        (
            "capacity_snapshot",
            "decision",
        ): "forecast review 2026-09-03: full decision record per tick",
        # ADR-0037 / M0 §4/§5 — phase timing columns (representative: one harness
        # and one eval; the full set is the init.sql ALTER block).
        ("instance_results", "task_observed_s"): "ADR-0037 M0 §4: harness phase-sum reconciliation",
        ("instance_results", "eval_test_s"): "ADR-0037 M0 §5: eval grading duration",
        # ADR-0038 — contamination + honesty columns.
        ("instance_results", "stripped_test_paths"): "ADR-0038: stripped gold-test paths",
        ("instance_results", "grade_invalid"): "ADR-0038: gold-tests-could-not-apply flag",
        ("instance_results", "leaked_node_ids"): "ADR-0038: absent FAIL_TO_PASS ids in patch+traj",
        (
            "instance_results",
            "gold_patch_similarity",
        ): "ADR-0038: model-vs-gold patch difflib ratio",
        # ADR-0037 / M0 §1.3 — per-instance token/cost + the adapter cross-check.
        ("instance_results", "input_tokens"): "M0 §1.3: shim (authoritative) input tokens",
        ("instance_results", "cost_usd"): "M0 §1.3: shim (authoritative) cost",
        ("instance_results", "adapter_input_tokens"): "M0 §1.3: adapter cross-check tokens",
        # N-1 (review): leak-detectability, distinct from leaked_node_ids.
        ("instance_results", "leak_detectable"): "ADR-0038 §2: was the instance leak-detectable",
        # Compaction build (BUILD-SPEC §6): the measurement is non-negotiable —
        # without compactions_fired you cannot answer "did compaction change this
        # result?".  Representative column; the full set is the init.sql ALTER.
        (
            "instance_results",
            "compactions_fired",
        ): "compaction build §6: per-instance compaction counter",
        # heartbeat-cas-review §3: control_state.updated_at is the CAS guard
        # value for the publisher's reconcile — if it is missing, the drift
        # check should catch it at startup rather than _read_updated_at
        # returning 0.0/None and the pause guard failing.
        ("control_state", "updated_at"): "heartbeat-cas-review: CAS reconcile guard",
        # builder4 run-launch §9: the claim mutex + per-run key identity + the
        # seed/dispatch ledger.  Representative columns; the full set is the
        # init.sql ALTER block.
        ("runs", "active_key"): "run-launch: the (harness, model_alias) claim mutex column",
        ("runs", "litellm_key_id"): "run-launch: per-run LiteLLM virtual key id (never the key)",
        (
            "instance_results",
            "seeded_at",
        ): "run-launch: pre-seeded row timestamp (expected = count(*) by construction)",
        # offline-analysis-design.md §9 DoD #3 (2026-09-01): distinguishes "never
        # scanned" from "scanned" for leak detection — its absence would silently
        # let leaked_node_ids=NULL mean two different things (§10.6).
        (
            "instance_results",
            "leak_scan_at",
        ): "offline-analysis §9 DoD #3: never-scanned vs scanned-unknown distinction",
        # Pass B — LLM judge (offline-analysis-design.md §4/§10, 2026-09-01).
        # Representative columns; the full set is the init.sql CREATE TABLE.
        ("judge_results", "rubric_sha256"): "offline-analysis §4: judge rubric provenance",
        ("judge_results", "judge_prune_mode"): "offline-analysis §10.4: configured prune mode",
        ("judge_sampling", "openrouter_key_hash"): "offline-analysis §10.2: per-pass minted key id",
        (
            "judge_sampling",
            "total_eligible",
        ): "review 2026-09-02 §3.2: pass-level completeness, so a budget-truncated pass is visible",
        (
            "judge_sampling",
            "synthesis",
        ): "2026-09-08: pass-level synthesis report (one judge call over the run's judgments)",
        (
            "judge_dimension_scores",
            "reasoning",
        ): "offline-analysis §10.7: normalized per-dimension reporting projection",
        (
            "judge_dimension_scores",
            "causes",
        ): "rubric v3 (2026-09-09): token_efficiency's classified causes",
        (
            "judge_results",
            "efficiency_profile",
        ): "rubric v3 (2026-09-09): the computed efficiency profile the judge was shown",
        ("judge_pass_lock", "pass_id"): "offline-analysis §10.2 point 4: judge-model claim mutex",
        (
            "judge_calibration_reviews",
            "reviewer_reasoning",
        ): "offline-analysis §11: in-app calibration review, supersedes §3.9 hand-labeling",
    }
    # (constraint name) — R2-2: results must be attributable to a registered run.
    # instance_progress FK removed at ADR-0018/R3-2 (the table is gone, progress
    # is on Redis).
    expected_fk: dict[str, str] = {
        "instance_results_run_id_fkey": "round-2: FK instance_results.run_id -> runs.run_id",
        "llm_calls_pkey": "ADR-0037 M0: composite PK makes batch redelivery a no-op",
    }

    with conn.cursor() as cur:
        for (table, column), description in expected.items():
            cur.execute(
                """SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = %s AND column_name = %s
                )""",
                (table, column),
            )
            exists = bool(cur.fetchone()[0])
            if not exists:
                raise RuntimeError(
                    f"Schema drift detected: column {table}.{column} is missing "
                    f"({description}).  The database schema does not match "
                    f"infra/docker/init.sql.  Run `docker compose down -v` to "
                    f"reset, or apply the missing ALTER statements manually."
                )

        for constraint, description in expected_fk.items():
            cur.execute(
                """SELECT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = %s
                )""",
                (constraint,),
            )
            exists = bool(cur.fetchone()[0])
            if not exists:
                raise RuntimeError(
                    f"Schema drift detected: constraint {constraint} is missing "
                    f"({description}).  The database schema does not match "
                    f"infra/docker/init.sql.  Run `docker compose down -v` to "
                    f"reset, or apply the missing ALTER statements manually."
                )

        # run-launch §9: uq_runs_active_key is a CREATE UNIQUE INDEX, not an
        # ALTER TABLE ADD CONSTRAINT, so it never appears in pg_constraint —
        # it needs its own existence check (pg_indexes) or a missing mutex
        # would fail silently (two concurrent claims for the same pair would
        # both succeed instead of the second getting a unique-violation 409).
        cur.execute(
            """SELECT EXISTS (
                SELECT 1 FROM pg_indexes WHERE indexname = %s
            )""",
            ("uq_runs_active_key",),
        )
        if not bool(cur.fetchone()[0]):
            raise RuntimeError(
                "Schema drift detected: index uq_runs_active_key is missing "
                "(run-launch: the (harness, model_alias) claim mutex).  The "
                "database schema does not match infra/docker/init.sql.  Run "
                "`docker compose down -v` to reset, or apply the missing "
                "CREATE UNIQUE INDEX manually."
            )

        # run-launch §6.3: state_rank() is the monotonic state guard every
        # results_writer upsert depends on — its absence would silently
        # disable the WHERE clause (a SQL error on every result write), so
        # catch it here rather than at the first result of the first run.
        cur.execute(
            """SELECT EXISTS (
                SELECT 1 FROM pg_proc WHERE proname = %s
            )""",
            ("state_rank",),
        )
        if not bool(cur.fetchone()[0]):
            raise RuntimeError(
                "Schema drift detected: function state_rank() is missing "
                "(run-launch §6.3: the monotonic state guard).  The database "
                "schema does not match infra/docker/init.sql.  Run `docker "
                "compose down -v` to reset, or apply the missing CREATE "
                "FUNCTION manually."
            )

    logger.info("Schema drift check passed — all expected columns/constraints present")
