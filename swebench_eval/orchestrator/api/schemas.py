"""Typed API response bodies (ADR-0009 — FastAPI's OpenAPI drives the frontend
types via ``openapi-typescript``, so these models ARE the UI contract).

Everything here is read-oriented or a mutation *report*: the API never writes
anything except through the existing control endpoints.  Models are deliberately
permissive (``| None`` on every column that the schema allows to be NULL) so a
live row can never 500 a response that an honest NULL would render correctly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Health(BaseModel):
    status: str


class ControlView(BaseModel):
    """GET /control — the fail-closed control surface (ADR-0034 M1.10).

    ``stale`` is the UI's third state: True means the response was built from a
    missing/expired Valkey key or a dead publisher and every pool reads paused
    because we cannot confirm otherwise — never a confident "paused".
    """

    harness_paused: bool
    eval_paused: bool
    gateway_paused: bool
    published_at: float
    stale: bool
    aborted_runs: list[str]
    updated_by: str = ""
    reason: str = ""


class ControlMutation(BaseModel):
    """POST /control/pause|resume — what the mutation did (the audit trail)."""

    pools: list[str]
    paused: bool
    reason: str = ""
    actor: str = "operator"


class AbortReport(BaseModel):
    """POST /runs/{run_id}/abort — the drain report, not a completion signal.

    ``settled`` is False while the drain is still bounded by stopTimeout +
    upload + the results queue settling; the UI must keep polling until the run
    reaches a terminal state instead of flipping to "aborted" on this 200
    (builder2-operator-dashboard-phase1.md §4a-b).
    """

    run_id: str
    status: str = "aborting"
    scope: str = "harness"
    reason: str = ""
    actor: str = "operator"
    in_flight_stopped: int = 0
    drained: int = 0
    drain_skipped: bool = False
    drain_skip_reason: str = ""
    settled: bool = False
    swept: int = 0
    abort_not_instant: bool = True
    # The reviewer's §2.5 finding (2026-09-01): main.py has passed note= since the restart
    # build, but no such field existed here — pydantic (extra=ignore) silently dropped the
    # kwarg, so the "abort is bounded by..." caveat never reached any API response. The
    # field, not the call site, was the missing half.
    note: str = ""


class CloseReport(BaseModel):
    """POST /runs/{run_id}/close — deliberate finalisation (BUILDER4-MANUAL-
    RESTART-DESIGN-V2-2026-08-29.md §1). The only path that revokes the run's
    keys; never automatic."""

    run_id: str
    status: str = "completed"


class RestartedInstance(BaseModel):
    instance_id: str
    attempt_number: int
    retry_reason: str


class SkippedInstance(BaseModel):
    instance_id: str
    reason: str


class RestartReport(BaseModel):
    """POST /runs/{run_id}/restart — per-instance outcome, never a whole-batch
    failure for an individual instance being ineligible (§2.2 of the v2
    design: unknown/in-flight/paused instances are skipped with a reason, not
    a 400 for the whole request)."""

    run_id: str
    restarted: list[RestartedInstance] = []
    skipped: list[SkippedInstance] = []


class RegradedInstance(BaseModel):
    instance_id: str
    attempt_number: int
    patch_s3_key: str


class RegradeReport(BaseModel):
    """POST /runs/{run_id}/regrade — re-grade the EXISTING patch as an
    eval-only attempt N+1 (2026-09-01, the EVAL-GRADE-RESOURCE-LIMITS
    follow-up).  The remedy for an eval-side failure (EVAL_OOM_KILLED,
    ABANDONED, dead-lettered) that /restart cannot give: no new model spend,
    the same diff graded again.  Same skip-with-reason shape as /restart."""

    run_id: str
    regraded: list[RegradedInstance] = []
    skipped: list[SkippedInstance] = []


class ValidatedInstance(BaseModel):
    instance_id: str
    attempt_number: int


class ImageValidationReport(BaseModel):
    """POST /images/validate — grade the dataset's GOLD patch in each selected
    instance's -inst image (dev/IMAGE-PARITY-ROOT-CAUSE-AND-FIX-2026-09-05
    Part B).  Rows land under the synthetic run ``image-validation`` as
    ordinary eval attempts (retry_reason 'image_validation'), so the run
    detail page shows the outcome; a gold that does not RESOLVE is an
    environment defect in that image, never a model result.  No model spend."""

    run_id: str
    validated: list[ValidatedInstance] = []
    skipped: list[SkippedInstance] = []


class ModelCeiling(BaseModel):
    """One row of ``GET /model-ceilings`` — BUILDER4-AUTOSCALER-TPM-CEILING-DISCOVERY-DESIGN-
    2026-08-31.md §5. Backed by the ``model_ceilings`` view (§3), so ``None`` here means "no
    qualifying observation yet", not a missing row — the UI's age/staleness display (§4.1) reads
    ``discovered_at`` directly, never inferring health from its absence."""

    model_alias: str
    discovered_tpm: int | None = None  # headline: the burst-admission edge (or legacy tpm rows)
    ceiling_source: str | None = (
        None  # 'discovery_initial' | 'manual' | 'reconciliation_peak' | ...
    )
    discovered_at: str | None = None  # ISO 8601; None when no observation exists yet
    provider: str | None = None  # the pinned provider pool the constants describe
    values: dict[str, int] | None = None  # every measured value_kind -> value (multi-axis, §6)
    is_stale: bool = False  # past TPM_CEILING_MAX_AGE_DAYS (§4.1) — never used as-is when True


class DiscoverCeilingRequest(BaseModel):
    """POST /model-ceilings/{model_alias}/discover body. ``target_concurrency`` defaults to the
    owner's stated real-fleet target (§2 of the design doc's cost-driven top-down revision) —
    never an unbounded doubling ramp."""

    target_concurrency: int = 150
    triggered_by: str = "operator"
    # 2026-09-05 (owner): target-first ramp — step 0 offers the rate `target_tasks` agent tasks
    # need and a clean step ends the ramp; strain steps down 25 %. "bottom_up" is the older
    # x1.5-per-step climb. 60 = twice the borrowed-curve cap, the most one 60-call step offers.
    ramp_mode: str = "target_first"
    target_tasks: int = 60


class DiscoverCeilingStarted(BaseModel):
    model_alias: str
    target_concurrency: int
    estimated_cost_usd: float
    ramp_mode: str | None = None
    target_tasks: int | None = None
    status: str = "started"


class ManualCeilingRequest(BaseModel):
    tpm_value: int
    notes: str | None = None


class GatewayPauseReport(BaseModel):
    """POST /runs/{run_id}/pause-gateway|resume-gateway (BUILDER4-GATEWAY-
    PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §5). ``gateway_key_blocked_by`` is
    ``'operator'`` after a pause, ``None`` after a resume — an explicit
    per-run action always wins over whatever the global flag currently is."""

    run_id: str
    gateway_key_blocked_by: str | None


class LaunchLimits(BaseModel):
    """The limits a run was launched with (run_launch.RunConfig, echoed from
    ``runs.config_snapshot`` — 2026-09-08). Every field is optional: None means the
    snapshot did not record it, never a default."""

    timeout_seconds: int | None = None
    max_tokens_per_instance: int | None = None
    max_cost_usd_per_instance: float | None = None
    max_turns_per_instance: int | None = None
    context_window_tokens: int | None = None
    max_parallel_harness_tasks: int | None = None
    ramp_step_pct: float | None = None
    ramp_cooldown_seconds: float | None = None
    autoscaler_enabled: bool | None = None
    initial_budget_override: dict[str, float] | None = None
    # 2026-09-09: not a limit — the operator text appended to every job's problem statement
    # (harness_instructions). Echoed here so a run with instructions is visibly different.
    harness_instructions: str | None = None


class Provenance(BaseModel):
    """Run provenance composed from ``runs.config_snapshot`` (architecture.md §8),
    NOT from run_summary.summary_json — see BUILDER2-UI-ISSUES-HANDOVER §A, resolved
    as option (b). Every field NULLable: a pre-image or partial run legitimately
    lacks some, never invented."""

    framework_sha: str | None = None
    swebench_version: str | None = None
    # ADR-0043's pin is the PAIR (dataset name + revision) plus the committed
    # image-digest snapshot the -inst images were built from (2026-09-06).
    dataset_name: str | None = None
    dataset_revision: str | None = None
    image_digest_snapshot: str | None = None
    harness_image_digest: str | None = None
    gateway_config_hash: str | None = None
    model_resolved: str | None = None
    resolved_models: dict[str, Any] | None = None
    context_window_tokens: int | None = None
    context_window_source: str | None = None


class RunItem(BaseModel):
    """One row of the run list (runs + its maintained ``run_summary`` blob)."""

    run_id: str
    status: str
    created_at: str | None
    estimated_cost_usd: float | None
    cost_confidence_tier: str | None
    compute_cost_estimated_usd: float | None
    compute_cost_reconciled_usd: float | None
    budget_cap_usd: float | None
    stop_requested_at: str | None
    stop_scope: str | None
    stop_reason: str | None
    stopped_at: str | None
    summary: dict[str, Any] | None
    # BUILDER2-UI-ISSUES-HANDOVER-2026-09-02.md buckets A/B: harness + model from
    # run_targets, provenance from config_snapshot, actual inference spend summed
    # from instance_results, distinct-instance count (never double-counting the
    # harness+eval phase rows), and the run's dispatch/finalise timestamps.
    harness: str | None = None
    model_alias: str | None = None
    provenance: Provenance | None = None
    # 2026-09-08: the launch-time limits (run_launch.RunConfig, from config_snapshot) — what
    # the run is bounded by, visible without the launch screen. None on pre-run-launch rows;
    # each field None when the snapshot lacks it, never a default invented by the API.
    launch_limits: LaunchLimits | None = None
    cost_usd_total: float | None = None
    instance_count: int | None = None
    dispatched_at: str | None = None
    finalised_at: str | None = None
    # computed (phase-2 §1a): same rule as RunDetail.terminal — aborted, or
    # instance rows exist and none are active.  Never read off runs.status,
    # which does not record a clean completion; the frontend stops polling a
    # run list once every run is terminal.
    terminal: bool


class RunStateCount(BaseModel):
    """One per-state bucket of a run's instance_results (the system-state banner)."""

    state: str
    count: int


