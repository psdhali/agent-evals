import type { PauseSpan, RunSim } from './store';
import type {
  LoadedBundle,
  ReplayCall,
  ReplayCapacity,
  ReplayLane,
  ReplayTick,
} from './types';
import { laneKey } from './types';

// Pure replay derivations (§8.1 of the 2026-09-10 plan): given a bundle, the
// simulated state and a replay time t, what did the run look like at t? Every
// function here is deterministic and has no I/O, so the ladder, the abort
// sweep and the pause hold are unit-testable (replay.test.ts).

export const ABORT_DRAIN_S = 60;
export const ABORT_SWEEP_S = 30;
export const STALE_AFTER_S = 300;
export const HARNESS_ACTIVE = new Set([
  'PENDING',
  'DISPATCHED',
  'HARNESS_RUNNING',
]);
export const EVAL_ACTIVE = new Set(['PENDING', 'EVAL_RUNNING']);

// ---- pause hold ------------------------------------------------------------

/** Shift a stamp by every pause span that began before it: a lane whose
 * dispatch falls inside a pause is held until the resume, and everything
 * after it slides by the pause's length (an open span holds forever). */
export function shiftStamp(
  stamp: number,
  spans: readonly PauseSpan[],
  now: number,
): number {
  let s = stamp;
  for (const span of [...spans].sort((a, b) => a.from - b.from)) {
    const to = span.to ?? Math.max(now, span.from);
    if (s >= span.from) s += to - span.from;
  }
  return s;
}

export function pausedAt(spans: readonly PauseSpan[], t: number): boolean {
  return spans.some((s) => t >= s.from && (s.to == null || t < s.to));
}

const HARNESS_STAMPS = [
  't_dispatched',
  't_running',
  't_first_call',
  't_harness_finished',
  't_eval_enqueued',
  't_eval_started',
  't_eval_finished',
] as const;
const EVAL_STAMPS = [
  't_eval_enqueued',
  't_eval_started',
  't_eval_finished',
] as const;

/** The lane with the simulated pauses and abort applied — stamps shifted by
 * the holds, final states rewritten by the sweep. `now` is the replay time
 * the open holds are measured against. */
export function effectiveLane(
  lane: ReplayLane,
  sim: RunSim | null,
  now: number,
): ReplayLane {
  if (!sim) return lane;
  let out: ReplayLane = { ...lane };
  const holdHarness = [...sim.control.harness, ...sim.control.gateway];
  if (holdHarness.length && out.t_dispatched != null) {
    const d = out.t_dispatched;
    const shift = shiftStamp(d, holdHarness, now) - d;
    if (shift > 0) {
      for (const k of HARNESS_STAMPS) {
        if (out[k] != null) out = { ...out, [k]: out[k]! + shift };
      }
    }
  }
  if (sim.control.eval.length && out.t_eval_enqueued != null) {
    const e = out.t_eval_enqueued;
    const shift = shiftStamp(e, sim.control.eval, now) - e;
    if (shift > 0) {
      for (const k of EVAL_STAMPS) {
        if (out[k] != null) out = { ...out, [k]: out[k]! + shift };
      }
    }
  }
  if (sim.abort) out = sweep(out, sim.abort.t, sim.abort.scope);
  return out;
}

/** The real abort sweep, applied at replay time `a` (control/abort.py's rules):
 * PENDING → NEVER_DISPATCHED, DISPATCHED / HARNESS_RUNNING → ABORTED_IN_FLIGHT,
 * EVAL_RUNNING (and eval PENDING) → ABANDONED. Scope `harness` leaves grading
 * in flight to finish; `eval` leaves agents to finish; `all` sweeps both. */
