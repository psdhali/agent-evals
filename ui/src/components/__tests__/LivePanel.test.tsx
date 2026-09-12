import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { LivePanel } from '../LivePanel';

// BUILDER2-LIVE-RUN-MONITORING-2026-08-31.md §3: three states, three
// renderings, everywhere. "No data" (whole response unknown), "zero" (no
// instances in flight), and "failed to load" (the fetch itself errored) must
// never look alike — and a `stale` instance must never read as "dead".

vi.mock('../../lib/api', () => ({
  api: {
    getRunLive: vi.fn(),
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

describe('LivePanel — pacer footprint column (§2.5)', () => {
  it('renders total hold, queued count and a hold-cap timeout marker; — when absent', async () => {
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'ok',
      reason: '',
      items: [
        {
          instance_id: 'matplotlib__matplotlib-1',
          attempt_number: 1,
          state: 'running',
          turn_number: 2,
          input_tokens: 130_000,
          output_tokens: 10,
          cached_tokens: 0,
          reasoning_tokens: 0,
          cost_usd: 0.4,
          observed_at: 1,
          age_s: 3,
          revived_after_reap: false,
          paced_wait_ms_total: 300_000,
          paced_calls: 3,
          pacer_timeouts: 3,
          overload_retries_total: 0,
          pacer_last_deny_axis: 'tok',
          pacer_last_queue_len: 4,
        },
        {
          instance_id: 'old-worker-payload',
          attempt_number: 1,
          state: 'running',
          turn_number: 5,
          input_tokens: 1,
          output_tokens: 1,
          cached_tokens: 0,
          reasoning_tokens: 0,
          cost_usd: 0.01,
          observed_at: 1,
          age_s: 3,
          revived_after_reap: false,
          paced_wait_ms_total: null,
          paced_calls: null,
          pacer_timeouts: null,
          overload_retries_total: null,
          pacer_last_deny_axis: null,
          pacer_last_queue_len: null,
        },
      ],
    });
    renderWithQuery(<LivePanel runId="run-1" terminal={false} />);
    await waitFor(() => expect(screen.getByText('Pacer')).toBeInTheDocument());
    const held = screen.getByTitle(/5m 0s total pacer admission time/i);
    expect(held).toHaveTextContent('3q');
    expect(held).toHaveTextContent('3✕');
    expect(held.getAttribute('title')).toMatch(
      /"tok" axis behind a queue of 4/,
    );
    // an older payload without the fields renders —, never 0
    const cells = screen.getAllByRole('cell').map((c) => c.textContent);
    expect(cells.filter((t) => t === '—').length).toBeGreaterThanOrEqual(1);
  });
});

describe("LivePanel — eval-phase rows show the grade's live progress", () => {
  it('renders lines + elapsed in the turn column and dashes the token column', async () => {
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'ok',
      reason: '',
      items: [
        {
          instance_id: 'django__django-10097',
          attempt_number: 1,
          state: 'running',
          turn_number: null,
          input_tokens: null,
          output_tokens: null,
          cached_tokens: null,
          reasoning_tokens: null,
          cost_usd: null,
          observed_at: 1,
          age_s: 3,
          revived_after_reap: false,
          paced_wait_ms_total: null,
          paced_calls: null,
          pacer_timeouts: null,
          overload_retries_total: null,
          pacer_last_deny_axis: null,
          pacer_last_queue_len: null,
          phase: 'eval',
          eval_elapsed_s: 1830,
          eval_lines: 3412,
          eval_last_line: 'test_broken_pipe_errors ... ok',
        },
      ],
    });
    renderWithQuery(<LivePanel runId="run-1" terminal={false} />);
    await waitFor(() =>
      expect(screen.getByText(/grading · 3,412 lines/)).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/grading · 3,412 lines/).getAttribute('title'),
    ).toMatch(/last line: test_broken_pipe_errors/);
  });
});

function liveItem(instanceId: string, extra: Record<string, unknown> = {}) {
  return {
    instance_id: instanceId,
    attempt_number: 1,
    state: 'running',
    turn_number: 1,
    input_tokens: 1,
    output_tokens: 1,
    cached_tokens: 0,
    reasoning_tokens: 0,
    cost_usd: 0.01,
    observed_at: 1,
    age_s: 3,
    revived_after_reap: false,
    ...extra,
  };
}

