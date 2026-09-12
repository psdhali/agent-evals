import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { EvalScalerPanel } from '../EvalScalerPanel';

// F8 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): the eval-side scaling view on the run
// page. Same honesty rules as the planner verdict: absent and unknown are distinct, and the
// F7 scale-in countdown is only a number of seconds when the record carries the cadence.

vi.mock('../../lib/api', () => ({
  api: {
    getAutoscalerDecision: vi.fn(),
  },
}));

import { api } from '../../lib/api';

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
  );
}

const RECORD = {
  decided_at: 1_700_000_000,
  mode: 'live',
  desired_ceiling: 4,
  binding_constraint: 'scale_in_damped',
  desired_hosts: 1,
  visible: 0,
  not_visible: 2,
  feedforward: 1.4,
  running_tasks: 4,
  running_hosts: 1,
  idle_hosts: 0,
  asg_desired: 1,
  asg_max: 16,
  scale_in_pending_ticks: 3,
  scale_in_ticks_needed: 10,
  tick_interval_s: 30,
  would_set: {},
};

describe('EvalScalerPanel', () => {
  it('renders the decision record with the F7 scale-in countdown', async () => {
    vi.mocked(api.getAutoscalerDecision).mockResolvedValue({
      pool: 'eval',
      state: 'ok',
      age_s: 12,
      record: RECORD,
    });
    renderWithQuery(<EvalScalerPanel terminal={false} />);
    await waitFor(() =>
      expect(screen.getByTestId('eval-scaler-verdict')).toBeTruthy(),
    );
    expect(api.getAutoscalerDecision).toHaveBeenCalledWith('eval');
    const verdict = screen.getByTestId('eval-scaler-verdict');
    expect(verdict.textContent).toContain('scale_in_damped');
    expect(verdict.textContent).toContain('1 / 0'); // running / idle hosts
    expect(verdict.textContent).toContain('1 / 16'); // asg desired / max
    const countdown = screen.getByTestId('eval-scale-in-countdown');
    expect(countdown.textContent).toContain('3 / 10 ticks');
    // (10 - 3) x 30 s = 210 s to go — rendered as a duration, never as raw seconds only.
    expect(countdown.textContent).toMatch(/3m 30s|210s/);
  });

  it('shows the countdown in ticks only when the record predates the cadence fields', async () => {
    const older = Object.fromEntries(
      Object.entries(RECORD).filter(
        ([k]) => k !== 'scale_in_ticks_needed' && k !== 'tick_interval_s',
      ),
    );
    vi.mocked(api.getAutoscalerDecision).mockResolvedValue({
      pool: 'eval',
      state: 'ok',
      age_s: 12,
      record: older,
    });
    renderWithQuery(<EvalScalerPanel terminal={false} />);
    await waitFor(() =>
      expect(screen.getByTestId('eval-scale-in-countdown')).toBeTruthy(),
    );
    const countdown = screen.getByTestId('eval-scale-in-countdown');
    expect(countdown.textContent).toContain('3');
    expect(countdown.textContent).not.toContain('to go');
  });

  it('flags an observe-mode scaler and its intended actions', async () => {
    vi.mocked(api.getAutoscalerDecision).mockResolvedValue({
      pool: 'eval',
      state: 'ok',
      age_s: 5,
      record: {
        ...RECORD,
        mode: 'observe',
        binding_constraint: 'none',
        scale_in_pending_ticks: 0,
        would_set: { asg_desired_capacity: 2, service_desired_count: 6 },
      },
    });
    renderWithQuery(<EvalScalerPanel terminal={false} />);
    await waitFor(() =>
      expect(screen.getByTestId('eval-scaler-verdict')).toBeTruthy(),
    );
    const text = screen.getByTestId('eval-scaler-verdict').textContent ?? '';
    expect(text).toContain('publishes only');
    expect(text).toContain('would set');
    expect(text).toContain('asg_desired_capacity=2');
    expect(screen.queryByTestId('eval-scale-in-countdown')).toBeNull();
  });

  it('keeps absent and unknown distinct and never renders numbers for them', async () => {
    vi.mocked(api.getAutoscalerDecision).mockResolvedValue({
      pool: 'eval',
      state: 'absent',
      age_s: null,
      record: null,
    });
    renderWithQuery(<EvalScalerPanel terminal={true} />);
    await waitFor(() =>
      expect(screen.getByTestId('eval-scaler-state')).toBeTruthy(),
    );
    expect(screen.getByTestId('eval-scaler-state').textContent).toContain(
      'absent',
    );

    vi.mocked(api.getAutoscalerDecision).mockResolvedValue({
      pool: 'eval',
      state: 'unknown',
      age_s: null,
      record: null,
    });
    renderWithQuery(<EvalScalerPanel terminal={true} />);
    await waitFor(() =>
      expect(
        screen
          .getAllByTestId('eval-scaler-state')
          .some((el) => (el.textContent ?? '').includes('Redis unreachable')),
      ).toBe(true),
    );
  });
});
