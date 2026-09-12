import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  api,
  type CalibrationReviewHistoryItem,
  type JudgeDimensionScoreItem,
} from '../lib/api';
import { fmtTime } from '../lib/format';
import { findJudgeResult } from '../lib/judge';
import { cn } from '../lib/utils';
import { Card, CardContent, CardHeader, CardTitle, SectionLabel } from './ui-primitives';

/**
 * The per-instance "Judge" panel (offline-analysis-design.md §9.6/§10.3,
 * calibration §11) — all 8 rubric dimensions with score/reasoning/evidence/
 * honesty flags, never a collapsed verdict, plus the approve/deny +
 * required-reasoning calibration controls next to each one.
 *
 * GET /runs/{run_id}/judge/results is NOT per-instance-scoped (it returns
 * every judged attempt in the run), so this fetches the whole run's results
 * once and finds this attempt client-side — the same query key RunDetail's
 * contamination column uses, so the two share cache rather than double-
 * fetching when navigating from one to the other.
 */

function renderScore(dim: JudgeDimensionScoreItem): string {
  if (dim.evidence_missing) return 'demoted — no evidence';
  switch (dim.scale_type) {
    case 'likert':
      return dim.score_numeric == null ? '—' : String(dim.score_numeric);
    case 'count_and_severity':
      return dim.score_numeric == null && dim.score_secondary == null
        ? '—'
        : `count ${dim.score_numeric ?? '—'} · severity ${dim.score_secondary ?? '—'}`;
    case 'ratio':
      return dim.score_numeric == null && dim.score_secondary == null
        ? '—'
        : `${dim.score_numeric ?? '—'} / ${dim.score_secondary ?? '—'} redundant`;
    case 'boolean_with_span':
      if (dim.flag == null) return '—';
      return dim.flag
        ? `present (turns ${dim.span_start_turn ?? '?'}–${dim.span_end_turn ?? '?'})`
        : 'not present';
    case 'causes':
      // rubric v3: avoidable share of the spend + severity; the causes list renders below
      return dim.score_numeric == null && dim.score_secondary == null
        ? '—'
        : `avoidable ${dim.score_numeric == null ? '—' : `${Math.round(dim.score_numeric * 100)}%`} · severity ${dim.score_secondary ?? '—'}`;
    default:
      return '—';
  }
}

type Cause = {
  cause?: unknown;
  label?: unknown;
  share?: unknown;
  recommendation?: unknown;
};

function CausesList({ causes }: { causes: Cause[] }) {
  if (causes.length === 0) return null;
  return (
    <ul className="mt-1 space-y-1" aria-label="avoidable-waste causes">
      {causes.map((c, i) => {
        const id = typeof c.label === 'string' ? c.label : String(c.cause ?? '?');
        const share =
          typeof c.share === 'number' ? ` · ${Math.round(c.share * 100)}% of the waste` : '';
        const rec = typeof c.recommendation === 'string' ? c.recommendation : null;
        return (
          <li key={i} className="text-[11px] text-zinc-700 dark:text-zinc-300">
            <span className="rounded bg-amber-50 px-1 py-0.5 font-mono text-[10px] font-semibold text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
              {id.replace(/_/g, ' ')}
            </span>
            {share}
            {rec && <span className="text-zinc-500"> — {rec}</span>}
          </li>
        );
      })}
    </ul>
  );
}

function evidenceField(e: Record<string, unknown>, key: string): string {
  const v = e[key];
  if (typeof v === 'string') return v;
  if (typeof v === 'number') return String(v);
  return '';
}