export function sweep(lane: ReplayLane, a: number, scope: string): ReplayLane {
  const st = laneStatesAt(lane, a, Number.POSITIVE_INFINITY);
  let out = lane;
  const harnessScope = scope === 'harness' || scope === 'all';
  const evalScope = scope === 'eval' || scope === 'all';
  if (harnessScope) {
    if (st.harness === 'PENDING') {
      out = {
        ...out,
        harness_state: 'NEVER_DISPATCHED',
        t_dispatched: null,
        t_running: null,
        t_first_call: null,
        t_harness_finished: a + ABORT_SWEEP_S,
        t_eval_enqueued: null,
        t_eval_started: null,
        t_eval_finished: null,
        eval_state: null,
        verdict: null,
        t_stop_requested: a,
      };
    } else if (
      st.harness === 'DISPATCHED' ||
      st.harness === 'HARNESS_RUNNING'
    ) {
      out = {
        ...out,
        harness_state: 'ABORTED_IN_FLIGHT',
        t_harness_finished: a + ABORT_SWEEP_S,
        t_eval_enqueued: null,
        t_eval_started: null,
        t_eval_finished: null,
        eval_state: null,
        verdict: null,
        t_stop_requested: a,
      };
    }
  }
  if (evalScope && out.t_eval_enqueued != null) {
    const evalDone = out.t_eval_finished != null && out.t_eval_finished <= a;
    if (!evalDone) {
      out = {
        ...out,
        eval_state: 'ABANDONED',
        verdict: null,
        grade_invalid: null,
        t_eval_started: Math.min(out.t_eval_started ?? a, a),
        t_eval_finished: Math.max(out.t_eval_enqueued, a) + ABORT_SWEEP_S,
      };
    }
  }
  return out;
}

// ---- the state ladder ------------------------------------------------------

export interface LaneStates {
  harness: string | null;
  /** null = the eval row does not exist yet at t */
  eval: string | null;
  harnessLanded: boolean;
  evalLanded: boolean;
}

/** PENDING → DISPATCHED → HARNESS_RUNNING → (harness final) and, once the eval
 * row exists, PENDING → EVAL_RUNNING → verdict — from the lane's stamps. A lane
 * with no stamps stays PENDING until its final state lands at the run's end. */
export function laneStatesAt(
  lane: ReplayLane,
  t: number,
  endT: number,
): LaneStates {
  const { t_dispatched: d, t_running: r, t_harness_finished: hf } = lane;
  const { t_eval_enqueued: ee, t_eval_started: es, t_eval_finished: ef } = lane;
  const hFinal = lane.harness_state;
  let harness: string | null;
  let harnessLanded = false;
  if (hFinal == null) {
    harness = null; // an eval-only (regrade) lane has no harness row
  } else if (hFinal === 'NEVER_DISPATCHED') {
    if (hf != null ? t >= hf : t >= endT) {
      harness = hFinal;
      harnessLanded = true;
    } else harness = 'PENDING';
  } else if (d == null) {
    if ((hf != null && t >= hf) || t >= endT) {
      harness = hFinal;
      harnessLanded = true;
    } else harness = 'PENDING';
  } else if (t < d) harness = 'PENDING';
  else if (r == null || t < r)
    harness = hf != null && t >= hf ? landed() : 'DISPATCHED';
  else if (hf == null || t < hf) harness = 'HARNESS_RUNNING';
  else harness = landed();

  function landed(): string {
    harnessLanded = true;
    return hFinal as string;
  }

  let evalState: string | null = null;
  let evalLanded = false;
  if (ee != null && t >= ee && lane.eval_state != null) {
    if (es == null || t < es) evalState = 'PENDING';
    else if (ef == null || t < ef) evalState = 'EVAL_RUNNING';
    else {
      evalState = lane.eval_state;
      evalLanded = true;
    }
  }
  return { harness, eval: evalState, harnessLanded, evalLanded };
}

export function laneInFlight(st: LaneStates): boolean {
  return (
    (st.harness != null &&
      HARNESS_ACTIVE.has(st.harness) &&
      st.harness !== 'PENDING') ||
    (st.eval != null && EVAL_ACTIVE.has(st.eval))
  );
}

/** when the lane's last row lands (harness-only lanes: the harness row) */
export function laneEnd(lane: ReplayLane): number | null {
  return lane.t_eval_finished ?? lane.t_harness_finished;
}

// ---- whole-run views ---------------------------------------------------------

export function allLanes(
  loaded: LoadedBundle,
  sim: RunSim | null,
  now: number,
): ReplayLane[] {
  const base = loaded.bundle.lanes;
  const extra = sim?.extraLanes ?? [];
  const lanes = extra.length ? [...base, ...extra] : base;
  return sim ? lanes.map((l) => effectiveLane(l, sim, now)) : lanes;
}

