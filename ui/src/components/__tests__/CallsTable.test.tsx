import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { CallsTable } from '../CallsTable';

// BUILDER4-PACER-SEED-AND-FAIRNESS §2.6: the per-call wall-clock decomposition. A pacer
// refusal (our own hold cap, latency 0, never reached the provider) must be labelled as
// such — it is the row that explains run 01788405363237319353's death.

vi.mock('../../lib/api', () => ({
  api: {
    getInstanceCalls: vi.fn(),
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

const BASE = {
  started_at: '2026-09-03T01:02:03+00:00',
  model_resolved: 'poolside/laguna-xs-2.1',
  rate_limit_scope: null,
  shim_preflight_ms: 12,
  overload_retries: null,
  overload_backoff_ms: null,
  retry_upstream_ms: null,
  ttft_ms: 900,
  input_tokens: 120_000,
  output_tokens: 300,
  cached_tokens: 100_000,
  cost_usd: 0.01,
  pacer_queue_len: 0,
  pacer_deny_axis: null,
};

describe('CallsTable — wall-clock decomposition', () => {
  it('says rows land after the attempt finishes while it is still active', async () => {
    vi.mocked(api.getInstanceCalls).mockResolvedValue({
      run_id: 'r',
      instance_id: 'i',
      attempt_number: 1,
      items: [],
    });
    renderWithQuery(<CallsTable runId="r" instanceId="i" attempt={1} active />);
    await waitFor(() =>
      expect(screen.getByText(/no call rows yet/i)).toBeInTheDocument(),
    );
  });

  it('labels a pacer refusal and sums the pacer wait across calls', async () => {
    vi.mocked(api.getInstanceCalls).mockResolvedValue({
      run_id: 'r',
      instance_id: 'i',
      attempt_number: 1,
      items: [
        {
          ...BASE,
          call_index: 1,
          http_status: 200,
          error_type: null,
          paced_wait_ms: 7,
          latency_ms: 13_000,
          pacer_was_queued: false,
        },
        {
          ...BASE,
          call_index: 2,
          http_status: 429,
          error_type: 'pacer_hold_cap_exceeded',
          paced_wait_ms: 100_000,
          latency_ms: 0,
          pacer_was_queued: true,
          pacer_queue_len: 4,
          pacer_deny_axis: 'tok',
        },
      ],
    });
    renderWithQuery(
      <CallsTable runId="r" instanceId="i" attempt={1} active={false} />,
    );
    await waitFor(() =>
      expect(screen.getByText(/pacer refusals/i)).toBeInTheDocument(),
    );
    // the refusal row's STATUS CELL is labelled as OUR pacer, not the provider
    // (getAllByText: the "429 retries" column header also starts with 429)
    const statusCell = screen
      .getAllByText(/^429/)
      .find((el) => el.tagName === 'TD');
    expect(statusCell).toHaveTextContent('pacer');
    // Σ pacer wait = 7 + 100000 ms -> 100.0s
    expect(screen.getByText(/pacer wait Σ/i).parentElement).toHaveTextContent('100.0s');
    // the queued call's WAIT CELL shows its deny axis next to its wait (the axis is a
    // nested span, so match on the cell's full textContent)
    expect(
      screen.getByText(
        (_, el) =>
          el?.tagName === 'TD' && /100\.0s\s*·tok/.test(el.textContent ?? ''),
      ),
    ).toBeInTheDocument();
  });
});