describe('LivePanel — narrowing and collapse (owner request, first 500-run)', () => {
  it('filters by instance-id search, phase and state without refetching', async () => {
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'ok',
      reason: '',
      items: [
        liveItem('django__django-1'),
        liveItem('sympy__sympy-2', { state: 'stale' }),
        liveItem('django__django-3', {
          phase: 'eval',
          eval_lines: 10,
          eval_elapsed_s: 5,
        }),
      ],
    });
    renderWithQuery(<LivePanel runId="run-1" terminal={false} />);
    await waitFor(() =>
      expect(screen.getByText('showing 3 of 3 in flight')).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByLabelText('search live instances'), {
      target: { value: 'django' },
    });
    expect(
      screen.getByText(/showing 2 of 3 in flight \(narrowed\)/),
    ).toBeInTheDocument();
    expect(screen.queryByText('sympy__sympy-2')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('filter live phase'), {
      target: { value: 'eval' },
    });
    expect(screen.getByText(/showing 1 of 3/)).toBeInTheDocument();
    expect(screen.getByText('django__django-3')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('filter live phase'), {
      target: { value: '' },
    });
    fireEvent.change(screen.getByLabelText('search live instances'), {
      target: { value: '' },
    });
    fireEvent.change(screen.getByLabelText('filter live state'), {
      target: { value: 'stale' },
    });
    expect(screen.getByText(/showing 1 of 3/)).toBeInTheDocument();
    expect(screen.getByText('sympy__sympy-2')).toBeInTheDocument();
  });

  it('starts collapsed above 60 rows, opens on expand, and a filter opens it', async () => {
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'ok',
      reason: '',
      items: Array.from({ length: 61 }, (_, i) =>
        liveItem(`django__django-${i}`),
      ),
    });
    renderWithQuery(<LivePanel runId="run-1" terminal={false} />);
    await waitFor(() =>
      expect(
        screen.getByText('showing 61 of 61 in flight'),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.getByText(/collapsed — many in flight/)).toBeInTheDocument();
    // a narrowing filter shows the (now small) list without an explicit expand
    fireEvent.change(screen.getByLabelText('search live instances'), {
      target: { value: 'django-6' },
    });
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByText(/showing 2 of 61/)).toBeInTheDocument(); // django-6, django-60
    fireEvent.change(screen.getByLabelText('search live instances'), {
      target: { value: '' },
    });
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    // an explicit expand / collapse by the operator wins over the size rule
    fireEvent.click(screen.getByLabelText('toggle live progress list'));
    expect(screen.getByRole('table')).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('toggle live progress list'));
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });
});

describe('LivePanel — honest states for in-flight progress', () => {
  it('renders the whole-response unknown state distinctly, never as an empty list', async () => {
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'unknown',
      reason: 'redis_unreachable',
      items: [],
    });
    renderWithQuery(<LivePanel runId="run-1" terminal={false} />);

    await waitFor(() =>
      expect(screen.getByText(/live progress unknown/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/redis_unreachable/)).toBeInTheDocument();
    expect(
      screen.queryByText(/no instances in flight/i),
    ).not.toBeInTheDocument();
  });

  it('renders the honest empty state only when Redis is actually reachable', async () => {
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'ok',
      reason: '',
      items: [],
    });
    renderWithQuery(<LivePanel runId="run-1" terminal={false} />);

    await waitFor(() =>
      expect(screen.getByText(/no instances in flight/i)).toBeInTheDocument(),
    );
    expect(
      screen.queryByText(/live progress unknown/i),
    ).not.toBeInTheDocument();
  });

  it('renders a failed fetch as an error, never as empty or unknown', async () => {
    vi.mocked(api.getRunLive).mockRejectedValue(new Error('boom'));
    renderWithQuery(<LivePanel runId="run-1" terminal={false} />);

    await waitFor(() =>
      expect(
        screen.getByText(/failed to load live progress/i),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByText(/no instances in flight/i),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/live progress unknown/i),
    ).not.toBeInTheDocument();
  });

  it('renders a stale instance as "no recent observation", never as failed, and preserves null as — not 0', async () => {
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'ok',
      reason: '',
      items: [
        {
          instance_id: 'django__django-1',
          attempt_number: 1,
          state: 'stale',
          turn_number: null,
          input_tokens: null,
          output_tokens: null,
          cached_tokens: null,
          reasoning_tokens: null,
          cost_usd: null,
          observed_at: null,
          age_s: null,
          revived_after_reap: false,
        },
      ],
    });
    renderWithQuery(<LivePanel runId="run-1" terminal={false} />);

    // scoped to the table: the state filter's <option> repeats the word
    await waitFor(() =>
      expect(
        within(screen.getByRole('table')).getByText('stale'),
      ).toBeInTheDocument(),
    );
    // Never rendered as a bare failure — carries its own non-alarming caption.
    expect(screen.getByText(/no recent observation/i)).toBeInTheDocument();
    expect(screen.queryByText(/^failed$/i)).not.toBeInTheDocument();
    // Every null numeric field renders as — , never as 0 (a 0 here would be
    // a lie that looks like data).
    const dashes = screen.getAllByText('—');
    expect(dashes.length).toBeGreaterThanOrEqual(3); // turn, input, output, cost, age
    expect(screen.queryByText(/^0$/)).not.toBeInTheDocument();
  });

  it('renders a running instance with real numbers and its observation age', async () => {
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'ok',
      reason: '',
      items: [
        {
          instance_id: 'astropy__astropy-1',
          attempt_number: 1,
          state: 'running',
          turn_number: 4,
          input_tokens: 12000,
          output_tokens: 800,
          cached_tokens: 6000,
          reasoning_tokens: null,
          cost_usd: 0.42,
          observed_at: 1700000000,
          age_s: 12,
          revived_after_reap: false,
        },
      ],
    });
    renderWithQuery(<LivePanel runId="run-1" terminal={false} />);

    await waitFor(() =>
      expect(
        within(screen.getByRole('table')).getByText('running'),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText('12,000')).toBeInTheDocument();
    expect(screen.getByText('$0.42')).toBeInTheDocument();
    expect(screen.getByText('12s old')).toBeInTheDocument();
  });
});
