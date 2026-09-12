import type {
  ApiShape,
  AutoscalerDecision,
  CalibrationReviewHistoryItem,
  CalibrationReviewHistoryResponse,
  CalibrationSummaryResponse,
  CapacityList,
  CapacityPoint,
  ControlView,
  DatasetInstancesResponse,
  HarnessesResponse,
  InstanceCalls,
  InstanceItem,
  InstancesList,
  InstructionPresetsResponse,
  JudgeLiveState,
  JudgePassItem,
  JudgePassesResponse,
  JudgeResultItem,
  JudgeResultsResponse,
  LaunchResult,
  LimitEditResult,
  LimitField,
  LimitsView,
  LiveInstance,
  LlmLiveCall,
  LlmLiveDetail,
  LlmLiveList,
  ModelCeiling,
  ModelsResponse,
  PacerAliasState,
  QueueView,
  QueuesList,
  RunDetail,
  RunItem,
  RunLaunchRequest,
  RunList,
  RunPacer,
  RunProgress,
  RunStateCount,
} from '../api';
import { ApiError } from '../apiError';
import {
  bundleIfLoaded,
  loadBundle,
  loadGzJson,
  loadGzText,
  loadJson,
} from './bundle';
import {
  ABORT_DRAIN_S,
  STALE_AFTER_S,
  allLanes,
  callsPrefixFor,
  capacityAt,
  effectiveEnd,
  laneInFlight,
  laneStatesAt,
  pausedAt,
  regradeLane,
  restartLane,
  runStatusAt,
  tickAt,
  totalsOf,
  type RunStatusAt,
} from './replay';
import { demoStore, type RunSim } from './store';
import {
  laneKey,
  type LoadedBundle,
  type ReplayCall,
  type ReplayCapacity,
  type ReplayLane,
} from './types';

// The demo implementation of the `api` seam (§8.1 / §8.2 of the 2026-09-10
// plan): every method of the live api, answered from the exported snapshot
// under `data/` at the demo clock's current replay time, every action
// simulated in the browser. It has EXACTLY the live api's shape (`ApiShape`);
// a method the live api gains without an answer here is a build error.
//
// House rule, same as the operator UI's: a field the snapshot does not carry
// is null → the UI's own "not measured" rendering; the whole-response
// `state: 'unknown'` shapes are used for what a Valkey outage would hide;
// nothing is ever a fabricated zero.

const DEFAULT_RUN_SUFFIX = '883d2571';

// ---- static data ------------------------------------------------------------

type JudgeCaptured = { results: JudgeResultItem[]; passes: JudgePassItem[] };
type ImageValidation = {
  items: Record<
    string,
    { run_id: string; attempt_number: number; verdict: string; state: string }
  >;
};

const runsJson = (): Promise<RunList> => loadJson<RunList>('global/runs.json');
const runJson = (id: string): Promise<RunDetail> =>
  loadJson<RunDetail>(`runs/${encodeURIComponent(id)}/run.json`);
const instancesJson = (id: string): Promise<InstancesList> =>
  loadJson<InstancesList>(`runs/${encodeURIComponent(id)}/instances.json`);
const progressJson = (id: string): Promise<RunProgress | null> =>
  loadJson<RunProgress>(`runs/${encodeURIComponent(id)}/progress.json`).catch(
    () => null,
  );

const judgeCache = new Map<string, Promise<JudgeCaptured | null>>();
function judgeCaptured(id: string): Promise<JudgeCaptured | null> {
  let p = judgeCache.get(id);
  if (!p) {
    p = Promise.all([
      loadJson<JudgeResultsResponse>(
        `runs/${encodeURIComponent(id)}/judge_results.json`,
      ),
      loadJson<JudgePassesResponse>(
        `runs/${encodeURIComponent(id)}/judge_passes.json`,
      ),
    ])
      .then(([r, ps]) => ({
        results: r.results ?? [],
        passes: ps.passes ?? [],
      }))
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      });
    judgeCache.set(id, p);
  }
  return p;
}

async function publishedIds(): Promise<string[]> {
  return (await runsJson()).items.map((r) => r.run_id);
}

async function assertPublished(runId: string): Promise<void> {
  if (!(await publishedIds()).includes(runId)) {
    throw new ApiError(404, `run ${runId} is not in this snapshot`);
  }
}

// ---- per-call context ---------------------------------------------------------

interface Rows {
  harness?: InstanceItem;
  eval?: InstanceItem;
}

interface Ctx {
  runId: string;
  loaded: LoadedBundle;
  run: RunDetail;
  rowsByLane: Map<string, Rows>;
  sim: RunSim | null;
  t: number;
  lanes: ReplayLane[];
  endT: number;
  status: RunStatusAt;
  outage: boolean;
}

const rowsCache = new Map<string, Promise<Map<string, Rows>>>();
function rowsByLane(runId: string): Promise<Map<string, Rows>> {
  let p = rowsCache.get(runId);
  if (!p) {
    p = instancesJson(runId).then((list) => {
      const m = new Map<string, Rows>();
      for (const row of list.items) {
        const k = laneKey(row.instance_id, row.attempt_number);
        const r = m.get(k) ?? {};
        if (row.phase === 'eval') r.eval = row;
        else r.harness = row;
        m.set(k, r);
      }
      return m;
    });
    rowsCache.set(runId, p);
  }
  return p;
}

async function ctx(runId: string): Promise<Ctx> {
  await assertPublished(runId);
  const [loaded, run, rows] = await Promise.all([
    loadBundle(runId),
    runJson(runId),
    rowsByLane(runId),
  ]);
  const sim = demoStore.hasSim(runId) ? demoStore.sim(runId) : null;
  const t = demoStore.tFor(runId);
  const lanes = allLanes(loaded, sim, t);
  const endT = effectiveEnd(lanes, loaded.bundle.window.end_t);
  const status = runStatusAt(loaded, lanes, sim, t);
  return {
    runId,
    loaded,
    run,
    rowsByLane: rows,
    sim,
    t,
    lanes,
    endT,
    status,
    outage: demoStore.global.valkeyOutage && runId === demoStore.runId,
  };
}

const iso = (c: Ctx, t: number | null): string | null =>
  t == null ? null : new Date((c.loaded.startEpoch + t) * 1000).toISOString();
const epoch = (c: Ctx, t: number): number => c.loaded.startEpoch + t;

function sourceRows(c: Ctx, lane: ReplayLane): Rows | undefined {
  const k = laneKey(lane.instance_id, lane.attempt);
  return c.rowsByLane.get(k) ?? c.rowsByLane.get(c.sim?.extraSources[k] ?? '');
}

/** the snapshot is exactly the captured, terminal truth — no rewinding needed */
function untouched(c: Ctx): boolean {
  return c.sim == null && c.t >= c.loaded.bundle.window.end_t;
}

// ---- instance rows at t -----------------------------------------------------------

const HARNESS_MEASURED = [
  'wall_clock_harness_s',
  'touches_test_files',
  'patch_path',
  'trajectory_path',
  'raw_log_path',
  'native_trajectory_s3_key',
  'input_tokens',
  'output_tokens',
  'cost_usd',
  'turns_used',
  'paced_wait_ms_total',
  'paced_calls',
  'pacer_timeouts',
  'overload_retries_total',
  'adapter_input_tokens',
  'adapter_output_tokens',
  'adapter_cost_usd',
  'agent_s',
  'task_observed_s',
  'task_billed_s',
  'repo_prep_s',
  'queue_wait_s',
  'provision_s',
  'image_pull_s',
  'worker_boot_s',
  'patch_extract_s',
  'artifact_upload_s',
  'repo_prep_cache_hit',
  'image_pull_cold',
  'cost_source',
  'error_category',
  'error_detail',
] as const;
const EVAL_MEASURED = [
  'wall_clock_eval_s',
  'touches_test_files',
  'report_path',
  'report_json',
  'test_output_s3_key',
  'run_log_s3_key',
  'eval_test_s',
  'eval_queue_wait_s',
  'eval_patch_fetch_s',
  'eval_image_pull_s',
  'eval_log_upload_s',
  'eval_image_pull_cold',
  'stripped_test_paths',
  'grade_invalid',
  'leaked_node_ids',
  'gold_patch_similarity',
  'leak_detectable',
  'error_category',
  'error_detail',
  'verdict',
] as const;

function nulled<T extends object>(row: T, keys: readonly (keyof T)[]): T {
  const out = { ...row } as Record<keyof T, unknown>;
  for (const k of keys) out[k] = null;
  return out as T;
}

