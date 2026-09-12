import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { RunDetail } from '../RunDetail';

// Implementation review, 2026-08-31 (§E of BUILDER4-RESTART-CLOSE-IMPLEMENTATION-
// REVIEW-2026-08-31.md): restart is the only surface in this system where a
// mis-click spends real inference money. This is the single component test
// the review asked for — asserting the confirm gate cannot be bypassed, not a
// general render check.

vi.mock('../../lib/api', () => ({
  api: {
    getRun: vi.fn(),
    listInstances: vi.fn(),
    restartInstances: vi.fn(),
    closeRun: vi.fn(),
    pauseGateway: vi.fn(),
    resumeGateway: vi.fn(),
    getRunProgress: vi.fn(),
    getRunLive: vi.fn(),
    getQueues: vi.fn(),
    getControl: vi.fn(),
    llmLiveCalls: vi.fn().mockResolvedValue({ run_id: 'r', items: [] }),
    llmLiveDetail: vi.fn(),
    listJudgePasses: vi.fn().mockResolvedValue({ run_id: 'run-1', passes: [] }),
    judgeEstimate: vi.fn(),
    launchJudge: vi.fn(),
    getJudgeResults: vi
      .fn()
      .mockResolvedValue({ run_id: 'run-1', results: [] }),
  },
  // Real class, not a vi.fn(): LlmLiveView narrows errors with `instanceof
  // ApiError` at render time — an undefined ApiError makes instanceof THROW
  // and takes the whole RunDetail down (exactly how the mock gap surfaced).
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  // Real implementations, not mocked — pure helpers RunDetail calls directly.
  summaryOf: (item: { summary?: Record<string, unknown> | null }) =>
    item.summary ?? null,
  flatSummary: (item?: { summary?: Record<string, unknown> | null }) =>
    item?.summary ?? {},
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

const RUN = {
  run_id: 'run-1',
  status: 'running',
  created_at: '2026-08-31T00:00:00Z',
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
  states: [{ state: 'FAILED_HARNESS', count: 1 }],
  instance_states: [{ state: 'FAILED_HARNESS', count: 1 }],
  resolve_rate_denominator: 1,
  ready_to_close: false,
  gateway_key_blocked_by: null,
};

const INSTANCE = {
  run_id: 'run-1',
  instance_id: 'django__django-1',
  attempt_number: 1,
  phase: 'harness',
  state: 'FAILED_HARNESS',
  error_category: 'MODEL_API_ERROR',
  error_detail: null,
  verdict: null,
  wall_clock_harness_s: null,
  wall_clock_eval_s: null,
  touches_test_files: null,
  patch_path: null,
  trajectory_path: null,
  raw_log_path: null,
  report_path: null,
  report_json: null,
  native_trajectory_s3_key: null,
  test_output_s3_key: null,
  run_log_s3_key: null,
  retry_reason: null,
  created_at: null,
  input_tokens: null,
  output_tokens: null,
  cost_usd: null,
  turns_used: null,
  adapter_input_tokens: null,
  adapter_output_tokens: null,
  adapter_cost_usd: null,
  agent_s: null,
  task_observed_s: null,
  task_billed_s: null,
  repo_prep_s: null,
  eval_test_s: null,
  queue_wait_s: null,
  provision_s: null,
  image_pull_s: null,
  worker_boot_s: null,
  patch_extract_s: null,
  artifact_upload_s: null,
  repo_prep_cache_hit: null,
  image_pull_cold: null,
  eval_queue_wait_s: null,
  eval_patch_fetch_s: null,
  eval_image_pull_s: null,
  eval_log_upload_s: null,
  eval_image_pull_cold: null,
  cost_source: null,
  stripped_test_paths: null,
  grade_invalid: null,
  leaked_node_ids: null,
  gold_patch_similarity: null,
  leak_detectable: null,
};

const RUN_PROGRESS = {
  run_id: 'run-1',
  status: 'running',
  terminal: false,
  phases: [],
  expected: null,
  denominator: null,
};

const QUEUES = { items: [] };

describe('RunDetail — restart confirm gate cannot be bypassed', () => {
  it('the restart button stays disabled until the confirm checkbox is checked', async () => {
    vi.mocked(api.getRun).mockResolvedValue(RUN);
    vi.mocked(api.listInstances).mockResolvedValue({
      items: [INSTANCE],
      total: 1,
      limit: 50,
      offset: 0,
    });
    vi.mocked(api.getRunProgress).mockResolvedValue(RUN_PROGRESS);
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'ok',
      reason: '',
      items: [],
    });
    vi.mocked(api.getQueues).mockResolvedValue(QUEUES);
    vi.mocked(api.getControl).mockResolvedValue({
      harness_paused: false,
      eval_paused: false,
      gateway_paused: false,
      published_at: Date.now() / 1000,
      stale: false,
      aborted_runs: [],
      updated_by: '',
      reason: '',
    });

    renderWithClient(
      <RunDetail runId="run-1" onBack={() => {}} onOpenInstance={() => {}} />,
    );

    const checkbox = await screen.findByLabelText(
      /select django__django-1 for restart/i,
    );
    await userEvent.click(checkbox);

    const restartButton = screen.getByRole('button', {
      name: /restart selected/i,
    });
    // Selected but not yet confirmed — must still be disabled.
    expect(restartButton).toBeDisabled();
    expect(api.restartInstances).not.toHaveBeenCalled();

    const confirm = screen.getByRole('checkbox', {
      name: /confirm restart/i,
    });
    await userEvent.click(confirm);

    // Only now, after both selecting AND confirming, is it clickable.
    expect(restartButton).toBeEnabled();

    vi.mocked(api.restartInstances).mockResolvedValue({
      run_id: 'run-1',
      restarted: [
        {
          instance_id: 'django__django-1',
          attempt_number: 2,
          retry_reason: 'operator_infra_retry',
        },
      ],
      skipped: [],
    });
    await userEvent.click(restartButton);

    await waitFor(() => expect(api.restartInstances).toHaveBeenCalledTimes(1));
  });

  it('changing the selection re-arms the confirm — a stale "yes" cannot cover a new selection', async () => {
    vi.mocked(api.getRun).mockResolvedValue(RUN);
    vi.mocked(api.listInstances).mockResolvedValue({
      items: [INSTANCE, { ...INSTANCE, instance_id: 'django__django-2' }],
      total: 2,
      limit: 50,
      offset: 0,
    });
    vi.mocked(api.getRunProgress).mockResolvedValue(RUN_PROGRESS);
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'ok',
      reason: '',
      items: [],
    });
    vi.mocked(api.getQueues).mockResolvedValue(QUEUES);
    vi.mocked(api.getControl).mockResolvedValue({
      harness_paused: false,
      eval_paused: false,
      gateway_paused: false,
      published_at: Date.now() / 1000,
      stale: false,
      aborted_runs: [],
      updated_by: '',
      reason: '',
    });

    renderWithClient(
      <RunDetail runId="run-1" onBack={() => {}} onOpenInstance={() => {}} />,
    );

    const first = await screen.findByLabelText(
      /select django__django-1 for restart/i,
    );
    await userEvent.click(first);
    const confirm = screen.getByRole('checkbox', { name: /confirm restart/i });
    await userEvent.click(confirm);
    expect(confirm).toBeChecked();

    // Selecting a second instance must drop the confirm — the prior "yes"
    // was for one instance, not two.
    const second = screen.getByLabelText(
      /select django__django-2 for restart/i,
    );
    await userEvent.click(second);
    expect(confirm).not.toBeChecked();
    expect(
      screen.getByRole('button', { name: /restart selected/i }),
    ).toBeDisabled();
  });
});

