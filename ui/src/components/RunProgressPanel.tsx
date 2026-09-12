import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import { cn } from '../lib/utils';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  SectionLabel,
} from './ui-primitives';

// Per-phase, per-state progress plus the two denominators (M2.4 / ADR-0034
// M1.8).  `expected` is the dispatch plan (900 for a 300-instance k=3 run) and
// `denominator` is gradeable — the honesty denominator.  They are displayed as
// two separate, labelled numbers because conflating them is precisely the
// honesty failure the ADR exists to prevent: a run aborted at 60 of 900 must
// never read as "60 of 60 measured".

export function RunProgressPanel({
  runId,
  gradeable,
}: {
  runId: string;
  /** The gradeable denominator DERIVED from the per-instance state buckets
   * (lib/rates.ts: verdicts + empty patches, infra excluded). When given it
   * replaces the stored summary's `denominator`, which dropped EMPTY_PATCH
   * until the results-writer fix and is never recomputed for a finished run. */
  gradeable?: number | null;
}) {
  const progress = useQuery({
    queryKey: ['run-progress', runId],
    queryFn: () => api.getRunProgress(runId),
    refetchInterval: (q) => (q.state.data?.terminal ? false : 5000),
    refetchIntervalInBackground: false,
    staleTime: 3000,
    enabled: Boolean(runId),
  });

  const data = progress.data;
  const phases = data?.phases ?? [];

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle>Progress</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {progress.isLoading && (
          <div className="text-sm text-zinc-400">Loading progress…</div>
        )}
        {progress.isError && (
          <div className="text-sm text-rose-500">
            failed to load progress: {(progress.error as Error).message}
          </div>
        )}
        {data && phases.length === 0 && (
          <div className="text-sm text-zinc-400">
            No instance rows yet — nothing has been dispatched for this run.
          </div>
        )}
        {data && phases.length > 0 && (
          <>
            {/* the two denominators, labelled so they cannot be confused */}
            <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs">
              <span>
                <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
                  expected (agent runs){' '}
                </span>
                <span className="font-mono font-semibold">
                  {data.expected ?? '—'}
                </span>
              </span>
              <span>
                <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
                  gradeable (verdicts + empty patches){' '}
                </span>
                <span className="font-mono font-semibold">
                  {gradeable ?? data.denominator ?? '—'}
                </span>
              </span>
              {data.expected === null && (
                <span className="text-[11px] text-amber-600 dark:text-amber-400">
                  expected unknown — run may not have been dispatched yet
                </span>
              )}
            </div>

            {Array.from(new Set(phases.map((p) => p.phase))).map((phase) => (
              <div key={phase}>
                <SectionLabel>{phase} phase</SectionLabel>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {phases
                    .filter((p) => p.phase === phase)
                    .map((p) => (
                      <span
                        key={p.state}
                        className={cn(
                          'rounded-md px-2 py-1 font-mono text-xs',
                          p.state === 'PENDING'
                            ? 'bg-zinc-100 dark:bg-zinc-800'
                            : 'bg-zinc-100 dark:bg-zinc-800',
                        )}
                      >
                        {p.state}{' '}
                        <span className="font-semibold">{p.count}</span>
                      </span>
                    ))}
                </div>
              </div>
            ))}
          </>
        )}
      </CardContent>
    </Card>
  );
}
