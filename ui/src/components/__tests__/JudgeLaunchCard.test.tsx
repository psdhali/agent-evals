import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { JudgeLaunchCard } from '../JudgeLaunchCard';

// BUILDER3-JUDGE-VERIFIED-AND-UI-BRIEF-2026-09-02.md §5: the pass-status
// banner is "a precondition, not a preference" — a pass the budget ceiling
// truncated must not look like a pass that finished. The launch confirm
// gate follows the same real-spend trust tier as RunDetail's restart.

vi.mock('../../lib/api', () => ({
  api: {
    listJudgePasses: vi.fn(),
    judgeEstimate: vi.fn(),
    launchJudge: vi.fn(),
    getJudgeLive: vi.fn(),
    getJudgeResults: vi.fn(),
  },
}));

import { api, type JudgeLiveState } from '../../lib/api';
import { beforeEach } from 'vitest';

beforeEach(() => {
  // No pass running unless a test says otherwise.
  vi.mocked(api.getJudgeLive).mockResolvedValue({
    run_id: 'run-1',
    live: null,
  });
  // No judgments unless a test says otherwise (the card reads them for its banner).
  vi.mocked(api.getJudgeResults).mockResolvedValue({
    run_id: 'run-1',
    results: [],
  });
});

function liveSnapshot(over: Partial<JudgeLiveState> = {}): JudgeLiveState {
  return {
    run_id: 'run-1',
    pass_id: 'judge-live-1',
    status: 'running',
    workers: 24,
    selected: 500,
    judged: 120,
    skipped_over_budget: 0,
    parse_failed: 2,
    skipped_artifacts: 1,
    call_failed: 0,
    timed_out: 0,
    already_judged: 0,
    spend_usd: 0.61,
    max_spend_usd: 25,
    started_at: 1_700_000_000,
    updated_at: 1_700_000_400,
    finished_at: null,
    elapsed_s: 400,
    eta_s: 1260,
    in_flight_count: 2,
    in_flight: [
      {
        instance_id: 'django__django-1',
        attempt_number: 1,
        started_at: 1_700_000_380,
      },
      {
        instance_id: 'sympy__sympy-2',
        attempt_number: 2,
        started_at: 1_700_000_390,
      },
    ],
    last_error: null,
    ...over,
  };
}

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
  );
}