class RunDetail(RunItem):
    """GET /runs/{run_id} — run metadata + maintained summary + live state counts.

    ``terminal`` is computed here (no living instance rows, or the abort
    finalised) because nothing in ``runs.status`` records a clean completion.
    """

    states: list[RunStateCount]
    # Per INSTANCE (latest attempt; eval row over harness row) — the run card's
    # buckets. ``states`` stays the per-(attempt, phase) row count (2026-09-06).
    instance_states: list[RunStateCount] = []
    terminal: bool
    # BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md M2/§3: computed live,
    # never stored — see queries.get_run.
    resolve_rate_denominator: int
    ready_to_close: bool
    # BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §2: NULL/'global'/
    # 'operator' — lets the UI show this run's current per-run gateway-pause
    # state and choose pause vs. resume, not just fire one blind action.
    gateway_key_blocked_by: str | None


class InstanceItem(BaseModel):
    """One instance_results row — columns the dashboard actually renders.

    Phase timings (ADR-0037) and contamination signals (ADR-0038) are all
    NULLable on purpose: NULL means "not measured", never an invented number.
    """

    run_id: str
    instance_id: str
    attempt_number: int
    phase: str
    state: str
    error_category: str | None
    error_detail: str | None
    verdict: str | None
    wall_clock_harness_s: float | None
    wall_clock_eval_s: float | None
    touches_test_files: bool | None
    patch_path: str | None
    trajectory_path: str | None
    raw_log_path: str | None
    report_path: str | None
    report_json: dict[str, Any] | None
    # dev/BUILDER4-EXPOSE-MISSING-ARTIFACTS-2026-08-28.md
    native_trajectory_s3_key: str | None
    test_output_s3_key: str | None
    run_log_s3_key: str | None
    # BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md M3: NULL = original
    # dispatch; 'operator_infra_retry' / 'operator_rerun_pass_at_k' otherwise.
    retry_reason: str | None
    created_at: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    # turns_used: the shim's authoritative turn meter (harness_worker), previously
    # reachable only via /export — surfaced for the Run/Instance turn-count display.
    turns_used: int | None
    # BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5: the pacer's per-instance rollup.
    paced_wait_ms_total: int | None = None
    paced_calls: int | None = None
    pacer_timeouts: int | None = None
    overload_retries_total: int | None = None
    adapter_input_tokens: int | None
    adapter_output_tokens: int | None
    adapter_cost_usd: float | None
    agent_s: float | None
    task_observed_s: float | None
    task_billed_s: float | None
    repo_prep_s: float | None
    eval_test_s: float | None
    # ADR-0037 / M0 §4-§5 phase-timing breakdown + cache/cold flags — persisted
    # by the workers on every attempt, previously reachable only via /export.
    queue_wait_s: float | None
    provision_s: float | None
    image_pull_s: float | None
    worker_boot_s: float | None
    patch_extract_s: float | None
    artifact_upload_s: float | None
    repo_prep_cache_hit: bool | None
    image_pull_cold: bool | None
    eval_queue_wait_s: float | None
    eval_patch_fetch_s: float | None
    eval_image_pull_s: float | None
    eval_log_upload_s: float | None
    eval_image_pull_cold: bool | None
    # METERING-COMPLETENESS: which meter produced cost_usd — 'provider' vs a
    # local_pricing fallback estimate; NULL = no cost recorded at all.
    cost_source: str | None
    stripped_test_paths: list[str] | None
    grade_invalid: bool | None
    leaked_node_ids: list[str] | None
    gold_patch_similarity: float | None
    leak_detectable: bool | None


