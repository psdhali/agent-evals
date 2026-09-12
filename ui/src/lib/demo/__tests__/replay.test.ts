import { describe, expect, it } from 'vitest';
import {
  ABORT_SWEEP_S,
  effectiveLane,
  laneStatesAt,
  shiftStamp,
  sweep,
} from '../replay';
import type { RunSim } from '../store';
import type { ReplayLane } from '../types';

// §8.4 of the 2026-09-10 plan: the replay's three correctness rules — a lane
// rewinds through the state ladder in order, a simulated abort produces the
// real sweep states, a pause holds dispatches until the resume.

function lane(over: Partial<ReplayLane> = {}): ReplayLane {
  return {
    instance_id: 'django__django-1',
    attempt: 1,
    t_dispatched: 100,
    t_running: 130,
    t_first_call: 150,
    t_harness_finished: 400,
    t_eval_enqueued: 401,
    t_eval_started: 410,
    t_eval_finished: 700,
    stamp_source: 'calls',
    harness_state: 'PATCH_READY',
    eval_state: 'RESOLVED',
    verdict: 'resolved',
    error_category: null,
    retry_reason: null,
    grade_invalid: false,
    turns: 12,
    tok_in: 1000,
    tok_out: 100,
    cost_usd: 0.01,
    calls: 12,
    t_stop_requested: null,
    ...over,
  };
}

function sim(over: Partial<RunSim> = {}): RunSim {
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
    ...over,
  };
}

const END = 10_000;

describe('laneStatesAt — the state ladder', () => {
  it('walks PENDING → DISPATCHED → HARNESS_RUNNING → PATCH_READY, then eval PENDING → EVAL_RUNNING → verdict, in order', () => {
    const l = lane();
    const seen: string[] = [];
    for (let t = 0; t <= 800; t += 1) {
      const st = laneStatesAt(l, t, END);
      const label = `${st.harness}${st.eval ? `/${st.eval}` : ''}`;
      if (seen[seen.length - 1] !== label) seen.push(label);
    }
    expect(seen).toEqual([
      'PENDING',
      'DISPATCHED',
      'HARNESS_RUNNING',
      'PATCH_READY',
      'PATCH_READY/PENDING',
      'PATCH_READY/EVAL_RUNNING',
      'PATCH_READY/RESOLVED',
    ]);
    expect(laneStatesAt(l, 399, END).harnessLanded).toBe(false);
    expect(laneStatesAt(l, 400, END).harnessLanded).toBe(true);
    expect(laneStatesAt(l, 699, END).evalLanded).toBe(false);
    expect(laneStatesAt(l, 700, END).evalLanded).toBe(true);
  });

  it('a lane with no derivable stamps stays PENDING until the run ends, then lands its final state', () => {
    const l = lane({
      t_dispatched: null,
      t_running: null,
      t_first_call: null,
      t_harness_finished: null,
      t_eval_enqueued: null,
      t_eval_started: null,
      t_eval_finished: null,
      stamp_source: 'none',
      harness_state: 'FAILED_HARNESS',
      eval_state: null,
      verdict: null,
    });
    expect(laneStatesAt(l, 5000, END).harness).toBe('PENDING');
    expect(laneStatesAt(l, END, END)).toMatchObject({
      harness: 'FAILED_HARNESS',
      harnessLanded: true,
      eval: null,
    });
  });

  it('a captured NEVER_DISPATCHED lane is PENDING until the sweep lands it', () => {
    const l = lane({
      t_dispatched: null,
      t_running: null,
      t_first_call: null,
      t_harness_finished: 6400,
      t_eval_enqueued: null,
      t_eval_started: null,
      t_eval_finished: null,
      harness_state: 'NEVER_DISPATCHED',
      eval_state: null,
      verdict: null,
    });
    expect(laneStatesAt(l, 6399, END).harness).toBe('PENDING');
    expect(laneStatesAt(l, 6400, END).harness).toBe('NEVER_DISPATCHED');
  });
});