describe('JudgeLaunchCard — confirm gate cannot be bypassed', () => {
  it('the launch button stays disabled until armed AND an estimate has loaded', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [],
    });
    vi.mocked(api.judgeEstimate).mockResolvedValue({
      run_id: 'run-1',
      candidate_count: 4,
      prune_mode: 'pruned',
      estimated_cost_usd: 0.42,
      status: 'estimate',
    });

    renderWithClient(
      <JudgeLaunchCard
        runId="run-1"
        terminal={false}
        selectedInstanceIds={[]}
      />,
    );

    const launchButton = await screen.findByRole('button', {
      name: /launch judge pass/i,
    });
    expect(launchButton).toBeDisabled();
    expect(api.judgeEstimate).not.toHaveBeenCalled();

    const arm = screen.getByLabelText('arm judge pass');
    await userEvent.click(arm);

    // Estimate is fetched only once armed — never before.
    await waitFor(() => expect(api.judgeEstimate).toHaveBeenCalled());
    await screen.findByText(/confirm ~\$0\.42 for 4 candidate/i);
    expect(launchButton).toBeEnabled();

    vi.mocked(api.launchJudge).mockResolvedValue({
      run_id: 'run-1',
      pass_id: 'judge-123',
      estimated_cost_usd: 0.42,
      status: 'started',
    });
    await userEvent.click(launchButton);
    await waitFor(() => expect(api.launchJudge).toHaveBeenCalledTimes(1));
    expect(api.launchJudge).toHaveBeenCalledWith('run-1', {
      instance_ids: undefined,
      prune_mode: 'pruned',
      max_spend_usd: 25,
      workers: 24,
      rejudge: false,
      retry_no_verdict: false,
      triggered_by: 'operator',
    });
  });

  it('the workers field is sent with the launch, clamped to 1..100, and changing it re-arms', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [],
    });
    vi.mocked(api.judgeEstimate).mockResolvedValue({
      run_id: 'run-1',
      candidate_count: 4,
      prune_mode: 'pruned',
      estimated_cost_usd: 0.42,
      status: 'estimate',
    });
    vi.mocked(api.launchJudge).mockClear();
    vi.mocked(api.launchJudge).mockResolvedValue({
      run_id: 'run-1',
      pass_id: 'judge-124',
      estimated_cost_usd: 0.42,
      status: 'started',
    });

    renderWithClient(
      <JudgeLaunchCard
        runId="run-1"
        terminal={false}
        selectedInstanceIds={[]}
      />,
    );

    const workers = await screen.findByLabelText('judge workers');
    expect(workers).toHaveValue(24);
    const arm = screen.getByLabelText('arm judge pass');
    await userEvent.click(arm);
    expect(arm).toBeChecked();

    await userEvent.clear(workers);
    await userEvent.type(workers, '250');
    // editing the worker count invalidates a prior confirm, like prune mode does
    expect(arm).not.toBeChecked();

    await userEvent.click(arm);
    const launchButton = screen.getByRole('button', {
      name: /launch judge pass/i,
    });
    await waitFor(() => expect(launchButton).toBeEnabled());
    await userEvent.click(launchButton);
    await waitFor(() => expect(api.launchJudge).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.launchJudge).mock.calls[0][1].workers).toBe(100);
  });

  it('re-judge is off by default (a relaunch resumes), is sent when ticked, and re-arms the confirm', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [],
    });
    vi.mocked(api.judgeEstimate).mockClear();
    vi.mocked(api.judgeEstimate).mockResolvedValue({
      run_id: 'run-1',
      candidate_count: 4,
      prune_mode: 'pruned',
      estimated_cost_usd: 0.42,
      status: 'estimate',
    });
    vi.mocked(api.launchJudge).mockClear();
    vi.mocked(api.launchJudge).mockResolvedValue({
      run_id: 'run-1',
      pass_id: 'judge-125',
      estimated_cost_usd: 0.42,
      status: 'started',
    });

    renderWithClient(
      <JudgeLaunchCard
        runId="run-1"
        terminal={false}
        selectedInstanceIds={[]}
      />,
    );

    const rejudge = await screen.findByLabelText('re-judge already judged');
    expect(rejudge).not.toBeChecked();
    const arm = screen.getByLabelText('arm judge pass');
    await userEvent.click(arm);
    expect(arm).toBeChecked();
    // the estimate mirrors the launch's resume rule: not a re-judge by default
    await waitFor(() => expect(api.judgeEstimate).toHaveBeenCalled());
    expect(vi.mocked(api.judgeEstimate).mock.calls[0][3]).toBe(false);

    await userEvent.click(rejudge);
    expect(rejudge).toBeChecked();
    // a stale confirm never covers a request that now re-judges everything
    expect(arm).not.toBeChecked();

    await userEvent.click(arm);
    await waitFor(() =>
      expect(
        vi.mocked(api.judgeEstimate).mock.calls[
          vi.mocked(api.judgeEstimate).mock.calls.length - 1
        ]?.[3],
      ).toBe(true),
    );
    const launchButton = screen.getByRole('button', {
      name: /launch judge pass/i,
    });
    await waitFor(() => expect(launchButton).toBeEnabled());
    await userEvent.click(launchButton);
    await waitFor(() => expect(api.launchJudge).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.launchJudge).mock.calls[0][1].rejudge).toBe(true);
  });

  it('the live block shows call failures and already-judged skips', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [],
    });
    vi.mocked(api.getJudgeLive).mockResolvedValue({
      run_id: 'run-1',
      live: liveSnapshot({ call_failed: 3, already_judged: 75, selected: 428 }),
    });

    renderWithClient(
      <JudgeLaunchCard
        runId="run-1"
        terminal={false}
        selectedInstanceIds={[]}
      />,
    );

    const live = await screen.findByTestId('judge-live');
    expect(live).toHaveTextContent(/120 of 428 judged/);
    expect(live).toHaveTextContent(/3 call failures/);
    expect(live).toHaveTextContent(/relaunch resumes them/);
    expect(live).toHaveTextContent(/75 already judged, skipped/);
  });

  it('changing prune mode re-arms the confirm — a stale estimate cannot cover a different request', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [],
    });
    vi.mocked(api.judgeEstimate).mockResolvedValue({
      run_id: 'run-1',
      candidate_count: 4,
      prune_mode: 'pruned',
      estimated_cost_usd: 0.42,
      status: 'estimate',
    });

    renderWithClient(
      <JudgeLaunchCard
        runId="run-1"
        terminal={false}
        selectedInstanceIds={[]}
      />,
    );

    const arm = await screen.findByLabelText('arm judge pass');
    await userEvent.click(arm);
    expect(arm).toBeChecked();

    const pruneSelect = screen.getByLabelText(/prune mode/i);
    await userEvent.selectOptions(pruneSelect, 'full');

    expect(arm).not.toBeChecked();
    expect(
      screen.getByRole('button', { name: /launch judge pass/i }),
    ).toBeDisabled();
  });
});

