import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PacerPanel } from '../PacerPanel';

// BUILDER4-PACER-SEED-AND-FAIRNESS §2.5: the live pacer ledger. Same honesty rules as
// LivePanel — the whole-response unknown state is never an empty list, an unmeasured
// alias is explicit, and a waiting call is a visible ROW (its size + wait), head first.

vi.mock('../../lib/api', () => ({
  api: {
    getRunPacer: vi.fn(),
    getAutoscalerDecision: vi.fn().mockResolvedValue({
      pool: 'harness',
      state: 'absent',
      age_s: null,
      record: null,
    }),
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

const MEASURED = {
  alias: 'laguna-xs-2.1-custom_minimal',
  harness: 'custom_minimal',
  measured: true,
  c_burst: 1_000_000,
  r_tok: 50_000,
  k_inflight: 2_000_000,
  c_req: 100,
  r_qps: 1.5,
  seeded_at: 1_700_000_000,
  bucket_level: 600_000,
  bucket_fill: 0.6,
  req_level: 100,
  req_fill: 1.0,
  inflight_calls: 1,
  inflight_tokens: 120_000,
  inflight_fill: 0.06,
  queue_len: 2,
  head_est_tokens: 130_000,
  head_waiting_s: 4,
  waiters: [
    { est_tokens: 130_000, waiting_s: 4 },
    { est_tokens: 1_000, waiting_s: 20 },
  ],
  admits_60s: 10,
  over_2s_60s: 1,
  mean_wait_ms_60s: 960,
  overloads_60s: 0,
  observed_at: 1_700_000_100,
};

describe('PacerPanel — the live admission ledger', () => {
  it('renders the whole-response unknown state distinctly, never as an empty list', async () => {
    vi.mocked(api.getRunPacer).mockResolvedValue({
      run_id: 'run-1',
      state: 'unknown',
      reason: 'redis_unreachable',
      items: [],
    });
    renderWithQuery(<PacerPanel runId="run-1" terminal={false} />);
    await waitFor(() =>
      expect(screen.getByText(/pacer state unknown/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/no pacer aliases/i)).not.toBeInTheDocument();
  });

  it('renders an unmeasured alias explicitly with no fabricated numbers', async () => {
    vi.mocked(api.getRunPacer).mockResolvedValue({
      run_id: 'run-1',
      state: 'ok',
      reason: null,
      items: [
        {
          ...MEASURED,
          measured: false,
          queue_len: null,
          waiters: [],
          bucket_fill: null,
        },
      ],
    });
    renderWithQuery(<PacerPanel runId="run-1" terminal={false} />);
    await waitFor(() =>
      expect(screen.getByText(/not measured/i)).toBeInTheDocument(),
    );
    // no queue readout, no bars — nothing numeric is rendered for an unmeasured alias
    // (the footer caption mentions "wait queue" in prose, so match the readout by title)
    expect(screen.queryByTitle(/calls currently DENIED/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/token bucket/i)).not.toBeInTheDocument();
  });

  it('shows the wait queue with the head-of-line call first, and the cfg in force', async () => {
    vi.mocked(api.getRunPacer).mockResolvedValue({
      run_id: 'run-1',
      state: 'ok',
      reason: null,
      items: [MEASURED],
    });
    renderWithQuery(<PacerPanel runId="run-1" terminal={false} />);
    // wait on DATA-dependent text (the footer caption mentions "wait queue" in prose and
    // renders before the query resolves — waiting on it would assert the loading state)
    await screen.findByText(/head 130,000 tok waiting/);
    // cfg line
    expect(screen.getByText(/r_tok 50,000\/s/)).toBeInTheDocument();
    // queue summary names the head (130K) and its wait
    expect(screen.getByText(/head 130,000 tok waiting/)).toBeInTheDocument();
    // both waiters rendered as chips, head first
    const chips = screen.getAllByText(/tok · /);
    expect(chips[0]).toHaveTextContent('130,000 tok');
    expect(chips[1]).toHaveTextContent('1,000 tok');
    // last-60s counters
    expect(screen.getByText(/waits>2s/i)).toBeInTheDocument();
  });

  it('renders the planner verdict (HOLDING + why) and the per-alias line from the record', async () => {
    vi.mocked(api.getRunPacer).mockResolvedValue({
      run_id: 'run-1',
      state: 'ok',
      reason: null,
      items: [MEASURED],
    });
    vi.mocked(api.getAutoscalerDecision).mockResolvedValue({
      pool: 'harness',
      state: 'ok',
      age_s: 4,
      record: {
        mode: 'live',
        desired_ceiling: 6,
        in_flight_tasks: 6,
        binding_constraint: 'arrival_budget',
        binding_alias: 'laguna-xs-2.1-custom_minimal',
        booting_tasks: 2,
        paced_timeouts: 0,
        queue_len: 2,
        queue_head_wait_s: 4,
        static_cap: 145,
        growth_clamped: true,
        recovery_set: { 'laguna-xs-2.1-custom_minimal': { r_tok: [1000, 680] } },
        aliases: {
          'laguna-xs-2.1-custom_minimal': {
            tasks: 4,
            booting: 2,
            ceiling: 6,
            binding: 'arrival_budget',
            curve_source: 'fitted:laguna-xs-2.1|custom_minimal',
            budgets_source: 'pacer_cfg',
          },
        },
      },
    });
    renderWithQuery(<PacerPanel runId="run-1" terminal={false} />);
    const verdict = await screen.findByTestId('planner-verdict');
    expect(verdict).toHaveTextContent('HOLDING');
    expect(verdict).toHaveTextContent('arrival_budget (laguna-xs-2.1-custom_minimal)');
    expect(verdict).toHaveTextContent('6 / 6');
    // Scaling review F5/F6/F2: the cap in force, and the two budget events a tick can carry.
    expect(verdict).toHaveTextContent('cap 145');
    expect(verdict).toHaveTextContent('growth clamped');
    expect(verdict).toHaveTextContent('recovery ↓ laguna-xs-2.1-custom_minimal');
    expect(await screen.findByText(/curve fitted:laguna-xs-2\.1\|custom_minimal/)).toBeInTheDocument();
  });

  it('says the planner record is absent, never "go", when there is none', async () => {
    vi.mocked(api.getRunPacer).mockResolvedValue({
      run_id: 'run-1',
      state: 'ok',
      reason: null,
      items: [],
    });
    vi.mocked(api.getAutoscalerDecision).mockResolvedValue({
      pool: 'harness',
      state: 'absent',
      age_s: null,
      record: null,
    });
    renderWithQuery(<PacerPanel runId="run-1" terminal={false} />);
    expect(await screen.findByText(/planner verdict absent/i)).toBeInTheDocument();
  });
});