class RunList(BaseModel):
    items: list[RunItem]
    total: int
    limit: int
    offset: int


class InstancesList(BaseModel):
    items: list[InstanceItem]
    total: int
    limit: int
    offset: int


class InstanceDetail(BaseModel):
    """GET /instances/{run_id}/{instance_id}/{attempt} — the phase rows for one attempt."""

    run_id: str
    instance_id: str
    attempt_number: int
    rows: list[InstanceItem]


class CapacityPoint(BaseModel):
    """One observation tick — feeds the Axis A/B charts (Recharts).

    Written by the capacity observer (CAPACITY-AND-PIPELINE-VIEW-DESIGN-2026-08-31.md §3.1),
    with or without an autoscaler running. None = not measured, never zero. ETAs are
    median–p90 ranges labelled estimates, never points."""

    ts: str
    pool: str
    queue_depth: int | None
    not_visible: int | None = None
    gateway_headroom: int | None  # permanently NULL — kept for chart back-compat only
    current_workers: int | None
    desired: int | None
    binding_constraint: str | None = None
    ceiling_utilization: float | None = None
    decision_age_s: float | None = None
    eta_low_s: int | None = None
    eta_high_s: int | None = None
    # Planner-constants provenance (harness rows): 'pacer_cfg' | 'pacer_cfg_stale' |
    # 'defaults'. 'defaults' renders as a degraded state — decisions computed from
    # unmeasured constants cannot support the observe->live comparison.
    constants_source: str | None = None
    # BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5 (harness rows): live pacer pressure
    # at the tick — max wait-queue depth over active aliases, and the share of the last
    # 60 s of admissions that waited > 2 s. None = not measured, never 0.
    pacer_queue_len: int | None = None
    paced_over_2s_share: float | None = None
    # Forecast review 2026-09-03: the pool's full decision record at the tick (per-alias
    # ceilings/bindings/curve sources, booting, timeouts, queue) — the "why" behind
    # desired/binding_constraint. None when no autoscaler record existed.
    decision: dict[str, Any] | None = None


class AutoscalerDecision(BaseModel):
    """GET /autoscaler/{pool} — the LIVE decision record straight from Redis (the capacity
    snapshot is the 30 s durable copy; this is the freshest verdict for the dashboard).
    ``state`` is ``ok`` (record present), ``absent`` (no record — no planner running, or its
    TTL expired), or ``unknown`` (Redis unreachable). ``age_s`` is how old the verdict is —
    never render an old record as a current one."""

    pool: str
    state: str
    age_s: float | None = None
    record: dict[str, Any] | None = None


class CapacityList(BaseModel):
    items: list[CapacityPoint]


class PhaseStateCount(BaseModel):
    """One per-phase, per-state bucket of a run's progress (M2.4).

    Mirrors ``RunStateCount`` but carries the phase, because the same state
    name exists in both the harness and eval phases and conflation is exactly
    the confusion this endpoint exists to separate.
    """

    phase: str
    state: str
    count: int


class RunProgress(BaseModel):
    """GET /runs/{id}/progress — per-phase, per-state counts, plus denominators.

    ``expected`` is the dispatch plan (instances × attempts — at k=3 a
    300-instance run is **900 agent runs**).  ``denominator`` is gradeable, the
    honesty denominator (ADR-0034 M1.8) — the two must never be conflated: a
    run aborted at 60 of 900 must report its resolves over 60, not 900.
    """

    run_id: str
    status: str
    terminal: bool
    phases: list[PhaseStateCount]
    expected: int | None
    denominator: int | None


