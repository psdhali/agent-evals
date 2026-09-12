import { describe, expect, it } from 'vitest';

import { deriveRates } from '../rates';

describe('deriveRates (ADR-0038 §4, client-side)', () => {
  it('counts empty patches in gradeable and excludes harness crashes', () => {
    // The first 500-run at 21:00Z: 379 R / 115 U / 3 EMPTY / 2 FAILED_HARNESS / 1 EVAL_RUNNING
    const r = deriveRates(
      [
        { state: 'RESOLVED', count: 379 },
        { state: 'UNRESOLVED', count: 115 },
        { state: 'EMPTY_PATCH', count: 3 },
        { state: 'FAILED_HARNESS', count: 2 },
        { state: 'EVAL_RUNNING', count: 1 },
      ],
      500,
    );
    expect(r.gradeable).toBe(497);
    expect(r.excluded).toBe(2);
    expect(r.excludedStates).toEqual(['FAILED_HARNESS 2']);
    expect(r.inFlight).toBe(1);
    expect(r.rateGradeable).toBeCloseTo(379 / 497, 6);
    expect(r.rateAttempted).toBeCloseTo(379 / 500, 6);
  });

  it('is empty-safe: no buckets, no denominator', () => {
    const r = deriveRates([], null);
    expect(r.gradeable).toBe(0);
    expect(r.rateGradeable).toBeNull();
    expect(r.rateAttempted).toBeNull();
    expect(r.attempted).toBeNull();
  });

  it('treats budget stops, aborts and abandoned as excluded, not as model verdicts', () => {
    const r = deriveRates(
      [
        { state: 'RESOLVED', count: 1 },
        { state: 'HARNESS_BUDGET_EXCEEDED', count: 1 },
        { state: 'ABANDONED', count: 1 },
        { state: 'ABORTED_IN_FLIGHT', count: 1 },
      ],
      4,
    );
    expect(r.gradeable).toBe(1);
    expect(r.excluded).toBe(3);
    expect(r.rateGradeable).toBe(1);
    expect(r.rateAttempted).toBe(0.25);
  });
});