function rowsAt(c: Ctx): InstanceItem[] {
  const out: InstanceItem[] = [];
  for (const lane of c.lanes) {
    const src = sourceRows(c, lane);
    if (!src) continue;
    const st = laneStatesAt(lane, c.t, c.endT);
    const simulatedLane = !c.loaded.laneIndex.has(
      laneKey(lane.instance_id, lane.attempt),
    );
    const sweptByDemo =
      c.sim?.abort != null &&
      (lane.harness_state === 'NEVER_DISPATCHED' ||
        lane.harness_state === 'ABORTED_IN_FLIGHT') &&
      src.harness?.state !== lane.harness_state;
    if (st.harness != null && src.harness) {
      const base: InstanceItem = {
        ...src.harness,
        attempt_number: lane.attempt,
        retry_reason: lane.retry_reason ?? src.harness.retry_reason,
        created_at: simulatedLane
          ? iso(c, lane.t_dispatched ?? c.t)
          : src.harness.created_at,
      };
      if (st.harnessLanded && !sweptByDemo) {
        out.push({ ...base, state: lane.harness_state ?? base.state });
      } else if (st.harnessLanded) {
        out.push({
          ...nulled(base, HARNESS_MEASURED),
          state: lane.harness_state ?? base.state,
          error_category: lane.harness_state,
        });
      } else {
        out.push({
          ...nulled(base, HARNESS_MEASURED),
          state: st.harness,
          verdict: null,
        });
      }
    }
    if (st.eval != null && src.eval) {
      const base: InstanceItem = {
        ...src.eval,
        attempt_number: lane.attempt,
        retry_reason: lane.retry_reason ?? src.eval.retry_reason,
        created_at: simulatedLane
          ? iso(c, lane.t_eval_enqueued ?? c.t)
          : src.eval.created_at,
      };
      const abandonedByDemo =
        lane.eval_state === 'ABANDONED' && src.eval.state !== 'ABANDONED';
      if (st.evalLanded && !abandonedByDemo) {
        out.push({
          ...base,
          state: lane.eval_state ?? base.state,
          verdict: lane.verdict,
        });
      } else if (st.evalLanded) {
        out.push({
          ...nulled(base, EVAL_MEASURED),
          state: 'ABANDONED',
          error_category: 'ABANDONED',
        });
      } else {
        out.push({ ...nulled(base, EVAL_MEASURED), state: st.eval });
      }
    }
  }
  return out;
}

function countStates(rows: readonly InstanceItem[]): RunStateCount[] {
  const m = new Map<string, number>();
  for (const r of rows) m.set(r.state, (m.get(r.state) ?? 0) + 1);
  return [...m]
    .map(([state, count]) => ({ state, count }))
    .sort((a, b) => b.count - a.count);
}

/** one bucket per instance — its latest attempt, the eval row over the harness row */
function instanceStates(rows: readonly InstanceItem[]): RunStateCount[] {
  const latest = new Map<string, InstanceItem>();
  for (const r of rows) {
    const cur = latest.get(r.instance_id);
    if (
      !cur ||
      r.attempt_number > cur.attempt_number ||
      (r.attempt_number === cur.attempt_number && r.phase === 'eval')
    )
      latest.set(r.instance_id, r);
  }
  return countStates([...latest.values()]);
}

function costAt(c: Ctx): number | null {
  let sum = 0;
  let any = false;
  for (const lane of c.lanes) {
    const st = laneStatesAt(lane, c.t, c.endT);
    if (lane.harness_state == null) continue;
    if (st.harnessLanded) {
      if (lane.cost_usd != null) {
        sum += lane.cost_usd;
        any = true;
      }
    } else if (st.harness === 'HARNESS_RUNNING') {
      const tot = totalsOf(callsPrefixFor(c.loaded, lane, c.sim, c.t));
      if (tot.cost_usd != null) {
        sum += tot.cost_usd;
        any = true;
      }
    }
  }
  return any ? Math.round(sum * 1e8) / 1e8 : 0;
}

function flatSummaryAt(
  c: Ctx,
  rows: readonly InstanceItem[],
): Record<string, unknown> {
  const buckets = instanceStates(rows);
  const n = (s: string): number =>
    buckets.find((b) => b.state === s)?.count ?? 0;
  const resolved = n('RESOLVED');
  const gradeable = resolved + n('UNRESOLVED') + n('EMPTY_PATCH');
  const dispatched = c.lanes.filter(
    (l) => l.t_dispatched != null && c.t >= l.t_dispatched,
  ).length;
  const completed = c.lanes.filter(
    (l) => laneStatesAt(l, c.t, c.endT).harnessLanded,
  ).length;
  return {
    expected: c.run.instance_count ?? c.lanes.length,
    resolved,
    attempted: dispatched,
    completed,
    gradeable,
    denominator: gradeable,
    empty_patches: n('EMPTY_PATCH'),
    never_dispatched: n('NEVER_DISPATCHED'),
    aborted_in_flight: n('ABORTED_IN_FLIGHT'),
    resolved_per_attempted: dispatched > 0 ? resolved / dispatched : null,
    resolved_per_gradeable: gradeable > 0 ? resolved / gradeable : null,
  };
}

function runDetailAt(c: Ctx): RunDetail {
  const rows = rowsAt(c);
  const st = c.status;
  const paused = pausedAt(c.sim?.control.gateway ?? [], c.t);
  const base: RunDetail = {
    ...c.run,
    status: st.status,
    terminal: st.terminal,
    stop_requested_at: iso(c, st.stop_requested_t),
    stop_scope: st.stop_scope,
    stop_reason: st.stop_reason,
    stopped_at: iso(c, st.stopped_t),
    finalised_at:
      iso(c, st.finalised_t) ?? (st.terminal ? c.run.finalised_at : null),
    ready_to_close:
      st.terminal &&
      !(c.sim?.closed && c.t >= c.sim.closed.t) &&
      st.status !== 'completed',
    gateway_key_blocked_by:
      c.sim?.gatewayBlockedBy ?? (paused ? 'global' : null),
    launch_limits: c.sim?.launchLimits
      ? { ...(c.run.launch_limits ?? {}), ...c.sim.launchLimits }
      : c.run.launch_limits,
  };
  if (untouched(c)) {
    return {
      ...base,
      ready_to_close: c.run.ready_to_close,
      finalised_at: c.run.finalised_at,
    };
  }
  const summary = flatSummaryAt(c, rows);
  return {
    ...base,
    summary,
    cost_usd_total: costAt(c),
    states: countStates(rows),
    instance_states: instanceStates(rows),
    resolve_rate_denominator: Number(summary.attempted ?? 0),
  };
}

function runItemAt(c: Ctx, item: RunItem): RunItem {
  const d = runDetailAt(c);
  return {
    ...item,
    status: d.status,
    terminal: d.terminal,
    summary: d.summary,
    cost_usd_total: d.cost_usd_total,
    stop_requested_at: d.stop_requested_at,
    stop_scope: d.stop_scope,
    stop_reason: d.stop_reason,
    stopped_at: d.stopped_at,
    finalised_at: d.finalised_at,
    launch_limits: d.launch_limits,
  };
}

// ---- live / pacer / planner --------------------------------------------------------

function liveAt(c: Ctx): LiveInstance[] {
  const items: LiveInstance[] = [];
  for (const lane of c.lanes) {
    const st = laneStatesAt(lane, c.t, c.endT);
    if (!laneInFlight(st)) continue;
    if (st.eval != null && st.eval !== 'PENDING') {
      const started = lane.t_eval_started ?? c.t;
      items.push({
        instance_id: lane.instance_id,
        attempt_number: lane.attempt,
        state: 'running',
        phase: 'eval',
        turn_number: null,
        input_tokens: null,
        output_tokens: null,
        cached_tokens: null,
        reasoning_tokens: null,
        cost_usd: null,
        observed_at: null,
        age_s: null,
        eval_elapsed_s: Math.max(0, c.t - started),
        eval_lines: null,
        eval_last_line: null,
        eval_silent_s: null,
        revived_after_reap: false,
      });
      continue;
    }
    if (st.harness === 'DISPATCHED' || st.harness === 'HARNESS_RUNNING') {
      const calls = callsPrefixFor(c.loaded, lane, c.sim, c.t);
      const tot = totalsOf(calls);
      const last = tot.last_t;
      const age = last == null ? null : c.t - last;
      items.push({
        instance_id: lane.instance_id,
        attempt_number: lane.attempt,
        state:
          calls.length === 0
            ? 'pending'
            : age != null && age > STALE_AFTER_S
              ? 'stale'
              : 'running',
        phase: 'harness',
        turn_number: calls.length ? calls.length : null,
        input_tokens: tot.tok_in,
        output_tokens: tot.tok_out,
        cached_tokens: tot.tok_cached,
        reasoning_tokens: tot.tok_reasoning,
        cost_usd: tot.cost_usd,
        observed_at: last == null ? null : epoch(c, last),
        age_s: age,
        revived_after_reap: false,
        paced_wait_ms_total: null,
        paced_calls: null,
        pacer_timeouts: null,
        overload_retries_total: null,
        pacer_last_deny_axis: null,
        pacer_last_queue_len: null,
      });
    }
  }
  return items;
}

interface Burst {
  phase: 'overload' | 'cooldown' | 'growth';
  overloads: number;
  factor: number;
}

/** the simulated provider-429 burst: 90 s of overloads, then the planner's
 * recovery (r_qps × 0.85) held for the run's cooldown, then +5 % growth per
 * tick back to the seed — the AIMD story on the run's real budgets */
function burstAt(c: Ctx): Burst | null {
  const b = c.sim?.burst?.t;
  if (b == null || c.t < b) return null;
  const cooldown = Number(c.run.launch_limits?.ramp_cooldown_seconds ?? 60);
  const step = Number(c.run.launch_limits?.ramp_step_pct ?? 5) / 100;
  const tick = c.loaded.bundle.tick_interval_s ?? 30;
  const overloadEnd = b + 90;
  const cooldownEnd = overloadEnd + cooldown;
  if (c.t < overloadEnd) {
    return {
      phase: 'overload',
      overloads: 2 + Math.floor((c.t - b) / 6),
      factor: 0.85,
    };
  }
  if (c.t < cooldownEnd)
    return { phase: 'cooldown', overloads: 0, factor: 0.85 };
  const k = Math.floor((c.t - cooldownEnd) / tick);
  const factor = Math.min(1, 0.85 * Math.pow(1 + step, k));
  return factor >= 1 ? null : { phase: 'growth', overloads: 0, factor };
}

