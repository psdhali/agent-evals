import { useQuery } from '@tanstack/react-query';
import { api, type QueueView } from '../lib/api';
import { fmtDuration } from '../lib/format';
import { isAnyRunActive, useAnyRunActive } from '../lib/queries';
import { cn } from '../lib/utils';
import { Card, CardContent, CardHeader, CardTitle } from './ui-primitives';

// Queue panels — the incident view (M2.1 / M2.4).  During the smoke the first
// question is always "is anything actually moving?"; the difference between
// 50 visible and 50 not-visible is the difference between capacity and an
// incident.  A DLQ depth above zero is an alarm (M2.6), rendered as one — a
// gated receive can redrive the whole backlog to the DLQ within seconds.
//
// Polling is gated on run activity (§1a): queues are only meaningful while a
// run is live, and an idle dashboard must not keep any store awake.

export function QueuePanels() {
  const anyActive = useAnyRunActive();
  const queues = useQuery({
    queryKey: ['queues'],
    queryFn: api.getQueues,
    refetchInterval: isAnyRunActive(anyActive.data?.items) ? 10000 : false,
    refetchIntervalInBackground: false,
    staleTime: 5000,
  });

  const items = queues.data?.items ?? [];

  const Cell = ({ q }: { q: QueueView }) => {
    const stuck = q.not_visible > 0;
    const alarm = q.dlq_depth > 0;
    return (
      <Card
        className={cn(alarm && 'border-rose-400/60 dark:border-rose-600/70')}
      >
        <CardHeader className="pb-1.5">
          <CardTitle className="flex items-center gap-2 font-mono text-xs">
            {q.queue}
            {alarm && (
              <span className="rounded-full bg-rose-100 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-rose-700 dark:bg-rose-500/15 dark:text-rose-400">
                DLQ alarm
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-2 gap-2 text-xs">
          <div className="flex flex-col">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
              waiting
            </span>
            <span className="font-mono text-lg font-semibold text-zinc-800 dark:text-zinc-100">
              {q.visible.toLocaleString()}
            </span>
          </div>
          <div className="flex flex-col">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
              in flight
            </span>
            <span
              className={cn(
                'font-mono text-lg font-semibold',
                stuck
                  ? 'text-amber-600 dark:text-amber-400'
                  : 'text-zinc-800 dark:text-zinc-100',
              )}
            >
              {q.not_visible.toLocaleString()}
            </span>
          </div>
          <div className="flex flex-col">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
              oldest msg
            </span>
            <span className="font-mono text-xs text-zinc-600 dark:text-zinc-300">
              {q.oldest_age_s === null ? '—' : fmtDuration(q.oldest_age_s)}
            </span>
          </div>
          <div className="flex flex-col">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
              DLQ
            </span>
            <span
              className={cn(
                'font-mono text-xs font-semibold',
                alarm
                  ? 'text-rose-600 dark:text-rose-400'
                  : 'text-zinc-600 dark:text-zinc-300',
              )}
            >
              {q.dlq_depth.toLocaleString()}
            </span>
          </div>
        </CardContent>
      </Card>
    );
  };

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <div className="flex items-center gap-2">
          <CardTitle>Work queues</CardTitle>
          {isAnyRunActive(anyActive.data?.items) && (
            <span className="text-[11px] text-zinc-400">
              live run active — polling on
            </span>
          )}
        </div>
        <span className="text-[11px] text-zinc-400">
          in cloudwatch-installed envs oldest age is CloudWatch-backed; locally
          it is unavailable — the em dash, not 0
        </span>
      </CardHeader>
      <CardContent>
        {queues.isLoading && (
          <div className="p-4 text-sm text-zinc-400">Loading queues…</div>
        )}
        {queues.isError && (
          <div className="p-4 text-sm text-rose-500">
            failed to load queues: {(queues.error as Error).message}
          </div>
        )}
        {queues.data && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            {items.map((q) => (
              <Cell key={q.queue} q={q} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
