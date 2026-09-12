import type { components } from '../api/schema';
import { ApiError } from './apiError';
import { DATA_MODE } from './dataMode';
import { demoApi } from './demo/demoApi';

// Every type here is derived from the generated OpenAPI types (ADR-0009).
// If the backend's response models change, `npm run gen:api -- --live` and a
// rebuild surface the drift as a type error here.

type Schemas = components['schemas'];

export type RunItem = Schemas['RunItem'];
export type RunList = Schemas['RunList'];
export type RunDetail = Schemas['RunDetail'];
export type RunStateCount = Schemas['RunStateCount'];
export type InstanceItem = Schemas['InstanceItem'];
export type InstancesList = Schemas['InstancesList'];
export type InstanceDetail = Schemas['InstanceDetail'];
export type CapacityPoint = Schemas['CapacityPoint'];
export type CapacityList = Schemas['CapacityList'];
export type ControlView = Schemas['ControlView'];
export type ControlMutation = Schemas['ControlMutation'];
export type AbortReport = Schemas['AbortReport'];
export type CloseReport = Schemas['CloseReport'];
export type RestartReport = Schemas['RestartReport'];
export type RegradeReport = Schemas['RegradeReport'];
export type ImageValidationReport = Schemas['ImageValidationReport'];
export type LlmLiveCall = Schemas['LlmLiveCall'];
export type LlmLiveList = Schemas['LlmLiveList'];
export type LlmLiveDetail = Schemas['LlmLiveDetail'];
export type RestartedInstance = Schemas['RestartedInstance'];
export type SkippedInstance = Schemas['SkippedInstance'];
export type GatewayPauseReport = Schemas['GatewayPauseReport'];
export type PhaseStateCount = Schemas['PhaseStateCount'];
export type RunProgress = Schemas['RunProgress'];
export type QueueView = Schemas['QueueView'];
export type QueuesList = Schemas['QueuesList'];
export type Health = Schemas['Health'];
export type RunLive = Schemas['RunLive'];
export type LiveInstance = Schemas['LiveInstance'];
// BUILDER4-PACER-SEED-AND-FAIRNESS §2.5 — the live L1 pacer ledger per alias, and the
// per-call wall-clock decomposition from llm_calls.
export type RunPacer = Schemas['RunPacer'];
export type PacerAliasState = Schemas['PacerAliasState'];
export type PacerWaiter = Schemas['PacerWaiter'];
export type InstanceCalls = Schemas['InstanceCalls'];
export type InstanceCall = Schemas['InstanceCall'];
// Forecast review 2026-09-03 — the L2 planner's live verdict per pool.
export type AutoscalerDecision = Schemas['AutoscalerDecision'];
// Operator limits (owner request 2026-09-04) — every runtime-adjustable knob with its
// effective value + source, and the edit results.
export type LimitsView = Schemas['LimitsView'];
export type LimitField = Schemas['LimitField'];
export type LimitSpec = Schemas['LimitSpec'];
export type PacerLimitRow = Schemas['PacerLimitRow'];
export type LimitEditResult = Schemas['LimitEditResult'];

// ---- run launch (builder 4's POST /runs; UI consumes it, never owns it) ----
export type RunLaunchRequest = Schemas['RunLaunchRequest'];
export type DatasetInstanceItem = Schemas['DatasetInstanceItem'];
export type DatasetInstancesResponse = Schemas['DatasetInstancesResponse'];
export type HarnessesResponse = Schemas['HarnessesResponse'];
export type InstructionPresetsResponse = Schemas['InstructionPresetsResponse'];
export type ModelItem = Schemas['ModelItem'];
export type ModelsResponse = Schemas['ModelsResponse'];
export type ModelCeiling = Schemas['ModelCeiling'];
export type DiscoverCeilingStarted = Schemas['DiscoverCeilingStarted'];

