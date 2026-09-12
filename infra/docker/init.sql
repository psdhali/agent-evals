-- Phase 2 — app-control-plane schema for the eval framework.
--
-- Executed automatically by the postgres container on first startup
-- (mounted to /docker-entrypoint-initdb.d/).  All DDL uses IF NOT EXISTS
-- so it's safe to re-run.
--
-- See architecture.md §8 for the full data model.

-- ---------------------------------------------------------------------------
-- Runs — one row per run, created by the orchestrator API at POST /runs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,          -- ULID, sortable by creation time
    config_snapshot     JSONB NOT NULL,            -- resolved config actually used (image digests, gateway hash, etc.)
    estimated_cost_usd  NUMERIC,                   -- upfront estimate from cost_estimator
    cost_confidence_tier TEXT,                     -- historical / calibration / default (architecture §7.2)
    compute_cost_estimated_usd NUMERIC,
    compute_cost_reconciled_usd NUMERIC,
    budget_cap_usd      NUMERIC,                   -- per-run aggregate spend ceiling
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    status              TEXT NOT NULL DEFAULT 'pending',
    -- builder4 run-launch: the claim mutex + per-run key identity.  active_key
    -- is '<harness>:<model_alias>' while the run holds the pair (decision 2),
    -- NULL once finalised — the unique partial index below IS the mutex a
    -- check-then-act cannot be (see control_plane/run_launch.py).  Never store
    -- key material: litellm_key_id is LiteLLM's key id (not the key), and
    -- openrouter_key_hash is a hash of the OpenRouter key, never the key itself.
    active_key          TEXT,
    litellm_key_id      TEXT,
    openrouter_key_hash TEXT,
    dispatched_at        TIMESTAMPTZ,
    finalised_at          TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- Run targets — which harness×model combos this run is evaluating.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_targets (
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    harness     TEXT NOT NULL,                     -- e.g. "custom_minimal", "aider", "claude_code"
    model_alias TEXT NOT NULL,                     -- gateway model alias, e.g. "cheap-oss-model"
    PRIMARY KEY (run_id, harness, model_alias)
);

-- ---------------------------------------------------------------------------
-- Instance results — one row per (run, instance, attempt, phase).
-- Idempotency key: (run_id, instance_id, attempt_number, phase).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instance_results (
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    instance_id             TEXT NOT NULL,
    attempt_number          INT NOT NULL,
    phase                   TEXT NOT NULL,          -- 'harness' or 'eval'
    state                   TEXT NOT NULL,          -- state machine state (PENDING, HARNESS_RUNNING, RESOLVED, ...)
    error_category          TEXT,                   -- from architecture §9.3 error taxonomy
    error_detail            TEXT,                   -- free-text detail (exception message, etc.)
    verdict                 TEXT,                   -- resolved/unresolved (eval phase only)
    wall_clock_harness_s    FLOAT,                  -- harness-phase wall-clock seconds
    wall_clock_eval_s       FLOAT,                  -- eval-phase wall-clock seconds
    touches_test_files      BOOLEAN DEFAULT FALSE,  -- does patch touch any test-patch path?
    patch_path              TEXT,                   -- S3 / local path to patch.diff
    trajectory_path         TEXT,                   -- S3 / local path to trajectory.jsonl
    raw_log_path            TEXT,                   -- S3 / local path to harness_stdout.log
    report_path             TEXT,                   -- S3 / local path to eval_report.json
    report_json             JSONB DEFAULT '{}',     -- eval report JSON (eval phase only)
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, instance_id, attempt_number, phase)
);
-- FK to runs: added at Phase 3 round 2 (R2-2).  Phase 2 deliberately omitted it
-- for write decoupling per ADR-0007; that decision was revisited when orphaned
-- instance_results rows (no matching run_targets) were found — an unattributed
-- result is silently dropped from the Phase 8 harness×model rollup denominator.
-- Every path that writes a result now calls orchestrator.dispatcher.register_run()
-- first (single way to start a run), so the parent row always exists and the FK
-- can enforce integrity instead of being an accident.

-- ---------------------------------------------------------------------------
-- Capacity snapshots — one row per autoscaler tick, feeds dashboard Axis A/B charts.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS capacity_snapshot (
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    queue_depth         INT,
    gateway_headroom    INT,
    current_workers     INT,                        -- current desiredCount (was "current" — reserved word)
    desired             INT,
    pool                TEXT NOT NULL,              -- 'harness' or 'eval'
    PRIMARY KEY (ts, pool)
);
-- 2026-09-09 query audit: idx_capacity_snapshot_ts duplicated the PK's leading column (ts, pool)
-- and could not serve GET /capacity?pool=... (pool is second in the PK). Replaced by (pool, ts DESC).
DROP INDEX IF EXISTS idx_capacity_snapshot_ts;
CREATE INDEX IF NOT EXISTS idx_capacity_snapshot_pool_ts ON capacity_snapshot (pool, ts DESC);

-- ---------------------------------------------------------------------------
-- Run summary — maintained incrementally by the Results Writer.
-- One row per run, updated as instance results land.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_summary (
    run_id      TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    summary_json JSONB NOT NULL DEFAULT '{}'
);

-- ---------------------------------------------------------------------------
-- Operator control plane (ADR-0034 / observability M1.1) — single-row pause
-- state + per-run abort intent.  Aurora is the source of truth; Valkey is the
-- read path (control/state.py).  The CHECK on the bool PK makes the break-glass
-- UPDATE unambiguous with no WHERE to get wrong at 3am.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS control_state (
    id             BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    harness_paused BOOLEAN     NOT NULL DEFAULT FALSE,
    eval_paused    BOOLEAN     NOT NULL DEFAULT FALSE,
    gateway_paused BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by     TEXT,
    reason         TEXT
);
INSERT INTO control_state (id) VALUES (TRUE) ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Schema evolution — idempotent ALTERs for existing databases.
--
-- CREATE TABLE IF NOT EXISTS is idempotent-by-skip: it can create a schema but
-- can never evolve one.  These ALTER statements handle schema changes made after
-- the initial creation.  Every statement is guarded (IF NOT EXISTS / IF EXISTS)
-- so they are safe to re-run.
--
-- Discipline: when a CREATE TABLE above is modified, append a corresponding
-- ALTER below.  This is a stopgap until Phase 5 (Aurora requires real
-- migrations).  A drift check in database/connection.py fails loudly if the
-- live schema and this file disagree.
-- ---------------------------------------------------------------------------

-- Round-1 review fixes (2026-08-10):
ALTER TABLE runs ADD COLUMN IF NOT EXISTS cost_confidence_tier TEXT;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'instance_results' AND column_name = 'report_json'
          AND data_type = 'text'
    ) THEN
        -- Three-step: DROP DEFAULT first — Postgres won't auto-cast a text
        -- default expression to jsonb, so ALTER TYPE alone raises DatatypeMismatch.
        ALTER TABLE instance_results ALTER COLUMN report_json DROP DEFAULT;
        ALTER TABLE instance_results ALTER COLUMN report_json TYPE JSONB USING report_json::jsonb;
        ALTER TABLE instance_results ALTER COLUMN report_json SET DEFAULT '{}'::jsonb;
    END IF;
