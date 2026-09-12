import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Capacity } from '../Capacity';

// Test 5 of phase 2 §5: empty states are honest.  "No capacity ticks yet"
// (the autoscaler has written nothing) must NOT be reachable from a failed
// fetch — an error renders as an error, and the empty string only appears when
// a fetch actually returned zero ticks.

vi.mock('../../lib/api', () => ({
  api: {
    listCapacity: vi.fn(),
    listRuns: vi.fn(), // the §1a activity gate inside Capacity
  },
}));
vi.mock('../../lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/queries')>();
  return {
    ...actual,
    useAnyRunActive: () => ({
      data: { items: [] },
      isSuccess: true,
      isLoading: false,
      isError: false,
    }),
  };
});

import { api } from '../../lib/api';

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
  );
}

describe('Capacity — honest empty vs error states (phase 2 §5)', () => {
  it('an empty result renders the honest empty state', async () => {
    vi.mocked(api.listCapacity).mockResolvedValue({ items: [] });
    renderWithQuery(<Capacity />);

    await waitFor(() =>
      expect(screen.getByText(/no capacity ticks yet/i)).toBeInTheDocument(),
    );
    expect(
      screen.queryByText(/failed to load capacity/i),
    ).not.toBeInTheDocument();
  });

  it('a failed fetch renders the error, never the empty state', async () => {
    vi.mocked(api.listCapacity).mockRejectedValue(new Error('boom'));
    renderWithQuery(<Capacity />);

    await waitFor(() =>
      expect(screen.getByText(/failed to load capacity/i)).toBeInTheDocument(),
    );
    // The empty state must not be reachable from an error.
    expect(
      screen.queryByText(/no capacity ticks yet/i),
    ).not.toBeInTheDocument();
  });
});
