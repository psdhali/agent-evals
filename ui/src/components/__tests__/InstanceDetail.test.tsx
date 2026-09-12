import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { InstanceDetail } from '../InstanceDetail';

// The artifact tabs must mirror the API's `_KINDS` (builder 4's extension of
// swebench_eval/orchestrator/api/artifacts.py): the 3 newly-exposed artifacts
// (test_output, run_log, native_trajectory) are real kinds the API serves, so
// each tab must issue the correct /artifacts request — never a client-side
// guess at an S3 key.  llm_calls.jsonl is deliberately NOT a tab (excluded by
// backend design).

vi.mock('../../lib/api', () => ({
  // Real class: LlmLiveView (now mounted inside InstanceDetail) does
  // `err instanceof ApiError` in its retry callback — an undefined export
  // would throw at render.
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  api: {
    getInstance: vi.fn(),
    artifact: vi.fn(),
    llmLiveCalls: vi.fn().mockResolvedValue({ run_id: 'run-1', items: [] }),
    llmLiveDetail: vi.fn(),
    getJudgeResults: vi
      .fn()
      .mockResolvedValue({ run_id: 'run-1', results: [] }),
    getJudgeReviewHistory: vi.fn(),
    reviewJudgeDimension: vi.fn(),
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

// One harness row is enough for the artifact query to enable (it gates on
// `inst.data.rows.length`).
const ROW = {
  run_id: 'run-1',
  instance_id: 'inst-1',
  attempt_number: 1,
  phase: 'harness',
  state: 'HARNESS_DONE',
  verdict: null,
  cost_usd: null,
  input_tokens: null,
  output_tokens: null,
  agent_s: null,
  task_observed_s: null,
  repo_prep_s: null,
  touches_test_files: null,
  leak_detectable: null,
  error_category: null,
  created_at: '2026-08-29T00:00:00+00:00',
};

function prime() {
  vi.mocked(api.getInstance).mockResolvedValue({
    run_id: 'run-1',
    instance_id: 'inst-1',
    attempt: 1,
    rows: [ROW],
  } as never);
  vi.mocked(api.artifact).mockResolvedValue('artifact-content');
}

describe('InstanceDetail — artifact tabs mirror the API kinds', () => {
  it('renders the full tab set, including the three newly-exposed artifacts', async () => {
    prime();
    renderWithClient(
      <InstanceDetail
        runId="run-1"
        instanceId="inst-1"
        attempt={1}
        onBack={() => {}}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: /test output/i }),
      ).toBeInTheDocument(),
    );
    // All 7 tabs present.
    for (const label of [
      /^patch$/i,
      /^trajectory$/i,
      /^harness log$/i,
      /^test output$/i,
      /^run log$/i,
      /^native trajectory$/i,
      /^eval report$/i,
    ]) {
      expect(screen.getByRole('button', { name: label })).toBeInTheDocument();
    }
  });

  it('the "test output" tab requests the test_output artifact kind', async () => {
    prime();
    renderWithClient(
      <InstanceDetail
        runId="run-1"
        instanceId="inst-1"
        attempt={1}
        onBack={() => {}}
      />,
    );
    await userEvent.click(
      await screen.findByRole('button', { name: /test output/i }),
    );
    await waitFor(() =>
      expect(api.artifact).toHaveBeenCalledWith(
        'run-1',
        'inst-1',
        1,
        'test_output',
      ),
    );
  });

  it('the "run log" tab requests the run_log artifact kind', async () => {
    prime();
    renderWithClient(
      <InstanceDetail
        runId="run-1"
        instanceId="inst-1"
        attempt={1}
        onBack={() => {}}
      />,
    );
    await userEvent.click(
      await screen.findByRole('button', { name: /run log/i }),
    );
    await waitFor(() =>
      expect(api.artifact).toHaveBeenCalledWith(
        'run-1',
        'inst-1',
        1,
        'run_log',
      ),
    );
  });

  it('the "native trajectory" tab requests the native_trajectory kind', async () => {
    prime();
    renderWithClient(
      <InstanceDetail
        runId="run-1"
        instanceId="inst-1"
        attempt={1}
        onBack={() => {}}
      />,
    );
    await userEvent.click(
      await screen.findByRole('button', { name: /native trajectory/i }),
    );
    await waitFor(() =>
      expect(api.artifact).toHaveBeenCalledWith(
        'run-1',
        'inst-1',
        1,
        'native_trajectory',
      ),
    );
  });
});

// ADR-0037 phase-timing breakdown + cost_source provenance: the columns are
// persisted on every attempt (results_writer._RESULT_EXTRA_COLUMNS) and are
// now threaded through InstanceItem — the detail screen must render them,
// phase-appropriately, with NULL as '—' (never an invented zero/false).
describe('InstanceDetail — timing breakdown + cost_source', () => {
  it('renders the harness-phase timing labels, with — for unmeasured values', async () => {
    prime();
    renderWithClient(
      <InstanceDetail
        runId="run-1"
        instanceId="inst-1"
        attempt={1}
        onBack={() => {}}
      />,
    );
    await screen.findByText(/timing breakdown/i);
    for (const label of [
      /queue wait/i,
      /provision/i,
      /worker boot/i,
      /patch extract/i,
      /artifact upload/i,
      /repo prep cache hit/i,
      /image pull cold/i,
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it('renders measured timings and labels a non-provider cost_source', async () => {
    vi.mocked(api.getInstance).mockResolvedValue({
      run_id: 'run-1',
      instance_id: 'inst-1',
      attempt: 1,
      rows: [
        {
          ...ROW,
          queue_wait_s: 12,
          image_pull_s: 95,
          cost_source: 'local_pricing',
        },
      ],
    } as never);
    vi.mocked(api.artifact).mockResolvedValue('artifact-content');
    renderWithClient(
      <InstanceDetail
        runId="run-1"
        instanceId="inst-1"
        attempt={1}
        onBack={() => {}}
      />,
    );
    await screen.findByText(/timing breakdown/i);
    expect(screen.getByText('12s')).toBeInTheDocument();
    expect(screen.getByText('1m 35s')).toBeInTheDocument();
    expect(screen.getByText('local_pricing')).toBeInTheDocument();
  });
});

// Run-1 owner feedback (2026-09-02): the live LLM-call panel lives HERE,
// scoped to this instance — not on the run overview screen.
describe('InstanceDetail — live LLM calls panel', () => {
  it('mounts the panel scoped to this instance', async () => {
    prime();
    renderWithClient(
      <InstanceDetail
        runId="run-1"
        instanceId="inst-1"
        attempt={1}
        onBack={() => {}}
      />,
    );
    await screen.findByText(/live llm calls/i);
    await waitFor(() =>
      expect(api.llmLiveCalls).toHaveBeenCalledWith('run-1', {
        limit: 50,
        instanceId: 'inst-1',
        attempt: 1, // the page is one attempt; the live view is scoped to it (2026-09-06)
      }),
    );
  });
});

// The adapter-reported cross-check block was REMOVED (owner, 2026-09-02): the
// adapter figures are unreliable and only invited confusion next to the shim's
// authoritative meter (ADR-0019). Its tests were removed with it.