// ---- LLM judge, Pass B (offline-analysis-design.md §9.5/§10, §11) ----
export type JudgeCandidateItem = Schemas['JudgeCandidateItem'];
export type JudgeCandidatesResponse = Schemas['JudgeCandidatesResponse'];
export type JudgeEstimateResponse = Schemas['JudgeEstimateResponse'];
export type JudgeLaunchRequest = Schemas['JudgeLaunchRequest'];
export type JudgeLaunchStarted = Schemas['JudgeLaunchStarted'];
export type JudgePassItem = Schemas['JudgePassItem'];
export type JudgePassesResponse = Schemas['JudgePassesResponse'];
export type JudgeLiveState = Schemas['JudgeLiveState'];
export type JudgeLiveResponse = Schemas['JudgeLiveResponse'];
export type JudgeDimensionScoreItem = Schemas['JudgeDimensionScoreItem'];
export type JudgeResultItem = Schemas['JudgeResultItem'];
export type JudgeResultsResponse = Schemas['JudgeResultsResponse'];
export type CalibrationReviewRequest = Schemas['CalibrationReviewRequest'];
export type CalibrationReviewRecorded = Schemas['CalibrationReviewRecorded'];
export type DimensionCalibrationItem = Schemas['DimensionCalibrationItem'];
export type CalibrationSummaryResponse = Schemas['CalibrationSummaryResponse'];
export type CalibrationReviewHistoryItem =
  Schemas['CalibrationReviewHistoryItem'];
export type CalibrationReviewHistoryResponse =
  Schemas['CalibrationReviewHistoryResponse'];

/**
 * POST /runs has no `response_model` (201/409/503 carry different shapes, and
 * FastAPI's default HTTPException would wrap the 409 in `{"detail": …}` —
 * neither is in the generated spec).  So the two non-201 shapes are declared
 * here, hand-aligned to run_launch_routes.py's contract:
 *   - 201 `{status:"launched", run_id, dispatched, seeded}`
 *   - 409 `{status:"duplicate", run_id, harness, model_alias, message}`
 * The 409's run_id is the payload, not decoration — the whole point (see the
 * launch screen: it must render that id and offer "open that run", never an
 * error toast).
 */

export interface RunLaunchResponse {
  status: string;
  run_id: string;
  dispatched: number;
  seeded: number;
}

export interface DuplicateRunResponse {
  status: string;
  run_id: string;
  harness: string;
  model_alias: string;
  message: string;
}

export type LaunchResult =
  | { kind: 'launched'; data: RunLaunchResponse }
  | { kind: 'duplicate'; data: DuplicateRunResponse }
  | { kind: 'refused'; message: string };

/** -summarised run_summary blob — the publication-shaped document (M0). */
export interface RunSummary {
  schema_version?: number;
  provenance?: {
    run_id: string;
    created_at: string;
    framework_sha?: string;
    swebench_version?: string;
    dataset_name?: string;
    dataset_revision?: string;
    image_digest_snapshot?: string;
    /** ADR-0043 triple as one line; absent unless all three halves are recorded. */
    pin?: string;
    harness_image_digest?: string;
    gateway_config_hash?: string;
    model_alias?: string;
    model_resolved?: string;
    harness?: string;
    harness_cli_version?: string;
    network_posture?: string;
    limits?: Record<string, unknown>;
  };
  totals?: {
    attempted?: number;
    gradeable?: number;
    resolved?: number;
    resolve_rate_attempted?: number;
    resolve_rate_gradeable?: number;
    pass_at_k?: Record<string, number>;
    cost_usd_total?: number;
    compute_cost_usd_total?: number;
    tokens?: {
      input?: number;
      output?: number;
      cached?: number | null;
      reasoning?: number | null;
    };
  };
  terminated_reasons?: Record<string, number>;
  timing_p50_s?: Record<string, number>;
  integrity?: {
    leak_detectable_instances?: number;
    leaked_instances?: number;
    touches_test_files?: number;
    grade_invalid?: number;
    gold_patch_similarity_p50?: number;
  };
  instances?: Array<{
    instance_id: string;
    attempt: number;
    verdict: string;
    error_category: string | null;
    terminated_reason?: string;
    input_tokens?: number;
    output_tokens?: number;
    cost_usd?: number;
    agent_s?: number;
    task_billed_s?: number;
    gold_patch_similarity?: number;
    leaked?: boolean;
  }>;
}

/** The dashboard SPA always talks to one origin (the Vite proxy in dev, a
 * reverse proxy in prod forwards /api -> the orchestrator API). Every call is
 * against `API_BASE`, so there is exactly one seam to swap — and it is swapped
 * exactly once, at the bottom of this file: `VITE_DATA_MODE=demo` selects the
 * replay adapter (`./demo/demoApi`) in place of `realApi`. */