END $$;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'capacity_snapshot' AND column_name = 'current'
    ) THEN
        ALTER TABLE capacity_snapshot RENAME COLUMN current TO current_workers;
    END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'capacity_snapshot'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE capacity_snapshot ADD PRIMARY KEY (ts, pool);
    END IF;
END $$;

-- Capacity view (CAPACITY-AND-PIPELINE-VIEW-DESIGN-2026-08-31.md §4.1), 2026-09-01: the
-- observation-tick columns. NULL means "not measured", never zero — a null renders as
-- "not measured" downstream (design §6). gateway_headroom stays but is written NULL
-- permanently: it must never carry a scraped LiteLLM number; ceiling_utilization replaces it.
ALTER TABLE capacity_snapshot ADD COLUMN IF NOT EXISTS not_visible         INT;
ALTER TABLE capacity_snapshot ADD COLUMN IF NOT EXISTS binding_constraint  TEXT;
ALTER TABLE capacity_snapshot ADD COLUMN IF NOT EXISTS ceiling_utilization DOUBLE PRECISION;
ALTER TABLE capacity_snapshot ADD COLUMN IF NOT EXISTS decision_age_s      DOUBLE PRECISION;
-- ETAs are RANGES (median and p90), never points — measured at concurrency ~1, so a fleet-wide
-- figure is a 5-6x extrapolation and a confident point would get planned against (design §5).
ALTER TABLE capacity_snapshot ADD COLUMN IF NOT EXISTS eta_low_s           INT;
ALTER TABLE capacity_snapshot ADD COLUMN IF NOT EXISTS eta_high_s          INT;
-- §2.4 of the 2026-09-01 wiring review: the planner constants' provenance per tick
-- ('pacer_cfg' | 'pacer_cfg_stale' | 'defaults', harness rows only). Decision records
-- computed from generic defaults must say so in the time series the live-flip comparison
-- reads — a full chart of plausible defaults-based decisions is this project's recurring
-- "check that runs, passes, and cannot fail" defect.
ALTER TABLE capacity_snapshot ADD COLUMN IF NOT EXISTS constants_source    TEXT;
-- BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5: the pacer's live pressure, per tick,
-- measured at the component that enforces it (harness rows only; NULL on eval rows and
-- when no alias has fresh traffic). pacer_queue_len = max wait-queue depth over active
-- aliases; paced_over_2s_share = share of admissions in the last 60s that waited > 2s (the
-- design's p95-back-pressure proxy), max over aliases. NULL = not measured, never 0.
ALTER TABLE capacity_snapshot ADD COLUMN IF NOT EXISTS pacer_queue_len     INT;
ALTER TABLE capacity_snapshot ADD COLUMN IF NOT EXISTS paced_over_2s_share DOUBLE PRECISION;
-- BUILDER4-DISPATCHER-FORECAST-REVIEW-2026-09-03: the pool's FULL autoscaler decision record
-- as published to Redis at the tick (per-alias ceilings / bindings / curve sources, booting
-- tasks, hold-cap timeouts, wait-queue depth, budgets, mode, in_flight) — so "why did the
-- dispatcher launch / hold at 14:07" is answerable from Aurora after the fact. NULL = no record.
ALTER TABLE capacity_snapshot ADD COLUMN IF NOT EXISTS decision            JSONB;

-- Round-3 fix (2026-08-11): eval report is stored as S3 object; reference it by
-- path column (report_path), keep report_json JSONB for the blob.
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS report_path TEXT;

-- Round-2 (R2-2): FK from instance_results to runs — added after every
-- result-writing path started calling register_run().  Orphaned rows were
-- cleaned first so the constraint can be added without failing.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'instance_results'::regclass AND conname = 'instance_results_run_id_fkey'
    ) THEN
        ALTER TABLE instance_results ADD CONSTRAINT instance_results_run_id_fkey
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE;
    END IF;
END $$;

-- Round-4 (ADR-0018/R3-2): live progress moved to Redis; the Postgres table is
-- gone.  Drop it for databases created before this change (the legacy-DB path),
-- so a database with the old table migrates cleanly instead of failing (LF-1).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'instance_progress' AND table_schema = 'public'
    ) THEN
        DROP TABLE instance_progress;
    END IF;
END $$;