# ── publication export (M6.2) — the site/data/runs/<run_id>.json contract ───
# Build to publication-site-design.md §5.1 EXACTLY.  Every numeric field is
# nullable: a null renders as "not measured" and must never be coerced to zero
# (an uninstrumented harness would otherwise chart as free and infinitely
# efficient).  These models are the API shape; the aggregation lives in
# swebench_eval/orchestrator/export.py.


class ExportLimits(BaseModel):
    """provenance.limits — the ceilings actually enforced."""

    max_tokens: int | None
    max_cost_usd_per_instance: float | None
    attempts_per_instance: int | None
    temperature: float | None


class ExportProvenance(BaseModel):
    """provenance — what produced the numbers (never omit model_resolved)."""

    run_id: str
    created_at: str | None
    framework_sha: str | None
    swebench_version: str | None
    # ADR-0043 pin halves + the one-line triple (2026-09-06). response_model
    # filtering DROPS anything not declared here — the first 500-gate export
    # came back without them although export._provenance emitted them.
    dataset_name: str | None = None
    dataset_revision: str | None
    image_digest_snapshot: str | None = None
    pin: str | None = None
    harness_image_digest: str | None
    gateway_config_hash: str | None
    model_alias: str | None
    model_resolved: str | None
    harness: str | None
    harness_cli_version: str | None
    network_posture: str
    limits: ExportLimits


class ExportTotals(BaseModel):
    """totals — both resolve rates always (ADR-0038), Wilson ci95, pass@k."""

    attempted: int
    gradeable: int
    resolved: int
    resolve_rate_attempted: float | None
    resolve_rate_gradeable: float | None
    ci95_attempted: list[float] | None
    ci95_gradeable: list[float] | None
    pass_at_k: dict[str, float | None]
    cost_usd_total: float | None
    compute_cost_usd_total: float | None
    tokens: dict[str, int | None]


class ExportInstance(BaseModel):
    """One (instance, attempt) row of the export's instances list."""

    instance_id: str
    attempt: int
    verdict: str | None
    error_category: str | None
    terminated_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    agent_s: float | None
    task_billed_s: float | None
    leak_detectable: bool | None
    leaked: bool | None
    touches_test_files: bool
    gold_patch_similarity: float | None


class RunExport(BaseModel):
    """GET /runs/{run_id}/export — the full publication artifact (M6.2).

    ``timing_p50_s`` and ``integrity`` are keyed dicts whose values are
    nullable numerics (median / counts) — the pydantic models keep the exact
    keys in the docs below while allowing each to be null.
    """

    schema_version: int
    provenance: ExportProvenance
    totals: ExportTotals
    terminated_reasons: dict[str, int]
    timing_p50_s: dict[str, float | None]
    integrity: dict[str, Any]
    instances: list[ExportInstance]


class LiveInstance(BaseModel):
    """One in-flight (instance, attempt) of GET /runs/{run_id}/live (F11).

    ``state`` is explicit so a missing Redis key never renders as "0 turns"
    (BUILDER1-EXPORT-AND-LIVE-ENDPOINTS-2026-08-31.md §2):

      * ``"running"`` — the Redis progress key is live; turn/tokens/cost/age
        are real observations;
      * ``"pending"`` — the attempt has not reached a RUNNING state and has no
        key (not started, or TTL expired before the first write);
      * ``"stale"`` — the attempt reached a RUNNING state but its key is gone
        (TTL expired mid-run, or the worker died).  The instance is in flight
        per Postgres but invisible in Redis.

    ``observed_at`` is the payload's ``updated_at`` epoch stamp and ``age_s``
    its age at response time — freshness is a first-class value because the
    key is TTL'd and best-effort, so every number carries how old it is.
    """

    instance_id: str
    attempt_number: int
    state: str
    turn_number: int | None
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: float | None
    observed_at: float | None
    age_s: float | None
    # 2026-09-06: the eval worker's heartbeat publishes the grade's live progress
    # under the same key (redis_client.write_eval_progress) — `phase` says which
    # worker wrote the payload; the eval_* fields are None on harness payloads.
    phase: str | None = None
    eval_elapsed_s: float | None = None
    eval_lines: int | None = None
    eval_last_line: str | None = None
    # 2026-09-08: seconds since the grade's last byte of output (0 while it flows) — a hung
    # suite shows as a growing number, not an unchanged line count the reader has to notice.
    eval_silent_s: float | None = None
    # 2026-09-08: True when Postgres already holds a TERMINAL row for this attempt (the reaper
    # abandoned it) but a fresh progress key says a worker is still grading it — the
    # SIGTERM-handback / regrade case that used to be invisible here. The straggler result
    # overwrites ABANDONED when it lands (state_rank, results_writer §6.3).
    revived_after_reap: bool = False
    # BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5: the L1 pacer's cumulative footprint
    # on this instance, live. None = not in the payload (older worker / missing key), never 0.
    paced_wait_ms_total: int | None = None
    paced_calls: int | None = None
    pacer_timeouts: int | None = None
    overload_retries_total: int | None = None
    pacer_last_deny_axis: str | None = None
    pacer_last_queue_len: int | None = None


class PacerWaiter(BaseModel):
    """One call currently denied at the L1 pacer, head first: its estimated prompt tokens and
    how long it has been waiting (from its first deny)."""

    est_tokens: int
    waiting_s: float