async function pacerAt(c: Ctx, models: ModelsResponse): Promise<RunPacer> {
  if (c.outage) {
    return {
      run_id: c.runId,
      state: 'unknown',
      reason: 'redis_unreachable (simulated Valkey outage)',
      items: [],
    };
  }
  const tick = tickAt(c.loaded.bundle.ticks, c.t);
  const ledger = tick?.pacer ?? {};
  const burst = burstAt(c);
  const items: PacerAliasState[] = [];
  for (const target of c.loaded.bundle.targets) {
    const a = ledger[target.model_alias];
    const seeded =
      models.items.find((m) => m.alias === target.model_alias)
        ?.pacer_seeded_at ?? null;
    const override = c.sim?.pacerOverrides[target.model_alias] ?? {};
    const num = (k: string): number | null => override[k] ?? a?.[k] ?? null;
    const measured = a != null && Object.values(a).some((v) => v != null);
    const rQps = num('r_qps');
    items.push({
      alias: target.model_alias,
      harness: target.harness,
      measured,
      c_burst: num('c_burst'),
      r_tok: num('r_tok'),
      k_inflight: num('k_inflight'),
      c_req: num('c_req'),
      r_qps:
        burst && rQps != null
          ? Math.round(rQps * burst.factor * 100) / 100
          : rQps,
      seeded_at: seeded,
      bucket_level: null,
      bucket_fill: num('bucket_fill'),
      req_level: null,
      req_fill: num('req_fill'),
      inflight_calls: num('inflight_calls'),
      inflight_tokens: num('inflight_tokens'),
      inflight_fill: num('inflight_fill'),
      queue_len: num('queue_len'),
      head_est_tokens: null,
      head_waiting_s: num('head_waiting_s'),
      waiters: [],
      admits_60s: num('admits_60s'),
      over_2s_60s: num('over_2s_60s'),
      mean_wait_ms_60s: num('mean_wait_ms_60s'),
      overloads_60s:
        burst?.phase === 'overload' ? burst.overloads : num('overloads_60s'),
      observed_at: epoch(c, tick?.t ?? c.t),
    });
  }
  return { run_id: c.runId, state: 'ok', reason: null, items };
}

function ceilingOverrideAt(
  c: Ctx,
): { value: number | null; actor: string | null } | null {
  if (c.sim && 'ceiling_override' in c.sim.runOverrides) {
    const edit = [...c.sim.limitEdits]
      .reverse()
      .find((e) => e.field === 'ceiling_override');
    return {
      value: c.sim.runOverrides.ceiling_override,
      actor: edit?.actor ?? 'operator',
    };
  }
  const captured = c.loaded.bundle.limit_edits
    .filter(
      (e) =>
        e.field === 'ceiling_override' &&
        e.scope === 'run' &&
        e.t != null &&
        e.t <= c.t,
    )
    .pop();
  if (captured) {
    const v =
      captured.new_value == null || captured.new_value === ''
        ? null
        : Number(captured.new_value);
    return {
      value: Number.isFinite(v as number) ? v : null,
      actor: captured.actor,
    };
  }
  return null;
}

function autoscalerAt(c: Ctx, pool: 'harness' | 'eval'): AutoscalerDecision {
  if (c.outage) return { pool, state: 'unknown', age_s: null, record: null };
  const row = capacityAt(c.loaded.bundle.capacity, pool, c.t);
  if (!row) return { pool, state: 'absent', age_s: null, record: null };
  const record: Record<string, unknown> = { ...row.decision };
  const paused =
    pausedAt(c.sim?.control[pool] ?? [], c.t) ||
    (pool === 'harness' && pausedAt(c.sim?.control.gateway ?? [], c.t));
  if (paused) record.binding_constraint = 'paused';
  if (pool === 'harness') {
    const ov = ceilingOverrideAt(c);
    if (c.sim && ov?.value != null) {
      record.desired_ceiling = ov.value;
      record.ceiling_override = ov.value;
      if (!paused) record.binding_constraint = 'ceiling_override';
    }
    const burst = burstAt(c);
    if (burst) {
      const budgets = {
        ...((record.budgets as Record<string, unknown>) ?? {}),
      };
      const seed = Number(budgets.r_qps ?? NaN);
      if (Number.isFinite(seed)) {
        budgets.r_qps = Math.round(seed * burst.factor * 100) / 100;
        const alias = String(
          record.binding_alias ??
            c.loaded.bundle.targets[0]?.model_alias ??
            'alias',
        );
        if (burst.phase !== 'growth') {
          record.recovery_set = { [alias]: { r_qps: [seed, budgets.r_qps] } };
          record.growth_applied = false;
        } else {
          record.recovery_set = {};
          record.growth_applied = true;
        }
      }
      record.budgets = budgets;
      if (burst.phase === 'overload') record.overloads_window = burst.overloads;
      if (!paused)
        record.binding_constraint =
          burst.phase === 'growth' ? 'ramp_limited' : 'cooldown';
    }
  }
  return { pool, state: 'ok', age_s: Math.max(0, c.t - row.t), record };
}

// ---- control -----------------------------------------------------------------

let capturedControl: Promise<ControlView | null> | null = null;
function controlCaptured(): Promise<ControlView | null> {
  if (!capturedControl)
    capturedControl = loadJson<ControlView>('global/control.json').catch(
      () => null,
    );
  return capturedControl;
}

async function controlAt(c: Ctx | null): Promise<ControlView> {
  const captured = await controlCaptured();
  const abortedRuns = new Set(captured?.aborted_runs ?? []);
  if (!c) {
    return {
      harness_paused: false,
      eval_paused: false,
      gateway_paused: false,
      published_at: Date.now() / 1000,
      stale: demoStore.global.valkeyOutage,
      aborted_runs: [...abortedRuns],
      updated_by: '',
      reason: '',
    };
  }
  const tick = tickAt(c.loaded.bundle.ticks, c.t);
  const flag = (pool: 'harness' | 'eval' | 'gateway'): boolean => {
    const ov = c.sim?.control.overrides[pool];
    if (ov != null) return ov;
    return Boolean(tick?.[`${pool}_paused`]);
  };
  const stale = c.outage || Boolean(tick?.control_stale);
  if (c.sim?.abort && c.t >= c.sim.abort.t) abortedRuns.add(c.runId);
  if (c.status.stop_requested_t != null) abortedRuns.add(c.runId);
  return {
    harness_paused: stale || flag('harness'),
    eval_paused: stale || flag('eval'),
    gateway_paused: stale || flag('gateway'),
    published_at: epoch(c, c.t),
    stale,
    aborted_runs: [...abortedRuns],
    updated_by: c.sim?.control.updated_by ?? '',
    reason: c.sim?.control.reason ?? '',
  };
}

function currentRunId(): string | null {
  return demoStore.runId;
}

async function currentCtx(): Promise<Ctx | null> {
  const id = currentRunId();
  if (!id) return null;
  try {
    return await ctx(id);
  } catch {
    return null;
  }
}

// ---- judge replay ------------------------------------------------------------

const JUDGE_REPLAY_MAX_S = 900;
const JUDGE_SYNTH_S = 60;

interface JudgeView {
  results: JudgeResultItem[];
  passes: JudgePassItem[];
  live: JudgeLiveState | null;
}