-- ADR-0034 / observability M1.1: per-run abort intent on `runs`.  `status`
-- gains 'aborting' and 'aborted' (kept as VARCHAR, no CHECK — the state machine
-- lives in Python).  `stop_scope` ∈ {harness|eval|all}.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS stop_requested_at TIMESTAMPTZ;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS stop_scope        TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS stop_reason       TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS stopped_at        TIMESTAMPTZ;
-- ADR-0037 / M0 §2.2 — one row per model call.  call_index is assigned by the
-- shim, monotonic within one (run, instance, attempt); the composite PK makes
-- whole-batch SQS redelivery a no-op (ON CONFLICT DO NOTHING) and lets N writers
-- run concurrently.  Token/cost fields are all NULLABLE — NULL means "not
-- reported", never zero (Trap 3).  No request/response bodies by design (§2.4).
CREATE TABLE IF NOT EXISTS llm_calls (
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    instance_id             TEXT NOT NULL,
    attempt_number          INT  NOT NULL,
    call_index              INT  NOT NULL,
    harness                 TEXT NOT NULL,

    generation_id           TEXT,
    model_requested         TEXT,
    model_resolved          TEXT,
    provider_name           TEXT,
    -- 1.6 (review 2026-08-26): stored-free build identity.
    system_fingerprint      TEXT,
    service_tier            TEXT,

    started_at              TIMESTAMPTZ NOT NULL,
    latency_ms              INT,
    ttft_ms                 INT,
    -- STEP 3 (review 2026-08-26): the per-call latency breakdown.
    stream_ms               INT,
    shim_preflight_ms       INT,
    gateway_response_ms     NUMERIC,
    gateway_overhead_ms     NUMERIC,
    gateway_callback_ms     NUMERIC,

    path                    TEXT,
    stream                  BOOLEAN,
    max_tokens_requested    INT,
    max_tokens_injected     INT,
    pacer_charge_tok        INT,
    temperature             NUMERIC,
    n_messages              INT,
    has_tools               BOOLEAN,
    request_bytes           INT,

    http_status             INT NOT NULL,
    finish_reason           TEXT,
    stop_reason             TEXT,
    -- 1.6 + G-4 (review 2026-08-26): provider-native stop signal + the OpenAI
    -- Responses-API completion fields (codex).
    native_finish_reason    TEXT,
    responses_status        TEXT,
    responses_incomplete_reason TEXT,
    error_type              TEXT,
    error_code              TEXT,
    response_bytes          INT,

    rate_limit_scope        TEXT,
    retry_after_s           NUMERIC,
    ratelimit_remaining_requests INT,
    ratelimit_remaining_tokens   INT,

    input_tokens            INT,
    output_tokens           INT,
    cached_tokens           INT,
    cache_write_tokens      INT,
    reasoning_tokens        INT,

    cost_usd                    NUMERIC,
    upstream_inference_cost_usd NUMERIC,
    -- 1.6 (review 2026-08-26): the provider's prompt/completion cost split.
    upstream_inference_prompt_cost_usd     NUMERIC,
    upstream_inference_completions_cost_usd NUMERIC,
    cost_source                 TEXT,
    -- M0-3 (review): True when the call SUCCEEDED (status < 400) with a body but
    -- no usage could be parsed — distinguishes "the provider reported nothing"
    -- from "we could not find what it reported" (the two are otherwise
    -- byte-identical in the data).
    usage_parse_failed          BOOLEAN,

    PRIMARY KEY (run_id, instance_id, attempt_number, call_index)
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_run_harness ON llm_calls (run_id, harness);
CREATE INDEX IF NOT EXISTS idx_llm_calls_started_at  ON llm_calls (started_at);
CREATE INDEX IF NOT EXISTS idx_llm_calls_errors      ON llm_calls (run_id, http_status)
    WHERE http_status >= 400;

-- R5.2 (review M2): the live DB predates the CREATE above, so the new column
-- needs an idempotent migration here — IF NOT EXISTS makes it a no-op on fresh
-- databases that already carry it from the CREATE block.
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS stop_reason TEXT;

-- 2026-08-26 (review to-the-verified-run, STEP 1.6/STEP 3): the metering + latency
-- capture columns.  The deployed Aurora predates the CREATE block above, so the
-- idempotent ALTER is what actually evolves it — IF NOT EXISTS makes each a no-op
-- on fresh databases that already carry them.
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS system_fingerprint        TEXT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS service_tier              TEXT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS stream_ms                 INT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS shim_preflight_ms         INT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS gateway_response_ms       NUMERIC;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS gateway_overhead_ms       NUMERIC;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS gateway_callback_ms       NUMERIC;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS native_finish_reason      TEXT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS responses_status          TEXT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS responses_incomplete_reason TEXT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS upstream_inference_prompt_cost_usd      NUMERIC;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS upstream_inference_completions_cost_usd NUMERIC;

-- BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5/§2.6: the L1 pacer's per-call footprint.
-- paced_wait_ms / overload_retries were on the shim's record since ADR-0041 but were never
-- columns — the writer's allowlist dropped them, so the 10x100s starvation of run
-- 01788405363237319353 left no trace in Aurora. The full wall-clock decomposition:
--   wall ≡ shim_preflight_ms + paced_wait_ms + retry_upstream_ms + overload_backoff_ms
--          + latency_ms (final attempt only)
-- plus the pacer's own diagnostics (was this call ever denied, queue depth at admission,
-- the last axis that denied it). All NULLABLE; NULL = not measured / not retried.
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS paced_wait_ms       INT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS overload_retries    INT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS overload_backoff_ms INT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS retry_upstream_ms   INT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS pacer_was_queued    BOOLEAN;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS pacer_queue_len     INT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS pacer_deny_axis     TEXT;
-- F1 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): the output cap the SHIM put on the wire when
-- the harness sent none (min(OUTPUT_RESERVE 16 384, W - est_prompt)). NULL = the harness set
-- its own (see max_tokens_requested) or the call was not a completion. Together the two
-- columns tie a provider "maximum context length" 400 to whichever side chose the cap.
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS max_tokens_injected INT;
-- F3 (same doc): the WEIGHTED amount the shim drew from the pacer's arrival bucket for this
-- call — uncached tokens at full price + the expected cached prefix x cached_weight. Compare
-- with input_tokens/cached_tokens to see what the pacer thought a call cost the pool.
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS pacer_charge_tok    INT;
-- 2026-09-04 (deepseek x mini run 01788550040741118596): OpenRouter embeds an upstream provider
-- failure in a 200 (choice with a one-space message, native_finish_reason "error", an error
-- object); the shim now retries such non-streaming completions invisibly. Present only on a
-- retried call, like overload_retries; the exhausted case also carries
-- error_type = 'upstream_error_embedded' + the provider's error_type in error_code.
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS upstream_error_retries INT;

-- ADR-0037 / M0 §4 + §5 — phase timing on instance_results (1:1 with a row).
-- All NULLABLE; NULL = not measured (Trap 3), never an invented number.
-- M0 §4.4: task_observed_s (container StartedAt → worker exit) reconciles
-- against the SUM of the phases; task_billed_s (PullStartedAt → exit) against
-- Cost Explorer.  The difference is the provisioning overhead per instance.
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS queue_wait_s        FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS provision_s         FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS image_pull_s        FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS worker_boot_s       FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS repo_prep_s         FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS agent_s             FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS patch_extract_s     FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS artifact_upload_s   FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS task_observed_s     FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS task_billed_s       FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS repo_prep_cache_hit BOOLEAN;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS image_pull_cold     BOOLEAN;

ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS eval_queue_wait_s    FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS eval_patch_fetch_s   FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS eval_image_pull_s    FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS eval_test_s          FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS eval_log_upload_s    FLOAT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS eval_image_pull_cold BOOLEAN;

-- ADR-0038 — contamination + honesty columns on instance_results.
--   stripped_test_paths: gold-test-file hunks stripped from the graded patch
--     (the strongest signal, was computed then discarded — ADR-0038 §1).
--   grade_invalid: True when the gold tests could not be established — an
--     INVALID grade, never a verdict.
--   leaked_node_ids: absent-at-base_commit FAIL_TO_PASS node ids that appear in
--     the attempt's patch+trajectory (memorization evidence, ADR-0038 §2).
--   gold_patch_similarity: normalized difflib ratio (model patch vs gold patch),
--     a near-exact reproduction being a memorization signal (ADR-0038 §3).
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS stripped_test_paths   TEXT[];
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS grade_invalid         BOOLEAN;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS leaked_node_ids       TEXT[];
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS gold_patch_similarity  NUMERIC;