describe('JudgeLaunchCard — live judge progress (2026-09-07)', () => {
  it('shows the running pass: judged/selected, in flight, spend, ETA and the in-flight ids', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [],
    });
    vi.mocked(api.getJudgeLive).mockResolvedValue({
      run_id: 'run-1',
      live: liveSnapshot(),
    });

    renderWithClient(
      <JudgeLaunchCard
        runId="run-1"
        terminal={false}
        selectedInstanceIds={[]}
      />,
    );

    const block = await screen.findByTestId('judge-live');
    expect(block).toHaveTextContent(/judge pass running/i);
    expect(block).toHaveTextContent(/120 of 500 judged/);
    expect(block).toHaveTextContent(/2 in flight/);
    expect(block).toHaveTextContent(/2 parse failures/);
    expect(block).toHaveTextContent(/1 skipped \(artifact fetch\)/);
    expect(block).toHaveTextContent(/24 workers/);
    expect(block).toHaveTextContent(/6m 40s elapsed/);
    expect(block).toHaveTextContent(/~21m 00s left/);
    expect(block).toHaveTextContent('django__django-1/1');
    expect(block).toHaveTextContent('sympy__sympy-2/2');
    expect(
      screen.getByRole('progressbar', { name: /judge pass progress/i }),
    ).toHaveAttribute('aria-valuenow', '24');
    // the history banner still says nothing ran — the live block is the only signal
    expect(screen.getByText(/no judge pass has run yet/i)).toBeInTheDocument();
  });

  it('a failed snapshot reads as FAILED with its error, never as still running', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [],
    });
    vi.mocked(api.getJudgeLive).mockResolvedValue({
      run_id: 'run-1',
      live: liveSnapshot({
        status: 'failed',
        in_flight: [],
        in_flight_count: 0,
        finished_at: 1_700_000_500,
        last_error: 'RuntimeError: gateway exploded',
      }),
    });

    renderWithClient(
      <JudgeLaunchCard
        runId="run-1"
        terminal={false}
        selectedInstanceIds={[]}
      />,
    );

    const block = await screen.findByTestId('judge-live');
    expect(block).toHaveTextContent(/judge pass FAILED/);
    expect(block).toHaveTextContent('RuntimeError: gateway exploded');
    expect(block).not.toHaveTextContent(/running/i);
    expect(block).not.toHaveTextContent(/left/);
  });

  it('renders nothing live when no pass is running', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [],
    });

    renderWithClient(
      <JudgeLaunchCard
        runId="run-1"
        terminal={false}
        selectedInstanceIds={[]}
      />,
    );

    await screen.findByText(/no judge pass has run yet/i);
    expect(screen.queryByTestId('judge-live')).toBeNull();
  });
});