const API_BASE = '/api';

export { ApiError };

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && typeof body.detail === 'string') detail = body.detail;
      else if (body && Array.isArray(body.detail))
        detail = JSON.stringify(body.detail);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

function qs(
  params: Record<string, string | number | boolean | undefined>,
): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : '';
}

const realApi = {
  // ---- control surface (M1.10; polled quickly, Valkey-backed) ----
  getControl: (): Promise<ControlView> =>
    fetch(`${API_BASE}/control`).then(handle<ControlView>),

  pause: (
    pools: string[],
    reason: string,
    actor: string,
  ): Promise<ControlMutation> =>
    fetch(`${API_BASE}/control/pause${qs({ reason, actor })}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pools),
    }).then(handle<ControlMutation>),

  resume: (pools: string[], actor: string): Promise<ControlMutation> =>
    fetch(`${API_BASE}/control/resume${qs({ actor })}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pools),
    }).then(handle<ControlMutation>),

  abort: (
    runId: string,
    opts: { scope: string; reason: string; actor: string },
  ): Promise<AbortReport> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/abort`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(opts),
    }).then(handle<AbortReport>),

  // ---- manual close + reviewed restart (BUILDER4-MANUAL-RESTART-DESIGN-V2-
  // 2026-08-29.md) — no auto-resume, no auto-close; both are deliberate
  // operator actions the same trust level as abort. ----
  closeRun: (runId: string): Promise<CloseReport> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/close`, {
      method: 'POST',
    }).then(handle<CloseReport>),

  restartInstances: (
    runId: string,
    instanceIds: string[],
    actor: string,
  ): Promise<RestartReport> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/restart`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instance_ids: instanceIds, actor }),
    }).then(handle<RestartReport>),

  // Regrade (EVAL-GRADE-RESOURCE-LIMITS follow-up, 2026-09-01): grade the
  // EXISTING patch again as an eval-only attempt N+1 — no model spend, so
  // no arm/confirm step in the UI (restart's confirm exists because restart
  // launches real inference; a regrade cannot).
  regradeInstances: (
    runId: string,
    instanceIds: string[],
    actor: string,
  ): Promise<RegradeReport> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/regrade`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instance_ids: instanceIds, actor }),
    }).then(handle<RegradeReport>),

  // Image validation (IMAGE-PARITY-ROOT-CAUSE-AND-FIX-2026-09-05 Part B):
  // grade the dataset's GOLD patch in each instance's -inst image, under the
  // synthetic run "image-validation". No model spend, so no confirm step; a
  // gold that does not RESOLVE is an environment defect in that image.
  validateImages: (
    instanceIds: string[],
    actor: string,
  ): Promise<ImageValidationReport> =>
    fetch(`${API_BASE}/images/validate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instance_ids: instanceIds, actor }),
    }).then(handle<ImageValidationReport>),

  // Live LLM-call view (BUILDER4-LITELLM-SPEND-DB-LIVE-VIEW-2026-09-02):
  // rows straight from LiteLLM's spend log, seconds behind the call — the
  // only trajectory that exists WHILE an attempt is still running. A 503
  // means the spend DB isn't wired/reachable; render it as a degraded
  // notice, never as an empty trajectory.
  llmLiveCalls: (
    runId: string,
    opts: {
      instanceId?: string;
      attempt?: number;
      limit?: number;
      before?: string;
    } = {},
  ): Promise<LlmLiveList> => {
    const params = new URLSearchParams();
    if (opts.instanceId) params.set('instance_id', opts.instanceId);
    // per-attempt scoping (2026-09-06): the instance page is one attempt; without
    // this a restarted instance's page showed the previous attempt's calls.
    if (opts.attempt != null) params.set('attempt', String(opts.attempt));
    if (opts.limit) params.set('limit', String(opts.limit));
    if (opts.before) params.set('before', opts.before);
    const qs = params.toString();
    return fetch(
      `${API_BASE}/runs/${encodeURIComponent(runId)}/llm-live${qs ? `?${qs}` : ''}`,
    ).then(handle<LlmLiveList>);
  },

  llmLiveDetail: (runId: string, requestId: string): Promise<LlmLiveDetail> =>
    fetch(
      `${API_BASE}/runs/${encodeURIComponent(runId)}/llm-live/${encodeURIComponent(requestId)}`,
    ).then(handle<LlmLiveDetail>),

  // ---- per-run gateway pause/resume (BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-
  // DESIGN-2026-08-31.md §5) — blocks/unblocks THIS run's LiteLLM key,
  // independent of the global "gateway" pool toggle above (§2's precedence
  // table: an explicit per-run action always wins for that one run). ----
  pauseGateway: (
    runId: string,
    actor = 'operator',
  ): Promise<GatewayPauseReport> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/pause-gateway`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ actor }),
    }).then(handle<GatewayPauseReport>),

  resumeGateway: (
    runId: string,
    actor = 'operator',
  ): Promise<GatewayPauseReport> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/resume-gateway`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ actor }),
    }).then(handle<GatewayPauseReport>),

  // ---- model TPM-ceiling discovery (BUILDER4-HARNESS-AUTOSCALER-EXACT-
  // DESIGN-2026-09-01.md §6) — MANUAL ONLY: this UI is the single trigger;
  // nothing in the system may launch discovery automatically. ----
  listModelCeilings: (): Promise<ModelCeiling[]> =>
    fetch(`${API_BASE}/model-ceilings`).then(handle<ModelCeiling[]>),

  // 2026-09-05: target-first ramp — `targetTasks` is the fleet size the probe proves a rate
  // for (default 60); `rampMode` 'target_first' (step 0 at that rate, step DOWN on strain)
  // or 'bottom_up' (the older x1.5 climb).
  estimateDiscovery: (
    modelAlias: string,
    targetConcurrency: number,
    targetTasks = 60,
    rampMode: 'target_first' | 'bottom_up' = 'target_first',
  ): Promise<DiscoverCeilingStarted> =>
    fetch(
      `${API_BASE}/model-ceilings/${encodeURIComponent(modelAlias)}/estimate${qs(
        {
          target_concurrency: targetConcurrency,
          target_tasks: targetTasks,
          ramp_mode: rampMode,
        },
      )}`,
    ).then(handle<DiscoverCeilingStarted>),

  discoverCeiling: (
    modelAlias: string,
    targetConcurrency: number,
    targetTasks = 60,
    rampMode: 'target_first' | 'bottom_up' = 'target_first',
  ): Promise<DiscoverCeilingStarted> =>
    fetch(
      `${API_BASE}/model-ceilings/${encodeURIComponent(modelAlias)}/discover`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          target_concurrency: targetConcurrency,
          target_tasks: targetTasks,
          ramp_mode: rampMode,
        }),
      },
    ).then(handle<DiscoverCeilingStarted>),

  manualCeiling: (
    modelAlias: string,
    tpmValue: number,
    notes?: string,
  ): Promise<ModelCeiling> =>
    fetch(
      `${API_BASE}/model-ceilings/${encodeURIComponent(modelAlias)}/manual`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tpm_value: tpmValue, notes }),
      },
    ).then(handle<ModelCeiling>),

  // ---- LLM judge, Pass B (offline-analysis-design.md §9.5/§10) — the launch
  // control's estimate/confirm pattern mirrors model-ceiling discovery above:
  // real spend, so no button fires without a live estimate shown first. ----
  judgeCandidates: (runId: string): Promise<JudgeCandidatesResponse> =>
    fetch(
      `${API_BASE}/runs/${encodeURIComponent(runId)}/judge/candidates`,
    ).then(handle<JudgeCandidatesResponse>),

  judgeEstimate: (
    runId: string,
    pruneMode: string,
    instanceIds?: string[],
    rejudge?: boolean,
    retryNoVerdict?: boolean,
  ): Promise<JudgeEstimateResponse> =>
    fetch(
      `${API_BASE}/runs/${encodeURIComponent(runId)}/judge/estimate${qs({
        prune_mode: pruneMode,
        instance_ids: instanceIds?.length ? instanceIds.join(',') : undefined,
        // 2026-09-08: the estimate mirrors the launch's resume rule
        rejudge: rejudge ? 'true' : undefined,
        // 2026-09-08: also count attempts whose latest judgment had no verdict
        retry_no_verdict: retryNoVerdict ? 'true' : undefined,
      })}`,
    ).then(handle<JudgeEstimateResponse>),

  launchJudge: (
    runId: string,
    body: {
      instance_ids?: string[];
      prune_mode: string;
      max_spend_usd: number;
      workers?: number;
      // 2026-09-08: judge already-judged candidates again (default: skip them = resume)
      rejudge?: boolean;
      // 2026-09-08: regenerate the pass report only — judge nothing
      synthesis_only?: boolean;
      // 2026-09-08: also judge attempts whose latest judgment timed out / failed to parse
      retry_no_verdict?: boolean;
      triggered_by?: string;
    },
  ): Promise<JudgeLaunchStarted> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/judge`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(handle<JudgeLaunchStarted>),

  // 2026-09-07: the pass in progress — judge_sampling is only written when a
  // pass ENDS, so this TTL'd Redis snapshot is the run screen's only view of
  // a running pass (judged / in flight / spend / ETA). `live: null` = no pass
  // running or nothing published; the launch card then shows pass history.
  getJudgeLive: (runId: string): Promise<JudgeLiveResponse> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/judge/live`).then(
      handle<JudgeLiveResponse>,
    ),

  // §3.2 of the 2026-09-02 review: a pass the budget ceiling truncated must
  // not look like a pass that finished — this is that banner's data source.
  // Newest pass first (backend-sorted); the UI reads element [0].
  listJudgePasses: (runId: string): Promise<JudgePassesResponse> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/judge/passes`).then(
      handle<JudgePassesResponse>,
    ),

  getJudgeResults: (runId: string): Promise<JudgeResultsResponse> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/judge/results`).then(
      handle<JudgeResultsResponse>,
    ),

  // §11: approve/deny + reasoning on one dimension of the judge's own
  // verdict — the in-app replacement for offline hand-labeling.
  reviewJudgeDimension: (
    runId: string,
    instanceId: string,
    attemptNumber: number,
    body: CalibrationReviewRequest,
  ): Promise<CalibrationReviewRecorded> =>
    fetch(
      `${API_BASE}/runs/${encodeURIComponent(runId)}/judge/results/${encodeURIComponent(instanceId)}/${attemptNumber}/review`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
    ).then(handle<CalibrationReviewRecorded>),

  // Deliberately NOT run-scoped (§11) — judge-model is one alias across
  // every run, so calibration coverage accumulates across all of them.
  getCalibrationSummary: (): Promise<CalibrationSummaryResponse> =>
    fetch(`${API_BASE}/judge/calibration`).then(
      handle<CalibrationSummaryResponse>,
    ),

  // §11: every review ever recorded against ONE judge_results row — so the
  // panel can show "already reviewed" state instead of a reviewer either
  // re-reviewing blind or trusting their own memory across a page reload.
  // judgedAt pins the exact row, same rule as reviewJudgeDimension.
  getJudgeReviewHistory: (
    runId: string,
    instanceId: string,
    attemptNumber: number,
    judgedAt: string,
  ): Promise<CalibrationReviewHistoryResponse> =>
    fetch(
      `${API_BASE}/runs/${encodeURIComponent(runId)}/judge/results/${encodeURIComponent(instanceId)}/${attemptNumber}/reviews${qs({ judged_at: judgedAt })}`,
    ).then(handle<CalibrationReviewHistoryResponse>),

  // ---- read-only dashboard (Aurora; polled slowly + adaptively, §4b) ----
  listRuns: (
    p: { status?: string; limit?: number; offset?: number } = {},
  ): Promise<RunList> =>
    fetch(`${API_BASE}/runs${qs(p)}`).then(handle<RunList>),

  getRun: (runId: string): Promise<RunDetail> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}`).then(
      handle<RunDetail>,
    ),

  getRunProgress: (runId: string): Promise<RunProgress> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/progress`).then(
      handle<RunProgress>,
    ),

  // ---- live in-flight progress (F11; BUILDER2-LIVE-RUN-MONITORING-2026-08-31.md)
  // — Redis per-instance progress over the Postgres attempt list. `state` on
  // the envelope is the whole-response health (`"ok"` | `"unknown"`); each
  // item's own `state` (`running`/`pending`/`stale`) is a different axis and
  // must be rendered separately — see LivePanel. ----
  getRunLive: (runId: string): Promise<RunLive> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/live`).then(
      handle<RunLive>,
    ),

  // ---- live L1 pacer ledger per alias (§2.5): cfg, bucket fill, in-flight, the WAIT
  // QUEUE (each denied call's size + wait), last-60s admission/over-2s/overload counters.
  // Same envelope contract as /live: `state` "ok" | "unknown" (Redis unreachable). ----
  getRunPacer: (runId: string): Promise<RunPacer> =>
    fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/pacer`).then(
      handle<RunPacer>,
    ),

  // ---- the L2 planner's LIVE decision record for a pool (forecast review 2026-09-03):
  // desired ceiling vs ECS in-flight, binding constraint (+ alias), per-alias ceilings /
  // bindings / curve sources, booting tasks, timeouts, wait queue. `state` ok | absent |
  // unknown — an expired record must never read as "planner says go". ----
  getAutoscalerDecision: (
    pool: 'harness' | 'eval',
  ): Promise<AutoscalerDecision> =>
    fetch(`${API_BASE}/autoscaler/${pool}`).then(handle<AutoscalerDecision>),

  // ---- operator limits (2026-09-04): the run-overrides hash the dispatcher re-reads
  // every tick, the global operator:limits hash, pacer:cfg per alias. Every edit is one
  // audit row. `state` ok | unknown (Redis unreachable — render nothing as a number). ----
  getLimits: (runId?: string): Promise<LimitsView> =>
    fetch(`${API_BASE}/limits${qs({ run_id: runId })}`).then(
      handle<LimitsView>,
    ),

  setRunLimit: (
    field: string,
    value: number | boolean | null,
    actor: string,
    reason: string,
  ): Promise<LimitEditResult> =>
    fetch(`${API_BASE}/limits/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ field, value, actor, reason }),
    }).then(handle<LimitEditResult>),

  setGlobalLimit: (
    field: string,
    value: number | boolean | null,
    actor: string,
    reason: string,
  ): Promise<LimitEditResult> =>
    fetch(`${API_BASE}/limits/global`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ field, value, actor, reason }),
    }).then(handle<LimitEditResult>),

  setPacerLimit: (
    alias: string,
    field: string,
    value: number,
    actor: string,
    reason: string,
    alsoPool: boolean,
  ): Promise<LimitEditResult> =>
    fetch(`${API_BASE}/limits/pacer/${encodeURIComponent(alias)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        field,
        value,
        actor,
        reason,
        also_pool: alsoPool,
      }),
    }).then(handle<LimitEditResult>),

  // ---- per-call wall-clock decomposition from llm_calls (§2.6). Lands after the
  // attempt finishes (the writer ingests llm_calls.jsonl then); in flight = /live. ----
  getInstanceCalls: (
    runId: string,
    instanceId: string,
    attemptNumber: number,
  ): Promise<InstanceCalls> =>
    fetch(
      `${API_BASE}/runs/${encodeURIComponent(runId)}/instances/${encodeURIComponent(instanceId)}/${attemptNumber}/calls`,
    ).then(handle<InstanceCalls>),

  getQueues: (): Promise<QueuesList> =>
    fetch(`${API_BASE}/queues`).then(handle<QueuesList>),

  listInstances: (
    runId: string,
    p: {
      state?: string;
      error_category?: string;
      limit?: number;
      offset?: number;
    } = {},
  ): Promise<InstancesList> =>
    fetch(
      `${API_BASE}/runs/${encodeURIComponent(runId)}/instances${qs(p)}`,
    ).then(handle<InstancesList>),

  getInstance: (
    runId: string,
    instanceId: string,
    attempt: number,
  ): Promise<InstanceDetail> =>
    fetch(
      `${API_BASE}/instances/${encodeURIComponent(runId)}/${encodeURIComponent(instanceId)}/${attempt}`,
    ).then(handle<InstanceDetail>),

  listCapacity: (
    p: { pool?: string; since?: string; limit?: number } = {},
  ): Promise<CapacityList> =>
    fetch(`${API_BASE}/capacity${qs(p)}`).then(handle<CapacityList>),

  /** artifact content (patch / trajectory / log / report) — plain text/bytes */
  artifact: async (
    runId: string,
    instanceId: string,
    attempt: number,
    kind: string,
  ): Promise<string> => {
    const res = await fetch(
      `${API_BASE}/artifacts/${encodeURIComponent(runId)}/${encodeURIComponent(instanceId)}/${attempt}/${kind}`,
    );
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        if (j && typeof j.detail === 'string') detail = j.detail;
      } catch {
        /* keep statusText */
      }
      throw new ApiError(res.status, detail);
    }
    return res.text();
  },

  // ---- run launch catalog (builder 4's; UI consumes, never owns) ----
  listDatasetInstances: (
    p: { limit?: number; offset?: number } = {},
  ): Promise<DatasetInstancesResponse> =>
    fetch(`${API_BASE}/dataset/instances${qs(p)}`).then(
      handle<DatasetInstancesResponse>,
    ),

  listHarnesses: (): Promise<HarnessesResponse> =>
    fetch(`${API_BASE}/harnesses`).then(handle<HarnessesResponse>),

  listModels: (): Promise<ModelsResponse> =>
    fetch(`${API_BASE}/models`).then(handle<ModelsResponse>),

  // 2026-09-09 efficiency prompt arm: starting texts for the launch screen's
  // harness-instructions field, plus the field's size cap.
  instructionPresets: (): Promise<InstructionPresetsResponse> =>
    fetch(`${API_BASE}/launch/instruction-presets`).then(
      handle<InstructionPresetsResponse>,
    ),

  /**
   * POST /runs — the three documented outcomes, never wrapped by `handle`.
   * `handle` throws ApiError on any non-ok and tries to read `.detail`, but the
   * 409 body is flat (`run_id` is the payload) and the 503 is
   * `{status:"refused", message}`.  So we parse the body ourselves and fold
   * each status into a tagged result for the launch screen to render.
   */
  launchRun: async (req: RunLaunchRequest): Promise<LaunchResult> => {
    const res = await fetch(`${API_BASE}/runs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    });
    if (res.status === 201) {
      return {
        kind: 'launched',
        data: (await res.json()) as RunLaunchResponse,
      };
    }
    if (res.status === 409) {
      return {
        kind: 'duplicate',
        data: (await res.json()) as DuplicateRunResponse,
      };
    }
    if (res.status === 503) {
      let message = res.statusText;
      try {
        const j = await res.json();
        if (j && typeof j.message === 'string') message = j.message;
      } catch {
        /* keep statusText */
      }
      return { kind: 'refused', message };
    }
    // Any other failure — fold into ApiError like every other call.
    return Promise.reject(new ApiError(res.status, res.statusText));
  },
};