function judgeViewAt(c: Ctx, captured: JudgeCaptured): JudgeView {
  const j = c.sim?.judge;
  if (!j) {
    if (c.status.terminal || c.t >= c.loaded.bundle.window.end_t) {
      return { results: captured.results, passes: captured.passes, live: null };
    }
    return { results: [], passes: [], live: null };
  }
  const scope = j.instance_ids ? new Set(j.instance_ids) : null;
  const selected = captured.results
    .filter((r) => !scope || scope.has(r.instance_id))
    .sort((a, b) => a.judged_at.localeCompare(b.judged_at));
  const stamps = selected.map((r) => Date.parse(r.judged_at) / 1000);
  const jmin = stamps.length ? Math.min(...stamps) : 0;
  const realSpan = stamps.length ? Math.max(...stamps) - jmin : 0;
  const replaySpan = j.synthesis_only
    ? 0
    : Math.min(Math.max(realSpan, 60), JUDGE_REPLAY_MAX_S);
  const scale = realSpan > 0 ? replaySpan / realSpan : 0;
  const landT = selected.map((_, i) =>
    realSpan > 0
      ? j.t_launch + (stamps[i] - jmin) * scale
      : j.t_launch + (replaySpan * (i + 1)) / Math.max(1, selected.length),
  );
  const judgingEnd = j.t_launch + replaySpan;
  const synthEnd = judgingEnd + JUDGE_SYNTH_S;

  const landed: JudgeResultItem[] = [];
  let spend = 0;
  let skippedOverBudget = 0;
  if (!j.synthesis_only) {
    selected.forEach((r, i) => {
      if (landT[i] > c.t) return;
      const cost = r.judge_cost_usd ?? 0;
      if (spend + cost > j.max_spend_usd) {
        skippedOverBudget += 1;
        return;
      }
      spend += cost;
      landed.push(r);
    });
  }
  const done = c.t >= synthEnd;
  const status =
    c.t < judgingEnd ? 'running' : c.t < synthEnd ? 'synthesizing' : 'done';
  const nextIdx = landed.length + skippedOverBudget;
  const inFlight =
    status === 'running'
      ? selected.slice(nextIdx, nextIdx + j.workers).map((r, k) => ({
          instance_id: r.instance_id,
          attempt_number: r.attempt_number,
          started_at: epoch(
            c,
            Math.max(j.t_launch, landT[nextIdx + k - 1] ?? j.t_launch),
          ),
        }))
      : [];
  const live: JudgeLiveState = {
    run_id: c.runId,
    pass_id: j.pass_id,
    status,
    workers: j.workers,
    selected: selected.length,
    judged: landed.length,
    skipped_over_budget: skippedOverBudget,
    parse_failed: landed.filter((r) => r.judge_parse_failed).length,
    skipped_artifacts: 0,
    call_failed: 0,
    timed_out: landed.filter((r) => r.judge_method === 'timeout').length,
    already_judged: 0,
    spend_usd: Math.round(spend * 1e6) / 1e6,
    max_spend_usd: j.max_spend_usd,
    started_at: epoch(c, j.t_launch),
    updated_at: epoch(c, c.t),
    finished_at: done ? epoch(c, synthEnd) : null,
    elapsed_s: Math.max(0, c.t - j.t_launch),
    eta_s:
      status === 'running'
        ? judgingEnd - c.t
        : status === 'synthesizing'
          ? synthEnd - c.t
          : null,
    in_flight_count: inFlight.length,
    in_flight: inFlight,
    last_error: null,
  };
  const passes: JudgePassItem[] = [];
  if (done) {
    const report = captured.passes.find(
      (p) => p.synthesis || p.synthesis_error,
    );
    passes.push({
      pass_id: j.pass_id,
      requested_rate: 1,
      seed: null,
      total_eligible: j.synthesis_only ? 0 : selected.length,
      total_judged: landed.length,
      total_skipped_over_budget: skippedOverBudget,
      total_parse_failed: live.parse_failed,
      created_at: iso(c, synthEnd) ?? new Date().toISOString(),
      synthesis: report?.synthesis ?? null,
      synthesis_cost_usd: report?.synthesis_cost_usd ?? null,
      synthesis_model_resolved: report?.synthesis_model_resolved ?? null,
      synthesis_error: report
        ? (report.synthesis_error ?? null)
        : 'no captured pass report for this run',
      synthesis_only: j.synthesis_only,
    });
  }
  const results = j.synthesis_only ? captured.results : landed;
  return { results, passes, live };
}

async function judgeFor(
  runId: string,
): Promise<{ c: Ctx; captured: JudgeCaptured; view: JudgeView }> {
  const c = await ctx(runId);
  const captured = await judgeCaptured(runId);
  if (!captured) {
    throw new ApiError(
      503,
      'judge results were not captured for this run in the snapshot — state unknown',
    );
  }
  return { c, captured, view: judgeViewAt(c, captured) };
}

// ---- calibration (localStorage) -----------------------------------------------

const REVIEWS_KEY = 'explorer.demo.calibrationReviews.v1';
type StoredReview = CalibrationReviewHistoryItem & {
  run_id: string;
  instance_id: string;
  attempt_number: number;
  judged_at: string;
};

function readReviews(): StoredReview[] {
  try {
    const raw = localStorage.getItem(REVIEWS_KEY);
    return raw ? (JSON.parse(raw) as StoredReview[]) : [];
  } catch {
    return [];
  }
}
function writeReviews(rows: StoredReview[]): void {
  try {
    localStorage.setItem(REVIEWS_KEY, JSON.stringify(rows));
  } catch {
    /* private mode / quota — the toast still says it was simulated */
  }
}

// ---- llm-live reconstruction ----------------------------------------------------

interface TrajTurn {
  turn?: number;
  role?: string;
  content?: string | null;
  reasoning?: string | null;
  tool_calls?: {
    id?: string;
    name?: string;
    input?: unknown;
    arguments?: unknown;
  }[];
  name?: string | null;
  output?: string | null;
  stdout?: string | null;
  normalized?: { command?: string | null } | null;
}

function parseTrajectory(raw: string): TrajTurn[] {
  const out: TrajTurn[] = [];
  for (const line of raw.split('\n')) {
    const s = line.trim();
    if (!s) continue;
    try {
      out.push(JSON.parse(s) as TrajTurn);
    } catch {
      /* skip a torn line */
    }
  }
  return out;
}

/** the conversation prefix that the k-th model call (1-based) saw + produced:
 * consecutive assistant turns are one call (reasoning + text + tool calls) */
function messagesForCall(
  turns: TrajTurn[],
  k: number,
): Record<string, unknown>[] {
  const msgs: Record<string, unknown>[] = [];
  let calls = 0;
  let prevAssistant = false;
  for (const t of turns) {
    const role = t.role ?? 'user';
    if (role === 'assistant') {
      if (!prevAssistant) {
        calls += 1;
        if (calls > k) break;
      }
      prevAssistant = true;
      const msg: Record<string, unknown> = {
        role: 'assistant',
        content: t.content ?? '',
      };
      if (t.reasoning) msg.reasoning_content = t.reasoning;
      if (t.tool_calls?.length) {
        msg.tool_calls = t.tool_calls.map((tc) => ({
          id: tc.id,
          type: 'function',
          function: {
            name: tc.name ?? 'tool',
            arguments:
              typeof tc.arguments === 'string'
                ? tc.arguments
                : JSON.stringify(tc.input ?? tc.arguments ?? {}),
          },
        }));
      }
      msgs.push(msg);
      continue;
    }
    if (calls >= k) break;
    prevAssistant = false;
    if (role === 'tool' || role === 'result') {
      const cmd = t.normalized?.command;
      const body = t.output ?? t.stdout ?? t.content ?? '';
      msgs.push({
        role: 'tool',
        content: `${t.name ? `[${t.name}]` : ''}${cmd ? ` ${cmd}` : ''}\n${body}`,
      });
    } else {
      msgs.push({ role, content: t.content ?? '' });
    }
  }
  return msgs;
}

function llmCallRow(
  c: Ctx,
  lane: ReplayLane,
  laneIdx: number,
  i: number,
  call: ReplayCall,
): LlmLiveCall {
  const [, tStart, , tokIn, tokOut, tokCached] = call;
  return {
    request_id: `${laneIdx}:${i}`,
    started_at: iso(c, tStart) ?? '',
    model: c.run.provenance?.model_resolved ?? c.run.model_alias ?? null,
    prompt_tokens: tokIn,
    completion_tokens: tokOut,
    text_tokens: tokIn != null && tokCached != null ? tokIn - tokCached : tokIn,
    cached_tokens: tokCached,
    session_id: null,
    instance_id: lane.instance_id,
    attempt: lane.attempt,
    harness: c.run.harness ?? null,
    status: call[8] == null ? null : call[8] === 200 ? 'success' : 'failure',
  };
}

// ---- launch mapping ---------------------------------------------------------------

interface LaunchTarget {
  run_id: string;
  harness: string;
  model_alias: string;
  preset: boolean;
  instances: number;
}

let launchTargets: Promise<LaunchTarget[]> | null = null;
function targets(): Promise<LaunchTarget[]> {
  if (!launchTargets) {
    launchTargets = publishedIds().then((ids) =>
      Promise.all(
        ids.map(async (id) => {
          const r = await runJson(id);
          return {
            run_id: id,
            harness: r.harness ?? '',
            model_alias: r.model_alias ?? '',
            preset: Boolean(r.launch_limits?.harness_instructions),
            instances: r.instance_count ?? 0,
          };
        }),
      ),
    );
  }
  return launchTargets;
}

// ---- helpers for actions --------------------------------------------------------------

function actionCtx(runId: string): Promise<Ctx> {
  if (demoStore.runId !== runId) demoStore.switchRun(runId);
  return ctx(runId);
}

function nextAttempt(
  c: Ctx,
  instanceId: string,
): { lane: ReplayLane | null; attempt: number } {
  let best: ReplayLane | null = null;
  for (const l of c.lanes) {
    if (l.instance_id === instanceId && (!best || l.attempt > best.attempt))
      best = l;
  }
  return { lane: best, attempt: (best?.attempt ?? 0) + 1 };
}

const CALLS_FILE = (instanceId: string, attempt: number): string =>
  `${instanceId.replace(/[^A-Za-z0-9_.-]/g, '%')}__${attempt}.json.gz`;

// ---- the adapter ----------------------------------------------------------------