describe('JudgeLaunchCard — pass-status banner never lets a truncated pass look complete', () => {
  it('shows the skipped-at-ceiling banner when a pass hit the budget ceiling', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [
        {
          pass_id: 'judge-truncated',
          requested_rate: 1,
          seed: null,
          total_eligible: 2500,
          total_judged: 1900,
          total_skipped_over_budget: 600,
          total_parse_failed: 3,
          created_at: '2026-09-02T00:00:00Z',
          synthesis_only: false,
        },
      ],
    });

    renderWithClient(
      <JudgeLaunchCard
        runId="run-1"
        terminal={false}
        selectedInstanceIds={[]}
      />,
    );

    await screen.findByText(/judged 1900 of 2500 eligible/i);
    expect(
      screen.getByText(/600 skipped at the budget ceiling/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/3 parse failures/i)).toBeInTheDocument();
  });

  it('shows "unknown", never 0, for a pass predating pass-level reporting', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [
        {
          pass_id: 'judge-old',
          requested_rate: 1,
          seed: null,
          total_eligible: null,
          total_judged: 12,
          total_skipped_over_budget: null,
          total_parse_failed: null,
          created_at: '2026-08-30T00:00:00Z',
          synthesis_only: false,
        },
      ],
    });

    renderWithClient(
      <JudgeLaunchCard
        runId="run-1"
        terminal={false}
        selectedInstanceIds={[]}
      />,
    );

    await screen.findByText(/eligible\/skipped counts unknown/i);
    // Never renders as a bare "0 skipped" or "0 eligible" — that would be
    // exactly the "unknown reads as healthy" failure this banner exists
    // to prevent.
    expect(screen.queryByText(/skipped at the budget ceiling/i)).toBeNull();
  });

  it('shows a clean completion banner when nothing was skipped', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [
        {
          pass_id: 'judge-clean',
          requested_rate: 1,
          seed: null,
          total_eligible: 300,
          total_judged: 300,
          total_skipped_over_budget: 0,
          total_parse_failed: 0,
          created_at: '2026-09-02T00:00:00Z',
          synthesis_only: false,
        },
      ],
    });

    renderWithClient(
      <JudgeLaunchCard
        runId="run-1"
        terminal={false}
        selectedInstanceIds={[]}
      />,
    );

    await screen.findByText(/judged 300 of 300 eligible/i);
    expect(screen.queryByText(/skipped/i)).toBeNull();
    expect(screen.queryByText(/parse failures/i)).toBeNull();
  });
});