class PacerAliasState(BaseModel):
    """GET /runs/{run_id}/pacer — one alias's live admission ledger, straight from Redis
    (BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03.md §2.5). ``measured=False`` means no
    pacer keys exist for this alias (no traffic yet / expired) and every number is None —
    never zeros that read as idle-and-healthy. Bucket levels are extrapolated to
    ``observed_at`` with the cfg refill rate. ``*_60s`` are the last 60 s of the pacer's own
    10 s counters (admissions, waits over 2 s, mean wait, real provider overloads)."""

    alias: str
    harness: str
    measured: bool
    c_burst: float | None = None
    r_tok: float | None = None
    k_inflight: float | None = None
    c_req: float | None = None
    r_qps: float | None = None
    seeded_at: float | None = None
    bucket_level: float | None = None
    bucket_fill: float | None = None
    req_level: float | None = None
    req_fill: float | None = None
    inflight_calls: int | None = None
    inflight_tokens: int | None = None
    inflight_fill: float | None = None
    queue_len: int | None = None
    head_est_tokens: int | None = None
    head_waiting_s: float | None = None
    waiters: list[PacerWaiter] = []
    admits_60s: int | None = None
    over_2s_60s: int | None = None
    mean_wait_ms_60s: float | None = None
    overloads_60s: int | None = None
    observed_at: float


class RunPacer(BaseModel):
    """GET /runs/{run_id}/pacer. ``state`` is the whole-response health, like /live:
    ``"ok"`` or ``"unknown"`` (Redis unreachable — an empty list must never read as
    "no pressure")."""

    run_id: str
    state: str
    reason: str | None = None
    items: list[PacerAliasState]


class InstanceCall(BaseModel):
    """One ``llm_calls`` row for the Calls table — the per-call wall-clock decomposition
    (design doc §2.6: preflight + paced wait + retried-attempt round-trips + backoff +
    final-attempt latency) plus the pacer's diagnostics. NULL = not measured / not retried."""

    call_index: int
    started_at: str | None
    http_status: int | None
    model_resolved: str | None
    error_type: str | None
    rate_limit_scope: str | None
    shim_preflight_ms: int | None
    paced_wait_ms: int | None
    overload_retries: int | None
    overload_backoff_ms: int | None
    retry_upstream_ms: int | None
    ttft_ms: int | None
    latency_ms: int | None
    pacer_was_queued: bool | None
    pacer_queue_len: int | None
    pacer_deny_axis: str | None
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    cost_usd: float | None


class InstanceCalls(BaseModel):
    run_id: str
    instance_id: str
    attempt_number: int
    items: list[InstanceCall]


class RunLive(BaseModel):
    """GET /runs/{run_id}/live — per-instance in-flight progress (F11).

    ``state`` is the WHOLE-response health: ``"ok"`` (Redis reachable; items
    carry per-instance states) or ``"unknown"`` (Redis itself is unreachable
    — an empty list must never read as "nothing is running").  ``reason``
    names the unknown, e.g. ``"redis_unreachable"``.
    """

    run_id: str
    status: str
    state: str = "ok"
    reason: str = ""
    items: list[LiveInstance]


class QueueView(BaseModel):
    """GET /queues — one work queue's depth + DLQ (M2.1 / M2.4).

    ``visible`` vs ``not_visible`` is the difference between "50 waiting"
    (capacity) and "50 stuck in flight" (incident); ``oldest_age_s`` is
    CloudWatch-derived and ``None`` (never 0) when unavailable.
    """

    queue: str
    visible: int
    not_visible: int
    oldest_age_s: int | None
    dlq_depth: int


class QueuesList(BaseModel):
    items: list[QueueView]


# ---------------------------------------------------------------------------
# run-launch (BUILDER4-RUN-LAUNCH-ORCHESTRATOR-2026-08-26 §3) — POST /runs +
# the three read endpoints the UI needs.
# ---------------------------------------------------------------------------


class RunLaunchRequest(BaseModel):
    """POST /runs body (§3.1).

    ``instance_ids`` is either an explicit list or the literal string
    ``"all"`` — resolved server-side (never client-side, see
    ``run_launch_routes._resolve_instances``).  ``context_window_tokens``
    defaults to ``None`` ("resolve from the gateway") — the UI must not send
    a value unless the operator deliberately overrides it; an explicit value
    is exactly what made ``context_window_source = 'run_config'`` on every
    Stage 6 run, which is why the live gateway resolution has never executed
    in production (§3.1's warning).
    """

    instance_ids: list[str] | str
    harness: str
    model_alias: str
    budget_cap_usd: float
    max_cost_usd_per_instance: float = 5.0
    # BUILDER1-METERING-COMPLETENESS-2026-08-28 §1 / owner decision: we never
    # abort on total token count — instances are bounded by per-instance cost
    # and by the turn cap. `None` is already the documented "explicitly
    # unlimited" mode, and both enforcement sites (local_proxy.py, harness_
    # worker.py) are `is not None`-guarded. Two-file change — this is builder
    # 4's half; run_config.py's `DEFAULT_MAX_TOKENS_PER_INSTANCE` is builder
    # 1's. Land together or the other silently re-imposes 500k.
    max_tokens_per_instance: int | None = None
    max_turns_per_instance: int | None = 500
    attempts_per_instance: int = 1
    # 2026-08-29 owner decision: raised from 600s (10 min) to 5400s (1h30m) —
    # some instances genuinely take over an hour to solve; the old default put
    # the reaper's rule-2 deadline within reach of a real in-progress task.
    # run_config.py's DEFAULT_TIMEOUT_SECONDS is the source of truth; this file
    # hardcodes its own literal like the two defaults above it (max_cost_usd_
    # per_instance, max_turns_per_instance) — keep all three in sync by hand.
    timeout_seconds: int = 5400
    context_window_tokens: int | None = None
    # ── Autoscaler per-run overrides (harness-autoscaler exact-design §8 / §6.7) ──
    # Defaults mirror run_config.py by hand, same convention as the three above.
    # max_parallel is the operator's per-run task ceiling (min-wins vs the dispatcher's
    # static MAX_CONCURRENT_HARNESS_TASKS); ramp_step_pct is clamped to 5 server-side
    # (owner-fixed "never more"); autoscaler_enabled=False disables only L2's dynamic
    # gate — the L1 pacer always stays on.
    max_parallel_harness_tasks: int = 150
    initial_budget_override: dict[str, float] | None = None
    ramp_step_pct: float = 5.0
    ramp_cooldown_seconds: int = 60
    autoscaler_enabled: bool = True
    # 2026-09-09 (efficiency prompt arm): operator text appended to every job's problem
    # statement at dispatch (orchestrator/harness_instructions). Advice, not a limit; recorded
    # in config_snapshot and echoed in LaunchLimits. None / blank = nothing appended.
    harness_instructions: str | None = Field(None, max_length=4000)