export const demoApi: ApiShape = {
  // ---- control ----
  getControl: async () => controlAt(await currentCtx()),

  pause: async (pools, reason, actor) => {
    const id = currentRunId();
    if (id) {
      const c = await ctx(id);
      demoStore.mutate(id, (s) => {
        for (const p of pools as ('harness' | 'eval' | 'gateway')[]) {
          if (!(p in s.control)) continue;
          const spans = s.control[p];
          if (!spans.some((x) => x.to == null))
            spans.push({ from: c.t, to: null });
          s.control.overrides[p] = true;
        }
        s.control.updated_by = actor;
        s.control.reason = reason;
        s.control.published_t = c.t;
      });
    }
    demoStore.simulated(
      `paused ${pools.join(', ')} — dispatches are held until resume`,
    );
    return { pools, paused: true, reason, actor };
  },

  resume: async (pools, actor) => {
    const id = currentRunId();
    if (id) {
      const c = await ctx(id);
      demoStore.mutate(id, (s) => {
        for (const p of pools as ('harness' | 'eval' | 'gateway')[]) {
          if (!(p in s.control)) continue;
          for (const span of s.control[p]) if (span.to == null) span.to = c.t;
          s.control.overrides[p] = false;
        }
        s.control.updated_by = actor;
        s.control.reason = '';
        s.control.published_t = c.t;
      });
    }
    demoStore.simulated(`resumed ${pools.join(', ')}`);
    return { pools, paused: false, reason: '', actor };
  },

  abort: async (runId, opts) => {
    const c = await actionCtx(runId);
    if (c.sim?.abort)
      throw new ApiError(409, 'abort already requested (simulated)');
    let inFlight = 0;
    let pending = 0;
    for (const l of c.lanes) {
      const st = laneStatesAt(l, c.t, c.endT);
      if (st.harness === 'DISPATCHED' || st.harness === 'HARNESS_RUNNING')
        inFlight += 1;
      else if (st.harness === 'PENDING') pending += 1;
    }
    demoStore.mutate(runId, (s) => {
      s.abort = {
        t: c.t,
        scope: opts.scope,
        reason: opts.reason,
        actor: opts.actor,
      };
    });
    demoStore.simulated(
      `abort (${opts.scope}) — the real sweep rules applied locally: PENDING → NEVER_DISPATCHED, in flight → ABORTED_IN_FLIGHT, grading → ABANDONED`,
    );
    return {
      run_id: runId,
      status: 'aborting',
      scope: opts.scope,
      reason: opts.reason,
      actor: opts.actor,
      in_flight_stopped: inFlight,
      drained: pending,
      drain_skipped: false,
      drain_skip_reason: '',
      settled: false,
      swept: pending + inFlight,
      abort_not_instant: true,
      note: `simulated — settles in ${ABORT_DRAIN_S} s of replay time`,
    };
  },

  closeRun: async (runId) => {
    const c = await actionCtx(runId);
    if (!c.status.terminal)
      throw new ApiError(
        409,
        'not ready to close — instances still in flight (simulated)',
      );
    demoStore.mutate(runId, (s) => {
      s.closed = { t: c.t };
    });
    demoStore.simulated(
      'closed the run — both the LiteLLM and OpenRouter keys would be revoked now; nothing was',
    );
    return { run_id: runId, status: 'completed' };
  },

  restartInstances: async (runId, instanceIds, actor) => {
    const c = await actionCtx(runId);
    const restarted: {
      instance_id: string;
      attempt_number: number;
      retry_reason: string;
    }[] = [];
    const skipped: { instance_id: string; reason: string }[] = [];
    const added: { lane: ReplayLane; source: string }[] = [];
    for (const id of instanceIds) {
      const { lane, attempt } = nextAttempt(c, id);
      if (!lane) {
        skipped.push({
          instance_id: id,
          reason: 'unknown instance for this run',
        });
        continue;
      }
      if (laneInFlight(laneStatesAt(lane, c.t, c.endT))) {
        skipped.push({
          instance_id: id,
          reason: 'still in flight — not restart-eligible',
        });
        continue;
      }
      const fresh = restartLane(
        c.loaded.bundle.lanes[
          c.loaded.laneIndex.get(laneKey(id, lane.attempt)) ?? -1
        ] ?? lane,
        c.t,
        attempt,
        'operator_restart',
      );
      if (!fresh) {
        skipped.push({
          instance_id: id,
          reason:
            'no replayable timing for this instance (never dispatched in the captured run)',
        });
        continue;
      }
      added.push({
        lane: fresh,
        source: laneKey(lane.instance_id, lane.attempt),
      });
      restarted.push({
        instance_id: id,
        attempt_number: attempt,
        retry_reason: 'operator_restart',
      });
    }
    if (added.length) {
      demoStore.mutate(runId, (s) => {
        for (const a of added) {
          s.extraLanes.push(a.lane);
          s.extraSources[laneKey(a.lane.instance_id, a.lane.attempt)] =
            s.extraSources[a.source] ?? a.source;
        }
      });
    }
    demoStore.simulated(
      `restart by ${actor} — ${restarted.length} attempt N+1 row(s) dispatch on the clock, replaying attempt N's timing`,
    );
    return { run_id: runId, restarted, skipped };
  },

  regradeInstances: async (runId, instanceIds, actor) => {
    const c = await actionCtx(runId);
    const regraded: {
      instance_id: string;
      attempt_number: number;
      patch_s3_key: string;
    }[] = [];
    const skipped: { instance_id: string; reason: string }[] = [];
    const added: { lane: ReplayLane; source: string }[] = [];
    for (const id of instanceIds) {
      const { lane, attempt } = nextAttempt(c, id);
      if (!lane) {
        skipped.push({
          instance_id: id,
          reason: 'unknown instance for this run',
        });
        continue;
      }
      if (laneInFlight(laneStatesAt(lane, c.t, c.endT))) {
        skipped.push({ instance_id: id, reason: 'still in flight' });
        continue;
      }
      const fresh = regradeLane(lane, c.t, attempt);
      if (!fresh) {
        skipped.push({
          instance_id: id,
          reason:
            'no graded patch to regrade (no eval timing in the captured run)',
        });
        continue;
      }
      const src = sourceRows(c, lane);
      added.push({
        lane: fresh,
        source: laneKey(lane.instance_id, lane.attempt),
      });
      regraded.push({
        instance_id: id,
        attempt_number: attempt,
        patch_s3_key: src?.harness?.patch_path ?? '',
      });
    }
    if (added.length) {
      demoStore.mutate(runId, (s) => {
        for (const a of added) {
          s.extraLanes.push(a.lane);
          s.extraSources[laneKey(a.lane.instance_id, a.lane.attempt)] =
            s.extraSources[a.source] ?? a.source;
        }
      });
    }
    demoStore.simulated(
      `regrade by ${actor} — ${regraded.length} eval-only attempt N+1 row(s) queued on the clock`,
    );
    return { run_id: runId, regraded, skipped };
  },

  validateImages: async (instanceIds) => {
    const v = await loadJson<ImageValidation>(
      'global/image_validation.json',
    ).catch(() => null);
    const validated: { instance_id: string; attempt_number: number }[] = [];
    const skipped: { instance_id: string; reason: string }[] = [];
    for (const id of instanceIds) {
      const hit = v?.items[id];
      if (hit)
        validated.push({ instance_id: id, attempt_number: hit.attempt_number });
      else
        skipped.push({
          instance_id: id,
          reason:
            'no captured gold-validation result for this image in the snapshot',
        });
    }
    demoStore.simulated(
      `image validation answered from ${validated.length} captured gold-validation result(s); ${skipped.length} not captured`,
    );
    return { run_id: 'image-validation', validated, skipped };
  },

  // ---- llm-live (reconstructed) ----
  llmLiveCalls: async (runId, opts = {}) => {
    const c = await ctx(runId);
    if (c.outage)
      throw new ApiError(503, 'spend DB unreachable (simulated Valkey outage)');
    const limit = opts.limit ?? 50;
    const rows: LlmLiveCall[] = [];
    c.lanes.forEach((lane) => {
      if (opts.instanceId && lane.instance_id !== opts.instanceId) return;
      if (opts.attempt != null && lane.attempt !== opts.attempt) return;
      const idx = c.loaded.laneIndex.get(
        laneKey(lane.instance_id, lane.attempt),
      );
      if (idx == null) return; // a simulated attempt has no spend rows
      const calls = callsPrefixFor(c.loaded, lane, c.sim, c.t);
      calls.forEach((call, i) => rows.push(llmCallRow(c, lane, idx, i, call)));
    });
    rows.sort((a, b) => b.started_at.localeCompare(a.started_at));
    const page = (
      opts.before ? rows.filter((r) => r.started_at < opts.before!) : rows
    ).slice(0, limit);
    const out: LlmLiveList = { run_id: runId, items: page };
    return out;
  },

  llmLiveDetail: async (runId, requestId) => {
    const c = await ctx(runId);
    const [laneStr, iStr] = requestId.split(':');
    const lane = c.loaded.bundle.lanes[Number(laneStr)];
    const i = Number(iStr);
    if (!lane || !Number.isFinite(i)) throw new ApiError(404, 'unknown call');
    const calls = c.loaded.callsByLane.get(Number(laneStr)) ?? [];
    const call = calls[i];
    if (!call) throw new ApiError(404, 'unknown call');
    const row = llmCallRow(c, lane, Number(laneStr), i, call);
    let messages: Record<string, unknown>[] = [];
    let toolsCount = 0;
    try {
      const raw = await loadGzText(
        `runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(lane.instance_id)}/${lane.attempt}/trajectory.gz`,
      );
      const turns = parseTrajectory(raw);
      messages = messagesForCall(turns, i + 1);
      toolsCount = new Set(
        turns.filter((t) => t.role === 'tool' && t.name).map((t) => t.name),
      ).size;
    } catch {
      /* no trajectory artifact for this run — the panel says "no conversation captured" */
    }
    const detail: LlmLiveDetail = {
      request_id: row.request_id,
      started_at: row.started_at,
      model: row.model,
      prompt_tokens: row.prompt_tokens,
      completion_tokens: row.completion_tokens,
      session_id: null,
      instance_id: row.instance_id,
      attempt: row.attempt,
      harness: row.harness,
      status: row.status,
      messages,
      system: null,
      tools_count: toolsCount,
      response: null,
    };
    return detail;
  },

  pauseGateway: async (runId, actor = 'operator') => {
    await actionCtx(runId);
    demoStore.mutate(runId, (s) => {
      s.gatewayBlockedBy = 'operator';
    });
    demoStore.simulated(`gateway key blocked for this run by ${actor}`);
    return { run_id: runId, gateway_key_blocked_by: 'operator' };
  },

  resumeGateway: async (runId, actor = 'operator') => {
    await actionCtx(runId);
    demoStore.mutate(runId, (s) => {
      s.gatewayBlockedBy = null;
    });
    demoStore.simulated(`gateway key unblocked for this run by ${actor}`);
    return { run_id: runId, gateway_key_blocked_by: null };
  },

  // ---- model ceilings ----
  listModelCeilings: async () => {
    const rows = await loadJson<ModelCeiling[]>('global/model_ceilings.json');
    const now = Date.now();
    const g = demoStore.global;
    return rows.map((row) => {
      const manual = g.manualCeilings[row.model_alias];
      if (manual) {
        return {
          ...row,
          discovered_tpm: manual.tpm,
          ceiling_source: 'manual (simulated)',
          discovered_at: manual.at,
          values: { burst_admission_tokens: manual.tpm },
          is_stale: false,
        };
      }
      const d = g.discoveries[row.model_alias];
      if (d) {
        const elapsed = (now - d.startedAt) / 1000;
        if (elapsed < 30) {
          return {
            ...row,
            ceiling_source: `discovery in progress (replaying the captured result, ${Math.round(30 - elapsed)} s)`,
          };
        }
        return {
          ...row,
          discovered_at: new Date(d.startedAt + 30_000).toISOString(),
          ceiling_source: `${row.ceiling_source ?? 'discovery'} (replayed)`,
          is_stale: false,
        };
      }
      return row;
    });
  },

  estimateDiscovery: async () => {
    throw new ApiError(
      503,
      'the discovery cost estimate is not part of the snapshot — a simulated discovery replays the captured result',
    );
  },

  discoverCeiling: async (
    modelAlias,
    targetConcurrency,
    targetTasks = 60,
    rampMode = 'target_first',
  ) => {
    demoStore.mutateGlobal((g) => {
      g.discoveries[modelAlias] = {
        startedAt: Date.now(),
        targetTasks,
        rampMode,
      };
    });
    demoStore.simulated(
      `discovery for ${modelAlias} — replays the captured observation over ~30 s (per-step observations were not captured)`,
    );
    return {
      model_alias: modelAlias,
      target_concurrency: targetConcurrency,
      estimated_cost_usd: 0,
      ramp_mode: rampMode,
      target_tasks: targetTasks,
      status: 'started (simulated)',
    };
  },

  manualCeiling: async (modelAlias, tpmValue, notes) => {
    const at = new Date().toISOString();
    demoStore.mutateGlobal((g) => {
      g.manualCeilings[modelAlias] = {
        tpm: tpmValue,
        notes: notes ?? null,
        at,
      };
    });
    demoStore.simulated(
      `manual ceiling ${tpmValue.toLocaleString()} for ${modelAlias}`,
    );
    const rows = await loadJson<ModelCeiling[]>('global/model_ceilings.json');
    const row = rows.find((r) => r.model_alias === modelAlias);
    return {
      model_alias: modelAlias,
      discovered_tpm: tpmValue,
      ceiling_source: 'manual (simulated)',
      discovered_at: at,
      provider: row?.provider ?? null,
      values: { burst_admission_tokens: tpmValue },
      is_stale: false,
    };
  },

  // ---- judge ----
  judgeCandidates: async (runId) => {
    const { c, view } = await judgeFor(runId);
    const judged = new Set(
      view.results.map((r) => laneKey(r.instance_id, r.attempt_number)),
    );
    const candidates = c.lanes
      .filter(
        (l) =>
          l.harness_state != null && laneStatesAt(l, c.t, c.endT).harnessLanded,
      )
      .map((l) => ({
        instance_id: l.instance_id,
        attempt_number: l.attempt,
        harness: c.run.harness ?? '',
        outcome: l.verdict ?? l.eval_state ?? l.harness_state ?? 'unknown',
        always_judge: false,
        already_judged: judged.has(laneKey(l.instance_id, l.attempt)),
        last_judgment: null,
      }));
    return { run_id: runId, candidates };
  },

  judgeEstimate: async (
    runId,
    pruneMode,
    instanceIds,
    rejudge,
    retryNoVerdict,
  ) => {
    const { captured, view } = await judgeFor(runId);
    const scope = instanceIds?.length ? new Set(instanceIds) : null;
    const visible = new Set(
      view.results.map((r) => laneKey(r.instance_id, r.attempt_number)),
    );
    const pick = captured.results.filter((r) => {
      if (scope && !scope.has(r.instance_id)) return false;
      const k = laneKey(r.instance_id, r.attempt_number);
      if (rejudge) return true;
      if (!visible.has(k)) return true;
      return Boolean(
        retryNoVerdict &&
        (r.judge_method === 'timeout' || r.judge_parse_failed),
      );
    });
    return {
      run_id: runId,
      candidate_count: pick.length,
      prune_mode: pruneMode,
      estimated_cost_usd:
        Math.round(
          pick.reduce((s, r) => s + (r.judge_cost_usd ?? 0), 0) * 1e4,
        ) / 1e4,
      status: 'estimate (from the captured pass costs)',
    };
  },

  launchJudge: async (runId, body) => {
    const { c, captured } = await judgeFor(runId);
    const passId = `judge-demo-${Math.floor(c.loaded.startEpoch + c.t)}`;
    demoStore.mutate(runId, (s) => {
      s.judge = {
        t_launch: c.t,
        pass_id: passId,
        prune_mode: body.prune_mode,
        max_spend_usd: body.max_spend_usd,
        workers: body.workers ?? 24,
        instance_ids: body.instance_ids?.length ? body.instance_ids : null,
        rejudge: Boolean(body.rejudge),
        synthesis_only: Boolean(body.synthesis_only),
      };
    });
    if (!demoStore.playing) demoStore.play();
    demoStore.simulated(
      body.synthesis_only
        ? 'report regeneration — replays the captured pass report'
        : `judge pass — replays the captured pass's ${captured.results.length} judgment(s) on the clock, in judged_at order`,
    );
    return {
      run_id: runId,
      pass_id: passId,
      estimated_cost_usd:
        Math.round(
          captured.results.reduce((s, r) => s + (r.judge_cost_usd ?? 0), 0) *
            1e4,
        ) / 1e4,
      status: 'started (simulated)',
    };
  },

  getJudgeLive: async (runId) => {
    const { view } = await judgeFor(runId);
    return { run_id: runId, live: view.live };
  },

  listJudgePasses: async (runId) => {
    const { view } = await judgeFor(runId);
    return { run_id: runId, passes: view.passes };
  },

  getJudgeResults: async (runId) => {
    const { view } = await judgeFor(runId);
    return { run_id: runId, results: view.results };
  },

  reviewJudgeDimension: async (runId, instanceId, attemptNumber, body) => {
    if (!body.reviewer_reasoning?.trim()) {
      throw new ApiError(
        422,
        'reviewer_reasoning is required on both approve and deny',
      );
    }
    const rows = readReviews();
    rows.push({
      run_id: runId,
      instance_id: instanceId,
      attempt_number: attemptNumber,
      judged_at: body.judged_at,
      dimension_id: body.dimension_id,
      decision: body.decision,
      reviewer_reasoning: body.reviewer_reasoning,
      reviewed_by: body.reviewed_by ?? 'operator',
      reviewed_at: new Date().toISOString(),
      corrected_score_numeric: body.corrected_score_numeric ?? null,
      corrected_score_secondary: body.corrected_score_secondary ?? null,
      corrected_flag: body.corrected_flag ?? null,
    });
    writeReviews(rows);
    demoStore.simulated(
      `calibration ${body.decision} on ${body.dimension_id} stored in this browser's localStorage`,
    );
    return {
      run_id: runId,
      instance_id: instanceId,
      attempt_number: attemptNumber,
      dimension_id: body.dimension_id,
      status: 'recorded (localStorage)',
    };
  },

  getCalibrationSummary: async () => {
    const captured = await loadJson<CalibrationSummaryResponse>(
      'global/calibration.json',
    );
    const local = readReviews();
    const dimensions = captured.dimensions.map((d) => {
      const mine = local.filter((r) => r.dimension_id === d.dimension_id);
      const approve =
        d.approve_count + mine.filter((r) => r.decision === 'approve').length;
      const deny =
        d.deny_count + mine.filter((r) => r.decision === 'deny').length;
      const distinct =
        new Set(mine.map((r) => `${r.run_id}/${r.instance_id}`)).size +
        d.distinct_instances_reviewed;
      return {
        ...d,
        reviewed_count: d.reviewed_count + mine.length,
        distinct_instances_reviewed: distinct,
        approve_count: approve,
        deny_count: deny,
        endorsement_rate:
          approve + deny > 0 ? approve / (approve + deny) : null,
        cleared_threshold: distinct >= captured.min_distinct_reviewed_instances,
      };
    });
    return {
      dimensions,
      min_distinct_reviewed_instances: captured.min_distinct_reviewed_instances,
    };
  },

  getJudgeReviewHistory: async (runId, instanceId, attemptNumber, judgedAt) => {
    const reviews = readReviews()
      .filter(
        (r) =>
          r.run_id === runId &&
          r.instance_id === instanceId &&
          r.attempt_number === attemptNumber &&
          r.judged_at === judgedAt,
      )
      .sort((a, b) => b.reviewed_at.localeCompare(a.reviewed_at));
    const out: CalibrationReviewHistoryResponse = {
      run_id: runId,
      instance_id: instanceId,
      attempt_number: attemptNumber,
      reviews: reviews.map((r) => ({
        dimension_id: r.dimension_id,
        decision: r.decision,
        reviewer_reasoning: r.reviewer_reasoning,
        reviewed_by: r.reviewed_by,
        reviewed_at: r.reviewed_at,
        corrected_score_numeric: r.corrected_score_numeric,
        corrected_score_secondary: r.corrected_score_secondary,
        corrected_flag: r.corrected_flag,
      })),
    };
    return out;
  },

  // ---- read-only dashboard ----
  listRuns: async (p = {}) => {
    const list = await runsJson();
    const items: RunItem[] = [];
    for (const item of list.items) {
      const rewind =
        item.run_id === demoStore.runId || demoStore.hasSim(item.run_id);
      if (rewind && bundleIfLoaded(item.run_id)) {
        try {
          items.push(runItemAt(await ctx(item.run_id), item));
          continue;
        } catch {
          /* fall through to the captured row */
        }
      }
      items.push(item);
    }
    const filtered = p.status
      ? items.filter((r) => r.status === p.status)
      : items;
    const offset = p.offset ?? 0;
    const limit = p.limit ?? 50;
    return {
      items: filtered.slice(offset, offset + limit),
      total: filtered.length,
      limit,
      offset,
    };
  },

  getRun: async (runId) => runDetailAt(await ctx(runId)),

  getRunProgress: async (runId) => {
    const c = await ctx(runId);
    const captured = await progressJson(runId);
    if (untouched(c) && captured) return captured;
    const rows = rowsAt(c);
    const m = new Map<
      string,
      { phase: string; state: string; count: number }
    >();
    for (const r of rows) {
      const k = `${r.phase}|${r.state}`;
      const e = m.get(k) ?? { phase: r.phase, state: r.state, count: 0 };
      e.count += 1;
      m.set(k, e);
    }
    const summary = flatSummaryAt(c, rows);
    return {
      run_id: runId,
      status: c.status.status,
      terminal: c.status.terminal,
      phases: [...m.values()],
      expected: captured?.expected ?? Number(summary.expected),
      denominator: Number(summary.denominator),
    };
  },

  getRunLive: async (runId) => {
    const c = await ctx(runId);
    if (c.outage) {
      return {
        run_id: runId,
        status: c.status.status,
        state: 'unknown',
        reason: 'redis_unreachable (simulated Valkey outage)',
        items: [],
      };
    }
    return {
      run_id: runId,
      status: c.status.status,
      state: 'ok',
      reason: '',
      items: liveAt(c),
    };
  },

  getRunPacer: async (runId) => {
    const c = await ctx(runId);
    const models = await loadJson<ModelsResponse>('global/models.json').catch(
      () => ({ items: [] }),
    );
    return pacerAt(c, models);
  },

  getAutoscalerDecision: async (pool) => {
    const c = await currentCtx();
    if (!c)
      return {
        pool,
        state: demoStore.global.valkeyOutage ? 'unknown' : 'absent',
        age_s: null,
        record: null,
      };
    return autoscalerAt(c, pool);
  },

  getLimits: async (runId) => {
    const captured = await loadJson<LimitsView>('global/limits.json');
    const g = demoStore.global;
    if (g.valkeyOutage)
      return { ...captured, state: 'unknown', run: null, pacer: [] };
    const globalFields: Record<string, LimitField> = {};
    for (const [k, f] of Object.entries(captured.global_?.fields ?? {})) {
      globalFields[k] =
        k in g.globalOverrides
          ? {
              value: g.globalOverrides[k],
              source: g.globalOverrides[k] == null ? 'default' : 'operator',
              set_by: g.globalOverrides[k] == null ? null : 'operator',
            }
          : f;
    }
    const base: LimitsView = {
      ...captured,
      global_: { fields: globalFields },
      run: null,
      pacer: [],
    };
    const id = runId ?? currentRunId();
    if (!id) return base;
    const c = await ctx(id);
    const ll = c.run.launch_limits ?? {};
    const launched = (v: number | boolean | null | undefined): LimitField => ({
      value: v == null ? null : Number(v),
      source: v == null ? 'default' : 'run_launch',
      set_by: null,
    });
    const fields: Record<string, LimitField> = {
      max_parallel: launched(
        (c.sim?.launchLimits?.max_parallel_harness_tasks as
          number | undefined) ?? ll.max_parallel_harness_tasks,
      ),
      ceiling_override: { value: null, source: 'default', set_by: null },
      ramp_step_pct: launched(ll.ramp_step_pct),
      cooldown_s: launched(ll.ramp_cooldown_seconds),
      enabled: launched(
        ll.autoscaler_enabled == null ? null : ll.autoscaler_enabled ? 1 : 0,
      ),
    };
    const ov = ceilingOverrideAt(c);
    if (ov)
      fields.ceiling_override = {
        value: ov.value,
        source: ov.value == null ? 'default' : 'operator',
        set_by: ov.actor,
      };
    for (const [k, v] of Object.entries(c.sim?.runOverrides ?? {})) {
      if (k === 'ceiling_override') continue;
      fields[k] = {
        value: v,
        source: v == null ? 'default' : 'operator',
        set_by: v == null ? null : 'operator',
      };
    }
    const tick = tickAt(c.loaded.bundle.ticks, c.t);
    const pacer = c.loaded.bundle.targets.map((tg) => {
      const a = tick?.pacer?.[tg.model_alias] ?? {};
      const o = c.sim?.pacerOverrides[tg.model_alias] ?? {};
      const cfg: Record<string, number | null> = {
        r_tok: o.r_tok ?? a.r_tok ?? null,
        k_inflight: o.k_inflight ?? a.k_inflight ?? null,
        r_qps: o.r_qps ?? a.r_qps ?? null,
        c_burst: o.c_burst ?? a.c_burst ?? null,
        c_req: o.c_req ?? null,
        cached_weight: o.cached_weight ?? null,
      };
      return {
        harness: tg.harness,
        alias: tg.model_alias,
        pool: null,
        cfg,
        pool_cfg: null,
      };
    });
    return {
      ...base,
      run: { run_id: id, set_at: c.loaded.startEpoch, fields },
      pacer,
    };
  },

  setRunLimit: async (field, value, actor, reason) => {
    const id = currentRunId();
    if (!id) throw new ApiError(409, 'no run is being replayed');
    const c = await ctx(id);
    const before =
      (await demoApi.getLimits(id)).run?.fields[field]?.value ?? null;
    const num = value == null ? null : Number(value);
    demoStore.mutate(id, (s) => {
      s.runOverrides[field] = num;
      s.limitEdits.push({
        t: c.t,
        scope: 'run',
        target: id,
        field,
        old: before == null ? null : String(before),
        new: num == null ? null : String(num),
        actor,
        reason,
        pool: null,
      });
    });
    demoStore.simulated(
      `run limit ${field} = ${num ?? 'cleared'} — appended to the local audit list`,
    );
    const out: LimitEditResult = {
      scope: 'run',
      target: id,
      field,
      old: before == null ? null : String(before),
      new: num == null ? null : String(num),
      pool: null,
    };
    return out;
  },

  setGlobalLimit: async (field, value, actor, reason) => {
    const captured = await loadJson<LimitsView>('global/limits.json');
    const before =
      demoStore.global.globalOverrides[field] ??
      captured.global_?.fields[field]?.value ??
      null;
    const num = value == null ? null : Number(value);
    demoStore.mutateGlobal((g) => {
      g.globalOverrides[field] = num;
      g.globalEdits.push({
        t: demoStore.now(),
        scope: 'global',
        target: 'operator:limits',
        field,
        old: before == null ? null : String(before),
        new: num == null ? null : String(num),
        actor,
        reason,
        pool: null,
      });
    });
    demoStore.simulated(
      `global limit ${field} = ${num ?? 'cleared'} — appended to the local audit list`,
    );
    return {
      scope: 'global',
      target: 'operator:limits',
      field,
      old: before == null ? null : String(before),
      new: num == null ? null : String(num),
      pool: null,
    };
  },

  setPacerLimit: async (alias, field, value, actor, reason, alsoPool) => {
    const id = currentRunId();
    if (!id) throw new ApiError(409, 'no run is being replayed');
    const c = await ctx(id);
    const before =
      c.sim?.pacerOverrides[alias]?.[field] ??
      tickAt(c.loaded.bundle.ticks, c.t)?.pacer?.[alias]?.[field] ??
      null;
    demoStore.mutate(id, (s) => {
      s.pacerOverrides[alias] = {
        ...(s.pacerOverrides[alias] ?? {}),
        [field]: value,
      };
      s.limitEdits.push({
        t: c.t,
        scope: 'pacer',
        target: alias,
        field,
        old: before == null ? null : String(before),
        new: String(value),
        actor,
        reason,
        pool: alsoPool ? alias : null,
      });
    });
    demoStore.simulated(
      `pacer ${alias}.${field} = ${value}${alsoPool ? ' (also the pool seed)' : ''} — appended to the local audit list`,
    );
    return {
      scope: 'pacer',
      target: alias,
      field,
      old: before == null ? null : String(before),
      new: String(value),
      pool: alsoPool ? alias : null,
    };
  },

  getInstanceCalls: async (runId, instanceId, attemptNumber) => {
    const c = await ctx(runId);
    const lane = c.lanes.find(
      (l) => l.instance_id === instanceId && l.attempt === attemptNumber,
    );
    if (!lane) throw new ApiError(404, 'unknown attempt');
    const st = laneStatesAt(lane, c.t, c.endT);
    const empty: InstanceCalls = {
      run_id: runId,
      instance_id: instanceId,
      attempt_number: attemptNumber,
      items: [],
    };
    if (!st.harnessLanded || lane.harness_state == null) return empty; // rows land with llm_calls.jsonl at attempt end
    const srcKey = c.sim?.extraSources[laneKey(instanceId, attemptNumber)];
    const [srcInst, srcAttempt] = srcKey
      ? [srcKey.split('#')[0], Number(srcKey.split('#')[1])]
      : [instanceId, attemptNumber];
    try {
      const file = await loadGzJson<InstanceCalls>(
        `runs/${encodeURIComponent(runId)}/calls/${CALLS_FILE(srcInst, srcAttempt)}`,
      );
      return { ...file, attempt_number: attemptNumber };
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return empty;
      throw e;
    }
  },

  getQueues: async () => {
    // SQS was not polled per tick, but the capacity observer recorded each pool's queue
    // depth (visible) and not-visible count on every 30 s decision, so the harness-jobs
    // and eval-jobs cards replay from those. The results queue and the DLQs were not
    // observed at all; they are shown empty, which is what the one-off capture at the end
    // of the runs showed (global/queues.json).
    const c = await currentCtx();
    if (!c) {
      return loadJson<QueuesList>('global/queues.json').catch(
        (): QueuesList => ({ items: [] }),
      );
    }
    const latest: Record<string, ReplayCapacity | undefined> = {};
    for (const r of c.loaded.bundle.capacity) {
      if (r.t > c.t) break;
      latest[r.pool] = r;
    }
    const num = (v: unknown): number | null =>
      typeof v === 'number' && Number.isFinite(v) ? v : null;
    const view = (
      queue: string,
      r: ReplayCapacity | undefined,
      oldest: number | null,
    ): QueueView => ({
      queue,
      visible: r?.queue_depth ?? 0,
      not_visible: r?.not_visible ?? 0,
      oldest_age_s: oldest,
      dlq_depth: 0,
    });
    const h = latest.harness;
    const out: QueuesList = {
      items: [
        view('harness-jobs', h, num(h?.decision?.queue_head_wait_s)),
        view('eval-jobs', latest.eval, null),
        view('results', undefined, null),
      ],
    };
    return out;
  },

  listInstances: async (runId, p = {}) => {
    const c = await ctx(runId);
    let rows = untouched(c) ? (await instancesJson(runId)).items : rowsAt(c);
    if (p.state) rows = rows.filter((r) => r.state === p.state);
    if (p.error_category)
      rows = rows.filter((r) => r.error_category === p.error_category);
    const offset = p.offset ?? 0;
    const limit = p.limit ?? 50;
    const out: InstancesList = {
      items: rows.slice(offset, offset + limit),
      total: rows.length,
      limit,
      offset,
    };
    return out;
  },

  getInstance: async (runId, instanceId, attempt) => {
    const c = await ctx(runId);
    const rows = rowsAt(c).filter(
      (r) => r.instance_id === instanceId && r.attempt_number === attempt,
    );
    if (rows.length === 0)
      throw new ApiError(404, 'no rows for this attempt yet');
    return {
      run_id: runId,
      instance_id: instanceId,
      attempt_number: attempt,
      rows,
    };
  },

  listCapacity: async (p = {}) => {
    const c = await currentCtx();
    if (!c) return { items: [] };
    const since = p.since ? Date.parse(p.since) / 1000 : null;
    const items: CapacityPoint[] = [];
    for (const r of c.loaded.bundle.capacity) {
      if (r.t > c.t) break;
      if (p.pool && r.pool !== p.pool) continue;
      const ts = epoch(c, r.t);
      if (since != null && ts < since) continue;
      items.push({
        ts: new Date(ts * 1000).toISOString(),
        pool: r.pool,
        queue_depth: r.queue_depth,
        not_visible: r.not_visible,
        gateway_headroom: null,
        current_workers: r.current_workers,
        desired: r.desired,
        binding_constraint: r.binding_constraint,
        ceiling_utilization: r.ceiling_utilization,
        decision_age_s: r.decision_age_s,
        eta_low_s: r.eta_low_s,
        eta_high_s: r.eta_high_s,
        constants_source: r.constants_source,
        pacer_queue_len: r.pacer_queue_len,
        paced_over_2s_share: r.paced_over_2s_share,
        decision: r.decision,
      });
    }
    const limit = p.limit ?? 500;
    const out: CapacityList = { items: items.slice(-limit) };
    return out;
  },

  artifact: async (runId, instanceId, attempt, kind) => {
    const c = await ctx(runId);
    const lane = c.lanes.find(
      (l) => l.instance_id === instanceId && l.attempt === attempt,
    );
    if (!lane) throw new ApiError(404, 'unknown attempt');
    if (!c.loaded.laneIndex.has(laneKey(instanceId, attempt))) {
      throw new ApiError(
        404,
        'simulated attempt — no artifacts were produced (nothing ran)',
      );
    }
    const st = laneStatesAt(lane, c.t, c.endT);
    const evalKind =
      kind === 'report' || kind === 'test_output' || kind === 'run_log';
    if (evalKind ? !st.evalLanded : !st.harnessLanded) {
      throw new ApiError(
        404,
        'not produced yet — this attempt is still in flight at the replay time',
      );
    }
    try {
      return await loadGzText(
        `runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(instanceId)}/${attempt}/${kind}.gz`,
      );
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        throw new ApiError(
          404,
          'artifact not published in this snapshot (artifacts are included for three of the runs)',
        );
      }
      throw e;
    }
  },

  // ---- launch catalog ----
  listDatasetInstances: async (p = {}) => {
    const d = await loadJson<DatasetInstancesResponse>(
      'global/dataset_instances.json',
    );
    const offset = p.offset ?? 0;
    const limit = p.limit ?? d.items.length;
    return { items: d.items.slice(offset, offset + limit), total: d.total };
  },

  listHarnesses: () => loadJson<HarnessesResponse>('global/harnesses.json'),

  listModels: () => loadJson<ModelsResponse>('global/models.json'),

  instructionPresets: () =>
    loadJson<InstructionPresetsResponse>('global/presets.json'),

  launchRun: async (req: RunLaunchRequest): Promise<LaunchResult> => {
    const all = await targets();
    const wantPreset = Boolean(req.harness_instructions?.trim());
    const n = req.instance_ids === 'all' ? 500 : req.instance_ids.length;
    const matches = all.filter(
      (t) => t.harness === req.harness && t.model_alias === req.model_alias,
    );
    if (matches.length === 0) {
      const pairs = [
        ...new Set(all.map((t) => `${t.harness} × ${t.model_alias}`)),
      ]
        .sort()
        .join('; ');
      demoStore.simulated(
        `launch refused — no captured run for ${req.harness} × ${req.model_alias}`,
      );
      return {
        kind: 'refused',
        message: `demo: no captured run for (${req.harness}, ${req.model_alias}). Captured pairs: ${pairs}`,
      };
    }
    const score = (t: LaunchTarget): number =>
      (t.preset === wantPreset ? 2 : 0) +
      (Math.abs(t.instances - n) < Math.abs(500 - n) === t.instances >= 300
        ? 1
        : 0);
    const target = [...matches].sort((a, b) => score(b) - score(a))[0];
    if (demoStore.runId === target.run_id) {
      const c = await ctx(target.run_id);
      if (!c.status.terminal) {
        demoStore.simulated(
          'launch → 409 duplicate — that (harness, model) is already replaying and not terminal',
        );
        return {
          kind: 'duplicate',
          data: {
            status: 'duplicate',
            run_id: target.run_id,
            harness: target.harness,
            model_alias: target.model_alias,
            message:
              'a run with this (harness, model_alias) is already active in the replay — open it or wait for it to finish',
          },
        };
      }
    }
    demoStore.switchRun(target.run_id, { reset: true });
    demoStore.mutate(target.run_id, (s) => {
      s.launchLimits = {
        max_cost_usd_per_instance: req.max_cost_usd_per_instance,
        max_turns_per_instance: req.max_turns_per_instance,
        timeout_seconds: req.timeout_seconds,
        max_parallel_harness_tasks: req.max_parallel_harness_tasks,
        ramp_step_pct: req.ramp_step_pct,
        ramp_cooldown_seconds: req.ramp_cooldown_seconds,
        autoscaler_enabled: req.autoscaler_enabled,
        harness_instructions: req.harness_instructions ?? null,
      };
    });
    demoStore.simulated(
      `launch resolved to the captured run ${target.run_id.slice(-8)} (${target.harness} × ${target.model_alias}${target.preset ? ', preset' : ''}) — its replay starts at t = 0 with your limits shown as the run's`,
    );
    return {
      kind: 'launched',
      data: {
        status: 'launched (simulated)',
        run_id: target.run_id,
        dispatched: target.instances,
        seeded: target.instances,
      },
    };
  },
};

/** the run the Explorer replays when nothing else was chosen */
export async function defaultRunId(): Promise<string> {
  const ids = await publishedIds();
  return ids.find((id) => id.endsWith(DEFAULT_RUN_SUFFIX)) ?? ids[0];
}

export { publishedIds };