/** the replay time at which the run has nothing left in flight */
export function effectiveEnd(
  lanes: readonly ReplayLane[],
  bundleEnd: number,
): number {
  let end = 0;
  for (const l of lanes) {
    const e = laneEnd(l);
    if (e != null && e > end) end = e;
  }
  return Math.max(end, Math.min(bundleEnd, end > 0 ? bundleEnd : 0));
}

export interface RunStatusAt {
  status: string;
  terminal: boolean;
  stop_requested_t: number | null;
  stop_scope: string | null;
  stop_reason: string | null;
  stopped_t: number | null;
  finalised_t: number | null;
}

export function runStatusAt(
  loaded: LoadedBundle,
  lanes: readonly ReplayLane[],
  sim: RunSim | null,
  t: number,
): RunStatusAt {
  const { stamps, final_status, window } = loaded.bundle;
  const endT = effectiveEnd(lanes, window.end_t);
  const inFlight = lanes.some((l) => laneInFlight(laneStatesAt(l, t, endT)));
  const started = lanes.some(
    (l) => l.t_dispatched != null && t >= l.t_dispatched,
  );
  let terminal = started && !inFlight && t >= endT;

  if (sim?.closed && t >= sim.closed.t) {
    return {
      status: 'completed',
      terminal: true,
      stop_requested_t: sim.abort?.t ?? stamps.t_stop_requested,
      stop_scope: sim.abort?.scope ?? stamps.stop_scope,
      stop_reason: sim.abort?.reason ?? stamps.stop_reason,
      stopped_t: sim.abort ? sim.abort.t + ABORT_DRAIN_S : stamps.t_stopped,
      finalised_t: sim.closed.t,
    };
  }
  if (sim?.abort && t >= sim.abort.t) {
    const settled = t >= sim.abort.t + ABORT_DRAIN_S;
    return {
      status: settled ? 'aborted' : 'aborting',
      terminal: settled && terminal,
      stop_requested_t: sim.abort.t,
      stop_scope: sim.abort.scope,
      stop_reason: sim.abort.reason,
      stopped_t: settled ? sim.abort.t + ABORT_DRAIN_S : null,
      finalised_t: null,
    };
  }
  if (stamps.t_stop_requested != null && t >= stamps.t_stop_requested) {
    const stopped = stamps.t_stopped != null && t >= stamps.t_stopped;
    return {
      status: stopped ? 'aborted' : 'aborting',
      terminal: stopped && terminal,
      stop_requested_t: stamps.t_stop_requested,
      stop_scope: stamps.stop_scope,
      stop_reason: stamps.stop_reason,
      stopped_t: stopped ? stamps.t_stopped : null,
      finalised_t: null,
    };
  }
  if (t >= endT && terminal) {
    return {
      status:
        final_status === 'aborted' ? 'aborted' : final_status || 'completed',
      terminal: true,
      stop_requested_t: null,
      stop_scope: null,
      stop_reason: null,
      stopped_t: null,
      finalised_t: stamps.t_finalised,
    };
  }
  const tick = tickAt(loaded.bundle.ticks, t);
  terminal = false;
  return {
    status:
      tick?.run_status && tick.run_status !== 'completed'
        ? tick.run_status
        : 'running',
    terminal,
    stop_requested_t: null,
    stop_scope: null,
    stop_reason: null,
    stopped_t: null,
    finalised_t: null,
  };
}

