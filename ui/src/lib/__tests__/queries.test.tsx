import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { isAnyRunActive, useAnyRunActive } from '../queries';
import type { RunItem } from '../api';

// Test 4 of phase 2 §5: polling stops while no run is active (§1a).
// `isAnyRunActive` is the decision; `useAnyRunActive` must re-poll the
// activity signal while active and stop (refetchInterval → false) once no
// run is non-terminal.  Aurora auto-pause makes this a money rule, not a
// tidiness one.

vi.mock('../../lib/api', () => ({
  api: {
    listRuns: vi.fn(),
  },
}));

import { api } from '../../lib/api';

function run(overrides: Partial<RunItem> = {}): RunItem {
  return {
    run_id: 'run-1',
    status: 'running',
    created_at: null,
    estimated_cost_usd: null,
    cost_confidence_tier: null,
    compute_cost_estimated_usd: null,
    compute_cost_reconciled_usd: null,
    budget_cap_usd: null,
    stop_requested_at: null,
    stop_scope: null,
    stop_reason: null,
    stopped_at: null,
    summary: null,
    terminal: false,
    ...overrides,
  };
}

describe('isAnyRunActive — the polling-gate decision (§1a)', () => {
  it('is false when there are no runs at all', () => {
    expect(isAnyRunActive([])).toBe(false);
    expect(isAnyRunActive(undefined)).toBe(false);
  });

  it('is false when every run is terminal', () => {
    expect(
      isAnyRunActive([
        run({ terminal: true, status: 'aborted' }),
        run({ terminal: true, status: 'running' }),
      ]),
    ).toBe(false);
  });

  it('is true when at least one run is non-terminal', () => {
    expect(
      isAnyRunActive([
        run({ terminal: true, status: 'aborted' }),
        run({ terminal: false }),
      ]),
    ).toBe(true);
  });
});

describe('useAnyRunActive — polling follows run activity', () => {
  it('re-polls while a run is active, stops when none is', async () => {
    vi.useFakeTimers();
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    // Phase 1: one active run → the activity query must keep polling.
    const activeList = { items: [run()], total: 1, limit: 500, offset: 0 };
    vi.mocked(api.listRuns).mockResolvedValue(activeList);
    const { result } = renderHook(() => useAnyRunActive(), { wrapper });

    await act(() => vi.advanceTimersByTimeAsync(0)); // flush initial fetch
    expect(result.current.isSuccess).toBe(true);
    expect(isAnyRunActive(result.current.data?.items)).toBe(true);
    const callsWhileActive = vi.mocked(api.listRuns).mock.calls.length;

    // Past two 20 s windows — polling on means more fetch calls.
    await act(() => vi.advanceTimersByTimeAsync(45_000));
    expect(vi.mocked(api.listRuns).mock.calls.length).toBeGreaterThan(
      callsWhileActive,
    );

    // Phase 2: every run terminal → polling must stop (no new calls).
    const terminalList = {
      items: [run({ terminal: true, status: 'aborted' })],
      total: 1,
      limit: 500,
      offset: 0,
    };
    vi.mocked(api.listRuns).mockResolvedValue(terminalList);
    await act(() => vi.advanceTimersByTimeAsync(45_000)); // let it notice
    expect(isAnyRunActive(result.current.data?.items)).toBe(false);

    const callsNow = vi.mocked(api.listRuns).mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(120_000));
    expect(vi.mocked(api.listRuns).mock.calls.length).toBe(callsNow);

    vi.useRealTimers();
  });
});
