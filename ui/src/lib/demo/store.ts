import { bundleIfLoaded } from './bundle';
import type { ReplayLane } from './types';

// The demo's one piece of mutable state: the replay clock plus everything the
// simulated actions changed. Plain class + subscribe so `demoApi` (plain
// functions) and React (useSyncExternalStore in DemoProvider) read the same
// object. Nothing here ever leaves the browser.

export interface PauseSpan {
  from: number;
  to: number | null;
}

export interface SimControl {
  harness: PauseSpan[];
  eval: PauseSpan[];
  gateway: PauseSpan[];
  /** the latest simulated pause/resume per pool — wins over the tick's flag */
  overrides: Partial<Record<'harness' | 'eval' | 'gateway', boolean>>;
  updated_by: string;
  reason: string;
  published_t: number | null;
}

export interface SimAbort {
  t: number;
  scope: string;
  reason: string;
  actor: string;
}

export interface SimLimitEdit {
  t: number;
  scope: string;
  target: string;
  field: string;
  old: string | null;
  new: string | null;
  actor: string;
  reason: string;
  pool: string | null;
}

export interface SimJudge {
  t_launch: number;
  pass_id: string;
  prune_mode: string;
  max_spend_usd: number;
  workers: number;
  instance_ids: string[] | null;
  rejudge: boolean;
  synthesis_only: boolean;
}

export interface RunSim {
  control: SimControl;
  abort: SimAbort | null;
  closed: { t: number } | null;
  gatewayBlockedBy: string | null;
  /** attempt N+1 lanes added by a simulated restart / regrade */
  extraLanes: ReplayLane[];
  /** `${instance}#${attempt}` of an extra lane → the lane it was copied from */
  extraSources: Record<string, string>;
  limitEdits: SimLimitEdit[];
  runOverrides: Record<string, number | null>;
  pacerOverrides: Record<string, Record<string, number>>;
  judge: SimJudge | null;
  burst: { t: number } | null;
  /** the limits a simulated launch chose (shown as the run's) */
  launchLimits: Record<string, unknown> | null;
}

export interface GlobalSim {
  globalOverrides: Record<string, number | null>;
  globalEdits: SimLimitEdit[];
  valkeyOutage: boolean;
  discoveries: Record<
    string,
    { startedAt: number; targetTasks: number; rampMode: string }
  >;
  manualCeilings: Record<
    string,
    { tpm: number; notes: string | null; at: string }
  >;
}

export type JumpKind =
  | 'first_dispatched'
  | 'first_resolved'
  | 'first_overload'
  | 'half_terminal'
  | 'terminal';

export const SPEEDS = [1, 2, 5, 10, 20, 60] as const;
export const DEFAULT_SPEED = 20;

function freshSim(): RunSim {
  return {
    control: {
      harness: [],
      eval: [],
      gateway: [],
      overrides: {},
      updated_by: '',
      reason: '',
      published_t: null,
    },
    abort: null,
    closed: null,
    gatewayBlockedBy: null,
    extraLanes: [],
    extraSources: {},
    limitEdits: [],
    runOverrides: {},
    pacerOverrides: {},
    judge: null,
    burst: null,
    launchLimits: null,
  };
}

function freshGlobal(): GlobalSim {
  return {
    globalOverrides: {},
    globalEdits: [],
    valkeyOutage: false,
    discoveries: {},
    manualCeilings: {},
  };
}

export interface Toast {
  id: number;
  text: string;
  at: number;
}

const wall = (): number =>
  typeof performance !== 'undefined' ? performance.now() : Date.now();

export class DemoStore {
  runId: string | null = null;
  playing = false;
  speed = DEFAULT_SPEED;
  /** bumped on every clock discontinuity or simulated action — the provider
   * invalidates every react-query on a change so polls re-answer at once */
  epoch = 0;
  version = 0;
  toasts: Toast[] = [];
  global: GlobalSim = freshGlobal();

  private baseT = 0;
  private baseWall = wall();
  private positions = new Map<string, { t: number; playing: boolean }>();
  private sims = new Map<string, RunSim>();
  private listeners = new Set<() => void>();
  private toastSeq = 0;