describe('sweep — a simulated abort applies the real sweep rules', () => {
  const pending = lane({
    instance_id: 'p',
    t_dispatched: 900,
    t_running: 930,
    t_first_call: 950,
    t_harness_finished: 1200,
    t_eval_enqueued: 1201,
    t_eval_started: 1210,
    t_eval_finished: 1500,
  });
  const dispatched = lane({
    instance_id: 'd',
    t_dispatched: 490,
    t_running: 520,
    t_first_call: 540,
    t_harness_finished: 900,
    t_eval_enqueued: 901,
    t_eval_started: 910,
    t_eval_finished: 1200,
  });
  const running = lane({
    instance_id: 'r',
    t_dispatched: 100,
    t_running: 130,
    t_harness_finished: 900,
    t_eval_enqueued: 901,
    t_eval_started: 910,
    t_eval_finished: 1200,
  });
  const grading = lane({
    instance_id: 'g',
    t_dispatched: 10,
    t_running: 20,
    t_harness_finished: 300,
    t_eval_enqueued: 301,
    t_eval_started: 310,
    t_eval_finished: 900,
  });
  const done = lane({
    instance_id: 'x',
    t_dispatched: 1,
    t_running: 2,
    t_harness_finished: 100,
    t_eval_enqueued: 101,
    t_eval_started: 110,
    t_eval_finished: 200,
  });

  it('scope harness: PENDING → NEVER_DISPATCHED, DISPATCHED / HARNESS_RUNNING → ABORTED_IN_FLIGHT, grading continues', () => {
    const a = 500;
    const after = a + ABORT_SWEEP_S + 1;
    expect(laneStatesAt(sweep(pending, a, 'harness'), after, END).harness).toBe(
      'NEVER_DISPATCHED',
    );
    expect(
      laneStatesAt(sweep(dispatched, a, 'harness'), after, END).harness,
    ).toBe('ABORTED_IN_FLIGHT');
    expect(laneStatesAt(sweep(running, a, 'harness'), after, END).harness).toBe(
      'ABORTED_IN_FLIGHT',
    );
    // a swept in-flight lane never grades
    expect(
      laneStatesAt(sweep(running, a, 'harness'), 2000, END).eval,
    ).toBeNull();
    // grading in flight at the abort is left to finish under scope harness
    expect(laneStatesAt(sweep(grading, a, 'harness'), 900, END)).toMatchObject({
      eval: 'RESOLVED',
      evalLanded: true,
    });
    // an already-landed lane is untouched
    expect(sweep(done, a, 'harness')).toEqual(done);
  });

  it('scope all: EVAL_RUNNING → ABANDONED as well', () => {
    const a = 500;
    const swept = sweep(grading, a, 'all');
    expect(laneStatesAt(swept, a + ABORT_SWEEP_S, END)).toMatchObject({
      eval: 'ABANDONED',
      evalLanded: true,
    });
    expect(swept.verdict).toBeNull();
  });

  it('before the abort time nothing changes', () => {
    const s = sim({
      abort: { t: 500, scope: 'harness', reason: 'test', actor: 'op' },
    });
    expect(laneStatesAt(effectiveLane(running, s, 400), 400, END).harness).toBe(
      'HARNESS_RUNNING',
    );
    expect(laneStatesAt(effectiveLane(pending, s, 400), 400, END).harness).toBe(
      'PENDING',
    );
  });
});

describe('pause — held dispatches', () => {
  it('shiftStamp holds a stamp inside a pause until the resume and slides later ones by the pause length', () => {
    const spans = [{ from: 100, to: 160 }];
    expect(shiftStamp(50, spans, 1000)).toBe(50); // before the pause: untouched
    expect(shiftStamp(120, spans, 1000)).toBe(180); // inside: released at resume (+60)
    expect(shiftStamp(500, spans, 1000)).toBe(560); // after: slid by the pause
  });

  it('an open pause holds every later dispatch for as long as it lasts', () => {
    const spans = [{ from: 100, to: null }];
    expect(shiftStamp(120, spans, 300)).toBeGreaterThanOrEqual(300);
    expect(shiftStamp(120, spans, 900)).toBeGreaterThanOrEqual(900);
  });

  it('a lane whose dispatch falls in a harness pause stays PENDING while paused and dispatches after the resume', () => {
    const l = lane({
      t_dispatched: 120,
      t_running: 150,
      t_first_call: 170,
      t_harness_finished: 420,
      t_eval_enqueued: 421,
      t_eval_started: 430,
      t_eval_finished: 720,
    });
    const paused = sim({
      control: {
        harness: [{ from: 100, to: null }],
        eval: [],
        gateway: [],
        overrides: { harness: true },
        updated_by: 'op',
        reason: 'hold',
        published_t: 100,
      },
    });
    for (const t of [130, 300, 600]) {
      expect(laneStatesAt(effectiveLane(l, paused, t), t, END).harness).toBe(
        'PENDING',
      );
    }
    const resumed = sim({
      control: {
        harness: [{ from: 100, to: 400 }],
        eval: [],
        gateway: [],
        overrides: { harness: false },
        updated_by: 'op',
        reason: '',
        published_t: 400,
      },
    });
    const e = effectiveLane(l, resumed, 1000);
    expect(e.t_dispatched).toBe(420); // 120 + the 300 s hold
    expect(laneStatesAt(e, 419, END).harness).toBe('PENDING');
    expect(laneStatesAt(e, 421, END).harness).toBe('DISPATCHED');
    expect(e.t_eval_finished).toBe(1020); // everything downstream slid too
  });

  it('a lane already in flight when the pause starts is not held', () => {
    const l = lane();
    const paused = sim({
      control: {
        harness: [{ from: 200, to: null }],
        eval: [],
        gateway: [],
        overrides: { harness: true },
        updated_by: '',
        reason: '',
        published_t: 200,
      },
    });
    expect(effectiveLane(l, paused, 300)).toEqual(l);
    expect(laneStatesAt(effectiveLane(l, paused, 300), 300, END).harness).toBe(
      'HARNESS_RUNNING',
    );
  });
});