class InstructionPreset(BaseModel):
    """One entry of GET /launch/instruction-presets — a starting text the launch screen
    can load into the harness-instructions field (the operator may edit it)."""

    id: str
    name: str
    harness: str  # the harness the wording was written against; "" = any
    text: str


class InstructionPresetsResponse(BaseModel):
    presets: list[InstructionPreset]
    max_chars: int


class RunLaunchResponse(BaseModel):
    """201 — dispatch is inline; this returns once jobs are on the queue (§3.1)."""

    status: str = "launched"
    run_id: str
    dispatched: int
    seeded: int


class DuplicateRunResponse(BaseModel):
    """409 body — "the id is the payload, not decoration" (§3.1)."""

    status: str = "duplicate"
    run_id: str
    harness: str
    model_alias: str
    message: str


class DatasetInstanceItem(BaseModel):
    instance_id: str
    repo: str
    # 2026-08-28 (round 2, item 7): whether this instance's -inst image
    # exists in ECR right now — dispatch_run's per-instance env-image gate
    # refuses exactly the instances that don't, but silently (only at
    # dispatch time), so an operator picking freely from ~500 rows had no
    # way to know in advance which ones would actually launch.
    launchable: bool


class DatasetInstancesResponse(BaseModel):
    """GET /dataset/instances (§3.2) — enough to render a multi-select."""

    items: list[DatasetInstanceItem]
    total: int


class HarnessesResponse(BaseModel):
    """GET /harnesses (§3.2) — the names a run may use."""

    harnesses: list[str]


class ModelItem(BaseModel):
    alias: str
    max_input_tokens: int | None
    # Item 14 (BUILDER4-RESUME-2026-09-04-FIXBATCH): the pool's pacer:cfg consistency ratio
    # r_tok / (k_inflight / L_A) from the last discovery (§2.3 of the seed-and-fairness doc).
    # Below 1 the arrival bucket refills slower than the in-flight cap turns over and binds
    # first; below ~0.5 that is the starvation mode of run 01788405363237319353. None = no
    # seeded cfg for the pool (or Redis unreachable) — never rendered as a healthy number.
    consistency_ratio: float | None = None
    pacer_seeded_at: float | None = None


class ModelsResponse(BaseModel):
    """GET /models (§3.2) — live from the gateway's /model/info, never a
    hardcoded list (drift there is invisible until a run produces wrong
    numbers)."""

    items: list[ModelItem]


# --- Pass B — LLM judge (offline-analysis-design.md §9.5/§9.6/§10.1, 2026-09-01) ---


class JudgeCandidateItem(BaseModel):
    """One row of GET /runs/{run_id}/judge/candidates — the instance
    selector's eval-status column (§10.1): lets the operator exclude, e.g.,
    everything that isn't verdict='resolved' before launching."""

    instance_id: str
    attempt_number: int
    harness: str
    outcome: str  # 'resolved' | 'unresolved' | 'invalid' | a harness error_category | 'unknown'
    always_judge: (
        bool  # detector fired / grade_invalid / stuck — always selected regardless of rate
    )
    already_judged: bool  # a judge_results row already exists — a re-judge, not a first pass
    # 2026-09-08: how the latest judgment ended — 'judged' | 'timeout' | 'parse_failed' | None
    last_judgment: str | None = None


class JudgeCandidatesResponse(BaseModel):
    run_id: str
    candidates: list[JudgeCandidateItem]


class JudgeEstimateResponse(BaseModel):
    run_id: str
    candidate_count: int
    prune_mode: str
    estimated_cost_usd: float
    status: str = "estimate"


class JudgeLaunchRequest(BaseModel):
    """POST /runs/{run_id}/judge body. instance_ids=None means all eligible
    candidates (§10.1's selector; default 100% coverage per §9.3 — the
    stratified sample_rate exists for when a narrower run is deliberately
    wanted, not because 100% is unaffordable)."""

    instance_ids: list[str] | None = None
    prune_mode: str = "pruned"  # 'full' | 'pruned' | 'auto' — §9.4
    model_alias: str = "judge-model"
    sample_rate: float = 1.0
    min_per_stratum: int = 5
    seed: int | None = None
    max_spend_usd: float = 25.0
    # 2026-09-07: concurrent judge calls. 24 by default — the deepseek pool's discovery seeds
    # (k_inflight 18.7M tok, c_req 180, r_tok 245k tok/s) take 100 comfortably, but the
    # pass's finish time is its slowest calls, not its throughput, and a pool-wide 429 would
    # reset the planner's r_tok for every deepseek run. Raise per pass from the UI.
    workers: int = Field(default=24, ge=1, le=100)
    # 2026-09-08 (resume): by default a pass SKIPS candidates that already have a
    # judge_results row for this run — a failed/truncated pass is relaunched for the cost of
    # what is left. rejudge=True judges everything selected again (a new judged_at row per
    # candidate, the old rows stay — §4/§10.6).
    rejudge: bool = False
    # 2026-09-08 (owner): "regenerate report" — judge nothing, only re-run the pass-level
    # synthesis over the judgments already recorded for the run (a relaunch would do the
    # same as a side effect, but this one cannot re-judge anything by accident and needs
    # no eligible candidates). instance_ids / prune / sampling fields are ignored.
    synthesis_only: bool = False
    # 2026-09-08 (owner): also judge the attempts whose latest judgment has NO verdict —
    # the call timed out at the 10-min ceiling or the answer failed to parse — without
    # re-judging everything (rejudge). The Judge card's "retry attempts with no verdict".
    retry_no_verdict: bool = False
    triggered_by: str = "operator"