-- ADR-0037 / M0 §1.3 — per-instance token/cost on instance_results.
-- The shim figure is authoritative (sole meter); the adapter figure is the
-- stored cross-check, never added (M0 §0.6 Trap 2 / DoD #2).  Both are NULL on
-- a run where the meter did not report (Trap 3).
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS input_tokens            INT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS output_tokens           INT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS cost_usd                NUMERIC;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS adapter_input_tokens    INT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS adapter_output_tokens   INT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS adapter_cost_usd        NUMERIC;

-- METERING-COMPLETENESS (2026-08-28; builder 1 wrote the shim/Usage/results_writer,
-- builder 4 owns this init.sql — review this schema block) — complete the rollup:
-- the shim's cumulative Usage carried cached/cache_write/reasoning + a
-- usage_parse_failed count + the reconciled cost_source, but instance_results
-- only projected input/output/cost.  Additive; ALTER ... IF NOT EXISTS new columns
-- only, nothing existing changes meaning.  (builder 1's vertical, per the metering
-- handover §0.)
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS cached_tokens            INT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS cache_write_tokens       INT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS reasoning_tokens         INT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS cost_source              TEXT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS usage_parse_failed_calls INT;
-- BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5: per-instance rollup of the L1 pacer's
-- footprint (same shape as usage_parse_failed_calls — a per-call fact rolled up so "why did
-- this instance take so long / die" is answerable from its own row). harness-phase only;
-- NULL on eval rows. paced_wait_ms_total = Σ every admission wait incl. hold-cap timeouts;
-- paced_calls = calls that were denied at least once; pacer_timeouts = calls that hit the
-- hold cap (each one surfaced a 429 to the CLI); overload_retries_total = §8b provider-429
-- retries the shim absorbed.
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS paced_wait_ms_total      INT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS paced_calls              INT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS pacer_timeouts           INT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS overload_retries_total   INT;
-- PERSIST-TURNS-USED (2026-08-28): the shim's serviced-completion turn count — the
-- harness-neutral turn definition (a forwarded model completion; probes / model lists /
-- count_tokens / shim refusals excluded).  harness-phase only; NULL on eval rows.
-- NOTE: COUNT(*) of llm_calls >= turns_used (non-completions still get a call record) —
-- never use the two interchangeably in a report.  An in-loop retry of a transient failure
-- DOES count as a turn if the retried call was forwarded and serviced (documented; do not
-- change — it drives the turn cap, now one of the two enforced bounds along with cost).
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS turns_used              INT;

-- Compaction build (BUILD-SPEC §6, non-negotiable measurement) — per-instance
-- counters on instance_results so "did compaction change this result?" is
-- answerable (the counter is 0 for most instances; the point is the 0 is EVIDENCE,
-- not an absence).  NULL = no compaction pass ran (not-measured, Trap 3), never a
-- fabricated 0.
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS compactions_fired            INT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS compaction_tokens_before     INT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS compaction_tokens_after      INT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS context_window_tokens        INT;

-- ADR-0038 §2 / M0-7 (N-1 review): whether THIS instance was leak-DETECTABLE at
-- all — has at least one FAIL_TO_PASS node id absent at base_commit.  Separates
-- "detectable and clean" from "never detectable": a run where no instance is
-- leak-detectable cannot claim to be clean.  NULL when the static artifact has
-- no entry (unknown), NOT a fabricated False.
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS leak_detectable BOOLEAN;

-- offline-analysis-design.md §2.3 / §9 DoD #3 (2026-09-01): leak_scan_at distinguishes
-- "never scanned" (NULL) from "scanned" — without it, leaked_node_ids=NULL is ambiguous
-- between "never scanned" and "scanned, absent from the map (UNKNOWN, P-3)", which
-- silently corrupts the Pass A vs Pass B confusion matrix (§3.9/§10.6): a chunk of
-- "unscanned" rows would read as "scanned, clean" negatives.  Set on EVERY row Pass A
-- examines, including the UNKNOWN-omit case — omitting is a completed decision, not an
-- absence of one.  leak_map_version records which committed map produced the scan
-- (leak_detection.leak_map_version(), a short sha256 of the artifact file) so a scan
-- against a stale map is visible rather than indistinguishable from a current one.
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS leak_scan_at     TIMESTAMPTZ;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS leak_map_version TEXT;

-- ---------------------------------------------------------------------------
-- Pass B — LLM-as-judge (offline-analysis-design.md §4, extended §9/§10,
-- 2026-09-01).  Own tables: judge spend and judge findings must never be
-- confused with harness/eval rows or with llm_calls' run-cost meaning (§3.7 —
-- the trap this whole split exists to avoid).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS judge_results (
    run_id              TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    instance_id         TEXT NOT NULL,
    attempt_number      INT  NOT NULL,
    judged_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Provenance: which judge, which rubric, which prompt (M0 §6's lesson,
    -- reapplied — a NULL judge_model_resolved here is the same failure family).
    judge_model_requested TEXT,
    judge_model_resolved  TEXT,
    judge_provider         TEXT,
    judge_generation_id    TEXT,
    rubric_version         TEXT NOT NULL,
    rubric_sha256          TEXT NOT NULL,
    judge_prompt_version   TEXT NOT NULL,
    judge_context_mode     TEXT,          -- 'absent_ids' | 'none' (§3.5)
    temperature             NUMERIC,

    -- §9.4 / §10.4: which pruning mode was CONFIGURED for this pass, distinct
    -- from tool_output_pruned below (whether pruning actually fired on THIS row).
    judge_prune_mode        TEXT,          -- 'full' | 'pruned' | 'auto'

    -- Honesty flags.  A finding from a truncated/pruned/elided input is scoped
    -- to what it can affect, never silently reported as a clean negative (§3.4).
    input_tokens            INT,
    output_tokens           INT,
    input_truncated         BOOLEAN NOT NULL DEFAULT FALSE,   -- step 3 fired
    tool_output_pruned      BOOLEAN NOT NULL DEFAULT FALSE,   -- step 2 fired
    events_elided           INT,
    judge_parse_failed      BOOLEAN NOT NULL DEFAULT FALSE,
    raw_response_s3_key     TEXT,          -- ALWAYS stored — the only way to re-score

    -- The scores.  JSONB because the rubric is configurable — a column per
    -- dimension would need a migration every time the rubric changes.  Each
    -- dimension carries score/flag + reasoning (§10.3, the "why", not only a
    -- quoted span) + evidence (turn/quote); see offline-analysis-design.md §3.6.
    scores                  JSONB NOT NULL DEFAULT '{}',
    summary                  TEXT,

    -- Judge spend.  NOT in llm_calls.  No run-cost query reads this column.
    judge_cost_usd           NUMERIC,

    PRIMARY KEY (run_id, instance_id, attempt_number, judged_at)
);

-- 2026-09-09 query audit: idx_judge_run duplicated the PK's leading column. The readers
-- (fetch_latest_results / latest_judgment_by_key, polled every 15 s by the UI) are
-- DISTINCT ON (instance_id, attempt_number) ... ORDER BY instance_id, attempt_number, judged_at DESC
-- — the PK's ASC judged_at forced a sort of every judge row of the run; this index is that order.
DROP INDEX IF EXISTS idx_judge_run;
CREATE INDEX IF NOT EXISTS idx_judge_latest ON judge_results (run_id, instance_id, attempt_number, judged_at DESC);
CREATE INDEX IF NOT EXISTS idx_judge_rubric ON judge_results (rubric_version);

-- ADR-0042 (2026-09-02): the empty-judgment recovery cascade. judge_method
-- records which step produced the scores ('primary' | 'retry' | 'transcribe' |
-- 'plain' | 'failed'); judge_attempts is how many judge-model calls this
-- candidate cost. Together they make "how often did the judge need recovery,
-- and at what cost" ordinary SQL — the number a publication claim about
-- grading reliability rests on. Additive, idempotent, same pattern as the
-- judge_sampling ALTERs below.
ALTER TABLE judge_results ADD COLUMN IF NOT EXISTS judge_method   TEXT;
ALTER TABLE judge_results ADD COLUMN IF NOT EXISTS judge_attempts INT;
-- rubric v3 (2026-09-09): the computed efficiency profile the judge was shown
-- (analysis/efficiency.py) — stored so the number the judge reasoned against is the
-- number the UI and the export show. NULL on rows judged before v3 and on timeout rows.
ALTER TABLE judge_results ADD COLUMN IF NOT EXISTS efficiency_profile JSONB;

-- The realised sample, so a published number is reconstructable (§3.2), and
-- (§10.2, 2026-09-01) the pass's minted-key identifiers, so judge spend is
-- auditable per pass the same way run spend is auditable per run.  Never the
-- raw key (rule 3) — litellm_key_id is LiteLLM's key id, openrouter_key_hash
-- is OpenRouter's own opaque hash, mirroring runs.litellm_key_id/openrouter_key_hash.
CREATE TABLE IF NOT EXISTS judge_sampling (
    run_id         TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    pass_id        TEXT NOT NULL,          -- ULID per judging pass
    requested_rate  NUMERIC,
    seed            BIGINT,
    strata_json     JSONB NOT NULL,         -- {(harness,outcome): {eligible, sampled}}
    total_judged    INT,
    total_skipped_over_budget INT,
    litellm_key_id       TEXT,
    openrouter_key_hash  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, pass_id)
);