function DimensionCard({
  runId,
  instanceId,
  attempt,
  judgedAt,
  dim,
  latestReview,
}: {
  runId: string;
  instanceId: string;
  attempt: number;
  judgedAt: string;
  dim: JudgeDimensionScoreItem;
  latestReview: CalibrationReviewHistoryItem | undefined;
}) {
  const qc = useQueryClient();
  const [reasoning, setReasoning] = useState('');
  const [correctedValue, setCorrectedValue] = useState('');

  const review = useMutation({
    mutationFn: (decision: 'approve' | 'deny') => {
      // No corrected-value input is shown for boolean_with_span dims (a
      // present/not-present override needs a checkbox, not a number field
      // — left for a later pass); corrected_score_numeric only ever comes
      // from the number input, which only renders for the other 3 scales.
      const corrected =
        correctedValue.trim() !== '' && !Number.isNaN(Number(correctedValue))
          ? Number(correctedValue)
          : null;
      return api.reviewJudgeDimension(runId, instanceId, attempt, {
        judged_at: judgedAt,
        dimension_id: dim.dimension_id,
        decision,
        reviewer_reasoning: reasoning,
        reviewed_by: 'operator',
        corrected_score_numeric: corrected,
      });
    },
    onSuccess: () => {
      setReasoning('');
      setCorrectedValue('');
      qc.invalidateQueries({
        queryKey: ['judge-review-history', runId, instanceId, attempt, judgedAt],
      });
    },
  });

  return (
    <div className="rounded-md border border-zinc-200 p-3 dark:border-zinc-800">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-semibold">
          {dim.dimension_id}
        </span>
        <span className="text-[10px] uppercase tracking-wide text-zinc-400">
          {dim.scale_type}
        </span>
        <span className="ml-auto text-xs font-medium tabular-nums">
          {renderScore(dim)}
        </span>
      </div>

      {dim.evidence_missing && (
        <div className="mt-1 text-[11px] font-medium text-amber-600 dark:text-amber-400">
          flagged above baseline, but no evidence was cited — demoted, not a
          clean score
        </div>
      )}

      <p className="mt-1 text-xs text-zinc-700 dark:text-zinc-300">
        {dim.reasoning ?? '—'}
      </p>

      {dim.scale_type === 'causes' && (
        <CausesList causes={(dim.causes ?? []) as Cause[]} />
      )}

      {dim.evidence.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {dim.evidence.map((e, i) => (
            <li key={i} className="text-[11px] text-zinc-500">
              turn {evidenceField(e, 'turn') || '?'}: "{evidenceField(e, 'quote')}"
            </li>
          ))}
        </ul>
      )}

      {latestReview && (
        <div className="mt-2 rounded bg-zinc-50 px-2 py-1 text-[11px] text-zinc-500 dark:bg-zinc-900">
          already reviewed:{' '}
          <span
            className={cn(
              'font-semibold',
              latestReview.decision === 'approve'
                ? 'text-emerald-600 dark:text-emerald-400'
                : 'text-rose-500',
            )}
          >
            {latestReview.decision}
          </span>{' '}
          by {latestReview.reviewed_by} · {fmtTime(latestReview.reviewed_at)}{' '}
          — "{latestReview.reviewer_reasoning}"
        </div>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-zinc-100 pt-2 dark:border-zinc-800">
        <textarea
          aria-label={`review reasoning for ${dim.dimension_id}`}
          placeholder="reasoning (required to approve or deny)"
          value={reasoning}
          onChange={(e) => setReasoning(e.target.value)}
          rows={2}
          className="w-full min-w-[220px] flex-1 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-700 dark:bg-zinc-900"
        />
        {dim.scale_type !== 'boolean_with_span' && (
          <input
            aria-label={`corrected value for ${dim.dimension_id}`}
            type="number"
            placeholder="corrected value (optional)"
            value={correctedValue}
            onChange={(e) => setCorrectedValue(e.target.value)}
            className="w-40 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-700 dark:bg-zinc-900"
          />
        )}
        <button
          type="button"
          disabled={!reasoning.trim() || review.isPending}
          onClick={() => review.mutate('approve')}
          className="rounded-md bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white disabled:opacity-40"
        >
          Approve
        </button>
        <button
          type="button"
          disabled={!reasoning.trim() || review.isPending}
          onClick={() => review.mutate('deny')}
          className="rounded-md bg-rose-600 px-2.5 py-1 text-xs font-medium text-white disabled:opacity-40"
        >
          Deny
        </button>
        {review.isSuccess && (
          <span className="text-xs text-emerald-600 dark:text-emerald-400">
            recorded ✓
          </span>
        )}
        {review.isError && (
          <span className="text-xs text-rose-500">
            {(review.error as Error).message}
          </span>
        )}
      </div>
    </div>
  );
}

export function JudgeResultPanel({
  runId,
  instanceId,
  attempt,
}: {
  runId: string;
  instanceId: string;
  attempt: number;
}) {
  const results = useQuery({
    queryKey: ['judge-results', runId],
    queryFn: () => api.getJudgeResults(runId),
    staleTime: 30_000,
  });

  const result = findJudgeResult(results.data?.results, instanceId, attempt);

  const history = useQuery({
    queryKey: [
      'judge-review-history',
      runId,
      instanceId,
      attempt,
      result?.judged_at,
    ],
    queryFn: () =>
      api.getJudgeReviewHistory(runId, instanceId, attempt, result!.judged_at),
    enabled: Boolean(result),
  });

  // Latest review per dimension — fetch_reviews_for_result returns newest
  // first within each dimension, so the first occurrence wins.
  const latestByDimension = new Map<string, CalibrationReviewHistoryItem>();
  for (const r of history.data?.reviews ?? []) {
    if (!latestByDimension.has(r.dimension_id)) {
      latestByDimension.set(r.dimension_id, r);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Judge (Pass B)</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {results.isLoading && (
          <p className="text-xs text-zinc-400">loading judge results…</p>
        )}
        {results.isError && (
          <p className="text-xs text-rose-500">
            could not load judge results — state unknown, not healthy
          </p>
        )}
        {results.data && !result && (
          <p className="text-xs text-zinc-500">
            This attempt has not been judged yet — launch a judge pass from
            the run screen.
          </p>
        )}

        {result && (
          <>
            <div className="grid grid-cols-2 gap-3 text-xs md:grid-cols-4">
              <div>
                <SectionLabel>model</SectionLabel>
                <span className="font-mono text-xs">
                  {result.judge_model_resolved ?? '—'}
                </span>
              </div>
              <div>
                <SectionLabel>rubric version</SectionLabel>
                <span className="font-mono text-xs">
                  {result.rubric_version}
                </span>
              </div>
              <div>
                <SectionLabel>prune mode</SectionLabel>
                <span className="font-mono text-xs">
                  {result.judge_prune_mode ?? '—'}
                </span>
              </div>
              <div>
                <SectionLabel>judged at</SectionLabel>
                <span className="font-mono text-xs">
                  {fmtTime(result.judged_at)}
                </span>
              </div>
            </div>

            {/* Honesty flags — a finding drawn from a pruned/truncated/
                unparseable response is scoped to what it could see, never
                silently treated as complete (§3.4/§3.6). */}
            <div className="flex flex-wrap gap-2">
              {result.tool_output_pruned && (
                <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
                  tool output pruned
                </span>
              )}
              {result.input_truncated && (
                <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
                  input truncated (events elided)
                </span>
              )}
              {result.judge_parse_failed && (
                <span className="rounded bg-rose-50 px-1.5 py-0.5 text-[10px] font-medium text-rose-700 dark:bg-rose-900/40 dark:text-rose-300">
                  judge response failed to parse — scores below are
                  unreliable
                </span>
              )}
            </div>

            {result.summary && (
              <p className="text-xs text-zinc-600 dark:text-zinc-300">
                {result.summary}
              </p>
            )}

            {history.isError && (
              <p className="text-[11px] text-rose-500">
                could not load review history — state unknown, not healthy
              </p>
            )}

            <div className="space-y-2">
              {result.dimensions.map((dim) => (
                <DimensionCard
                  key={dim.dimension_id}
                  runId={runId}
                  instanceId={instanceId}
                  attempt={attempt}
                  judgedAt={result.judged_at}
                  dim={dim}
                  latestReview={latestByDimension.get(dim.dimension_id)}
                />
              ))}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