class JudgeLaunchStarted(BaseModel):
    run_id: str
    pass_id: str
    estimated_cost_usd: float
    status: str = "started"


class JudgeLiveInFlight(BaseModel):
    instance_id: str
    attempt_number: int
    started_at: float


class JudgeLiveState(BaseModel):
    """One judge pass's live progress (judge:live:{run_id}, TTL'd, best-effort): what the DB
    cannot show while the pass runs — judge_sampling is written only at the end."""

    run_id: str
    pass_id: str
    status: str  # running | synthesizing (pass report being written) | done | failed | stopped
    workers: int
    selected: int
    judged: int
    skipped_over_budget: int
    parse_failed: int
    skipped_artifacts: int
    # 2026-09-08: judge call never answered (429/5xx, retries exhausted) — the pass went on
    call_failed: int = 0
    # 2026-09-08: judged-with-no-verdict rows written for calls that hit the 10-min ceiling
    timed_out: int = 0
    # candidates skipped because a judge_results row already exists (resume; 0 on rejudge)
    already_judged: int = 0
    spend_usd: float
    max_spend_usd: float
    started_at: float
    updated_at: float
    finished_at: float | None
    elapsed_s: float
    eta_s: float | None
    in_flight_count: int
    in_flight: list[JudgeLiveInFlight]
    last_error: str | None


class JudgeLiveResponse(BaseModel):
    run_id: str
    live: JudgeLiveState | None  # None: no pass running (or its snapshot expired)


class JudgeDimensionScoreItem(BaseModel):
    """One dimension's score within one judge_results row (§10.7's
    normalized projection) — this is what the UI's per-instance rubric tab
    renders, all eight, not a collapsed verdict."""

    dimension_id: str
    scale_type: str
    score_numeric: float | None
    score_secondary: float | None
    flag: bool | None
    span_start_turn: int | None
    span_end_turn: int | None
    reasoning: str | None
    evidence: list[dict[str, Any]]
    evidence_missing: bool
    # rubric v3 (2026-09-09): the `causes` scale's classified waste —
    # [{cause, share, recommendation}]; empty for every other scale.
    causes: list[dict[str, Any]] = []


class JudgeResultItem(BaseModel):
    instance_id: str
    attempt_number: int
    judged_at: str
    judge_model_resolved: str | None
    rubric_version: str
    judge_prune_mode: str | None
    input_truncated: bool
    tool_output_pruned: bool
    judge_parse_failed: bool
    # ADR-0042: which recovery step produced the scores, and how many judge-model
    # calls it took. Null on rows written before the cascade shipped.
    judge_method: str | None = None
    judge_attempts: int | None = None
    summary: str | None
    judge_cost_usd: float | None
    dimensions: list[JudgeDimensionScoreItem]
    # rubric v3 (2026-09-09): the computed efficiency profile (analysis/efficiency.py)
    # the judge reasoned against; null on rows judged before v3 and on timeout rows.
    efficiency_profile: dict[str, Any] | None = None


class JudgeResultsResponse(BaseModel):
    run_id: str
    results: list[JudgeResultItem]


class JudgePassItem(BaseModel):
    """One judge_sampling row (review 2026-09-02 §3.2): a pass that hit the
    budget ceiling must not look like a pass that finished — the UI's
    launch-control banner reads `judged N of M eligible — K skipped at the
    $X ceiling` from this, and total_parse_failed the same way."""

    pass_id: str
    requested_rate: float | None
    seed: int | None
    total_eligible: int | None
    total_judged: int | None
    total_skipped_over_budget: int | None
    total_parse_failed: int | None
    created_at: str
    # 2026-09-08 (owner): the pass-level synthesis — markdown written by the judge model
    # over a digest of every recorded judgment for the run at the end of the pass. None
    # with synthesis_error set = skipped/failed (the pass is still complete); both None =
    # a pass from before the synthesis existed.
    synthesis: str | None = None
    synthesis_cost_usd: float | None = None
    synthesis_model_resolved: str | None = None
    synthesis_error: str | None = None
    # True for a "regenerate report" pass (judged nothing by design) — the UI's pass
    # banner should read the latest JUDGING pass, the report the latest pass that has one.
    synthesis_only: bool = False


class JudgePassesResponse(BaseModel):
    run_id: str
    passes: list[JudgePassItem]


class CalibrationReviewRequest(BaseModel):
    """POST .../judge/results/{instance_id}/{attempt_number}/review body
    (offline-analysis-design.md §11). ``judged_at`` pins the exact
    judge_results row reviewed — required because a re-judge produces a new
    row, and reviewing "whatever's latest" would silently re-target a
    future re-judge instead of what the reviewer actually looked at.
    ``reviewer_reasoning`` is required on both approve and deny."""

    judged_at: str
    dimension_id: str
    decision: str  # 'approve' | 'deny'
    reviewer_reasoning: str
    reviewed_by: str = "operator"
    corrected_score_numeric: float | None = None
    corrected_score_secondary: float | None = None
    corrected_flag: bool | None = None


