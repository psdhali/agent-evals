import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ControlPanel } from '../ControlPanel';

// Tests 1–3 of phase-2 §5: the three-way control-state render is a
// correctness rule, not a render check.  All three build a mock /control
// response and assert what the operator must be able to trust.

vi.mock('../../lib/api', () => ({
  api: {
    getControl: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    abort: vi.fn(),
    closeRun: vi.fn(),
    pauseGateway: vi.fn(),
    resumeGateway: vi.fn(),
  },
}));

import { api } from '../../lib/api';

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    client,
    ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>),
  };
}

const STALE_PAUSED = {
  harness_paused: true,
  eval_paused: true,
  gateway_paused: true,
  published_at: 0,
  stale: true,
  aborted_runs: [],
  updated_by: '',
  reason: '',
};

const LIVE_RUNNING = {
  harness_paused: false,
  eval_paused: false,
  gateway_paused: false,
  published_at: Date.now() / 1000,
  stale: false,
  aborted_runs: [],
  updated_by: '',
  reason: '',
};

describe('ControlPanel — phase 2 §5 correctness rules', () => {
  it('stale never renders as a confident PAUSED', async () => {
    vi.mocked(api.getControl).mockResolvedValue(STALE_PAUSED);
    renderWithClient(<ControlPanel />);

    // The stale badge must show, and every pool must render *unknown* —
    // never a confident PAUSED off a read we cannot trust.
    await waitFor(() =>
      expect(screen.getByText(/stale · fail-closed/i)).toBeInTheDocument(),
    );
    expect(screen.getAllByText('unknown').length).toBeGreaterThan(0);
    expect(screen.queryByText('PAUSED')).not.toBeInTheDocument();
  });

  it('unreachable beats stale — an error must not read data.stale', async () => {
    vi.mocked(api.getControl).mockRejectedValue(new Error('ECONNREFUSED'));
    renderWithClient(<ControlPanel />);

    await waitFor(() =>
      expect(screen.getByText(/unreachable/i)).toBeInTheDocument(),
    );
    // isError takes precedence over data.stale (ControlPanel.tsx phase chain).
    expect(screen.getByText(/GET \/control failed/i)).toBeInTheDocument();
    expect(screen.queryByText('PAUSED')).not.toBeInTheDocument();
  });

  it('abort does not flip to "aborted" on a 200 with settled=false', async () => {
    vi.mocked(api.getControl).mockResolvedValue(LIVE_RUNNING);
    const abortReport = {
      run_id: 'run-1',
      status: 'aborted',
      scope: 'eval',
      reason: 'test',
      actor: 'operator',
      in_flight_stopped: 3,
      drained: 0,
      drain_skipped: false,
      drain_skip_reason: '',
      settled: false,
      swept: 0,
      abort_not_instant: true,
      note: 'abort is bounded by stopTimeout + upload + results-queue settle',
    };
    vi.mocked(api.abort).mockResolvedValue(abortReport);

    renderWithClient(<ControlPanel runId="run-1" />);

    const arm = await screen.findByRole('checkbox', {
      name: /confirm abort/i,
    });
    await userEvent.click(arm);
    await userEvent.click(screen.getByRole('button', { name: 'Abort' }));

    // settled=false must render *draining*, not a done state.
    await waitFor(() =>
      expect(
        screen.getByText(/draining — abort is bounded/i),
      ).toBeInTheDocument(),
    );
    // the "do not treat this 200 as done" guidance (curly quotes in the copy)
    expect(screen.getByText(/do not treat this 200 as/i)).toBeInTheDocument();
  });

  // BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §5: resuming a
  // run's gateway key is the same risk class as restart ("launches real
  // inference calls") — the resume button must stay disabled until the
  // confirm checkbox is checked, same discipline as RunDetail's restart gate.
  it('resume-gateway confirm gate cannot be bypassed', async () => {
    vi.mocked(api.getControl).mockResolvedValue(LIVE_RUNNING);
    vi.mocked(api.resumeGateway).mockResolvedValue({
      run_id: 'run-1',
      gateway_key_blocked_by: null,
    });

    renderWithClient(
      <ControlPanel runId="run-1" gatewayKeyBlockedBy="operator" />,
    );

    const resumeButton = await screen.findByRole('button', {
      name: /resume gateway/i,
    });
    // Blocked (operator), but not yet confirmed — must still be disabled.
    expect(resumeButton).toBeDisabled();
    expect(api.resumeGateway).not.toHaveBeenCalled();

    const confirm = screen.getByRole('checkbox', { name: /confirm resume/i });
    await userEvent.click(confirm);
    expect(resumeButton).toBeEnabled();

    await userEvent.click(resumeButton);
    await waitFor(() => expect(api.resumeGateway).toHaveBeenCalledTimes(1));
  });

  // F3 (implementation review, 2026-08-31): the GLOBAL resume button had no
  // confirm at all, and it's the higher-consequence action — it unblocks
  // EVERY active run's key at once vs. one for the per-run button above.
  it('global resume confirm gate: required when gateway is selected, not otherwise', async () => {
    vi.mocked(api.getControl).mockResolvedValue(LIVE_RUNNING);
    vi.mocked(api.resume).mockResolvedValue({
      pools: ['harness'],
      paused: false,
      reason: '',
      actor: 'operator',
    });

    renderWithClient(<ControlPanel />);

    // Default selection is harness only (no gateway) — Resume must be
    // clickable with no confirm needed, same as before this fix.
    const resumeButton = await screen.findByRole('button', { name: 'Resume' });
    expect(resumeButton).toBeEnabled();
    expect(
      screen.queryByRole('checkbox', {
        name: /confirm resume — includes gateway/i,
      }),
    ).not.toBeInTheDocument();

    // Selecting "gateway" must gate the SAME button behind a confirm.
    await userEvent.click(screen.getByRole('button', { name: 'Gateway' }));
    expect(resumeButton).toBeDisabled();

    const confirm = await screen.findByRole('checkbox', {
      name: /confirm resume — includes gateway/i,
    });
    await userEvent.click(confirm);
    expect(resumeButton).toBeEnabled();

    await userEvent.click(resumeButton);
    await waitFor(() => expect(api.resume).toHaveBeenCalledTimes(1));
  });

  it('global resume confirm re-arms when the pool selection changes', async () => {
    vi.mocked(api.getControl).mockResolvedValue(LIVE_RUNNING);

    renderWithClient(<ControlPanel />);

    await userEvent.click(
      await screen.findByRole('button', { name: 'Gateway' }),
    );
    const confirm = await screen.findByRole('checkbox', {
      name: /confirm resume — includes gateway/i,
    });
    await userEvent.click(confirm);
    expect(confirm).toBeChecked();

    // Toggling any pool re-arms — the prior "yes" was for one selection,
    // not a different one (same discipline as RunDetail's restart gate).
    await userEvent.click(screen.getByRole('button', { name: 'Eval' }));
    expect(
      screen.getByRole('checkbox', {
        name: /confirm resume — includes gateway/i,
      }),
    ).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled();
  });

  it('resume-gateway confirm is disabled with nothing blocked to resume', async () => {
    vi.mocked(api.getControl).mockResolvedValue(LIVE_RUNNING);

    renderWithClient(<ControlPanel runId="run-1" gatewayKeyBlockedBy={null} />);

    expect(await screen.findByText(/not blocked/i)).toBeInTheDocument();
    expect(
      screen.getByRole('checkbox', { name: /confirm resume/i }),
    ).toBeDisabled();
    expect(
      screen.getByRole('button', { name: /resume gateway/i }),
    ).toBeDisabled();
    // Pause is the safe direction — no confirm needed, must be clickable.
    expect(
      screen.getByRole('button', { name: /pause gateway/i }),
    ).toBeEnabled();
  });
});