// 2026-09-08 (owner): the pass report — the judge model's synthesis over every
// recorded judgment, written at the end of the pass and stored on the pass row.
describe('JudgeLaunchCard — pass report (synthesis)', () => {
  const basePass = {
    pass_id: 'judge-1',
    requested_rate: 1,
    seed: null,
    total_eligible: 500,
    total_judged: 500,
    total_skipped_over_budget: 0,
    total_parse_failed: 0,
    created_at: '2026-09-08T00:00:00Z',
    synthesis_only: false,
  };

  it('shows the report with its model and cost when the pass wrote one', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [
        {
          ...basePass,
          synthesis:
            '## Overview\n500 attempts judged. Two showed contamination (django__django-10973).',
          synthesis_cost_usd: 0.021,
          synthesis_model_resolved: 'deepseek/deepseek-v4-flash-0731',
          synthesis_error: null,
        },
      ],
    });
    renderWithClient(
      <JudgeLaunchCard runId="run-1" terminal selectedInstanceIds={[]} />,
    );
    const report = await screen.findByTestId('judge-pass-report');
    expect(report).toHaveTextContent('pass report');
    expect(report).toHaveTextContent('by deepseek/deepseek-v4-flash-0731');
    expect(report).toHaveTextContent(
      'Two showed contamination (django__django-10973)',
    );
  });

  it('says why when the report was not written, and shows nothing for an older pass', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [
        {
          ...basePass,
          synthesis: null,
          synthesis_cost_usd: null,
          synthesis_model_resolved: null,
          synthesis_error:
            'skipped: budget ceiling reached before the synthesis call',
        },
      ],
    });
    renderWithClient(
      <JudgeLaunchCard runId="run-1" terminal selectedInstanceIds={[]} />,
    );
    const report = await screen.findByTestId('judge-pass-report');
    expect(report).toHaveTextContent(
      'not written — skipped: budget ceiling reached',
    );

    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-2',
      passes: [{ ...basePass, pass_id: 'judge-old' }],
    });
    renderWithClient(
      <JudgeLaunchCard runId="run-2" terminal selectedInstanceIds={[]} />,
    );
    await screen.findAllByText(/judged 500 of 500 eligible/i);
    expect(screen.getAllByTestId('judge-pass-report')).toHaveLength(1);
  });

  it('a synthesizing snapshot reads as still active, with its own label', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [],
    });
    vi.mocked(api.getJudgeLive).mockResolvedValue({
      run_id: 'run-1',
      live: {
        run_id: 'run-1',
        pass_id: 'judge-live',
        status: 'synthesizing',
        workers: 30,
        selected: 500,
        judged: 500,
        skipped_over_budget: 0,
        parse_failed: 0,
        skipped_artifacts: 0,
        call_failed: 0,
        timed_out: 0,
        already_judged: 0,
        spend_usd: 1.3,
        max_spend_usd: 6,
        started_at: 0,
        updated_at: 0,
        finished_at: null,
        elapsed_s: 3000,
        eta_s: null,
        in_flight_count: 0,
        in_flight: [],
        last_error: null,
      },
    });
    renderWithClient(
      <JudgeLaunchCard runId="run-1" terminal selectedInstanceIds={[]} />,
    );
    const live = await screen.findByTestId('judge-live');
    expect(live).toHaveTextContent('judge pass writing its report');
    expect(live).not.toHaveTextContent('judge pass finished');
  });
});

// 2026-09-08 (owner): "regenerate report" — a synthesis-only pass, behind a
// confirm; the banner keeps reading the latest JUDGING pass.
describe('JudgeLaunchCard — regenerate report', () => {
  const judgingPass = {
    pass_id: 'judge-1',
    requested_rate: 1,
    seed: null,
    total_eligible: 500,
    total_judged: 500,
    total_skipped_over_budget: 0,
    total_parse_failed: 0,
    created_at: '2026-09-08T00:00:00Z',
    synthesis: null,
    synthesis_cost_usd: null,
    synthesis_model_resolved: null,
    synthesis_error:
      'skipped: budget ceiling reached before the synthesis call',
    synthesis_only: false,
  };
  const reportPass = {
    ...judgingPass,
    pass_id: 'judge-2',
    total_eligible: 0,
    total_judged: 0,
    created_at: '2026-09-08T01:00:00Z',
    synthesis: '## Overview\nRegenerated.',
    synthesis_cost_usd: 0.02,
    synthesis_model_resolved: 'deepseek/deepseek-v4-flash-0731',
    synthesis_error: null,
    synthesis_only: true,
  };

  it('banner reads the judging pass, report reads the report-only pass', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [reportPass, judgingPass], // newest first
    });
    renderWithClient(
      <JudgeLaunchCard runId="run-1" terminal selectedInstanceIds={[]} />,
    );
    await screen.findByText(/judged 500 of 500 eligible/i);
    const report = screen.getByTestId('judge-pass-report');
    expect(report).toHaveTextContent('Regenerated.');
    expect(report).toHaveTextContent('by deepseek/deepseek-v4-flash-0731');
    expect(report).not.toHaveTextContent('not written');
  });

  it('is armed then confirmed, and launches a synthesis-only pass', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [judgingPass],
    });
    vi.mocked(api.launchJudge).mockResolvedValue({
      run_id: 'run-1',
      pass_id: 'judge-3',
      estimated_cost_usd: 0.02,
      status: 'started',
    });
    renderWithClient(
      <JudgeLaunchCard runId="run-1" terminal selectedInstanceIds={[]} />,
    );
    vi.mocked(api.launchJudge).mockClear();
    const regen = await screen.findByRole('button', {
      name: 'regenerate report',
    });
    expect(api.launchJudge).not.toHaveBeenCalled();
    await userEvent.click(regen);
    await userEvent.click(
      screen.getByRole('button', { name: 'confirm regenerate' }),
    );
    await screen.findByText(/report pass judge-3 started/);
    expect(api.launchJudge).toHaveBeenCalledTimes(1);
    const body = vi.mocked(api.launchJudge).mock.calls[0][1];
    expect(body.synthesis_only).toBe(true);
    expect(body.workers).toBe(1);
    expect(body.instance_ids).toBeUndefined();
  });
});