class CalibrationReviewRecorded(BaseModel):
    run_id: str
    instance_id: str
    attempt_number: int
    dimension_id: str
    status: str = "recorded"


class DimensionCalibrationItem(BaseModel):
    """§11's coverage readout: one rubric dimension's calibration state,
    aggregated globally (every run) since judge-model is one alias across
    all of them. ``endorsement_rate`` is null, never 0, when nothing has
    been reviewed yet — a 0% rate and "not reviewed" are different facts."""

    dimension_id: str
    reviewed_count: int
    distinct_instances_reviewed: int
    approve_count: int
    deny_count: int
    endorsement_rate: float | None
    cleared_threshold: bool  # distinct_instances_reviewed >= 20 (§3.9's original bar)


class CalibrationSummaryResponse(BaseModel):
    dimensions: list[DimensionCalibrationItem]
    min_distinct_reviewed_instances: int


class CalibrationReviewHistoryItem(BaseModel):
    """One judge_calibration_reviews row — GET .../reviews's data source, so
    the per-instance Judge panel can show "already reviewed" state instead
    of a reviewer either re-reviewing blind or trusting their own memory
    across a page reload."""

    dimension_id: str
    decision: str
    reviewer_reasoning: str
    reviewed_by: str
    reviewed_at: str
    corrected_score_numeric: float | None
    corrected_score_secondary: float | None
    corrected_flag: bool | None


class CalibrationReviewHistoryResponse(BaseModel):
    run_id: str
    instance_id: str
    attempt_number: int
    reviews: list[CalibrationReviewHistoryItem]


class LlmLiveCall(BaseModel):
    """One light spend-log row for the live LLM-call view (llm_live.py facts).

    ``spend`` is deliberately absent — the column is always 0.0 for our custom
    models and must never be rendered as cost; the ledger's ``cost_usd`` is
    the money truth.
    """

    request_id: str
    started_at: str
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    text_tokens: int | None  # uncached slice of the prompt (cache telemetry)
    cached_tokens: int | None
    session_id: str | None
    instance_id: str | None  # x-eval-instance-id header, verbatim
    attempt: int | None
    harness: str | None
    # LiteLLM's own row status: "success" / "failure" (a provider-rejected call
    # is logged as failure WITHOUT the shim's coordinates). None on old rows.
    status: str | None = None


class LlmLiveList(BaseModel):
    run_id: str
    items: list[LlmLiveCall]


class LlmLiveDetail(BaseModel):
    request_id: str
    started_at: str
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    session_id: str | None
    instance_id: str | None
    attempt: int | None
    harness: str | None
    status: str | None = None
    # The conversation, verbatim from proxy_server_request (the messages COLUMN
    # is always {} in this LiteLLM version — llm_live.py fact 2).
    messages: list[dict[str, Any]]
    system: Any | None
    tools_count: int
    response: dict[str, Any] | None


# ── operator limits (owner request 2026-09-04; control_plane/operator_limits.py) ─────────


class LimitField(BaseModel):
    """One knob's effective value and where it came from."""

    value: float | None
    source: str  # operator | run_launch | default
    set_by: str | None = None


class LimitsRunView(BaseModel):
    run_id: str | None
    set_at: float | None
    fields: dict[str, LimitField]


class LimitsGlobalView(BaseModel):
    fields: dict[str, LimitField]


class PacerLimitRow(BaseModel):
    harness: str
    alias: str
    pool: str | None
    cfg: dict[str, float | None]
    pool_cfg: dict[str, float | None] | None


class LimitSpec(BaseModel):
    field: str
    scope: str
    kind: str
    label: str
    description: str
    lo: float | None = None
    hi: float | None = None
    default: float | None = None
    default_note: str = ""
    unit: str = ""
    read_by: str = ""


class LimitsView(BaseModel):
    """GET /limits — every operator-adjustable knob with its effective value + source, the
    static rails from the live decision records, and the pacer cfg per alias of a run."""

    state: str  # ok | unknown (Redis unreachable — nothing here is trustworthy then)
    run: LimitsRunView | None = None
    global_: LimitsGlobalView | None = None
    static: dict[str, Any] = {}
    pacer: list[PacerLimitRow] = []
    specs: list[LimitSpec] = []

    model_config = {"populate_by_name": True}


class LimitEditRequest(BaseModel):
    """POST /limits/run and /limits/global: ``value`` null clears the field (defaults)."""

    field: str
    value: float | bool | None = None
    actor: str = "operator"
    reason: str = ""


class PacerLimitEditRequest(BaseModel):
    """POST /limits/pacer/{alias}: a pacer field is set, never cleared. ``also_pool`` writes
    the pool key too and persists a pacer_cfg_seeds row (the next launch / bring-up)."""

    field: str
    value: float
    actor: str = "operator"
    reason: str = ""
    also_pool: bool = False


class LimitEditResult(BaseModel):
    scope: str
    target: str
    field: str
    old: str | None
    new: str | None
    pool: str | None = None


class RunTimeline(BaseModel):
    """GET /runs/{run_id}/timeline — the raw timeline read (timeline plan §4.4, 2026-09-04).

    Every list is verbatim rows from Aurora with ISO timestamps; the exporter, not this
    endpoint, produces the site's columnar / decimated / scrubbed files. ``window_end`` is
    None while the run is open. Every measured number is nullable — None is "not measured
    at that tick", never 0."""

    run_id: str
    status: str
    window_start: str | None
    window_end: str | None
    stamps: dict[str, Any]
    targets: list[dict[str, str]]
    tick_interval_s: int
    ticks: list[dict[str, Any]]
    capacity: list[dict[str, Any]]
    events: list[dict[str, Any]]
    limit_edits: list[dict[str, Any]]
    discovery: list[dict[str, Any]]
    lane_rows: list[dict[str, Any]]
    calls: list[dict[str, Any]]
