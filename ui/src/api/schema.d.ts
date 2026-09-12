// GENERATED FILE - do not edit by hand.
// npm run gen:api regenerates this from src/api/openapi.json (ADR-0009);
// npm run gen:api -- --live refreshes the spec from the live API first.

export interface paths {
    "/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Health */
        get: operations["health_health_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/control": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Control
         * @description The live control view: pause flags, published_at, staleness, aborted runs.
         *
         *     ``stale`` is fail-closed: a missing/expired Valkey key or a dead publisher
         *     reads as *all pools paused* (control/state.py), and the UI must render that
         *     as "cannot confirm", never as a confident PAUSED.
         */
        get: operations["get_control_control_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/control/pause": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Pause
         * @description Pause the given pools (default harness-only — the money pool, M1 §1.10).
         *
         *     BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §5: pausing
         *     "gateway" additionally sweeps every active run's LiteLLM key blocked —
         *     the flag alone (below) is a UI-visible fact, this is what actually stops
         *     spend. Runs already individually paused by an operator action are left
         *     alone (see gateway_pause.sweep_global_pause's docstring — §2's table).
         */
        post: operations["pause_control_pause_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/control/resume": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Resume
         * @description Resume the given pools.
         *
         *     Resuming "gateway" unblocks every run the global sweep blocked — never a
         *     run an operator individually paused (§2's table: that survives a global
         *     resume, it needs its own per-run resume).
         */
        post: operations["resume_control_resume_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/abort": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Abort Run
         * @description Abort a run (scope {harness, eval, all}; default harness).
         *
         *     The report is a *draining* description, not a completion: ``settled`` only
         *     becomes True once the drain finalises (the UI polls until the run is
         *     terminal rather than flipping to "aborted" on this 200, phase 1 §4a-b).
         */
        post: operations["abort_run_runs__run_id__abort_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/close": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Close Run Route
         * @description Deliberately finalise a run — stop stragglers, revoke both keys,
         *     release the (harness, model_alias) pair. 409 if the run isn't 'running'
         *     or still has instances in flight (§1 M1: re-checked under a row lock, not
         *     just before this call started).
         */
        post: operations["close_run_route_runs__run_id__close_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/restart": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Restart Instances Route
         * @description Restart specific failed instances as attempt N+1, operator-selected —
         *     never automatic (§2.2). Allowed any time the run isn't closed, whether
         *     mid-run or fully idle awaiting review (§2.2/§3: there is no separate
         *     'ready to review' state to gate on).
         */
        post: operations["restart_instances_route_runs__run_id__restart_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/regrade": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Regrade Instances Route
         * @description Re-grade the EXISTING patch of each selected instance as an eval-only
         *     attempt N+1 (2026-09-01: the eval-side counterpart /restart lacked). No
         *     model spend — the same captured diff goes back through grading; the
         *     remedy after EVAL_OOM_KILLED / ABANDONED / dead-lettered eval outcomes,
         *     a host resize, or a mem_limit change. Instances with no captured patch
         *     are skipped with a reason pointing at /restart.
         */
        post: operations["regrade_instances_route_runs__run_id__regrade_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/images/validate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Validate Images Route
         * @description Gold-grade the selected instances' -inst images (image-parity Part B).
         *     Same body shape as /regrade (``instance_ids``, optional ``actor``); the
         *     grades run as eval attempts of the synthetic run ``image-validation``.
         */
        post: operations["validate_images_route_images_validate_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/llm-live": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Llm Live List Route
         * @description Newest-first LLM calls of a run, straight from LiteLLM's spend-log table
         *     (rows land live, batched at 5 — seconds behind the call). ``attempt``
         *     narrows ``instance_id`` to one attempt (the instance page is per attempt).
         *     503 when the spend DB is not configured/reachable: the view is a
         *     convenience atop the run, never a dependency of it.
         */
        get: operations["llm_live_list_route_runs__run_id__llm_live_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/llm-live/{request_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Llm Live Detail Route
         * @description One call's conversation + response, from proxy_server_request (the
         *     messages COLUMN is always empty in this LiteLLM version). Run-scoped: a
         *     request_id belonging to another run 404s, never leaks.
         */
        get: operations["llm_live_detail_route_runs__run_id__llm_live__request_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/judge/candidates": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Judge Candidates Route
         * @description §10.1: the launch UI's instance selector — every harness-phase attempt
         *     with its eval status (state/verdict/grade_invalid derived into `outcome`)
         *     and whether a judge_results row already exists, so a re-judge is visibly
         *     a re-judge. Mirrors LaunchScreen.tsx's data shape, plus the eval-status
         *     column that screen doesn't need.
         */
        get: operations["judge_candidates_route_runs__run_id__judge_candidates_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/judge/estimate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Judge Estimate Route
         * @description Cost preview BEFORE spend (§10.5's estimate/confirm pattern, same
         *     shape as GET /model-ceilings/{alias}/estimate). Mirrors the launch's
         *     resume rule: already-judged candidates are excluded unless ``rejudge``.
         */
        get: operations["judge_estimate_route_runs__run_id__judge_estimate_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/judge": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Judge Launch Route
         * @description Launches one judge pass — RunTask when JUDGE_TASK_FAMILY is configured
         *     (deployed path); the in-process background task remains ONLY the
         *     local-dev fallback, where no ECS exists (same split as
         *     discover_model_ceiling_route). A failed RunTask surfaces as 502 — never
         *     looks started.
         */
        post: operations["judge_launch_route_runs__run_id__judge_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/judge/results": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Judge Results Route
         * @description The per-instance rubric tab's data source (§9.6): every dimension,
         *     reasoning, evidence, and honesty flag — never a collapsed verdict. Latest
         *     judge_results row per (instance, attempt); a re-judge is a new row, not
         *     an overwrite, but this endpoint shows the latest by default.
         */
        get: operations["judge_results_route_runs__run_id__judge_results_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/judge/passes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Judge Passes Route
         * @description Pass-level completeness (review 2026-09-02 §3.2): judge_sampling was
         *     written every pass and never read back, so an operator asking for 2,500
         *     judgments and getting 1,900 rows had no way to tell a complete pass from
         *     one the budget ceiling truncated. Newest pass first — the launch
         *     control's banner reads element [0].
         */
        get: operations["judge_passes_route_runs__run_id__judge_passes_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/judge/live": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Judge Live Route
         * @description The pass in progress (2026-09-07): judge_sampling is written only when a pass ENDS,
         *     so this is the run screen's only view of a running pass — judged / in flight / spend /
         *     ETA, from the TTL'd Redis snapshot the pass's writer thread publishes. `live: null`
         *     means no pass is running (or nothing was published — no Redis), never "healthy".
         */
        get: operations["judge_live_route_runs__run_id__judge_live_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/judge/results/{instance_id}/{attempt_number}/review": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Judge Calibration Review Route
         * @description offline-analysis-design.md §11: approve/deny + reasoning on one
         *     dimension of the judge's own verdict — the in-app replacement for §3.9's
         *     offline hand-labeling. ``body.judged_at`` pins the exact judge_results
         *     row a re-judge can't silently retarget.
         */
        post: operations["judge_calibration_review_route_runs__run_id__judge_results__instance_id___attempt_number__review_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/judge/results/{instance_id}/{attempt_number}/reviews": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Judge Review History Route
         * @description §11: so the per-instance Judge panel can show "already reviewed"
         *     state — without this, a reviewer has no way to see a prior decision and
         *     would either re-review blind or have to trust their own memory across a
         *     page reload. ``judged_at`` pins the exact judge_results row, same rule
         *     as the POST review route.
         */
        get: operations["judge_review_history_route_runs__run_id__judge_results__instance_id___attempt_number__reviews_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/judge/calibration": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Judge Calibration Summary Route
         * @description offline-analysis-design.md §11: deliberately NOT run-scoped — the
         *     judge-model alias being calibrated is the same one across every run, so
         *     the 20-distinct-instance threshold (§3.9's original bar) accumulates
         *     across runs. The launch control's coverage readout reads this.
         */
        get: operations["judge_calibration_summary_route_judge_calibration_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/pause-gateway": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Pause Gateway Route
         * @description Block this run's LiteLLM key. Legal any time the run isn't closed,
         *     regardless of the current global gateway-pause state — an explicit
         *     per-run action is always authoritative for that one run.
         */
        post: operations["pause_gateway_route_runs__run_id__pause_gateway_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/resume-gateway": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Resume Gateway Route
         * @description Unblock this run's LiteLLM key — a carve-out even while the gateway
         *     pool is globally paused (§2's table: this run resumes, others stay
         *     blocked).
         */
        post: operations["resume_gateway_route_runs__run_id__resume_gateway_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/model-ceilings": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Model Ceilings */
        get: operations["list_model_ceilings_model_ceilings_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/model-ceilings/{model_alias}/estimate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Estimate Discovery Cost Route
         * @description Cost preview for the UI's confirm gate (design §5): the operator sees the number BEFORE
         *     confirming real spend. Same fields as the started response, status='estimate'.
         */
        get: operations["estimate_discovery_cost_route_model_ceilings__model_alias__estimate_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/model-ceilings/{model_alias}/discover": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Discover Model Ceiling Route
         * @description Kicks off one discovery probe as a background task and returns immediately — a full
         *     ramp+bisect can take minutes (design doc §2), too long to hold an HTTP request open. The UI
         *     polls ``GET /model-ceilings`` for the result; a fresh ``discovered_at`` IS the completion
         *     signal, no separate job-status tracking needed.
         */
        post: operations["discover_model_ceiling_route_model_ceilings__model_alias__discover_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/model-ceilings/{model_alias}/manual": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Manual Model Ceiling Route */
        post: operations["manual_model_ceiling_route_model_ceilings__model_alias__manual_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/limits": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Limits
         * @description Every knob with its effective value and source; with ``run_id`` also the pacer cfg
         *     of each alias the run targets (and of its pool). ``state='unknown'`` when Redis is
         *     unreachable — nothing here is trustworthy then, and the UI must not render zeros.
         */
        get: operations["get_limits_limits_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/limits/run": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Set Run Limit
         * @description Set (value) or clear (null) one field of the run-overrides hash. Live within one
         *     dispatcher tick (15 s) / ground-truth refresh (10 s).
         */
        post: operations["set_run_limit_limits_run_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/limits/global": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Set Global Limit
         * @description Set (value) or clear (null) one global knob. Live within one dispatcher / eval-scaler
         *     tick; survives an eval-tier destroy (rehydrated from the audit table at supervisor start).
         */
        post: operations["set_global_limit_limits_global_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/limits/pacer/{alias}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Set Pacer Limit
         * @description Set one pacer:cfg field on the alias, live on the next admission. Seed-relative fields
         *     re-base their _seed; seeded_at is re-stamped. ``also_pool`` writes the pool key too and
         *     persists a pacer_cfg_seeds row so the next launch and bring-up inherit it.
         */
        post: operations["set_pacer_limit_limits_pacer__alias__post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Runs
         * @description The run list, newest-first.  Each item carries its maintained summary.
         */
        get: operations["list_runs_runs_get"];
        put?: never;
        /**
         * Create Run
         * @description §3.1: 201 on launch; 409 (flat body, "the id is the payload, not
         *     decoration") on a duplicate (harness, model_alias) pair; 503 (D4: fail
         *     closed) when the OpenRouter provisioning key is unavailable.  No
         *     ``response_model`` here — the three outcomes have different shapes, and
         *     FastAPI's default ``HTTPException`` would wrap the 409 body in
         *     ``{"detail": ...}``, which is not the contract.
         */
        post: operations["create_run_runs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/dataset/instances": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Dataset Instances */
        get: operations["get_dataset_instances_dataset_instances_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/harnesses": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Harnesses */
        get: operations["get_harnesses_harnesses_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/models": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Models */
        get: operations["get_models_models_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/launch/instruction-presets": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Instruction Presets
         * @description 2026-09-09 efficiency prompt arm: starting texts for the launch screen's
         *     harness-instructions field, plus its size cap.
         */
        get: operations["get_instruction_presets_launch_instruction_presets_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Run
         * @description One run: metadata, maintained summary, per-state buckets, terminal flag.
         */
        get: operations["get_run_runs__run_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/progress": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Run Progress
         * @description Per-phase, per-state counts + expected + denominator for one run (M2.4).
         *
         *     Counts come from Postgres (the report path is Postgres-truth, M2.5) rather
         *     than the M2.2 Redis sets that do not exist yet; ``expected``/``denominator``
         *     come from the maintained ``run_summary`` blob.  They are surfaced as two
         *     separate, labelled numbers on purpose — an aborted run must never report a
         *     resolve rate over the dispatch plan.
         */
        get: operations["get_run_progress_runs__run_id__progress_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/live": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Run Live
         * @description Per-instance in-flight progress from Redis (F11), over the Postgres
         *     attempt list.
         *
         *     The shim writes per-turn progress to Redis on every serviced turn
         *     (``harness_worker`` → ``redis_client.write_progress``); nothing read it.
         *     This enumerates the run's non-terminal attempts from Postgres
         *     (``queries.list_active_attempts``) and reads each one's TTL'd key.
         *
         *     The part that is easy to get wrong: a missing key has at least three
         *     causes (not started / TTL expired / worker died) and they must not all
         *     render as "0 turns" — each instance gets an explicit state (running /
         *     pending / stale), and if Redis itself is unreachable the WHOLE response
         *     is ``state="unknown"``, never an empty list that reads as "nothing is
         *     running".
         */
        get: operations["get_run_live_runs__run_id__live_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/pacer": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Run Pacer
         * @description Live L1 pacer state per alias of this run, straight from the Redis ledger the shims
         *     share (BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03.md §2.5): cfg, bucket fill, in-flight
         *     volume, the WAIT QUEUE (each denied call's size + wait), and the last 60 s of the
         *     pacer's own admission / over-2s / overload counters. Same health contract as /live:
         *     Redis unreachable → whole response ``unknown``, never an empty list.
         */
        get: operations["get_run_pacer_runs__run_id__pacer_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/autoscaler/{pool}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Autoscaler Decision
         * @description The pool's live L2 decision record from Redis (forecast review 2026-09-03): mode,
         *     desired ceiling vs ECS in-flight, binding constraint (+ which alias), per-alias
         *     ceilings / bindings / curve sources, booting tasks, hold-cap timeouts, wait-queue depth.
         *     Absent and unknown are distinct states — an expired record must never read as 'planner
         *     says go'.
         */
        get: operations["get_autoscaler_decision_autoscaler__pool__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/instances/{instance_id}/{attempt_number}/calls": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Instance Calls
         * @description The attempt's ``llm_calls`` rows in call order — per-call wall-clock decomposition
         *     (preflight / paced wait / retried round-trips / backoff / final latency) + the pacer's
         *     diagnostics (design doc §2.6). Empty list = no rows landed (yet); the writer ingests
         *     ``llm_calls.jsonl`` after the attempt finishes, so an in-flight attempt reads empty here
         *     — the live view is /live and /pacer.
         */
        get: operations["get_instance_calls_runs__run_id__instances__instance_id___attempt_number__calls_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/timeline": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Run Timeline
         * @description The undecimated, un-scrubbed timeline read for one run (timeline plan §4.4):
         *     the per-run ticks (``run_timeline_tick``), both pools' ``capacity_snapshot`` rows in the
         *     run's window, every event source (runs.*_at stamps, run_events, operator_limit_edits,
         *     model_tpm_observations), the instance rows the lanes are built from, and the llm_calls
         *     rows. Aurora only — works during and after the run. ``scripts/export_run_timeline.py``
         *     turns this into the site's files (columnar, decimated, scrubbed); nothing here is
         *     shaped for the site directly.
         */
        get: operations["get_run_timeline_runs__run_id__timeline_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/export": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Run Export
         * @description The M6.2 publication artifact — the contract in publication-site-design
         *     §5.1, built EXACTLY (builder 2 builds the site against it).
         *
         *     Every numeric field is nullable: a null renders as "not measured" and is
         *     never coerced to zero.  ``totals.attempted`` comes from the resolve-rate
         *     denominator (infra-retry collapse), not the frozen dispatch ``expected``;
         *     ``pass_at_k`` honours ``retry_reason`` (operator_rerun_pass_at_k and
         *     configured attempts_per_instance slots are legitimate k, operator_infra_
         *     retry is not); aborted instances are excluded from both denominators.
         */
        get: operations["get_run_export_runs__run_id__export_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/runs/{run_id}/instances": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Run Instances
         * @description Paginated, filterable instance rows for one run.
         */
        get: operations["list_run_instances_runs__run_id__instances_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/instances/{run_id}/{instance_id}/{attempt}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Instance
         * @description The phase rows for one (run, instance, attempt) — harness + eval side by side.
         *
         *     ``instance_id`` may contain a slash (some SWE-bench ids are
         *     ``repo/owner``-shaped); FastAPI path conversion keeps this unambiguous.
         */
        get: operations["get_instance_instances__run_id___instance_id___attempt__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/capacity": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Capacity
         * @description Recent capacity_snapshot ticks, oldest-first, for the Axis A/B charts.
         */
        get: operations["list_capacity_capacity_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/queues": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Queues
         * @description Depth + DLQ reading for the three work queues (M2.1 / M2.4).
         *
         *     ``visible`` vs ``not_visible`` is the incident-vs-capacity signal and is
         *     never collapsed; ``oldest_age_s`` is CloudWatch-derived and ``None`` (not 0)
         *     when unavailable.  A DLQ depth above zero is an alarm, not a stat.
         */
        get: operations["list_queues_queues_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/artifacts/{run_id}/{instance_id}/{attempt}/{kind}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Artifact
         * @description Proxy one stored artifact (patch / trajectory / log / report) from S3.
         *
         *     The S3 key comes from THIS attempt's own instance_results row — a caller
         *     can only ever fetch an artifact the run actually produced.
         */
        get: operations["get_artifact_artifacts__run_id___instance_id___attempt___kind__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /**
         * AbortReport
         * @description POST /runs/{run_id}/abort — the drain report, not a completion signal.
         *
         *     ``settled`` is False while the drain is still bounded by stopTimeout +
         *     upload + the results queue settling; the UI must keep polling until the run
         *     reaches a terminal state instead of flipping to "aborted" on this 200
         *     (builder2-operator-dashboard-phase1.md §4a-b).
         */
        AbortReport: {
            /** Run Id */
            run_id: string;
            /**
             * Status
             * @default aborting
             */
            status: string;
            /**
             * Scope
             * @default harness
             */
            scope: string;
            /**
             * Reason
             * @default
             */
            reason: string;
            /**
             * Actor
             * @default operator
             */
            actor: string;
            /**
             * In Flight Stopped
             * @default 0
             */
            in_flight_stopped: number;
            /**
             * Drained
             * @default 0
             */
            drained: number;
            /**
             * Drain Skipped
             * @default false
             */
            drain_skipped: boolean;
            /**
             * Drain Skip Reason
             * @default
             */
            drain_skip_reason: string;
            /**
             * Settled
             * @default false
             */
            settled: boolean;
            /**
             * Swept
             * @default 0
             */
            swept: number;
            /**
             * Abort Not Instant
             * @default true
             */
            abort_not_instant: boolean;
            /**
             * Note
             * @default
             */
            note: string;
        };
        /**
         * AutoscalerDecision
         * @description GET /autoscaler/{pool} — the LIVE decision record straight from Redis (the capacity
         *     snapshot is the 30 s durable copy; this is the freshest verdict for the dashboard).
         *     ``state`` is ``ok`` (record present), ``absent`` (no record — no planner running, or its
         *     TTL expired), or ``unknown`` (Redis unreachable). ``age_s`` is how old the verdict is —
         *     never render an old record as a current one.
         */
        AutoscalerDecision: {
            /** Pool */
            pool: string;
            /** State */
            state: string;
            /** Age S */
            age_s?: number | null;
            /** Record */
            record?: {
                [key: string]: unknown;
            } | null;
        };
        /**
         * CalibrationReviewHistoryItem
         * @description One judge_calibration_reviews row — GET .../reviews's data source, so
         *     the per-instance Judge panel can show "already reviewed" state instead
         *     of a reviewer either re-reviewing blind or trusting their own memory
         *     across a page reload.
         */
        CalibrationReviewHistoryItem: {
            /** Dimension Id */
            dimension_id: string;
            /** Decision */
            decision: string;
            /** Reviewer Reasoning */
            reviewer_reasoning: string;
            /** Reviewed By */
            reviewed_by: string;
            /** Reviewed At */
            reviewed_at: string;
            /** Corrected Score Numeric */
            corrected_score_numeric: number | null;
            /** Corrected Score Secondary */
            corrected_score_secondary: number | null;
            /** Corrected Flag */
            corrected_flag: boolean | null;
        };
        /** CalibrationReviewHistoryResponse */
        CalibrationReviewHistoryResponse: {
            /** Run Id */
            run_id: string;
            /** Instance Id */
            instance_id: string;
            /** Attempt Number */
            attempt_number: number;
            /** Reviews */
            reviews: components["schemas"]["CalibrationReviewHistoryItem"][];
        };
        /** CalibrationReviewRecorded */
        CalibrationReviewRecorded: {
            /** Run Id */
            run_id: string;
            /** Instance Id */
            instance_id: string;
            /** Attempt Number */
            attempt_number: number;
            /** Dimension Id */
            dimension_id: string;
            /**
             * Status
             * @default recorded
             */
            status: string;
        };
        /**
         * CalibrationReviewRequest
         * @description POST .../judge/results/{instance_id}/{attempt_number}/review body
         *     (offline-analysis-design.md §11). ``judged_at`` pins the exact
         *     judge_results row reviewed — required because a re-judge produces a new
         *     row, and reviewing "whatever's latest" would silently re-target a
         *     future re-judge instead of what the reviewer actually looked at.
         *     ``reviewer_reasoning`` is required on both approve and deny.
         */
        CalibrationReviewRequest: {
            /** Judged At */
            judged_at: string;
            /** Dimension Id */
            dimension_id: string;
            /** Decision */
            decision: string;
            /** Reviewer Reasoning */
            reviewer_reasoning: string;
            /**
             * Reviewed By
             * @default operator
             */
            reviewed_by: string;
            /** Corrected Score Numeric */
            corrected_score_numeric?: number | null;
            /** Corrected Score Secondary */
            corrected_score_secondary?: number | null;
            /** Corrected Flag */
            corrected_flag?: boolean | null;
        };
        /** CalibrationSummaryResponse */
        CalibrationSummaryResponse: {
            /** Dimensions */
            dimensions: components["schemas"]["DimensionCalibrationItem"][];
            /** Min Distinct Reviewed Instances */
            min_distinct_reviewed_instances: number;
        };
        /** CapacityList */
        CapacityList: {
            /** Items */
            items: components["schemas"]["CapacityPoint"][];
        };
        /**
         * CapacityPoint
         * @description One observation tick — feeds the Axis A/B charts (Recharts).
         *
         *     Written by the capacity observer (CAPACITY-AND-PIPELINE-VIEW-DESIGN-2026-08-31.md §3.1),
         *     with or without an autoscaler running. None = not measured, never zero. ETAs are
         *     median–p90 ranges labelled estimates, never points.
         */
        CapacityPoint: {
            /** Ts */
            ts: string;
            /** Pool */
            pool: string;
            /** Queue Depth */
            queue_depth: number | null;
            /** Not Visible */
            not_visible?: number | null;
            /** Gateway Headroom */
            gateway_headroom: number | null;
            /** Current Workers */
            current_workers: number | null;
            /** Desired */
            desired: number | null;
            /** Binding Constraint */
            binding_constraint?: string | null;
            /** Ceiling Utilization */
            ceiling_utilization?: number | null;
            /** Decision Age S */
            decision_age_s?: number | null;
            /** Eta Low S */
            eta_low_s?: number | null;
            /** Eta High S */
            eta_high_s?: number | null;
            /** Constants Source */
            constants_source?: string | null;
            /** Pacer Queue Len */
            pacer_queue_len?: number | null;
            /** Paced Over 2S Share */
            paced_over_2s_share?: number | null;
            /** Decision */
            decision?: {
                [key: string]: unknown;
            } | null;
        };
        /**
         * CloseReport
         * @description POST /runs/{run_id}/close — deliberate finalisation (BUILDER4-MANUAL-
         *     RESTART-DESIGN-V2-2026-08-29.md §1). The only path that revokes the run's
         *     keys; never automatic.
         */
        CloseReport: {
            /** Run Id */
            run_id: string;
            /**
             * Status
             * @default completed
             */
            status: string;
        };
        /**
         * ControlMutation
         * @description POST /control/pause|resume — what the mutation did (the audit trail).
         */
        ControlMutation: {
            /** Pools */
            pools: string[];
            /** Paused */
            paused: boolean;
            /**
             * Reason
             * @default
             */
            reason: string;
            /**
             * Actor
             * @default operator
             */
            actor: string;
        };
        /**
         * ControlView
         * @description GET /control — the fail-closed control surface (ADR-0034 M1.10).
         *
         *     ``stale`` is the UI's third state: True means the response was built from a
         *     missing/expired Valkey key or a dead publisher and every pool reads paused
         *     because we cannot confirm otherwise — never a confident "paused".
         */
        ControlView: {
            /** Harness Paused */
            harness_paused: boolean;
            /** Eval Paused */
            eval_paused: boolean;
            /** Gateway Paused */
            gateway_paused: boolean;
            /** Published At */
            published_at: number;
            /** Stale */
            stale: boolean;
            /** Aborted Runs */
            aborted_runs: string[];
            /**
             * Updated By
             * @default
             */
            updated_by: string;
            /**
             * Reason
             * @default
             */
            reason: string;
        };
        /** DatasetInstanceItem */
        DatasetInstanceItem: {
            /** Instance Id */
            instance_id: string;
            /** Repo */
            repo: string;
            /** Launchable */
            launchable: boolean;
        };
        /**
         * DatasetInstancesResponse
         * @description GET /dataset/instances (§3.2) — enough to render a multi-select.
         */
        DatasetInstancesResponse: {
            /** Items */
            items: components["schemas"]["DatasetInstanceItem"][];
            /** Total */
            total: number;
        };
        /**
         * DimensionCalibrationItem
         * @description §11's coverage readout: one rubric dimension's calibration state,
         *     aggregated globally (every run) since judge-model is one alias across
         *     all of them. ``endorsement_rate`` is null, never 0, when nothing has
         *     been reviewed yet — a 0% rate and "not reviewed" are different facts.
         */
        DimensionCalibrationItem: {
            /** Dimension Id */
            dimension_id: string;
            /** Reviewed Count */
            reviewed_count: number;
            /** Distinct Instances Reviewed */
            distinct_instances_reviewed: number;
            /** Approve Count */
            approve_count: number;
            /** Deny Count */
            deny_count: number;
            /** Endorsement Rate */
            endorsement_rate: number | null;
            /** Cleared Threshold */
            cleared_threshold: boolean;
        };
        /**
         * DiscoverCeilingRequest
         * @description POST /model-ceilings/{model_alias}/discover body. ``target_concurrency`` defaults to the
         *     owner's stated real-fleet target (§2 of the design doc's cost-driven top-down revision) —
         *     never an unbounded doubling ramp.
         */
        DiscoverCeilingRequest: {
            /**
             * Target Concurrency
             * @default 150
             */
            target_concurrency: number;
            /**
             * Triggered By
             * @default operator
             */
            triggered_by: string;
            /**
             * Ramp Mode
             * @default target_first
             */
            ramp_mode: string;
            /**
             * Target Tasks
             * @default 60
             */
            target_tasks: number;
        };
        /** DiscoverCeilingStarted */
        DiscoverCeilingStarted: {
            /** Model Alias */
            model_alias: string;
            /** Target Concurrency */
            target_concurrency: number;
            /** Estimated Cost Usd */
            estimated_cost_usd: number;
            /** Ramp Mode */
            ramp_mode?: string | null;
            /** Target Tasks */
            target_tasks?: number | null;
            /**
             * Status
             * @default started
             */
            status: string;
        };
        /**
         * ExportInstance
         * @description One (instance, attempt) row of the export's instances list.
         */
        ExportInstance: {
            /** Instance Id */
            instance_id: string;
            /** Attempt */
            attempt: number;
            /** Verdict */
            verdict: string | null;
            /** Error Category */
            error_category: string | null;
            /** Terminated Reason */
            terminated_reason: string | null;
            /** Input Tokens */
            input_tokens: number | null;
            /** Output Tokens */
            output_tokens: number | null;
            /** Cost Usd */
            cost_usd: number | null;
            /** Agent S */
            agent_s: number | null;
            /** Task Billed S */
            task_billed_s: number | null;
            /** Leak Detectable */
            leak_detectable: boolean | null;
            /** Leaked */
            leaked: boolean | null;
            /** Touches Test Files */
            touches_test_files: boolean;
            /** Gold Patch Similarity */
            gold_patch_similarity: number | null;
        };
        /**
         * ExportLimits
         * @description provenance.limits — the ceilings actually enforced.
         */
        ExportLimits: {
            /** Max Tokens */
            max_tokens: number | null;
            /** Max Cost Usd Per Instance */
            max_cost_usd_per_instance: number | null;
            /** Attempts Per Instance */
            attempts_per_instance: number | null;
            /** Temperature */
            temperature: number | null;
        };
        /**
         * ExportProvenance
         * @description provenance — what produced the numbers (never omit model_resolved).
         */
        ExportProvenance: {
            /** Run Id */
            run_id: string;
            /** Created At */
            created_at: string | null;
            /** Framework Sha */
            framework_sha: string | null;
            /** Swebench Version */
            swebench_version: string | null;
            /** Dataset Name */
            dataset_name?: string | null;
            /** Dataset Revision */
            dataset_revision: string | null;
            /** Image Digest Snapshot */
            image_digest_snapshot?: string | null;
            /** Pin */
            pin?: string | null;
            /** Harness Image Digest */
            harness_image_digest: string | null;
            /** Gateway Config Hash */
            gateway_config_hash: string | null;
            /** Model Alias */
            model_alias: string | null;
            /** Model Resolved */
            model_resolved: string | null;
            /** Harness */
            harness: string | null;
            /** Harness Cli Version */
            harness_cli_version: string | null;
            /** Network Posture */
            network_posture: string;
            limits: components["schemas"]["ExportLimits"];
        };
        /**
         * ExportTotals
         * @description totals — both resolve rates always (ADR-0038), Wilson ci95, pass@k.
         */
        ExportTotals: {
            /** Attempted */
            attempted: number;
            /** Gradeable */
            gradeable: number;
            /** Resolved */
            resolved: number;
            /** Resolve Rate Attempted */
            resolve_rate_attempted: number | null;
            /** Resolve Rate Gradeable */
            resolve_rate_gradeable: number | null;
            /** Ci95 Attempted */
            ci95_attempted: number[] | null;
            /** Ci95 Gradeable */
            ci95_gradeable: number[] | null;
            /** Pass At K */
            pass_at_k: {
                [key: string]: number | null;
            };
            /** Cost Usd Total */
            cost_usd_total: number | null;
            /** Compute Cost Usd Total */
            compute_cost_usd_total: number | null;
            /** Tokens */
            tokens: {
                [key: string]: number | null;
            };
        };
        /**
         * GatewayPauseReport
         * @description POST /runs/{run_id}/pause-gateway|resume-gateway (BUILDER4-GATEWAY-
         *     PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §5). ``gateway_key_blocked_by`` is
         *     ``'operator'`` after a pause, ``None`` after a resume — an explicit
         *     per-run action always wins over whatever the global flag currently is.
         */
        GatewayPauseReport: {
            /** Run Id */
            run_id: string;
            /** Gateway Key Blocked By */
            gateway_key_blocked_by: string | null;
        };
        /** HTTPValidationError */
        HTTPValidationError: {
            /** Detail */
            detail?: components["schemas"]["ValidationError"][];
        };
        /**
         * HarnessesResponse
         * @description GET /harnesses (§3.2) — the names a run may use.
         */
        HarnessesResponse: {
            /** Harnesses */
            harnesses: string[];
        };
        /** Health */
        Health: {
            /** Status */
            status: string;
        };
        /**
         * ImageValidationReport
         * @description POST /images/validate — grade the dataset's GOLD patch in each selected
         *     instance's -inst image (dev/IMAGE-PARITY-ROOT-CAUSE-AND-FIX-2026-09-05
         *     Part B).  Rows land under the synthetic run ``image-validation`` as
         *     ordinary eval attempts (retry_reason 'image_validation'), so the run
         *     detail page shows the outcome; a gold that does not RESOLVE is an
         *     environment defect in that image, never a model result.  No model spend.
         */
        ImageValidationReport: {
            /** Run Id */
            run_id: string;
            /**
             * Validated
             * @default []
             */
            validated: components["schemas"]["ValidatedInstance"][];
            /**
             * Skipped
             * @default []
             */
            skipped: components["schemas"]["SkippedInstance"][];
        };
        /**
         * InstanceCall
         * @description One ``llm_calls`` row for the Calls table — the per-call wall-clock decomposition
         *     (design doc §2.6: preflight + paced wait + retried-attempt round-trips + backoff +
         *     final-attempt latency) plus the pacer's diagnostics. NULL = not measured / not retried.
         */
        InstanceCall: {
            /** Call Index */
            call_index: number;
            /** Started At */
            started_at: string | null;
            /** Http Status */
            http_status: number | null;
            /** Model Resolved */
            model_resolved: string | null;
            /** Error Type */
            error_type: string | null;
            /** Rate Limit Scope */
            rate_limit_scope: string | null;
            /** Shim Preflight Ms */
            shim_preflight_ms: number | null;
            /** Paced Wait Ms */
            paced_wait_ms: number | null;
            /** Overload Retries */
            overload_retries: number | null;
            /** Overload Backoff Ms */
            overload_backoff_ms: number | null;
            /** Retry Upstream Ms */
            retry_upstream_ms: number | null;
            /** Ttft Ms */
            ttft_ms: number | null;
            /** Latency Ms */
            latency_ms: number | null;
            /** Pacer Was Queued */
            pacer_was_queued: boolean | null;
            /** Pacer Queue Len */
            pacer_queue_len: number | null;
            /** Pacer Deny Axis */
            pacer_deny_axis: string | null;
            /** Input Tokens */
            input_tokens: number | null;
            /** Output Tokens */
            output_tokens: number | null;
            /** Cached Tokens */
            cached_tokens: number | null;
            /** Cost Usd */
            cost_usd: number | null;
        };
        /** InstanceCalls */
        InstanceCalls: {
            /** Run Id */
            run_id: string;
            /** Instance Id */
            instance_id: string;
            /** Attempt Number */
            attempt_number: number;
            /** Items */
            items: components["schemas"]["InstanceCall"][];
        };
        /**
         * InstanceDetail
         * @description GET /instances/{run_id}/{instance_id}/{attempt} — the phase rows for one attempt.
         */
        InstanceDetail: {
            /** Run Id */
            run_id: string;
            /** Instance Id */
            instance_id: string;
            /** Attempt Number */
            attempt_number: number;
            /** Rows */
            rows: components["schemas"]["InstanceItem"][];
        };
        /**
         * InstanceItem
         * @description One instance_results row — columns the dashboard actually renders.
         *
         *     Phase timings (ADR-0037) and contamination signals (ADR-0038) are all
         *     NULLable on purpose: NULL means "not measured", never an invented number.
         */
        InstanceItem: {
            /** Run Id */
            run_id: string;
            /** Instance Id */
            instance_id: string;
            /** Attempt Number */
            attempt_number: number;
            /** Phase */
            phase: string;
            /** State */
            state: string;
            /** Error Category */
            error_category: string | null;
            /** Error Detail */
            error_detail: string | null;
            /** Verdict */
            verdict: string | null;
            /** Wall Clock Harness S */
            wall_clock_harness_s: number | null;
            /** Wall Clock Eval S */
            wall_clock_eval_s: number | null;
            /** Touches Test Files */
            touches_test_files: boolean | null;
            /** Patch Path */
            patch_path: string | null;
            /** Trajectory Path */
            trajectory_path: string | null;
            /** Raw Log Path */
            raw_log_path: string | null;
            /** Report Path */
            report_path: string | null;
            /** Report Json */
            report_json: {
                [key: string]: unknown;
            } | null;
            /** Native Trajectory S3 Key */
            native_trajectory_s3_key: string | null;
            /** Test Output S3 Key */
            test_output_s3_key: string | null;
            /** Run Log S3 Key */
            run_log_s3_key: string | null;
            /** Retry Reason */
            retry_reason: string | null;
            /** Created At */
            created_at: string | null;
            /** Input Tokens */
            input_tokens: number | null;
            /** Output Tokens */
            output_tokens: number | null;
            /** Cost Usd */
            cost_usd: number | null;
            /** Turns Used */
            turns_used: number | null;
            /** Paced Wait Ms Total */
            paced_wait_ms_total?: number | null;
            /** Paced Calls */
            paced_calls?: number | null;
            /** Pacer Timeouts */
            pacer_timeouts?: number | null;
            /** Overload Retries Total */
            overload_retries_total?: number | null;
            /** Adapter Input Tokens */
            adapter_input_tokens: number | null;
            /** Adapter Output Tokens */
            adapter_output_tokens: number | null;
            /** Adapter Cost Usd */
            adapter_cost_usd: number | null;
            /** Agent S */
            agent_s: number | null;
            /** Task Observed S */
            task_observed_s: number | null;
            /** Task Billed S */
            task_billed_s: number | null;
            /** Repo Prep S */
            repo_prep_s: number | null;
            /** Eval Test S */
            eval_test_s: number | null;
            /** Queue Wait S */
            queue_wait_s: number | null;
            /** Provision S */
            provision_s: number | null;
            /** Image Pull S */
            image_pull_s: number | null;
            /** Worker Boot S */
            worker_boot_s: number | null;
            /** Patch Extract S */
            patch_extract_s: number | null;
            /** Artifact Upload S */
            artifact_upload_s: number | null;
            /** Repo Prep Cache Hit */
            repo_prep_cache_hit: boolean | null;
            /** Image Pull Cold */
            image_pull_cold: boolean | null;
            /** Eval Queue Wait S */
            eval_queue_wait_s: number | null;
            /** Eval Patch Fetch S */
            eval_patch_fetch_s: number | null;
            /** Eval Image Pull S */
            eval_image_pull_s: number | null;
            /** Eval Log Upload S */
            eval_log_upload_s: number | null;
            /** Eval Image Pull Cold */
            eval_image_pull_cold: boolean | null;
            /** Cost Source */
            cost_source: string | null;
            /** Stripped Test Paths */
            stripped_test_paths: string[] | null;
            /** Grade Invalid */
            grade_invalid: boolean | null;
            /** Leaked Node Ids */
            leaked_node_ids: string[] | null;
            /** Gold Patch Similarity */
            gold_patch_similarity: number | null;
            /** Leak Detectable */
            leak_detectable: boolean | null;
        };
        /** InstancesList */
        InstancesList: {
            /** Items */
            items: components["schemas"]["InstanceItem"][];
            /** Total */
            total: number;
            /** Limit */
            limit: number;
            /** Offset */
            offset: number;
        };
        /**
         * InstructionPreset
         * @description One entry of GET /launch/instruction-presets — a starting text the launch screen
         *     can load into the harness-instructions field (the operator may edit it).
         */
        InstructionPreset: {
            /** Id */
            id: string;
            /** Name */
            name: string;
            /** Harness */
            harness: string;
            /** Text */
            text: string;
        };
        /** InstructionPresetsResponse */
        InstructionPresetsResponse: {
            /** Presets */
            presets: components["schemas"]["InstructionPreset"][];
            /** Max Chars */
            max_chars: number;
        };
        /**
         * JudgeCandidateItem
         * @description One row of GET /runs/{run_id}/judge/candidates — the instance
         *     selector's eval-status column (§10.1): lets the operator exclude, e.g.,
         *     everything that isn't verdict='resolved' before launching.
         */
        JudgeCandidateItem: {
            /** Instance Id */
            instance_id: string;
            /** Attempt Number */
            attempt_number: number;
            /** Harness */
            harness: string;
            /** Outcome */
            outcome: string;
            /** Always Judge */
            always_judge: boolean;
            /** Already Judged */
            already_judged: boolean;
            /** Last Judgment */
            last_judgment?: string | null;
        };
        /** JudgeCandidatesResponse */
        JudgeCandidatesResponse: {
            /** Run Id */
            run_id: string;
            /** Candidates */
            candidates: components["schemas"]["JudgeCandidateItem"][];
        };
        /**
         * JudgeDimensionScoreItem
         * @description One dimension's score within one judge_results row (§10.7's
         *     normalized projection) — this is what the UI's per-instance rubric tab
         *     renders, all eight, not a collapsed verdict.
         */
        JudgeDimensionScoreItem: {
            /** Dimension Id */
            dimension_id: string;
            /** Scale Type */
            scale_type: string;
            /** Score Numeric */
            score_numeric: number | null;
            /** Score Secondary */
            score_secondary: number | null;
            /** Flag */
            flag: boolean | null;
            /** Span Start Turn */
            span_start_turn: number | null;
            /** Span End Turn */
            span_end_turn: number | null;
            /** Reasoning */
            reasoning: string | null;
            /** Evidence */
            evidence: {
                [key: string]: unknown;
            }[];
            /** Evidence Missing */
            evidence_missing: boolean;
            /**
             * Causes
             * @default []
             */
            causes: {
                [key: string]: unknown;
            }[];
        };
        /** JudgeEstimateResponse */
        JudgeEstimateResponse: {
            /** Run Id */
            run_id: string;
            /** Candidate Count */
            candidate_count: number;
            /** Prune Mode */
            prune_mode: string;
            /** Estimated Cost Usd */
            estimated_cost_usd: number;
            /**
             * Status
             * @default estimate
             */
            status: string;
        };
        /**
         * JudgeLaunchRequest
         * @description POST /runs/{run_id}/judge body. instance_ids=None means all eligible
         *     candidates (§10.1's selector; default 100% coverage per §9.3 — the
         *     stratified sample_rate exists for when a narrower run is deliberately
         *     wanted, not because 100% is unaffordable).
         */
        JudgeLaunchRequest: {
            /** Instance Ids */
            instance_ids?: string[] | null;
            /**
             * Prune Mode
             * @default pruned
             */
            prune_mode: string;
            /**
             * Model Alias
             * @default judge-model
             */
            model_alias: string;
            /**
             * Sample Rate
             * @default 1
             */
            sample_rate: number;
            /**
             * Min Per Stratum
             * @default 5
             */
            min_per_stratum: number;
            /** Seed */
            seed?: number | null;
            /**
             * Max Spend Usd
             * @default 25
             */
            max_spend_usd: number;
            /**
             * Workers
             * @default 24
             */
            workers: number;
            /**
             * Rejudge
             * @default false
             */
            rejudge: boolean;
            /**
             * Synthesis Only
             * @default false
             */
            synthesis_only: boolean;
            /**
             * Retry No Verdict
             * @default false
             */
            retry_no_verdict: boolean;
            /**
             * Triggered By
             * @default operator
             */
            triggered_by: string;
        };
        /** JudgeLaunchStarted */
        JudgeLaunchStarted: {
            /** Run Id */
            run_id: string;
            /** Pass Id */
            pass_id: string;
            /** Estimated Cost Usd */
            estimated_cost_usd: number;
            /**
             * Status
             * @default started
             */
            status: string;
        };
        /** JudgeLiveInFlight */
        JudgeLiveInFlight: {
            /** Instance Id */
            instance_id: string;
            /** Attempt Number */
            attempt_number: number;
            /** Started At */
            started_at: number;
        };
        /** JudgeLiveResponse */
        JudgeLiveResponse: {
            /** Run Id */
            run_id: string;
            live: components["schemas"]["JudgeLiveState"] | null;
        };
        /**
         * JudgeLiveState
         * @description One judge pass's live progress (judge:live:{run_id}, TTL'd, best-effort): what the DB
         *     cannot show while the pass runs — judge_sampling is written only at the end.
         */
        JudgeLiveState: {
            /** Run Id */
            run_id: string;
            /** Pass Id */
            pass_id: string;
            /** Status */
            status: string;
            /** Workers */
            workers: number;
            /** Selected */
            selected: number;
            /** Judged */
            judged: number;
            /** Skipped Over Budget */
            skipped_over_budget: number;
            /** Parse Failed */
            parse_failed: number;
            /** Skipped Artifacts */
            skipped_artifacts: number;
            /**
             * Call Failed
             * @default 0
             */
            call_failed: number;
            /**
             * Timed Out
             * @default 0
             */
            timed_out: number;
            /**
             * Already Judged
             * @default 0
             */
            already_judged: number;
            /** Spend Usd */
            spend_usd: number;
            /** Max Spend Usd */
            max_spend_usd: number;
            /** Started At */
            started_at: number;
            /** Updated At */
            updated_at: number;
            /** Finished At */
            finished_at: number | null;
            /** Elapsed S */
            elapsed_s: number;
            /** Eta S */
            eta_s: number | null;
            /** In Flight Count */
            in_flight_count: number;
            /** In Flight */
            in_flight: components["schemas"]["JudgeLiveInFlight"][];
            /** Last Error */
            last_error: string | null;
        };
        /**
         * JudgePassItem
         * @description One judge_sampling row (review 2026-09-02 §3.2): a pass that hit the
         *     budget ceiling must not look like a pass that finished — the UI's
         *     launch-control banner reads `judged N of M eligible — K skipped at the
         *     $X ceiling` from this, and total_parse_failed the same way.
         */
        JudgePassItem: {
            /** Pass Id */
            pass_id: string;
            /** Requested Rate */
            requested_rate: number | null;
            /** Seed */
            seed: number | null;
            /** Total Eligible */
            total_eligible: number | null;
            /** Total Judged */
            total_judged: number | null;
            /** Total Skipped Over Budget */
            total_skipped_over_budget: number | null;
            /** Total Parse Failed */
            total_parse_failed: number | null;
            /** Created At */
            created_at: string;
            /** Synthesis */
            synthesis?: string | null;
            /** Synthesis Cost Usd */
            synthesis_cost_usd?: number | null;
            /** Synthesis Model Resolved */
            synthesis_model_resolved?: string | null;
            /** Synthesis Error */
            synthesis_error?: string | null;
            /**
             * Synthesis Only
             * @default false
             */
            synthesis_only: boolean;
        };
        /** JudgePassesResponse */
        JudgePassesResponse: {
            /** Run Id */
            run_id: string;
            /** Passes */
            passes: components["schemas"]["JudgePassItem"][];
        };
        /** JudgeResultItem */
        JudgeResultItem: {
            /** Instance Id */
            instance_id: string;
            /** Attempt Number */
            attempt_number: number;
            /** Judged At */
            judged_at: string;
            /** Judge Model Resolved */
            judge_model_resolved: string | null;
            /** Rubric Version */
            rubric_version: string;
            /** Judge Prune Mode */
            judge_prune_mode: string | null;
            /** Input Truncated */
            input_truncated: boolean;
            /** Tool Output Pruned */
            tool_output_pruned: boolean;
            /** Judge Parse Failed */
            judge_parse_failed: boolean;
            /** Judge Method */
            judge_method?: string | null;
            /** Judge Attempts */
            judge_attempts?: number | null;
            /** Summary */
            summary: string | null;
            /** Judge Cost Usd */
            judge_cost_usd: number | null;
            /** Dimensions */
            dimensions: components["schemas"]["JudgeDimensionScoreItem"][];
            /** Efficiency Profile */
            efficiency_profile?: {
                [key: string]: unknown;
            } | null;
        };
        /** JudgeResultsResponse */
        JudgeResultsResponse: {
            /** Run Id */
            run_id: string;
            /** Results */
            results: components["schemas"]["JudgeResultItem"][];
        };
        /**
         * LaunchLimits
         * @description The limits a run was launched with (run_launch.RunConfig, echoed from
         *     ``runs.config_snapshot`` — 2026-09-08). Every field is optional: None means the
         *     snapshot did not record it, never a default.
         */
        LaunchLimits: {
            /** Timeout Seconds */
            timeout_seconds?: number | null;
            /** Max Tokens Per Instance */
            max_tokens_per_instance?: number | null;
            /** Max Cost Usd Per Instance */
            max_cost_usd_per_instance?: number | null;
            /** Max Turns Per Instance */
            max_turns_per_instance?: number | null;
            /** Context Window Tokens */
            context_window_tokens?: number | null;
            /** Max Parallel Harness Tasks */
            max_parallel_harness_tasks?: number | null;
            /** Ramp Step Pct */
            ramp_step_pct?: number | null;
            /** Ramp Cooldown Seconds */
            ramp_cooldown_seconds?: number | null;
            /** Autoscaler Enabled */
            autoscaler_enabled?: boolean | null;
            /** Initial Budget Override */
            initial_budget_override?: {
                [key: string]: number;
            } | null;
            /** Harness Instructions */
            harness_instructions?: string | null;
        };
        /**
         * LimitEditRequest
         * @description POST /limits/run and /limits/global: ``value`` null clears the field (defaults).
         */
        LimitEditRequest: {
            /** Field */
            field: string;
            /** Value */
            value?: number | boolean | null;
            /**
             * Actor
             * @default operator
             */
            actor: string;
            /**
             * Reason
             * @default
             */
            reason: string;
        };
        /** LimitEditResult */
        LimitEditResult: {
            /** Scope */
            scope: string;
            /** Target */
            target: string;
            /** Field */
            field: string;
            /** Old */
            old: string | null;
            /** New */
            new: string | null;
            /** Pool */
            pool?: string | null;
        };
        /**
         * LimitField
         * @description One knob's effective value and where it came from.
         */
        LimitField: {
            /** Value */
            value: number | null;
            /** Source */
            source: string;
            /** Set By */
            set_by?: string | null;
        };
        /** LimitSpec */
        LimitSpec: {
            /** Field */
            field: string;
            /** Scope */
            scope: string;
            /** Kind */
            kind: string;
            /** Label */
            label: string;
            /** Description */
            description: string;
            /** Lo */
            lo?: number | null;
            /** Hi */
            hi?: number | null;
            /** Default */
            default?: number | null;
            /**
             * Default Note
             * @default
             */
            default_note: string;
            /**
             * Unit
             * @default
             */
            unit: string;
            /**
             * Read By
             * @default
             */
            read_by: string;
        };
        /** LimitsGlobalView */
        LimitsGlobalView: {
            /** Fields */
            fields: {
                [key: string]: components["schemas"]["LimitField"];
            };
        };
        /** LimitsRunView */
        LimitsRunView: {
            /** Run Id */
            run_id: string | null;
            /** Set At */
            set_at: number | null;
            /** Fields */
            fields: {
                [key: string]: components["schemas"]["LimitField"];
            };
        };
        /**
         * LimitsView
         * @description GET /limits — every operator-adjustable knob with its effective value + source, the
         *     static rails from the live decision records, and the pacer cfg per alias of a run.
         */
        LimitsView: {
            /** State */
            state: string;
            run?: components["schemas"]["LimitsRunView"] | null;
            global_?: components["schemas"]["LimitsGlobalView"] | null;
            /**
             * Static
             * @default {}
             */
            static: {
                [key: string]: unknown;
            };
            /**
             * Pacer
             * @default []
             */
            pacer: components["schemas"]["PacerLimitRow"][];
            /**
             * Specs
             * @default []
             */
            specs: components["schemas"]["LimitSpec"][];
        };
        /**
         * LiveInstance
         * @description One in-flight (instance, attempt) of GET /runs/{run_id}/live (F11).
         *
         *     ``state`` is explicit so a missing Redis key never renders as "0 turns"
         *     (BUILDER1-EXPORT-AND-LIVE-ENDPOINTS-2026-08-31.md §2):
         *
         *       * ``"running"`` — the Redis progress key is live; turn/tokens/cost/age
         *         are real observations;
         *       * ``"pending"`` — the attempt has not reached a RUNNING state and has no
         *         key (not started, or TTL expired before the first write);
         *       * ``"stale"`` — the attempt reached a RUNNING state but its key is gone
         *         (TTL expired mid-run, or the worker died).  The instance is in flight
         *         per Postgres but invisible in Redis.
         *
         *     ``observed_at`` is the payload's ``updated_at`` epoch stamp and ``age_s``
         *     its age at response time — freshness is a first-class value because the
         *     key is TTL'd and best-effort, so every number carries how old it is.
         */
        LiveInstance: {
            /** Instance Id */
            instance_id: string;
            /** Attempt Number */
            attempt_number: number;
            /** State */
            state: string;
            /** Turn Number */
            turn_number: number | null;
            /** Input Tokens */
            input_tokens: number | null;
            /** Output Tokens */
            output_tokens: number | null;
            /** Cached Tokens */
            cached_tokens: number | null;
            /** Reasoning Tokens */
            reasoning_tokens: number | null;
            /** Cost Usd */
            cost_usd: number | null;
            /** Observed At */
            observed_at: number | null;
            /** Age S */
            age_s: number | null;
            /** Phase */
            phase?: string | null;
            /** Eval Elapsed S */
            eval_elapsed_s?: number | null;
            /** Eval Lines */
            eval_lines?: number | null;
            /** Eval Last Line */
            eval_last_line?: string | null;
            /** Eval Silent S */
            eval_silent_s?: number | null;
            /**
             * Revived After Reap
             * @default false
             */
            revived_after_reap: boolean;
            /** Paced Wait Ms Total */
            paced_wait_ms_total?: number | null;
            /** Paced Calls */
            paced_calls?: number | null;
            /** Pacer Timeouts */
            pacer_timeouts?: number | null;
            /** Overload Retries Total */
            overload_retries_total?: number | null;
            /** Pacer Last Deny Axis */
            pacer_last_deny_axis?: string | null;
            /** Pacer Last Queue Len */
            pacer_last_queue_len?: number | null;
        };
        /**
         * LlmLiveCall
         * @description One light spend-log row for the live LLM-call view (llm_live.py facts).
         *
         *     ``spend`` is deliberately absent — the column is always 0.0 for our custom
         *     models and must never be rendered as cost; the ledger's ``cost_usd`` is
         *     the money truth.
         */
        LlmLiveCall: {
            /** Request Id */
            request_id: string;
            /** Started At */
            started_at: string;
            /** Model */
            model: string | null;
            /** Prompt Tokens */
            prompt_tokens: number | null;
            /** Completion Tokens */
            completion_tokens: number | null;
            /** Text Tokens */
            text_tokens: number | null;
            /** Cached Tokens */
            cached_tokens: number | null;
            /** Session Id */
            session_id: string | null;
            /** Instance Id */
            instance_id: string | null;
            /** Attempt */
            attempt: number | null;
            /** Harness */
            harness: string | null;
            /** Status */
            status?: string | null;
        };
        /** LlmLiveDetail */
        LlmLiveDetail: {
            /** Request Id */
            request_id: string;
            /** Started At */
            started_at: string;
            /** Model */
            model: string | null;
            /** Prompt Tokens */
            prompt_tokens: number | null;
            /** Completion Tokens */
            completion_tokens: number | null;
            /** Session Id */
            session_id: string | null;
            /** Instance Id */
            instance_id: string | null;
            /** Attempt */
            attempt: number | null;
            /** Harness */
            harness: string | null;
            /** Status */
            status?: string | null;
            /** Messages */
            messages: {
                [key: string]: unknown;
            }[];
            /** System */
            system: unknown | null;
            /** Tools Count */
            tools_count: number;
            /** Response */
            response: {
                [key: string]: unknown;
            } | null;
        };
        /** LlmLiveList */
        LlmLiveList: {
            /** Run Id */
            run_id: string;
            /** Items */
            items: components["schemas"]["LlmLiveCall"][];
        };
        /** ManualCeilingRequest */
        ManualCeilingRequest: {
            /** Tpm Value */
            tpm_value: number;
            /** Notes */
            notes?: string | null;
        };
        /**
         * ModelCeiling
         * @description One row of ``GET /model-ceilings`` — BUILDER4-AUTOSCALER-TPM-CEILING-DISCOVERY-DESIGN-
         *     2026-08-31.md §5. Backed by the ``model_ceilings`` view (§3), so ``None`` here means "no
         *     qualifying observation yet", not a missing row — the UI's age/staleness display (§4.1) reads
         *     ``discovered_at`` directly, never inferring health from its absence.
         */
        ModelCeiling: {
            /** Model Alias */
            model_alias: string;
            /** Discovered Tpm */
            discovered_tpm?: number | null;
            /** Ceiling Source */
            ceiling_source?: string | null;
            /** Discovered At */
            discovered_at?: string | null;
            /** Provider */
            provider?: string | null;
            /** Values */
            values?: {
                [key: string]: number;
            } | null;
            /**
             * Is Stale
             * @default false
             */
            is_stale: boolean;
        };
        /** ModelItem */
        ModelItem: {
            /** Alias */
            alias: string;
            /** Max Input Tokens */
            max_input_tokens: number | null;
            /** Consistency Ratio */
            consistency_ratio?: number | null;
            /** Pacer Seeded At */
            pacer_seeded_at?: number | null;
        };
        /**
         * ModelsResponse
         * @description GET /models (§3.2) — live from the gateway's /model/info, never a
         *     hardcoded list (drift there is invisible until a run produces wrong
         *     numbers).
         */
        ModelsResponse: {
            /** Items */
            items: components["schemas"]["ModelItem"][];
        };
        /**
         * PacerAliasState
         * @description GET /runs/{run_id}/pacer — one alias's live admission ledger, straight from Redis
         *     (BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03.md §2.5). ``measured=False`` means no
         *     pacer keys exist for this alias (no traffic yet / expired) and every number is None —
         *     never zeros that read as idle-and-healthy. Bucket levels are extrapolated to
         *     ``observed_at`` with the cfg refill rate. ``*_60s`` are the last 60 s of the pacer's own
         *     10 s counters (admissions, waits over 2 s, mean wait, real provider overloads).
         */
        PacerAliasState: {
            /** Alias */
            alias: string;
            /** Harness */
            harness: string;
            /** Measured */
            measured: boolean;
            /** C Burst */
            c_burst?: number | null;
            /** R Tok */
            r_tok?: number | null;
            /** K Inflight */
            k_inflight?: number | null;
            /** C Req */
            c_req?: number | null;
            /** R Qps */
            r_qps?: number | null;
            /** Seeded At */
            seeded_at?: number | null;
            /** Bucket Level */
            bucket_level?: number | null;
            /** Bucket Fill */
            bucket_fill?: number | null;
            /** Req Level */
            req_level?: number | null;
            /** Req Fill */
            req_fill?: number | null;
            /** Inflight Calls */
            inflight_calls?: number | null;
            /** Inflight Tokens */
            inflight_tokens?: number | null;
            /** Inflight Fill */
            inflight_fill?: number | null;
            /** Queue Len */
            queue_len?: number | null;
            /** Head Est Tokens */
            head_est_tokens?: number | null;
            /** Head Waiting S */
            head_waiting_s?: number | null;
            /**
             * Waiters
             * @default []
             */
            waiters: components["schemas"]["PacerWaiter"][];
            /** Admits 60S */
            admits_60s?: number | null;
            /** Over 2S 60S */
            over_2s_60s?: number | null;
            /** Mean Wait Ms 60S */
            mean_wait_ms_60s?: number | null;
            /** Overloads 60S */
            overloads_60s?: number | null;
            /** Observed At */
            observed_at: number;
        };
        /**
         * PacerLimitEditRequest
         * @description POST /limits/pacer/{alias}: a pacer field is set, never cleared. ``also_pool`` writes
         *     the pool key too and persists a pacer_cfg_seeds row (the next launch / bring-up).
         */
        PacerLimitEditRequest: {
            /** Field */
            field: string;
            /** Value */
            value: number;
            /**
             * Actor
             * @default operator
             */
            actor: string;
            /**
             * Reason
             * @default
             */
            reason: string;
            /**
             * Also Pool
             * @default false
             */
            also_pool: boolean;
        };
        /** PacerLimitRow */
        PacerLimitRow: {
            /** Harness */
            harness: string;
            /** Alias */
            alias: string;
            /** Pool */
            pool: string | null;
            /** Cfg */
            cfg: {
                [key: string]: number | null;
            };
            /** Pool Cfg */
            pool_cfg: {
                [key: string]: number | null;
            } | null;
        };
        /**
         * PacerWaiter
         * @description One call currently denied at the L1 pacer, head first: its estimated prompt tokens and
         *     how long it has been waiting (from its first deny).
         */
        PacerWaiter: {
            /** Est Tokens */
            est_tokens: number;
            /** Waiting S */
            waiting_s: number;
        };
        /**
         * PhaseStateCount
         * @description One per-phase, per-state bucket of a run's progress (M2.4).
         *
         *     Mirrors ``RunStateCount`` but carries the phase, because the same state
         *     name exists in both the harness and eval phases and conflation is exactly
         *     the confusion this endpoint exists to separate.
         */
        PhaseStateCount: {
            /** Phase */
            phase: string;
            /** State */
            state: string;
            /** Count */
            count: number;
        };
        /**
         * Provenance
         * @description Run provenance composed from ``runs.config_snapshot`` (architecture.md §8),
         *     NOT from run_summary.summary_json — see BUILDER2-UI-ISSUES-HANDOVER §A, resolved
         *     as option (b). Every field NULLable: a pre-image or partial run legitimately
         *     lacks some, never invented.
         */
        Provenance: {
            /** Framework Sha */
            framework_sha?: string | null;
            /** Swebench Version */
            swebench_version?: string | null;
            /** Dataset Name */
            dataset_name?: string | null;
            /** Dataset Revision */
            dataset_revision?: string | null;
            /** Image Digest Snapshot */
            image_digest_snapshot?: string | null;
            /** Harness Image Digest */
            harness_image_digest?: string | null;
            /** Gateway Config Hash */
            gateway_config_hash?: string | null;
            /** Model Resolved */
            model_resolved?: string | null;
            /** Resolved Models */
            resolved_models?: {
                [key: string]: unknown;
            } | null;
            /** Context Window Tokens */
            context_window_tokens?: number | null;
            /** Context Window Source */
            context_window_source?: string | null;
        };
        /**
         * QueueView
         * @description GET /queues — one work queue's depth + DLQ (M2.1 / M2.4).
         *
         *     ``visible`` vs ``not_visible`` is the difference between "50 waiting"
         *     (capacity) and "50 stuck in flight" (incident); ``oldest_age_s`` is
         *     CloudWatch-derived and ``None`` (never 0) when unavailable.
         */
        QueueView: {
            /** Queue */
            queue: string;
            /** Visible */
            visible: number;
            /** Not Visible */
            not_visible: number;
            /** Oldest Age S */
            oldest_age_s: number | null;
            /** Dlq Depth */
            dlq_depth: number;
        };
        /** QueuesList */
        QueuesList: {
            /** Items */
            items: components["schemas"]["QueueView"][];
        };
        /**
         * RegradeReport
         * @description POST /runs/{run_id}/regrade — re-grade the EXISTING patch as an
         *     eval-only attempt N+1 (2026-09-01, the EVAL-GRADE-RESOURCE-LIMITS
         *     follow-up).  The remedy for an eval-side failure (EVAL_OOM_KILLED,
         *     ABANDONED, dead-lettered) that /restart cannot give: no new model spend,
         *     the same diff graded again.  Same skip-with-reason shape as /restart.
         */
        RegradeReport: {
            /** Run Id */
            run_id: string;
            /**
             * Regraded
             * @default []
             */
            regraded: components["schemas"]["RegradedInstance"][];
            /**
             * Skipped
             * @default []
             */
            skipped: components["schemas"]["SkippedInstance"][];
        };
        /** RegradedInstance */
        RegradedInstance: {
            /** Instance Id */
            instance_id: string;
            /** Attempt Number */
            attempt_number: number;
            /** Patch S3 Key */
            patch_s3_key: string;
        };
        /**
         * RestartReport
         * @description POST /runs/{run_id}/restart — per-instance outcome, never a whole-batch
         *     failure for an individual instance being ineligible (§2.2 of the v2
         *     design: unknown/in-flight/paused instances are skipped with a reason, not
         *     a 400 for the whole request).
         */
        RestartReport: {
            /** Run Id */
            run_id: string;
            /**
             * Restarted
             * @default []
             */
            restarted: components["schemas"]["RestartedInstance"][];
            /**
             * Skipped
             * @default []
             */
            skipped: components["schemas"]["SkippedInstance"][];
        };
        /** RestartedInstance */
        RestartedInstance: {
            /** Instance Id */
            instance_id: string;
            /** Attempt Number */
            attempt_number: number;
            /** Retry Reason */
            retry_reason: string;
        };
        /**
         * RunDetail
         * @description GET /runs/{run_id} — run metadata + maintained summary + live state counts.
         *
         *     ``terminal`` is computed here (no living instance rows, or the abort
         *     finalised) because nothing in ``runs.status`` records a clean completion.
         */
        RunDetail: {
            /** Run Id */
            run_id: string;
            /** Status */
            status: string;
            /** Created At */
            created_at: string | null;
            /** Estimated Cost Usd */
            estimated_cost_usd: number | null;
            /** Cost Confidence Tier */
            cost_confidence_tier: string | null;
            /** Compute Cost Estimated Usd */
            compute_cost_estimated_usd: number | null;
            /** Compute Cost Reconciled Usd */
            compute_cost_reconciled_usd: number | null;
            /** Budget Cap Usd */
            budget_cap_usd: number | null;
            /** Stop Requested At */
            stop_requested_at: string | null;
            /** Stop Scope */
            stop_scope: string | null;
            /** Stop Reason */
            stop_reason: string | null;
            /** Stopped At */
            stopped_at: string | null;
            /** Summary */
            summary: {
                [key: string]: unknown;
            } | null;
            /** Harness */
            harness?: string | null;
            /** Model Alias */
            model_alias?: string | null;
            provenance?: components["schemas"]["Provenance"] | null;
            launch_limits?: components["schemas"]["LaunchLimits"] | null;
            /** Cost Usd Total */
            cost_usd_total?: number | null;
            /** Instance Count */
            instance_count?: number | null;
            /** Dispatched At */
            dispatched_at?: string | null;
            /** Finalised At */
            finalised_at?: string | null;
            /** Terminal */
            terminal: boolean;
            /** States */
            states: components["schemas"]["RunStateCount"][];
            /**
             * Instance States
             * @default []
             */
            instance_states: components["schemas"]["RunStateCount"][];
            /** Resolve Rate Denominator */
            resolve_rate_denominator: number;
            /** Ready To Close */
            ready_to_close: boolean;
            /** Gateway Key Blocked By */
            gateway_key_blocked_by: string | null;
        };
        /**
         * RunExport
         * @description GET /runs/{run_id}/export — the full publication artifact (M6.2).
         *
         *     ``timing_p50_s`` and ``integrity`` are keyed dicts whose values are
         *     nullable numerics (median / counts) — the pydantic models keep the exact
         *     keys in the docs below while allowing each to be null.
         */
        RunExport: {
            /** Schema Version */
            schema_version: number;
            provenance: components["schemas"]["ExportProvenance"];
            totals: components["schemas"]["ExportTotals"];
            /** Terminated Reasons */
            terminated_reasons: {
                [key: string]: number;
            };
            /** Timing P50 S */
            timing_p50_s: {
                [key: string]: number | null;
            };
            /** Integrity */
            integrity: {
                [key: string]: unknown;
            };
            /** Instances */
            instances: components["schemas"]["ExportInstance"][];
        };
        /**
         * RunItem
         * @description One row of the run list (runs + its maintained ``run_summary`` blob).
         */
        RunItem: {
            /** Run Id */
            run_id: string;
            /** Status */
            status: string;
            /** Created At */
            created_at: string | null;
            /** Estimated Cost Usd */
            estimated_cost_usd: number | null;
            /** Cost Confidence Tier */
            cost_confidence_tier: string | null;
            /** Compute Cost Estimated Usd */
            compute_cost_estimated_usd: number | null;
            /** Compute Cost Reconciled Usd */
            compute_cost_reconciled_usd: number | null;
            /** Budget Cap Usd */
            budget_cap_usd: number | null;
            /** Stop Requested At */
            stop_requested_at: string | null;
            /** Stop Scope */
            stop_scope: string | null;
            /** Stop Reason */
            stop_reason: string | null;
            /** Stopped At */
            stopped_at: string | null;
            /** Summary */
            summary: {
                [key: string]: unknown;
            } | null;
            /** Harness */
            harness?: string | null;
            /** Model Alias */
            model_alias?: string | null;
            provenance?: components["schemas"]["Provenance"] | null;
            launch_limits?: components["schemas"]["LaunchLimits"] | null;
            /** Cost Usd Total */
            cost_usd_total?: number | null;
            /** Instance Count */
            instance_count?: number | null;
            /** Dispatched At */
            dispatched_at?: string | null;
            /** Finalised At */
            finalised_at?: string | null;
            /** Terminal */
            terminal: boolean;
        };
        /**
         * RunLaunchRequest
         * @description POST /runs body (§3.1).
         *
         *     ``instance_ids`` is either an explicit list or the literal string
         *     ``"all"`` — resolved server-side (never client-side, see
         *     ``run_launch_routes._resolve_instances``).  ``context_window_tokens``
         *     defaults to ``None`` ("resolve from the gateway") — the UI must not send
         *     a value unless the operator deliberately overrides it; an explicit value
         *     is exactly what made ``context_window_source = 'run_config'`` on every
         *     Stage 6 run, which is why the live gateway resolution has never executed
         *     in production (§3.1's warning).
         */
        RunLaunchRequest: {
            /** Instance Ids */
            instance_ids: string[] | string;
            /** Harness */
            harness: string;
            /** Model Alias */
            model_alias: string;
            /** Budget Cap Usd */
            budget_cap_usd: number;
            /**
             * Max Cost Usd Per Instance
             * @default 5
             */
            max_cost_usd_per_instance: number;
            /** Max Tokens Per Instance */
            max_tokens_per_instance?: number | null;
            /**
             * Max Turns Per Instance
             * @default 500
             */
            max_turns_per_instance: number | null;
            /**
             * Attempts Per Instance
             * @default 1
             */
            attempts_per_instance: number;
            /**
             * Timeout Seconds
             * @default 5400
             */
            timeout_seconds: number;
            /** Context Window Tokens */
            context_window_tokens?: number | null;
            /**
             * Max Parallel Harness Tasks
             * @default 150
             */
            max_parallel_harness_tasks: number;
            /** Initial Budget Override */
            initial_budget_override?: {
                [key: string]: number;
            } | null;
            /**
             * Ramp Step Pct
             * @default 5
             */
            ramp_step_pct: number;
            /**
             * Ramp Cooldown Seconds
             * @default 60
             */
            ramp_cooldown_seconds: number;
            /**
             * Autoscaler Enabled
             * @default true
             */
            autoscaler_enabled: boolean;
            /** Harness Instructions */
            harness_instructions?: string | null;
        };
        /** RunList */
        RunList: {
            /** Items */
            items: components["schemas"]["RunItem"][];
            /** Total */
            total: number;
            /** Limit */
            limit: number;
            /** Offset */
            offset: number;
        };
        /**
         * RunLive
         * @description GET /runs/{run_id}/live — per-instance in-flight progress (F11).
         *
         *     ``state`` is the WHOLE-response health: ``"ok"`` (Redis reachable; items
         *     carry per-instance states) or ``"unknown"`` (Redis itself is unreachable
         *     — an empty list must never read as "nothing is running").  ``reason``
         *     names the unknown, e.g. ``"redis_unreachable"``.
         */
        RunLive: {
            /** Run Id */
            run_id: string;
            /** Status */
            status: string;
            /**
             * State
             * @default ok
             */
            state: string;
            /**
             * Reason
             * @default
             */
            reason: string;
            /** Items */
            items: components["schemas"]["LiveInstance"][];
        };
        /**
         * RunPacer
         * @description GET /runs/{run_id}/pacer. ``state`` is the whole-response health, like /live:
         *     ``"ok"`` or ``"unknown"`` (Redis unreachable — an empty list must never read as
         *     "no pressure").
         */
        RunPacer: {
            /** Run Id */
            run_id: string;
            /** State */
            state: string;
            /** Reason */
            reason?: string | null;
            /** Items */
            items: components["schemas"]["PacerAliasState"][];
        };
        /**
         * RunProgress
         * @description GET /runs/{id}/progress — per-phase, per-state counts, plus denominators.
         *
         *     ``expected`` is the dispatch plan (instances × attempts — at k=3 a
         *     300-instance run is **900 agent runs**).  ``denominator`` is gradeable, the
         *     honesty denominator (ADR-0034 M1.8) — the two must never be conflated: a
         *     run aborted at 60 of 900 must report its resolves over 60, not 900.
         */
        RunProgress: {
            /** Run Id */
            run_id: string;
            /** Status */
            status: string;
            /** Terminal */
            terminal: boolean;
            /** Phases */
            phases: components["schemas"]["PhaseStateCount"][];
            /** Expected */
            expected: number | null;
            /** Denominator */
            denominator: number | null;
        };
        /**
         * RunStateCount
         * @description One per-state bucket of a run's instance_results (the system-state banner).
         */
        RunStateCount: {
            /** State */
            state: string;
            /** Count */
            count: number;
        };
        /**
         * RunTimeline
         * @description GET /runs/{run_id}/timeline — the raw timeline read (timeline plan §4.4, 2026-09-04).
         *
         *     Every list is verbatim rows from Aurora with ISO timestamps; the exporter, not this
         *     endpoint, produces the site's columnar / decimated / scrubbed files. ``window_end`` is
         *     None while the run is open. Every measured number is nullable — None is "not measured
         *     at that tick", never 0.
         */
        RunTimeline: {
            /** Run Id */
            run_id: string;
            /** Status */
            status: string;
            /** Window Start */
            window_start: string | null;
            /** Window End */
            window_end: string | null;
            /** Stamps */
            stamps: {
                [key: string]: unknown;
            };
            /** Targets */
            targets: {
                [key: string]: string;
            }[];
            /** Tick Interval S */
            tick_interval_s: number;
            /** Ticks */
            ticks: {
                [key: string]: unknown;
            }[];
            /** Capacity */
            capacity: {
                [key: string]: unknown;
            }[];
            /** Events */
            events: {
                [key: string]: unknown;
            }[];
            /** Limit Edits */
            limit_edits: {
                [key: string]: unknown;
            }[];
            /** Discovery */
            discovery: {
                [key: string]: unknown;
            }[];
            /** Lane Rows */
            lane_rows: {
                [key: string]: unknown;
            }[];
            /** Calls */
            calls: {
                [key: string]: unknown;
            }[];
        };
        /** SkippedInstance */
        SkippedInstance: {
            /** Instance Id */
            instance_id: string;
            /** Reason */
            reason: string;
        };
        /** ValidatedInstance */
        ValidatedInstance: {
            /** Instance Id */
            instance_id: string;
            /** Attempt Number */
            attempt_number: number;
        };
        /** ValidationError */
        ValidationError: {
            /** Location */
            loc: (string | number)[];
            /** Message */
            msg: string;
            /** Error Type */
            type: string;
            /** Input */
            input?: unknown;
            /** Context */
            ctx?: Record<string, never>;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    health_health_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Health"];
                };
            };
        };
    };
    get_control_control_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ControlView"];
                };
            };
        };
    };
    pause_control_pause_post: {
        parameters: {
            query?: {
                /** @description why the operator paused (audit trail) */
                reason?: string;
                /** @description who issued the pause */
                actor?: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": string[];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ControlMutation"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    resume_control_resume_post: {
        parameters: {
            query?: {
                actor?: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": string[];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ControlMutation"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    abort_run_runs__run_id__abort_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                } | null;
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AbortReport"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    close_run_route_runs__run_id__close_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CloseReport"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    restart_instances_route_runs__run_id__restart_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RestartReport"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    regrade_instances_route_runs__run_id__regrade_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RegradeReport"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    validate_images_route_images_validate_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ImageValidationReport"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    llm_live_list_route_runs__run_id__llm_live_get: {
        parameters: {
            query?: {
                instance_id?: string | null;
                attempt?: number | null;
                limit?: number;
                before?: string | null;
            };
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LlmLiveList"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    llm_live_detail_route_runs__run_id__llm_live__request_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
                request_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LlmLiveDetail"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    judge_candidates_route_runs__run_id__judge_candidates_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JudgeCandidatesResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    judge_estimate_route_runs__run_id__judge_estimate_get: {
        parameters: {
            query?: {
                prune_mode?: string;
                /** @description comma-separated; omit for all eligible */
                instance_ids?: string | null;
                /** @description count candidates that already have a judgment */
                rejudge?: boolean;
                /** @description also count attempts whose latest judgment timed out / failed to parse */
                retry_no_verdict?: boolean;
            };
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JudgeEstimateResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    judge_launch_route_runs__run_id__judge_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["JudgeLaunchRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JudgeLaunchStarted"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    judge_results_route_runs__run_id__judge_results_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JudgeResultsResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    judge_passes_route_runs__run_id__judge_passes_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JudgePassesResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    judge_live_route_runs__run_id__judge_live_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JudgeLiveResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    judge_calibration_review_route_runs__run_id__judge_results__instance_id___attempt_number__review_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
                instance_id: string;
                attempt_number: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CalibrationReviewRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CalibrationReviewRecorded"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    judge_review_history_route_runs__run_id__judge_results__instance_id___attempt_number__reviews_get: {
        parameters: {
            query: {
                judged_at: string;
            };
            header?: never;
            path: {
                run_id: string;
                instance_id: string;
                attempt_number: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CalibrationReviewHistoryResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    judge_calibration_summary_route_judge_calibration_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CalibrationSummaryResponse"];
                };
            };
        };
    };
    pause_gateway_route_runs__run_id__pause_gateway_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                } | null;
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GatewayPauseReport"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    resume_gateway_route_runs__run_id__resume_gateway_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                } | null;
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GatewayPauseReport"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_model_ceilings_model_ceilings_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModelCeiling"][];
                };
            };
        };
    };
    estimate_discovery_cost_route_model_ceilings__model_alias__estimate_get: {
        parameters: {
            query?: {
                target_concurrency?: number;
                ramp_mode?: string;
                target_tasks?: number;
            };
            header?: never;
            path: {
                model_alias: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DiscoverCeilingStarted"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    discover_model_ceiling_route_model_ceilings__model_alias__discover_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                model_alias: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DiscoverCeilingRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DiscoverCeilingStarted"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    manual_model_ceiling_route_model_ceilings__model_alias__manual_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                model_alias: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ManualCeilingRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModelCeiling"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_limits_limits_get: {
        parameters: {
            query?: {
                run_id?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LimitsView"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    set_run_limit_limits_run_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LimitEditRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LimitEditResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    set_global_limit_limits_global_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LimitEditRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LimitEditResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    set_pacer_limit_limits_pacer__alias__post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                alias: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PacerLimitEditRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LimitEditResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_runs_runs_get: {
        parameters: {
            query?: {
                limit?: number;
                offset?: number;
                /** @description filter by runs.status */
                status?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunList"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_run_runs_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RunLaunchRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_dataset_instances_dataset_instances_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DatasetInstancesResponse"];
                };
            };
        };
    };
    get_harnesses_harnesses_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HarnessesResponse"];
                };
            };
        };
    };
    get_models_models_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModelsResponse"];
                };
            };
        };
    };
    get_instruction_presets_launch_instruction_presets_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["InstructionPresetsResponse"];
                };
            };
        };
    };
    get_run_runs__run_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunDetail"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_run_progress_runs__run_id__progress_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunProgress"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_run_live_runs__run_id__live_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunLive"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_run_pacer_runs__run_id__pacer_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunPacer"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_autoscaler_decision_autoscaler__pool__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                pool: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AutoscalerDecision"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_instance_calls_runs__run_id__instances__instance_id___attempt_number__calls_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
                instance_id: string;
                attempt_number: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["InstanceCalls"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_run_timeline_runs__run_id__timeline_get: {
        parameters: {
            query?: {
                /** @description ISO timestamp; ticks and capacity rows at or after it only */
                since?: string | null;
                /** @description include the run's llm_calls rows (the per-lane timelines) */
                include_calls?: boolean;
            };
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunTimeline"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_run_export_runs__run_id__export_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunExport"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_run_instances_runs__run_id__instances_get: {
        parameters: {
            query?: {
                /** @description filter by instance state */
                state?: string | null;
                /** @description filter by error taxonomy category (§9.3) */
                error_category?: string | null;
                limit?: number;
                offset?: number;
            };
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["InstancesList"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_instance_instances__run_id___instance_id___attempt__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
                instance_id: string;
                attempt: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["InstanceDetail"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_capacity_capacity_get: {
        parameters: {
            query?: {
                pool?: string | null;
                /** @description ISO timestamp; fetch only newer */
                since?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CapacityList"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_queues_queues_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["QueuesList"];
                };
            };
        };
    };
    get_artifact_artifacts__run_id___instance_id___attempt___kind__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
                instance_id: string;
                attempt: number;
                kind: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
}