/** the last tick at or before t (ticks are sorted) */
export function tickAt(
  ticks: readonly ReplayTick[],
  t: number,
): ReplayTick | null {
  let lo = 0;
  let hi = ticks.length - 1;
  let best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (ticks[mid].t <= t) {
      best = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return best >= 0 ? ticks[best] : null;
}

export function capacityAt(
  rows: readonly ReplayCapacity[],
  pool: string,
  t: number,
): ReplayCapacity | null {
  let best: ReplayCapacity | null = null;
  for (const r of rows) {
    if (r.t > t) break;
    if (r.pool === pool) best = r;
  }
  return best;
}

/** the lane's calls that started at or before t (sorted) */
export function callsPrefix(
  loaded: LoadedBundle,
  laneIdx: number,
  t: number,
): ReplayCall[] {
  const calls = loaded.callsByLane.get(laneIdx) ?? [];
  let n = 0;
  while (n < calls.length && calls[n][1] <= t) n += 1;
  return calls.slice(0, n);
}

export function laneIndexOf(
  loaded: LoadedBundle,
  lane: ReplayLane,
  sim: RunSim | null,
): number | undefined {
  const key = laneKey(lane.instance_id, lane.attempt);
  const direct = loaded.laneIndex.get(key);
  if (direct != null) return direct;
  const src = sim?.extraSources[key];
  return src ? loaded.laneIndex.get(src) : undefined;
}

/** For an extra (restart) lane the calls are the source lane's, re-based on the
 * new dispatch time. */
export function callsPrefixFor(
  loaded: LoadedBundle,
  lane: ReplayLane,
  sim: RunSim | null,
  t: number,
): ReplayCall[] {
  const idx = laneIndexOf(loaded, lane, sim);
  if (idx == null) return [];
  const key = laneKey(lane.instance_id, lane.attempt);
  if (
    loaded.laneIndex.has(key) &&
    !sim?.control.harness.length &&
    !sim?.control.gateway.length
  ) {
    return callsPrefix(loaded, idx, t);
  }
  // re-base: the source lane's first call → this lane's first call
  const src = loaded.bundle.lanes[idx];
  const shift = (lane.t_first_call ?? 0) - (src.t_first_call ?? 0);
  return callsPrefix(loaded, idx, t - shift).map(
    (c) =>
      [
        c[0],
        c[1] + shift,
        c[2] + shift,
        c[3],
        c[4],
        c[5],
        c[6],
        c[7],
        c[8],
      ] as ReplayCall,
  );
}

export interface CallTotals {
  calls: number;
  tok_in: number | null;
  tok_out: number | null;
  tok_cached: number | null;
  tok_reasoning: number | null;
  cost_usd: number | null;
  last_t: number | null;
}

export function totalsOf(calls: readonly ReplayCall[]): CallTotals {
  const sum = (i: 3 | 4 | 5 | 6 | 7): number | null => {
    let s = 0;
    let any = false;
    for (const c of calls) {
      const v = c[i];
      if (v != null) {
        s += v;
        any = true;
      }
    }
    return any ? s : null;
  };
  return {
    calls: calls.length,
    tok_in: sum(3),
    tok_out: sum(4),
    tok_cached: sum(5),
    tok_reasoning: sum(6),
    cost_usd: sum(7),
    last_t: calls.length ? calls[calls.length - 1][1] : null,
  };
}

/** a simulated restart: attempt N+1 of `src`, its whole timeline re-based so
 * it dispatches at `t` */
export function restartLane(
  src: ReplayLane,
  t: number,
  attempt: number,
  retryReason: string,
): ReplayLane | null {
  const base = src.t_dispatched;
  if (base == null) return null;
  const move = (v: number | null): number | null =>
    v == null ? null : v - base + t;
  return {
    ...src,
    attempt,
    retry_reason: retryReason,
    t_dispatched: t,
    t_running: move(src.t_running),
    t_first_call: move(src.t_first_call),
    t_harness_finished: move(src.t_harness_finished),
    t_eval_enqueued: move(src.t_eval_enqueued),
    t_eval_started: move(src.t_eval_started),
    t_eval_finished: move(src.t_eval_finished),
    t_stop_requested: null,
  };
}

/** a simulated regrade: an eval-only attempt N+1 of `src`, enqueued at `t` */
export function regradeLane(
  src: ReplayLane,
  t: number,
  attempt: number,
): ReplayLane | null {
  if (src.t_eval_enqueued == null || src.eval_state == null) return null;
  const base = src.t_eval_enqueued;
  const move = (v: number | null): number | null =>
    v == null ? null : v - base + t;
  return {
    ...src,
    attempt,
    retry_reason: 'operator_regrade',
    harness_state: null,
    t_dispatched: null,
    t_running: null,
    t_first_call: null,
    t_harness_finished: null,
    t_eval_enqueued: t,
    t_eval_started: move(src.t_eval_started),
    t_eval_finished: move(src.t_eval_finished),
    t_stop_requested: null,
    turns: null,
    tok_in: null,
    tok_out: null,
    cost_usd: null,
    calls: null,
  };
}
