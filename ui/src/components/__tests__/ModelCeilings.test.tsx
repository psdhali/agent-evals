import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { ModelCeilings } from '../ModelCeilings';

// Exact-design §6: discovery is MANUAL-ONLY and confirm-gated behind a live cost
// estimate — the gate is a correctness rule (per-invocation spend sign-off, reviewer
// F5), not a render preference. Staleness must render as an explicit not-in-use
// state, and "never measured" must never look healthy.

vi.mock('../../lib/api', () => ({
  api: {
    listModelCeilings: vi.fn(),
    estimateDiscovery: vi.fn(),
    discoverCeiling: vi.fn(),
    manualCeiling: vi.fn(),
  },
}));

import { api } from '../../lib/api';

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
  );
}

const MEASURED = {
  model_alias: 'laguna-xs-2.1',
  discovered_tpm: 2_129_367,
  ceiling_source: 'discovery_initial',
  discovered_at: new Date(Date.now() - 3_600_000).toISOString(),
  provider: 'Poolside',
  values: { burst_admission_tokens: 2_129_367, paced_rate_tok_per_s: 49_000 },
  is_stale: false,
};

const NEVER_MEASURED = {
  model_alias: 'qwen3-coder-next',
  discovered_tpm: null,
  ceiling_source: null,
  discovered_at: null,
  provider: null,
  values: null,
  is_stale: false,
};

beforeEach(() => {
  vi.mocked(api.estimateDiscovery).mockResolvedValue({
    model_alias: 'laguna-xs-2.1',
    target_concurrency: 150,
    estimated_cost_usd: 1.62,
    status: 'estimate',
  });
});

describe('ModelCeilings', () => {
  it('discover is confirm-gated: disabled until armed, and arming surfaces the cost', async () => {
    vi.mocked(api.listModelCeilings).mockResolvedValue([MEASURED]);
    renderWithClient(<ModelCeilings />);

    const button = await screen.findByRole('button', {
      name: /re-discover \(force\)/i,
    });
    expect(button).toBeDisabled();
    expect(api.discoverCeiling).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('checkbox'));
    await waitFor(() =>
      expect(
        screen.getByText(/confirm ~\$1\.62 of real probe spend/i),
      ).toBeInTheDocument(),
    );
    await waitFor(() => expect(button).toBeEnabled());

    await userEvent.click(button);
    await waitFor(() =>
      expect(api.discoverCeiling).toHaveBeenCalledWith('laguna-xs-2.1', 150, 60, 'target_first'),
    );
  });

  it('never-measured renders as explicitly unmeasured, not as a quiet healthy row', async () => {
    vi.mocked(api.listModelCeilings).mockResolvedValue([NEVER_MEASURED]);
    renderWithClient(<ModelCeilings />);

    expect(await screen.findByText(/never measured/i)).toBeInTheDocument();
    expect(screen.getByText(/historical floor/i)).toBeInTheDocument();
    // And its button is the initial Discover, not the force variant.
    expect(
      screen.getByRole('button', { name: /^discover$/i }),
    ).toBeInTheDocument();
  });

  it('a stale value renders the explicit not-in-use state', async () => {
    vi.mocked(api.listModelCeilings).mockResolvedValue([
      { ...MEASURED, is_stale: true },
    ]);
    renderWithClient(<ModelCeilings />);

    expect(
      await screen.findByText(/stale — not in use, re-discover recommended/i),
    ).toBeInTheDocument();
  });

  it('a failed list renders unknown-not-healthy, never an empty healthy panel', async () => {
    vi.mocked(api.listModelCeilings).mockRejectedValue(new Error('boom'));
    renderWithClient(<ModelCeilings />);

    expect(
      await screen.findByText(/state unknown, not healthy/i),
    ).toBeInTheDocument();
  });

  it('manual entry submits the typed value and only when non-empty', async () => {
    vi.mocked(api.listModelCeilings).mockResolvedValue([MEASURED]);
    vi.mocked(api.manualCeiling).mockResolvedValue(MEASURED);
    renderWithClient(<ModelCeilings />);

    const save = await screen.findByRole('button', {
      name: /save manual value/i,
    });
    expect(save).toBeDisabled(); // empty input -> no accidental zero writes

    await userEvent.type(
      screen.getByPlaceholderText(/manual burst-edge tokens/i),
      '1500000',
    );
    await userEvent.click(save);
    await waitFor(() =>
      expect(api.manualCeiling).toHaveBeenCalledWith(
        'laguna-xs-2.1',
        1_500_000,
      ),
    );
  });
});