/** The exact shape every implementation of the seam must have. The demo
 * adapter is typed against this, so a method added here without a replay
 * answer is a build error, not a silent runtime miss. */
export type ApiShape = typeof realApi;

/** THE seam (§8 of the 2026-09-10 plan): the live API, or — only when the
 * build was made with `VITE_DATA_MODE=demo` — the replay adapter that answers
 * every method from exported run snapshots at the demo clock's current t and
 * simulates every action locally. Nothing else in the app knows which. */
export const api: ApiShape = DATA_MODE === 'demo' ? demoApi : realApi;

/** Convenience: strip the schema envelope off a run's summary_json blob. */
export function summaryOf(item: {
  summary?: Record<string, unknown> | null;
}): RunSummary | null {
  return (item.summary as RunSummary | undefined) ?? null;
}

/**
 * The ACTUAL shape of `run_summary.summary_json` written by
 * results_writer._maintain_run_summary() — a FLAT object. The aspirational
 * nested `RunSummary` (provenance/totals/integrity) above was never produced by
 * the backend (BUILDER2-UI-ISSUES-HANDOVER §A). Provenance now arrives as the
 * typed top-level `run.provenance`; the resolve/attempt counts are these flat
 * keys. Read them through this, not through summaryOf's cast.
 */
export interface FlatRunSummary {
  expected?: number;
  completed?: number;
  aborted_in_flight?: number;
  never_dispatched?: number;
  resolved?: number;
  attempted?: number;
  gradeable?: number;
  denominator?: number;
  resolved_per_attempted?: number;
  resolved_per_gradeable?: number;
}

export function flatSummary(item?: {
  summary?: Record<string, unknown> | null;
}): FlatRunSummary {
  return (item?.summary ?? {}) as FlatRunSummary;
}