// Follow-up to the run-1 owner's grading work: the contamination column is
// the judge's own contamination dimension, never Pass A's deterministic
// leak_detectable — "unjudged" must render as a dash, not a silent "No".
describe('RunDetail — contamination column', () => {
  it('renders a dash, not "No", when the attempt has not been judged', async () => {
    vi.mocked(api.getRun).mockResolvedValue(RUN);
    vi.mocked(api.listInstances).mockResolvedValue({
      items: [INSTANCE],
      total: 1,
      limit: 50,
      offset: 0,
    });
    vi.mocked(api.getRunProgress).mockResolvedValue(RUN_PROGRESS);
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'ok',
      reason: '',
      items: [],
    });
    vi.mocked(api.getQueues).mockResolvedValue(QUEUES);
    vi.mocked(api.getControl).mockResolvedValue({
      harness_paused: false,
      eval_paused: false,
      gateway_paused: false,
      published_at: Date.now() / 1000,
      stale: false,
      aborted_runs: [],
      updated_by: '',
      reason: '',
    });
    vi.mocked(api.getJudgeResults).mockResolvedValue({
      run_id: 'run-1',
      results: [],
    });

    renderWithClient(
      <RunDetail runId="run-1" onBack={() => {}} onOpenInstance={() => {}} />,
    );

    await screen.findByText('django__django-1');
    // Unjudged renders as a dash somewhere on the row — never a badge, and
    // specifically never anything clickable claiming a judge result exists.
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
    expect(
      screen.queryByRole('button', { name: /open judge result/i }),
    ).not.toBeInTheDocument();
  });

  it('renders "Yes" for a scored contamination dimension and opens the instance on click', async () => {
    vi.mocked(api.getRun).mockResolvedValue(RUN);
    vi.mocked(api.listInstances).mockResolvedValue({
      items: [INSTANCE],
      total: 1,
      limit: 50,
      offset: 0,
    });
    vi.mocked(api.getRunProgress).mockResolvedValue(RUN_PROGRESS);
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'ok',
      reason: '',
      items: [],
    });
    vi.mocked(api.getQueues).mockResolvedValue(QUEUES);
    vi.mocked(api.getControl).mockResolvedValue({
      harness_paused: false,
      eval_paused: false,
      gateway_paused: false,
      published_at: Date.now() / 1000,
      stale: false,
      aborted_runs: [],
      updated_by: '',
      reason: '',
    });
    vi.mocked(api.getJudgeResults).mockResolvedValue({
      run_id: 'run-1',
      results: [
        {
          instance_id: 'django__django-1',
          attempt_number: 1,
          judged_at: '2026-09-02T00:00:00+00:00',
          judge_model_resolved: 'deepseek/deepseek-v4-flash-0731',
          rubric_version: '1',
          judge_prune_mode: 'pruned',
          input_truncated: false,
          tool_output_pruned: false,
          judge_parse_failed: false,
          summary: null,
          judge_cost_usd: 0.001,
          dimensions: [
            {
              dimension_id: 'contamination',
              scale_type: 'likert',
              score_numeric: 2,
              score_secondary: null,
              flag: null,
              span_start_turn: null,
              span_end_turn: null,
              reasoning: 'named a test it never read',
              evidence: [{ turn: 3, quote: 'x' }],
              evidence_missing: false,
              causes: [],
            },
          ],
        },
      ],
    });

    const onOpenInstance = vi.fn();
    renderWithClient(
      <RunDetail
        runId="run-1"
        onBack={() => {}}
        onOpenInstance={onOpenInstance}
      />,
    );

    const badge = await screen.findByRole('button', {
      name: /open judge result/i,
    });
    expect(badge).toHaveTextContent('Yes');
    await userEvent.click(badge);
    expect(onOpenInstance).toHaveBeenCalledWith('django__django-1', 1);
    // The row's own click handler must not ALSO fire — stopPropagation on
    // the cell means exactly one navigation, not two.
    expect(onOpenInstance).toHaveBeenCalledTimes(1);
  });
});