// 2026-09-08 (owner): "add the ability to include failed / timed out in the LLM
// judge panel itself" — and never say "no judge pass has run" when judgments exist.
describe('JudgeLaunchCard — retry attempts with no verdict', () => {
  const baseResult = {
    attempt_number: 1,
    judged_at: '2026-09-08T00:00:00+00:00',
    judge_model_resolved: 'judge-model',
    rubric_version: '2',
    judge_prune_mode: 'pruned',
    input_truncated: false,
    tool_output_pruned: false,
    judge_parse_failed: false,
    summary: null,
    judge_cost_usd: 0.001,
    dimensions: [],
  };

  it('shows the no-verdict count, sends retry_no_verdict, and never claims no pass ran', async () => {
    vi.mocked(api.listJudgePasses).mockResolvedValue({
      run_id: 'run-1',
      passes: [], // the earlier pass died: no ledger row
    });
    vi.mocked(api.getJudgeResults).mockResolvedValue({
      run_id: 'run-1',
      results: [
        { ...baseResult, instance_id: 'i1', judge_method: 'primary' },
        { ...baseResult, instance_id: 'i2', judge_method: 'timeout' },
        {
          ...baseResult,
          instance_id: 'i3',
          judge_method: 'primary',
          judge_parse_failed: true,
        },
      ],
    });
    vi.mocked(api.judgeEstimate).mockResolvedValue({
      run_id: 'run-1',
      candidate_count: 2,
      prune_mode: 'pruned',
      estimated_cost_usd: 0.01,
      status: 'estimate',
    });
    vi.mocked(api.launchJudge).mockClear();
    vi.mocked(api.launchJudge).mockResolvedValue({
      run_id: 'run-1',
      pass_id: 'judge-7',
      estimated_cost_usd: 0.01,
      status: 'started',
    });
    renderWithClient(
      <JudgeLaunchCard runId="run-1" terminal selectedInstanceIds={[]} />,
    );
    await screen.findByText(
      /no completed judge pass yet — 3 attempt\(s\) judged/,
    );
    expect(screen.getByTestId('judge-no-verdict')).toHaveTextContent(
      '2 attempt(s) have a judgment with no verdict',
    );
    expect(
      screen.queryByText('no judge pass has run yet for this run.'),
    ).not.toBeInTheDocument();

    await userEvent.click(
      screen.getByRole('checkbox', { name: 'retry attempts with no verdict' }),
    );
    await userEvent.click(screen.getByLabelText('arm judge pass'));
    await screen.findByText(/confirm .* for 2 candidate\(s\)/);
    const calls = vi.mocked(api.judgeEstimate).mock.calls;
    expect(calls[calls.length - 1][4]).toBe(true);
    await userEvent.click(
      screen.getByRole('button', { name: /launch judge pass/i }),
    );
    await screen.findByText(/judge-7/);
    expect(vi.mocked(api.launchJudge).mock.calls[0][1].retry_no_verdict).toBe(
      true,
    );
  });
});
