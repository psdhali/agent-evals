// The replay bundle written by scripts/export_run_ui_snapshot.py — one per run,
// `data/runs/<run_id>/replay.json.gz`. Every `t` is seconds since the run's
// window start (its dispatched_at); every measured number is nullable and null
// means "not measured", never 0.

export interface ReplayLane {
  instance_id: string;
  attempt: number;
  /** DISPATCHED — placed from the observer's own per-tick PENDING curve. */
  t_dispatched: number | null;
  /** HARNESS_RUNNING — worker pickup (first call − image pull − boot − prep). */
  t_running: number | null;
  t_first_call: number | null;
  /** the harness row lands (PATCH_READY / EMPTY_PATCH / …). */
  t_harness_finished: number | null;
  /** the eval row is inserted (eval PENDING). */
  t_eval_enqueued: number | null;
  t_eval_started: number | null;
  /** the verdict lands. */
  t_eval_finished: number | null;
  stamp_source: 'calls' | 'eval_row' | 'tick_curve' | 'none' | string;
  harness_state: string | null;
  eval_state: string | null;
  verdict: string | null;
  error_category: string | null;
  retry_reason: string | null;
  grade_invalid: boolean | null;
  turns: number | null;
  tok_in: number | null;
  tok_out: number | null;
  cost_usd: number | null;
  calls: number | null;
  t_stop_requested: number | null;
}

export interface ReplayCount {
  phase: string;
  state: string;
  count: number;
}

export type PacerLedgerAlias = Record<string, number | null>;

export interface ReplayTick {
  t: number;
  run_status: string | null;
  in_flight: number | null;
  stale: number | null;
  pending: number | null;
  harness_running: number | null;
  eval_running: number | null;
  resolved: number | null;
  unresolved: number | null;
  aborted: number | null;
  expected: number | null;
  denominator: number | null;
  tok_in: number | null;
  tok_out: number | null;
  tok_cached: number | null;
  tok_reasoning: number | null;
  cost_usd_live: number | null;
  cost_usd_landed: number | null;
  harness_paused: boolean | null;
  eval_paused: boolean | null;
  gateway_paused: boolean | null;
  control_stale: boolean | null;
  counts: ReplayCount[];
  pacer: Record<string, PacerLedgerAlias>;
}

export interface ReplayCapacity {
  t: number;
  pool: string;
  queue_depth: number | null;
  not_visible: number | null;
  current_workers: number | null;
  desired: number | null;
  binding_constraint: string | null;
  ceiling_utilization: number | null;
  decision_age_s: number | null;
  eta_low_s: number | null;
  eta_high_s: number | null;
  constants_source: string | null;
  pacer_queue_len: number | null;
  paced_over_2s_share: number | null;
  decision: Record<string, unknown>;
}

export interface ReplayLimitEdit {
  t: number | null;
  scope: string;
  target: string;
  field: string;
  old_value: string | null;
  new_value: string | null;
  actor: string;
  reason: string;
}

/** [lane index, t_start, t_end, tok_in, tok_out, tok_cached, tok_reasoning,
 *  cost_usd, http_status] */
export type ReplayCall = [
  number,
  number,
  number,
  number | null,
  number | null,
  number | null,
  number | null,
  number | null,
  number | null,
];

export interface ReplayBundle {
  schema_version: number;
  run_id: string;
  harness: string | null;
  model_alias: string | null;
  window: { start: string; end_t: number; seconds: number };
  final_status: string;
  stamps: {
    created_at: string | null;
    dispatched_at: string | null;
    stop_requested_at: string | null;
    stop_scope: string | null;
    stop_reason: string | null;
    stopped_at: string | null;
    finalised_at: string | null;
    t_stop_requested: number | null;
    t_stopped: number | null;
    t_finalised: number | null;
  };
  tick_interval_s: number | null;
  targets: { harness: string; model_alias: string }[];
  lanes: ReplayLane[];
  ticks: ReplayTick[];
  capacity: ReplayCapacity[];
  events: Record<string, unknown>[];
  limit_edits: ReplayLimitEdit[];
  discovery: Record<string, unknown>[];
  calls_columns: string[];
  calls: ReplayCall[];
}

/** A bundle plus the indexes the adapter needs on every poll. */
export interface LoadedBundle {
  bundle: ReplayBundle;
  /** epoch seconds of window.start */
  startEpoch: number;
  /** calls per lane index, sorted by t_start */
  callsByLane: Map<number, ReplayCall[]>;
  laneIndex: Map<string, number>; // `${instance}#${attempt}` → lane index
}

export const laneKey = (instanceId: string, attempt: number): string =>
  `${instanceId}#${attempt}`;