// 2026-09-08 (first live judge pass): the instances table can be narrowed
// to the rows the LLM judge has / has not scored, keyed (instance, attempt)
// exactly like the Contamination column. Client-side, over the fetched rows.
describe('RunDetail — judged filter', () => {
  it('narrows the table to judged rows, then to unjudged rows', async () => {
    const SECOND = { ...INSTANCE, instance_id: 'django__django-2' };
    vi.mocked(api.getRun).mockResolvedValue(RUN);
    vi.mocked(api.listInstances).mockResolvedValue({
      items: [INSTANCE, SECOND],
      total: 2,
      limit: 50,
      offset: 0,
    });
    vi.mocked(api.getRunProgress).mockResolvedValue(RUN_PROGRESS);
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'ok',
      reason: '',
      items: [],
    });
    vi.mocked(api.getQueues).mockResolvedValue(QUEUES);
    vi.mocked(api.getControl).mockResolvedValue({
      harness_paused: false,
      eval_paused: false,
      gateway_paused: false,
      published_at: Date.now() / 1000,
      stale: false,
      aborted_runs: [],
      updated_by: '',
      reason: '',
    });
    vi.mocked(api.getJudgeResults).mockResolvedValue({
      run_id: 'run-1',
      results: [
        {
          instance_id: 'django__django-1',
          attempt_number: 1,
          judged_at: '2026-09-08T00:00:00+00:00',
          judge_model_resolved: 'judge-model',
          rubric_version: '1',
          judge_prune_mode: 'pruned',
          input_truncated: false,
          tool_output_pruned: false,
          judge_parse_failed: false,
          summary: null,
          judge_cost_usd: 0.001,
          dimensions: [],
        },
      ],
    });

    renderWithClient(
      <RunDetail runId="run-1" onBack={() => {}} onOpenInstance={() => {}} />,
    );

    await screen.findByText('django__django-1');
    await screen.findByText('django__django-2');
    const filter = screen.getByLabelText('filter judged');
    // the option label carries the live judged count
    expect(
      screen.getByRole('option', { name: 'judged (1)' }),
    ).toBeInTheDocument();

    await userEvent.selectOptions(filter, 'judged');
    expect(screen.getByText('django__django-1')).toBeInTheDocument();
    expect(screen.queryByText('django__django-2')).not.toBeInTheDocument();

    await userEvent.selectOptions(filter, 'unjudged');
    expect(screen.queryByText('django__django-1')).not.toBeInTheDocument();
    expect(screen.getByText('django__django-2')).toBeInTheDocument();

    await userEvent.selectOptions(filter, '');
    expect(screen.getByText('django__django-1')).toBeInTheDocument();
    expect(screen.getByText('django__django-2')).toBeInTheDocument();
  });
});