-- BUILDER3-JUDGE-VERIFIED-AND-UI-BRIEF-2026-09-02.md §3.2: judge_sampling was written and
-- never read — an operator asking for 2,500 judgments and getting 1,900 rows back had no way
-- to tell "1,900 were selected" apart from "600 were dropped when the money ran out". These two
-- columns, plus GET /runs/{run_id}/judge/passes reading them, make a budget-truncated pass
-- visible instead of indistinguishable from a complete one.
ALTER TABLE judge_sampling ADD COLUMN IF NOT EXISTS total_eligible     INT;
ALTER TABLE judge_sampling ADD COLUMN IF NOT EXISTS total_parse_failed INT;

-- 2026-09-08 (owner, after the first 500-attempt pass): the pass-level synthesis — one
-- judge-model call at the end of run_pass over a digest of EVERY recorded judgment for the
-- run (judge.build_pass_digest), so the operator gets "two instances showed contamination
-- because …, a recurring environment problem was …" without reading 500 Judge panels.
-- NULL synthesis + synthesis_error = skipped or failed; the pass is still complete.
ALTER TABLE judge_sampling ADD COLUMN IF NOT EXISTS synthesis                TEXT;
ALTER TABLE judge_sampling ADD COLUMN IF NOT EXISTS synthesis_cost_usd       NUMERIC;
ALTER TABLE judge_sampling ADD COLUMN IF NOT EXISTS synthesis_model_resolved TEXT;
ALTER TABLE judge_sampling ADD COLUMN IF NOT EXISTS synthesis_error          TEXT;
ALTER TABLE judge_sampling ADD COLUMN IF NOT EXISTS synthesis_prompt_version TEXT;
-- "regenerate report": a pass that judged nothing by design (synthesis over recorded rows only).
ALTER TABLE judge_sampling ADD COLUMN IF NOT EXISTS synthesis_only BOOLEAN NOT NULL DEFAULT FALSE;