  // ---- subscription ----
  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };
  getVersion = (): number => this.version;
  private notify(discontinuity = false): void {
    this.version += 1;
    if (discontinuity) this.epoch += 1;
    for (const fn of this.listeners) fn();
  }

  // ---- clock ----
  endOf(runId: string): number | null {
    return bundleIfLoaded(runId)?.bundle.window.end_t ?? null;
  }

  /** replay seconds since the current run's window start (clamped to its end) */
  now(): number {
    if (!this.runId) return 0;
    let t = this.playing
      ? this.baseT + ((wall() - this.baseWall) / 1000) * this.speed
      : this.baseT;
    const end = this.endOf(this.runId);
    if (end != null && t >= end) {
      t = end;
      if (this.playing) {
        this.baseT = end;
        this.baseWall = wall();
        this.playing = false;
        this.notify();
      }
    }
    return Math.max(0, t);
  }

  /** the replay time to answer a run's query at: the clock for the current run,
   * the window's end (its captured, terminal state) for every other run */
  tFor(runId: string): number {
    if (runId === this.runId) return this.now();
    return this.endOf(runId) ?? Number.POSITIVE_INFINITY;
  }

  play(): void {
    if (this.playing) return;
    this.baseWall = wall();
    this.playing = true;
    this.notify();
  }
  pause(): void {
    if (!this.playing) return;
    this.baseT = this.now();
    this.baseWall = wall();
    this.playing = false;
    this.notify();
  }
  toggle(): void {
    if (this.playing) this.pause();
    else this.play();
  }
  setSpeed(speed: number): void {
    this.baseT = this.now();
    this.baseWall = wall();
    this.speed = speed;
    this.notify();
  }
  seek(t: number): void {
    const end = this.runId ? this.endOf(this.runId) : null;
    this.baseT = Math.max(0, end != null ? Math.min(t, end) : t);
    this.baseWall = wall();
    this.notify(true);
  }
  /** t = 0, every simulated action of this run undone, outage/burst cleared */
  restart(): void {
    if (this.runId) this.sims.delete(this.runId);
    this.global.valkeyOutage = false;
    this.baseT = 0;
    this.baseWall = wall();
    this.playing = true;
    this.notify(true);
  }
  jump(kind: JumpKind): number | null {
    if (!this.runId) return null;
    const loaded = bundleIfLoaded(this.runId);
    if (!loaded) return null;
    const { lanes, ticks, window } = loaded.bundle;
    const min = (xs: (number | null)[]): number | null => {
      const v = xs.filter((x): x is number => x != null);
      return v.length ? Math.min(...v) : null;
    };
    let t: number | null = null;
    if (kind === 'first_dispatched') t = min(lanes.map((l) => l.t_dispatched));
    else if (kind === 'first_resolved')
      t = min(
        lanes.map((l) => (l.verdict === 'resolved' ? l.t_eval_finished : null)),
      );
    else if (kind === 'first_overload') {
      const tick = ticks.find((tk) =>
        Object.values(tk.pacer ?? {}).some((a) => (a.overloads_60s ?? 0) > 0),
      );
      t = tick ? tick.t : null;
    } else if (kind === 'half_terminal') {
      const ends = lanes
        .map((l) => l.t_eval_finished ?? l.t_harness_finished)
        .filter((x): x is number => x != null)
        .sort((a, b) => a - b);
      t = ends.length ? ends[Math.floor(ends.length / 2)] : null;
    } else if (kind === 'terminal') t = window.end_t;
    if (t == null) return null;
    this.seek(t);
    return t;
  }

  /** make `runId` the replayed run; resumes its saved position (or t = 0,
   * playing) — the run's simulated state is kept until "restart replay" */
  switchRun(runId: string, opts: { reset?: boolean } = {}): void {
    if (this.runId === runId && !opts.reset) return;
    if (this.runId) {
      this.positions.set(this.runId, { t: this.now(), playing: this.playing });
    }
    this.runId = runId;
    const saved = opts.reset ? undefined : this.positions.get(runId);
    if (opts.reset) this.sims.delete(runId);
    this.baseT = saved?.t ?? 0;
    this.baseWall = wall();
    this.playing = saved?.playing ?? true;
    this.notify(true);
  }

  // ---- simulated state ----
  sim(runId: string): RunSim {
    let s = this.sims.get(runId);
    if (!s) {
      s = freshSim();
      this.sims.set(runId, s);
    }
    return s;
  }
  hasSim(runId: string): boolean {
    return this.sims.has(runId);
  }
  /** mutate a run's simulated state and wake every poller */
  mutate(runId: string, fn: (sim: RunSim) => void): void {
    fn(this.sim(runId));
    this.notify(true);
  }
  mutateGlobal(fn: (g: GlobalSim) => void): void {
    fn(this.global);
    this.notify(true);
  }

  // ---- toasts ----
  toast(text: string): void {
    const id = ++this.toastSeq;
    this.toasts = [...this.toasts.slice(-3), { id, text, at: Date.now() }];
    this.notify();
    setTimeout(() => {
      this.toasts = this.toasts.filter((t) => t.id !== id);
      this.notify();
    }, 6000);
  }
  /** every simulated action says so, the same way, every time */
  simulated(what: string): void {
    this.toast(`simulated — ${what} · nothing left the browser`);
  }
}

export const demoStore = new DemoStore();