// 2026-09-08 (owner, after the first 500-attempt pass): "focus on the ones
// which had issues" — a per-rubric findings filter next to the judged
// filter, and a Judge findings column of chips. Client-side over the same
// judge-results query the Contamination column uses.
describe('RunDetail — judge findings filter + column', () => {
  it('narrows to the rows with a given finding and shows chips per row', async () => {
    const SECOND = { ...INSTANCE, instance_id: 'django__django-2' };
    const THIRD = { ...INSTANCE, instance_id: 'django__django-3' };
    vi.mocked(api.getRun).mockResolvedValue(RUN);
    vi.mocked(api.listInstances).mockResolvedValue({
      items: [INSTANCE, SECOND, THIRD],
      total: 3,
      limit: 50,
      offset: 0,
    });
    vi.mocked(api.getRunProgress).mockResolvedValue(RUN_PROGRESS);
    vi.mocked(api.getRunLive).mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      state: 'ok',
      reason: '',
      items: [],
    });
    vi.mocked(api.getQueues).mockResolvedValue(QUEUES);
    vi.mocked(api.getControl).mockResolvedValue({
      harness_paused: false,
      eval_paused: false,
      gateway_paused: false,
      published_at: Date.now() / 1000,
      stale: false,
      aborted_runs: [],
      updated_by: '',
      reason: '',
    });
    const base = {
      attempt_number: 1,
      judged_at: '2026-09-08T00:00:00+00:00',
      judge_model_resolved: 'judge-model',
      rubric_version: '1',
      judge_prune_mode: 'pruned',
      input_truncated: false,
      tool_output_pruned: false,
      judge_parse_failed: false,
      summary: null,
      judge_cost_usd: 0.001,
    };
    const dimBase = {
      scale_type: 'likert',
      score_numeric: 0,
      score_secondary: null,
      flag: null,
      span_start_turn: null,
      span_end_turn: null,
      reasoning: null,
      evidence: [],
      evidence_missing: false,
      causes: [],
    };
    vi.mocked(api.getJudgeResults).mockResolvedValue({
      run_id: 'run-1',
      results: [
        // contamination 2 + an environment problem
        {
          ...base,
          instance_id: 'django__django-1',
          dimensions: [
            { ...dimBase, dimension_id: 'contamination', score_numeric: 2 },
            {
              ...dimBase,
              dimension_id: 'environment_problem',
              scale_type: 'boolean_with_span',
              score_numeric: null,
              flag: true,
            },
          ],
        },
        // judged, nothing raised
        {
          ...base,
          instance_id: 'django__django-2',
          dimensions: [
            { ...dimBase, dimension_id: 'contamination', score_numeric: 0 },
          ],
        },
        // django-3 is NOT judged
      ],
    });

    renderWithClient(
      <RunDetail runId="run-1" onBack={() => {}} onOpenInstance={() => {}} />,
    );

    await screen.findByText('django__django-1');
    await screen.findByText('django__django-3');

    // chips on the judged rows; the unjudged row shows no "clean"
    expect(screen.getByText('contam 2')).toBeInTheDocument();
    expect(screen.getByText('env')).toBeInTheDocument();
    expect(screen.getAllByText('clean')).toHaveLength(1);

    const filter = screen.getByLabelText('filter judge findings');
    // per-option counts over judged attempts
    expect(
      screen.getByRole('option', { name: 'contamination: yes (1)' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('option', { name: 'environment / tool problem (1)' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('option', { name: 'any finding (1)' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('option', { name: 'no findings (1)' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('option', { name: 'loop (0)' }),
    ).toBeInTheDocument();

    await userEvent.selectOptions(filter, 'contamination');
    expect(screen.getByText('django__django-1')).toBeInTheDocument();
    expect(screen.queryByText('django__django-2')).not.toBeInTheDocument();
    expect(screen.queryByText('django__django-3')).not.toBeInTheDocument();

    // "none" = judged with nothing raised; the unjudged row stays out
    await userEvent.selectOptions(filter, 'none');
    expect(screen.queryByText('django__django-1')).not.toBeInTheDocument();
    expect(screen.getByText('django__django-2')).toBeInTheDocument();
    expect(screen.queryByText('django__django-3')).not.toBeInTheDocument();

    await userEvent.selectOptions(filter, '');
    expect(screen.getByText('django__django-3')).toBeInTheDocument();
  });
});
