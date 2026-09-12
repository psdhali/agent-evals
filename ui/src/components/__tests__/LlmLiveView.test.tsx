// LlmLiveView — the live spend-log trajectory panel.
// Two renderings that must never be confused (the panel's whole contract):
//   1. rows -> the table, with the per-call cache split visible;
//   2. a 503 -> the explicit "spend DB not configured" degraded notice —
//      NEVER the empty-list caption (an empty list impersonating a down
//      dependency is how a live run looks silently call-less).
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../lib/api', () => ({
  api: {
    llmLiveCalls: vi.fn(),
    llmLiveDetail: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

import { api, ApiError } from '../../lib/api';
import { LlmLiveView } from '../LlmLiveView';

function renderPanel(props: { instanceId?: string; attempt?: number } = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <LlmLiveView runId="run-1" terminal={true} {...props} />
    </QueryClientProvider>,
  );
}

const labelledCall = {
  request_id: 'req-9',
  started_at: '2026-09-06T04:00:00+00:00',
  model: 'minimax-m2.5-codex',
  prompt_tokens: 1000,
  completion_tokens: 10,
  text_tokens: 100,
  cached_tokens: 900,
  session_id: 's9',
  instance_id: 'django__django-11099',
  attempt: 1,
  harness: 'codex',
  status: 'success',
};

describe('LlmLiveView', () => {
  it('renders call rows with the cache split', async () => {
    vi.mocked(api.llmLiveCalls).mockResolvedValue({
      run_id: 'run-1',
      items: [
        {
          request_id: 'req-1',
          started_at: '2026-09-02T02:27:00+00:00',
          model: 'laguna-xs-2.1-claude_code',
          prompt_tokens: 50_400,
          completion_tokens: 80,
          text_tokens: 400,
          cached_tokens: 50_000,
          session_id: 's1',
          instance_id: 'sphinx-doc__sphinx-10614',
          attempt: 1,
          harness: 'claude_code',
        },
      ],
    });
    renderPanel();
    expect(
      await screen.findByText(/sphinx-doc__sphinx-10614/),
    ).toBeInTheDocument();
    expect(screen.getByText('50,000')).toBeInTheDocument(); // cached
    expect(screen.getByText('400')).toBeInTheDocument(); // uncached
  });

  // 2026-09-06: the instance page is one ATTEMPT — the query carries it, so a
  // restarted instance's page never shows the previous attempt's calls.
  it('scopes the query to the attempt', async () => {
    vi.mocked(api.llmLiveCalls).mockResolvedValue({
      run_id: 'run-1',
      items: [labelledCall],
    });
    renderPanel({ instanceId: 'django__django-11099', attempt: 2 });
    expect(await screen.findByText(/django__django-11099/)).toBeInTheDocument();
    expect(api.llmLiveCalls).toHaveBeenCalledWith('run-1', {
      limit: 50,
      instanceId: 'django__django-11099',
      attempt: 2,
    });
  });

  // The run-level list replaces the scoped one ONLY when the gateway stored no
  // labels at all. Labelled rows on OTHER instances mean "nothing for this
  // attempt yet" — never another instance's calls rendered as this one's.
  it('does not show other instances calls when the run has labelled rows', async () => {
    vi.mocked(api.llmLiveCalls).mockImplementation((_run, opts) =>
      Promise.resolve({
        run_id: 'run-1',
        items: opts?.instanceId
          ? []
          : [
              labelledCall,
              {
                ...labelledCall,
                request_id: 'req-f',
                instance_id: null,
                attempt: null,
                status: 'failure',
              },
            ],
      }),
    );
    renderPanel({ instanceId: 'sympy__sympy-20590', attempt: 1 });
    expect(
      await screen.findByText(/no calls for this attempt yet/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/1 labelled call on other instances/),
    ).toBeInTheDocument();
    expect(screen.getByText(/1 failed call/)).toBeInTheDocument();
    expect(screen.queryByText(/django__django-11099/)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/carry no instance labels/),
    ).not.toBeInTheDocument();
  });

  it('falls back to the run-level list only when NO row carries a label', async () => {
    vi.mocked(api.llmLiveCalls).mockImplementation((_run, opts) =>
      Promise.resolve({
        run_id: 'run-1',
        items: opts?.instanceId
          ? []
          : [
              {
                ...labelledCall,
                instance_id: null,
                attempt: null,
                harness: null,
              },
            ],
      }),
    );
    renderPanel({ instanceId: 'sympy__sympy-20590', attempt: 1 });
    expect(
      await screen.findByText(/carry no instance labels/),
    ).toBeInTheDocument();
  });

  it('labels a provider-rejected row as a failed call, not as unlabelled', async () => {
    vi.mocked(api.llmLiveCalls).mockResolvedValue({
      run_id: 'run-1',
      items: [
        {
          ...labelledCall,
          instance_id: null,
          attempt: null,
          status: 'failure',
        },
      ],
    });
    renderPanel();
    expect(await screen.findByText('failed call')).toBeInTheDocument();
  });

  it('renders the 503 as a degraded notice, not an empty list', async () => {
    vi.mocked(api.llmLiveCalls).mockRejectedValue(new ApiError(503, 'no dsn'));
    renderPanel();
    expect(
      await screen.findByText(/spend DB is not configured or not reachable/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/no calls recorded yet/)).not.toBeInTheDocument();
  });
});

// 2026-09-07 (owner): a 200-call attempt showed only its newest 50 — the API's
// keyset cursor (`before` = the oldest started_at shown) pages the rest in on
// demand; a short page marks the end.
describe('LlmLiveView — older pages', () => {
  const call = (i: number) => ({
    ...labelledCall,
    request_id: `req-${i}`,
    started_at: `2026-09-07T03:${String(59 - Math.floor(i / 60)).padStart(2, '0')}:${String(59 - (i % 60)).padStart(2, '0')}+00:00`,
    attempt: 2,
  });
  it('offers "load older" only when the first page is full, appends the page, and stops on a short page', async () => {
    const first = Array.from({ length: 50 }, (_, i) => call(i));
    const second = Array.from({ length: 3 }, (_, i) => call(50 + i));
    vi.mocked(api.llmLiveCalls).mockImplementation((_run, opts) =>
      Promise.resolve({
        run_id: 'run-1',
        items: opts?.before ? second : first,
      }),
    );
    renderPanel({ instanceId: 'django__django-11099', attempt: 2 });
    expect(
      await screen.findByText(/showing 50 calls — newest first/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('load older calls'));
    await waitFor(() =>
      expect(
        screen.getByText(/showing 53 calls — all recorded calls/),
      ).toBeInTheDocument(),
    );
    expect(api.llmLiveCalls).toHaveBeenLastCalledWith('run-1', {
      limit: 50,
      instanceId: 'django__django-11099',
      attempt: 2,
      before: first[49].started_at,
    });
    expect(screen.queryByLabelText('load older calls')).not.toBeInTheDocument();
  });

  it('does not offer "load older" when the first page is short', async () => {
    vi.mocked(api.llmLiveCalls).mockResolvedValue({
      run_id: 'run-1',
      items: [labelledCall],
    });
    renderPanel({ instanceId: 'django__django-11099', attempt: 1 });
    expect(
      await screen.findByText(/showing 1 call — all recorded calls/),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText('load older calls')).not.toBeInTheDocument();
  });
});