-- offline-analysis-design.md §10.7 (2026-09-01): judge_results.scores (JSONB) is the raw,
-- rubric-version-safe capture; this is the normalized PROJECTION of it, written in the same
-- transaction, so per-harness/per-dimension reporting is ordinary SQL instead of a ->>'x'
-- extraction on every query.  Same raw-blob-plus-projected-columns split instance_results.
-- report_json already uses.  Not every scale_type uses every column (§10.7's own note) — a NULL
-- in an unused column means "not applicable to this scale_type", never "missing".
CREATE TABLE IF NOT EXISTS judge_dimension_scores (
    run_id           TEXT NOT NULL,
    instance_id      TEXT NOT NULL,
    attempt_number   INT  NOT NULL,
    judged_at        TIMESTAMPTZ NOT NULL,
    dimension_id     TEXT NOT NULL,     -- 'contamination', 'hallucination', 'tool_efficiency', ...
    scale_type       TEXT NOT NULL,     -- 'likert' | 'count_and_severity' | 'ratio' | 'boolean_with_span'

    score_numeric     NUMERIC,          -- likert score / count_and_severity's count / ratio's redundant
    score_secondary   NUMERIC,          -- count_and_severity's severity / ratio's total
    flag              BOOLEAN,          -- boolean_with_span's present
    span_start_turn   INT,
    span_end_turn     INT,

    reasoning         TEXT,             -- §10.3's per-dimension "why"
    evidence          JSONB,            -- [{turn, quote}, ...]
    evidence_missing  BOOLEAN NOT NULL DEFAULT FALSE,   -- §3.3 require_evidence, unmet

    PRIMARY KEY (run_id, instance_id, attempt_number, judged_at, dimension_id),
    FOREIGN KEY (run_id, instance_id, attempt_number, judged_at)
        REFERENCES judge_results (run_id, instance_id, attempt_number, judged_at) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_jds_run_dim ON judge_dimension_scores (run_id, dimension_id);
-- rubric v3 (2026-09-09): the `causes` scale — [{cause, share, recommendation}] per
-- token_efficiency row (score_numeric = avoidable share, score_secondary = severity).
ALTER TABLE judge_dimension_scores ADD COLUMN IF NOT EXISTS causes JSONB;
CREATE INDEX IF NOT EXISTS idx_jds_dim     ON judge_dimension_scores (dimension_id);

-- offline-analysis-design.md §10.2 point 4 (2026-09-01): a judge pass is not a run — it has no
-- runs.active_key row to piggyback a mutex on — but judge-model is still a single db-model with
-- exactly one current OpenRouter key, so two concurrent passes rotating it would race the same
-- way two concurrent harness runs sharing a model_alias would (rotatable_models.py's own
-- documented reason per-harness aliases exist). This table's PRIMARY KEY IS the mutex: the
-- INSERT either succeeds (this pass holds it) or raises UniqueViolation (another pass holds it)
-- — a real constraint, not a check-then-act race, same principle as uq_runs_active_key.
CREATE TABLE IF NOT EXISTS judge_pass_lock (
    model_alias TEXT PRIMARY KEY,
    pass_id     TEXT NOT NULL,
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- offline-analysis-design.md §11 (2026-09-02): calibration via in-app review, not offline
-- hand-labeling — supersedes §3.9 steps 1-2. One row per reviewer decision on one dimension of
-- one judge_results row; append-only (a re-review is a new row, never an overwrite), same
-- convention judge_results itself uses. reviewer_reasoning is NOT NULL on both approve and deny —
-- a bare thumbs-up costs nothing, a written reason costs a little, which is the point (§11's
-- honesty-tradeoff note: this is reviewer-endorsement rate, not blind inter-rater agreement).
CREATE TABLE IF NOT EXISTS judge_calibration_reviews (
    run_id                     TEXT NOT NULL,
    instance_id                TEXT NOT NULL,
    attempt_number             INT  NOT NULL,
    judged_at                  TIMESTAMPTZ NOT NULL,   -- pins the exact judge_results row reviewed
    dimension_id               TEXT NOT NULL,
    reviewed_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by                TEXT NOT NULL DEFAULT 'operator',
    decision                   TEXT NOT NULL CHECK (decision IN ('approve', 'deny')),
    reviewer_reasoning         TEXT NOT NULL,
    corrected_score_numeric    NUMERIC,   -- reviewer's own value; NULL unless supplied (deny only)
    corrected_score_secondary  NUMERIC,
    corrected_flag             BOOLEAN,
    PRIMARY KEY (run_id, instance_id, attempt_number, judged_at, dimension_id, reviewed_at),
    FOREIGN KEY (run_id, instance_id, attempt_number, judged_at, dimension_id)
        REFERENCES judge_dimension_scores (run_id, instance_id, attempt_number, judged_at, dimension_id)
        ON DELETE CASCADE
);
-- GET /judge/calibration is deliberately global (not run-scoped, §11) — this index is its access path.
CREATE INDEX IF NOT EXISTS idx_jcr_dimension ON judge_calibration_reviews (dimension_id);

-- ---------------------------------------------------------------------------
-- builder4 run-launch (BUILDER4-RUN-LAUNCH-ORCHESTRATOR-2026-08-26 §9) — the
-- claim mutex, per-run key identity, and the seed/dispatch ledger columns.
-- The CREATE TABLE above and this ALTER block both carry every column: a
-- column added only to CREATE passes on a fresh database and silently does
-- nothing on the already-deployed one.
-- ---------------------------------------------------------------------------
ALTER TABLE runs ADD COLUMN IF NOT EXISTS active_key            TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS litellm_key_id        TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS openrouter_key_hash   TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS dispatched_at         TIMESTAMPTZ;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS finalised_at          TIMESTAMPTZ;
-- The mutex (§4 "Why CLAIM is first"): a second run for the same (harness,
-- model_alias) pair cannot pass this insert while the first is active — the
-- database is the only thing here that can be atomic, and the insert is the
-- cheapest thing to make atomic.  active_key is set NULL at finalisation
-- (§8 release / abort), so a run's pair frees up for reuse once it is done.
CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_active_key
  ON runs (active_key) WHERE active_key IS NOT NULL;

-- §6.2 / §6.3: seed rows are written directly by the orchestrator inside the
-- claim transaction (dispatched_at/dispatch_count are then owned by the
-- harness dispatcher's DISPATCHED emit — column ownership, never a shared
-- last-writer-wins field).
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS seeded_at      TIMESTAMPTZ;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS dispatched_at  TIMESTAMPTZ;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS dispatch_count INT DEFAULT 0;

-- §6.3: the monotonic state guard.  SQS Standard is unordered and
-- at-least-once, so a late DISPATCHED can arrive after PATCH_READY; the old
-- upsert (`state = EXCLUDED.state` unconditionally) let that silently
-- regress a finished instance.  state_rank() orders the ladder
-- PENDING(0) < DISPATCHED(1) < {HARNESS,EVAL}_RUNNING(2) < ABANDONED(3) <
-- everything else (real terminal, 4).  ABANDONED ranks BELOW every real
-- terminal on purpose: a premature reap is then self-correcting — a
-- straggler result landing later still wins (§7 "the reaper's gate").
--
-- abort-ledger-reconciliation-2026-08-28 (decided with the owner): abort
-- STANDS.  ABORTED_IN_FLIGHT and NEVER_DISPATCHED are named here at rank 4,
-- not left to ELSE — this is a same-value, no-behavior-change edit (they
-- already fell into ELSE->4), made explicit because leaving it accidental is
-- how ABANDONED's deliberate rank-3 self-correcting property gets confused
-- with these two, which are NOT self-correcting on purpose: a run an
-- operator killed must not silently re-acquire a late result (ADR-0034 §5 —
-- an aborted run does not resume).  The alternative (rank them below
-- terminal, like ABANDONED) was rejected: it would require moving every
-- genuine terminal state off the ELSE default to leave room, defaults an
-- unrecognized future state to outranking a deliberate abort, and reopens a
-- concrete gap — a late PATCH_READY beating an abort record would still pass
-- results_writer.py's eval-enqueue guard (no runs.status check there),
-- spawning an eval job for a run that must not resume.
CREATE OR REPLACE FUNCTION state_rank(s TEXT) RETURNS INT AS $$
  SELECT CASE s
    WHEN 'PENDING' THEN 0
    WHEN 'DISPATCHED' THEN 1
    WHEN 'HARNESS_RUNNING' THEN 2
    WHEN 'EVAL_RUNNING' THEN 2
    WHEN 'ABANDONED' THEN 3
    WHEN 'ABORTED_IN_FLIGHT' THEN 4
    WHEN 'NEVER_DISPATCHED' THEN 4
    ELSE 4
  END;
$$ LANGUAGE SQL IMMUTABLE;

-- 2026-08-29 (dev/BUILDER4-NATIVE-TRAJECTORY-S3-KEY-NEVER-PERSISTED-2026-08-29.md):
-- B6/E11a's native_trajectory_s3_key has been genuinely uploaded to S3 by the
-- harness worker (harness_worker.py, mini's pre-normalisation trajectory)
-- since that feature landed, but never had a column here at all — not a
-- _parse_result gap like the six metering fields, a structural one. Added to
-- _RESULT_EXTRA_COLUMNS (results_writer.py) rather than hardcoded alongside
-- patch_path/trajectory_path/raw_log_path, so the INSERT's column list,
-- VALUES placeholders, and ON CONFLICT SET all stay name-driven — one place
-- to add a field, not four.
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS native_trajectory_s3_key TEXT;

-- 2026-08-29 (dev/BUILDER4-EXPOSE-MISSING-ARTIFACTS-2026-08-28.md): SWE-bench's
-- own grade logs (test_output.txt, run_instance.log) have been uploaded next
-- to eval_report.json since the 5b review (eval_worker._upload_run_logs) —
-- the dashboard's artifact surface never had a column to resolve them
-- against, so a failed grade stayed silent in the UI despite the evidence
-- already sitting in S3. Same _RESULT_EXTRA_COLUMNS treatment as
-- native_trajectory_s3_key above, for the same reason.
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS test_output_s3_key TEXT;
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS run_log_s3_key     TEXT;

-- 2026-08-29 (dev/BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md M3): manual,
-- operator-reviewed restart of failed instances (attempt N+1, dispatched
-- through the normal path) collides with already-configured pass@k
-- (attempts_per_instance > 1) on the SAME attempt_number sequence — nothing on
-- a bare row tells the two apart after the fact. NULL = original dispatch
-- (config.attempts_per_instance, not a restart); 'operator_infra_retry' = the
-- prior attempt for this instance was an infra-shaped failure, collapses with
-- it into ONE in the honest resolve-rate denominator; 'operator_rerun_pass_at_k'
-- = the prior attempt was already terminal (RESOLVED/UNRESOLVED/...), this is
-- a deliberate extra attempt, counts as an ADDITIONAL one. No backfill:
-- every existing row predates restart and is correctly NULL.
ALTER TABLE instance_results ADD COLUMN IF NOT EXISTS retry_reason TEXT;

-- 2026-08-31 (dev/BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §2/§6):
-- gateway pause via LiteLLM /key/block against runs.litellm_key_id. Two
-- independent actors (a global sweep and a per-run operator action) can both
-- want to block/unblock the SAME run's key, so precedence needs a value, not
-- just a boolean: NULL = key not blocked; 'global' = blocked by the sweep
-- (a global resume may un-block it); 'operator' = blocked by an explicit
-- per-run action (survives a global resume — most-specific-wins, see the
-- design doc's §2 precedence table). Purely orchestrator-side bookkeeping —
-- the shim never reads this column, only the block/unblock EFFECT (the 401
-- marker) on a call.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS gateway_key_blocked_by TEXT;

-- 2026-08-31 (dev/BUILDER4-AUTOSCALER-TPM-CEILING-DISCOVERY-DESIGN-2026-08-31.md §3, reviewer F3):
-- per-model tpm ceiling evidence as an append-only observation log, never a mutable row. A single
-- mutable "current ceiling" value is exactly the failure mode this exists to avoid — a bad run's
-- report could silently overwrite good prior evidence. So the log is the source of truth; the
-- current value below is a VIEW over it (recency-wins, an overload row is evidence but never itself
-- a ceiling to start from), not a table anything writes to directly. event_type:
-- 'discovery_initial' = the Part-1 ceiling-discovery ECS task; 'manual' = an operator-entered
-- value; 'reconciliation_peak' / 'overload' / 'recovery_stabilized' = Part 2's live control law
-- (not yet built). run_id is NULL for discovery/manual rows (they aren't tied to a run).
CREATE TABLE IF NOT EXISTS model_tpm_observations (
    id             BIGSERIAL PRIMARY KEY,
    model_alias    TEXT NOT NULL,
    run_id         TEXT,
    event_type     TEXT NOT NULL,
    tpm_value      BIGINT NOT NULL,
    at_concurrency INT,
    task_id        TEXT,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS model_tpm_observations_alias_ts
    ON model_tpm_observations (model_alias, ts DESC);

-- 2026-09-01 (found by builder 3 re-running this file against an already-migrated DB,
-- offline-analysis-design.md work): CREATE OR REPLACE VIEW cannot narrow an existing view's
-- column list, and the block below (§ "the constraint is MULTI-VALUED") replaces this 6-column
-- shape with an 8-column one 20 lines down. On a FRESH database this statement runs first and
-- is fine; on any database that has already been migrated once (i.e. every real boot after the
-- first), Postgres refuses with "cannot drop columns from view" and run_migrations() rolls back
-- its ENTIRE transaction — every statement in this file, not just this one. DROP VIEW IF EXISTS
-- first, matching the idempotency fix already used 20 lines below, so re-running this file is
-- safe regardless of which shape the live view currently has.
DROP VIEW IF EXISTS model_ceilings;
CREATE VIEW model_ceilings AS
SELECT DISTINCT ON (model_alias)
    model_alias,
    tpm_value        AS discovered_tpm,
    event_type       AS ceiling_source,
    ts               AS discovered_at,
    at_concurrency   AS concurrency_at_discovery,
    task_id
FROM model_tpm_observations
WHERE event_type <> 'overload'
ORDER BY model_alias, ts DESC;

-- 2026-09-01 (BUILDER4-HARNESS-AUTOSCALER-EXACT-DESIGN-2026-09-01.md §6): the live experiments
-- (E1-E16 in that doc) showed the constraint is MULTI-VALUED — a simultaneous-arrival token
-- budget, a sustained call-start rate, an in-flight cap, per provider — not one tpm number. So
-- observations gain a value_kind and a provider, and the ceilings view becomes per
-- (model_alias, value_kind), still recency-wins and still never surfacing an overload row as a
-- value to start from. The old single-value model_ceilings view above is REPLACED by this one;
-- readers select the value_kind they need.
ALTER TABLE model_tpm_observations ADD COLUMN IF NOT EXISTS value_kind TEXT NOT NULL DEFAULT 'tpm';
ALTER TABLE model_tpm_observations ADD COLUMN IF NOT EXISTS provider   TEXT;

DROP VIEW IF EXISTS model_ceilings;
CREATE VIEW model_ceilings AS
SELECT DISTINCT ON (model_alias, value_kind)
    model_alias,
    value_kind,
    tpm_value        AS value,
    event_type       AS ceiling_source,
    ts               AS discovered_at,
    at_concurrency   AS concurrency_at_discovery,
    provider,
    task_id
FROM model_tpm_observations
WHERE event_type <> 'overload'
ORDER BY model_alias, value_kind, ts DESC;

-- 2026-09-04 (owner decision, builder 4): the pacer/planner run on the DERIVED seeds discovery
-- writes to Valkey (pacer:cfg:{pool} — c_burst, r_tok, k_inflight, c_req, r_qps, their _seed
-- copies, latency_s_max_context, cached_weight, seeded_at), and Valkey lives in the eval tier:
-- every eval destroy wiped them and forced a ~$20 re-probe per pool at the next bring-up. The
-- observation rows above are the MEASUREMENTS; the seeds are the margined/floored constants
-- derived from them, and that derivation has changed three times in a week — so the seeds are
-- persisted verbatim (one JSONB row per probe) rather than re-derived. control_plane/
-- pacer_seeds.py rehydrates pacer:cfg:{pool} from the latest row when the hash is empty (at
-- run-supervisor start and at run launch), keeping the ORIGINAL seeded_at so the planner's
-- staleness policy (PACER_CFG_MAX_AGE_S) still applies to a day-old probe. Discovery seeds only —
-- the planner's live growth values are ephemeral by design.
CREATE TABLE IF NOT EXISTS pacer_cfg_seeds (
    id           BIGSERIAL PRIMARY KEY,
    model_alias  TEXT NOT NULL,             -- the POOL alias (pacer:cfg:{model_alias})
    seeds        JSONB NOT NULL,            -- exactly what discovery HSETs, minus seeded_at
    seeded_at    DOUBLE PRECISION NOT NULL, -- epoch seconds; the Redis seeded_at stamp
    provider     TEXT,
    triggered_by TEXT,
    task_id      TEXT,
    report       JSONB,                     -- the probe's full report, for the record
    ts           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pacer_cfg_seeds_alias_seeded_at
    ON pacer_cfg_seeds (model_alias, seeded_at DESC);

-- Operator-adjustable limits (owner request 2026-09-04; control_plane/operator_limits.py).
-- Append-only audit of every edit made from the UI / API: the run-overrides hash the
-- dispatcher re-reads (per-run cap, ceiling override, growth step, cooldown, planner gate),
-- the global operator:limits hash (borrowed-curve cap, growth clamp, utilisation, eval max
-- workers, eval scale-in delay) and pacer:cfg fields per alias. For the global scope this
-- table is also the truth: rehydrate_global restores the latest value per field into Valkey
-- at run-supervisor start (the eval tier's Valkey is wiped by every destroy). NULL new_value
-- = the field was cleared back to its default.
CREATE TABLE IF NOT EXISTS operator_limit_edits (
    id         BIGSERIAL PRIMARY KEY,
    scope      TEXT NOT NULL,              -- run | global | pacer
    target     TEXT NOT NULL DEFAULT '',   -- run_id | '' | pacer alias / pool
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    actor      TEXT NOT NULL DEFAULT 'operator',
    reason     TEXT NOT NULL DEFAULT '',
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS operator_limit_edits_scope_field_id
    ON operator_limit_edits (scope, field, id DESC);

-- Live-run timeline capture for the publication site (2026-09-04;
-- dev/LIVE-RUN-TIMELINE-SITE-DATA-CONTRACT-AND-BUILD-PLAN-2026-09-04.md §4;
-- control_plane/run_timeline.py). One row per ACTIVE run per capacity-observer tick (30 s):
-- the per-run aggregates that otherwise vanish with the 300 s Redis progress-key TTL —
-- in-flight count, live token/cost sums, per-phase state counts, the pacer ledger per alias,
-- and the control flags at the tick. The planner side is already durable in
-- capacity_snapshot (same tick, same thread). Every measured column is nullable: NULL is
-- "not measured at this tick", never 0. Read back by GET /runs/{run_id}/timeline.
CREATE TABLE IF NOT EXISTS run_timeline_tick (
    run_id           TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_status       TEXT,
    in_flight        INT,        -- live (non-stale) progress keys for this run
    stale            INT,        -- progress keys older than the staleness window
    pending          INT,        -- harness rows PENDING + DISPATCHED
    harness_running  INT,
    eval_running     INT,
    resolved         INT,
    unresolved       INT,
    aborted          INT,        -- NEVER_DISPATCHED + ABORTED_IN_FLIGHT
    expected         INT,        -- the dispatch plan (run_summary)
    denominator      INT,        -- gradeable (run_summary)
    tok_in           BIGINT,     -- landed rows + live keys
    tok_out          BIGINT,
    tok_cached       BIGINT,
    tok_reasoning    BIGINT,
    cost_usd_live    DOUBLE PRECISION,  -- landed + live keys' cost so far
    cost_usd_landed  DOUBLE PRECISION,  -- instance_results only
    harness_paused   BOOLEAN,
    eval_paused      BOOLEAN,
    gateway_paused   BOOLEAN,
    control_stale    BOOLEAN,    -- the control read was fail-closed (flags unreliable)
    counts           JSONB,      -- [{phase, state, count}] verbatim
    pacer            JSONB,      -- {alias: {r_tok, bucket_fill, inflight_calls, queue_len, ...}}
    PRIMARY KEY (run_id, ts)
);

-- Run-scoped or global operator moments that have no other record (pause / resume have
-- only a current-state row in control_state). run_id NULL = a global event; the timeline
-- read folds global events into every run open at the time. Launch / dispatch / abort /
-- finalise already live on runs.*_at; limit edits on operator_limit_edits; discovery steps
-- on model_tpm_observations.
CREATE TABLE IF NOT EXISTS run_events (
    id      BIGSERIAL PRIMARY KEY,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id  TEXT,
    kind    TEXT NOT NULL,       -- pause | resume | ...
    actor   TEXT NOT NULL DEFAULT 'operator',
    reason  TEXT NOT NULL DEFAULT '',
    detail  JSONB NOT NULL DEFAULT '{}'
);
-- 2026-09-09 query audit: the timeline window is WHERE (run_id = %s OR run_id IS NULL) AND ts BETWEEN;
-- carrying run_id makes it index-only.
DROP INDEX IF EXISTS run_events_ts;
CREATE INDEX IF NOT EXISTS run_events_ts_run ON run_events (ts, run_id);


-- ============================================================================================
-- 2026-09-09 query audit (BUILDER4, after the live-view fix): indexes for the hot paths that had
-- none. Every per-instance / per-call query already seeks on a PK prefix; these cover the run-list,
-- the status-scoped control/reaper/timeline ticks, the DISTINCT-ON readers whose trailing key is
-- DESC, and the append-only observation log behind the model_ceilings view. Plain CREATE INDEX
-- (this file runs inside one migration transaction, so CONCURRENTLY is not available); the
-- tables are small enough that each build is sub-second.
-- ============================================================================================
-- runs had no index but its PK: /runs (every 20 s, ORDER BY created_at DESC), and five
-- status-scoped reads every 30 s (reaper, timeline sampler, control aborted-set, gateway pause).
CREATE INDEX IF NOT EXISTS runs_created_at        ON runs (created_at DESC);
CREATE INDEX IF NOT EXISTS runs_status_created_at ON runs (status, created_at DESC);
CREATE INDEX IF NOT EXISTS runs_gateway_blocked   ON runs (gateway_key_blocked_by)
    WHERE gateway_key_blocked_by IS NOT NULL;
-- GET /judge/calibration is global and unbounded (§11); its DISTINCT ON ends in reviewed_at DESC.
CREATE INDEX IF NOT EXISTS idx_jcr_latest ON judge_calibration_reviews
    (run_id, instance_id, attempt_number, judged_at, dimension_id, reviewed_at DESC);
-- /runs/{id}/timeline windows operator_limit_edits by ts with an OR on scope — needs ts.
CREATE INDEX IF NOT EXISTS operator_limit_edits_ts ON operator_limit_edits (ts);
-- The model_ceilings view is DISTINCT ON (model_alias, value_kind) ... ORDER BY ts DESC; the
-- existing (model_alias, ts DESC) index cannot provide that order. Polled every 30 s.
CREATE INDEX IF NOT EXISTS model_tpm_observations_alias_kind_ts
    ON model_tpm_observations (model_alias, value_kind, ts DESC);
-- results_writer's seven FILTERed counts per result message + is_ready_to_close every 30 s:
-- (run_id, phase, state) lets them run index-only instead of touching ~1,000 heap rows each.
CREATE INDEX IF NOT EXISTS instance_results_run_phase_state ON instance_results (run_id, phase, state);
