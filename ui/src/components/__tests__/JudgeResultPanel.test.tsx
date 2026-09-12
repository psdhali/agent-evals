import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { JudgeResultPanel } from '../JudgeResultPanel';

// offline-analysis-design.md §9.6/§11: all 8 rubric dimensions, never a
// collapsed verdict, plus the approve/deny + required-reasoning calibration
// controls next to each one. §5 point 3 of the 2026-09-02 review: unknown
// must render as unknown — evidence_missing is "demoted, no evidence
// cited", never folded into a plain score.

vi.mock('../../lib/api', () => ({
  api: {
    getJudgeResults: vi.fn(),
    getJudgeReviewHistory: vi.fn(),
    reviewJudgeDimension: vi.fn(),
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

const RESULT = {
  instance_id: 'inst-1',
  attempt_number: 1,
  judged_at: '2026-09-02T00:00:00+00:00',
  judge_model_resolved: 'deepseek/deepseek-v4-flash-0731',
  rubric_version: '1',
  judge_prune_mode: 'pruned',
  input_truncated: false,
  tool_output_pruned: true,
  judge_parse_failed: false,
  summary: 'clean run overall',
  judge_cost_usd: 0.004,
  dimensions: [
    {
      dimension_id: 'contamination',
      scale_type: 'likert',
      score_numeric: 2,
      score_secondary: null,
      flag: null,
      span_start_turn: null,
      span_end_turn: null,
      reasoning: 'the agent named a test file it never read',
      evidence: [{ turn: 7, quote: 'test_structured_masked_column' }],
      evidence_missing: false,
      causes: [],
    },
    {
      dimension_id: 'test_gaming',
      scale_type: 'likert',
      score_numeric: null,
      score_secondary: null,
      flag: null,
      span_start_turn: null,
      span_end_turn: null,
      reasoning: 'possibly weakened an assertion but no quote available',
      evidence: [],
      evidence_missing: true,
      causes: [],
    },
  ],
};

describe('JudgeResultPanel — not judged yet', () => {
  it('shows a clear message instead of an empty table when no result exists', async () => {
    vi.mocked(api.getJudgeResults).mockResolvedValue({
      run_id: 'run-1',
      results: [],
    });
    renderWithClient(
      <JudgeResultPanel runId="run-1" instanceId="inst-1" attempt={1} />,
    );
    await screen.findByText(/has not been judged yet/i);
    expect(api.getJudgeReviewHistory).not.toHaveBeenCalled();
  });
});

describe('JudgeResultPanel — renders every dimension honestly', () => {
  it('renders a scored dimension with its evidence', async () => {
    vi.mocked(api.getJudgeResults).mockResolvedValue({
      run_id: 'run-1',
      results: [RESULT],
    });
    vi.mocked(api.getJudgeReviewHistory).mockResolvedValue({
      run_id: 'run-1',
      instance_id: 'inst-1',
      attempt_number: 1,
      reviews: [],
    });
    renderWithClient(
      <JudgeResultPanel runId="run-1" instanceId="inst-1" attempt={1} />,
    );
    await screen.findByText('contamination');
    expect(
      screen.getByText(/the agent named a test file it never read/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/test_structured_masked_column/)).toBeInTheDocument();
  });

  it('renders evidence_missing as "demoted", never as a plain score of 0', async () => {
    vi.mocked(api.getJudgeResults).mockResolvedValue({
      run_id: 'run-1',
      results: [RESULT],
    });
    vi.mocked(api.getJudgeReviewHistory).mockResolvedValue({
      run_id: 'run-1',
      instance_id: 'inst-1',
      attempt_number: 1,
      reviews: [],
    });
    renderWithClient(
      <JudgeResultPanel runId="run-1" instanceId="inst-1" attempt={1} />,
    );
    await screen.findByText('test_gaming');
    expect(screen.getByText(/demoted — no evidence/i)).toBeInTheDocument();
    expect(
      screen.getByText(/flagged above baseline, but no evidence/i),
    ).toBeInTheDocument();
  });

  it('shows the honesty flags when pruning affected this row', async () => {
    vi.mocked(api.getJudgeResults).mockResolvedValue({
      run_id: 'run-1',
      results: [RESULT],
    });
    vi.mocked(api.getJudgeReviewHistory).mockResolvedValue({
      run_id: 'run-1',
      instance_id: 'inst-1',
      attempt_number: 1,
      reviews: [],
    });
    renderWithClient(
      <JudgeResultPanel runId="run-1" instanceId="inst-1" attempt={1} />,
    );
    await screen.findByText(/tool output pruned/i);
  });
});

describe('JudgeResultPanel — the approve/deny calibration controls', () => {
  it('the approve and deny buttons stay disabled until reasoning is filled in', async () => {
    vi.mocked(api.getJudgeResults).mockResolvedValue({
      run_id: 'run-1',
      results: [RESULT],
    });
    vi.mocked(api.getJudgeReviewHistory).mockResolvedValue({
      run_id: 'run-1',
      instance_id: 'inst-1',
      attempt_number: 1,
      reviews: [],
    });
    renderWithClient(
      <JudgeResultPanel runId="run-1" instanceId="inst-1" attempt={1} />,
    );
    await screen.findByText('contamination');

    const approveButtons = screen.getAllByRole('button', { name: /^approve$/i });
    expect(approveButtons[0]).toBeDisabled();

    const reasoningBox = screen.getByLabelText(
      /review reasoning for contamination/i,
    );
    await userEvent.type(reasoningBox, 'agreed, evidence is solid');

    expect(approveButtons[0]).toBeEnabled();
    expect(api.reviewJudgeDimension).not.toHaveBeenCalled();
  });

  it('submits the review with judged_at pinned to this result and the typed reasoning', async () => {
    vi.mocked(api.getJudgeResults).mockResolvedValue({
      run_id: 'run-1',
      results: [RESULT],
    });
    vi.mocked(api.getJudgeReviewHistory).mockResolvedValue({
      run_id: 'run-1',
      instance_id: 'inst-1',
      attempt_number: 1,
      reviews: [],
    });
    vi.mocked(api.reviewJudgeDimension).mockResolvedValue({
      run_id: 'run-1',
      instance_id: 'inst-1',
      attempt_number: 1,
      dimension_id: 'contamination',
      status: 'recorded',
    });
    renderWithClient(
      <JudgeResultPanel runId="run-1" instanceId="inst-1" attempt={1} />,
    );
    await screen.findByText('contamination');

    const reasoningBox = screen.getByLabelText(
      /review reasoning for contamination/i,
    );
    await userEvent.type(reasoningBox, 'agreed, evidence is solid');
    const denyButtons = screen.getAllByRole('button', { name: /^deny$/i });
    await userEvent.click(denyButtons[0]);

    await waitFor(() =>
      expect(api.reviewJudgeDimension).toHaveBeenCalledWith(
        'run-1',
        'inst-1',
        1,
        expect.objectContaining({
          judged_at: '2026-09-02T00:00:00+00:00',
          dimension_id: 'contamination',
          decision: 'deny',
          reviewer_reasoning: 'agreed, evidence is solid',
        }),
      ),
    );
  });

  it('shows a prior review from history as "already reviewed"', async () => {
    vi.mocked(api.getJudgeResults).mockResolvedValue({
      run_id: 'run-1',
      results: [RESULT],
    });
    vi.mocked(api.getJudgeReviewHistory).mockResolvedValue({
      run_id: 'run-1',
      instance_id: 'inst-1',
      attempt_number: 1,
      reviews: [
        {
          dimension_id: 'contamination',
          decision: 'approve',
          reviewer_reasoning: 'checked independently, agrees',
          reviewed_by: 'operator',
          reviewed_at: '2026-09-02T01:00:00+00:00',
          corrected_score_numeric: null,
          corrected_score_secondary: null,
          corrected_flag: null,
        },
      ],
    });
    renderWithClient(
      <JudgeResultPanel runId="run-1" instanceId="inst-1" attempt={1} />,
    );
    await screen.findByText(/already reviewed/i);
    expect(
      screen.getByText(/checked independently, agrees/i),
    ).toBeInTheDocument();
  });
});
