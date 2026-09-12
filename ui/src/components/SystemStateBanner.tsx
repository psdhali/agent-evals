import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import { POOL_LABELS } from '../lib/format';
import { cn, POOLS } from '../lib/utils';

// Persistent system-state banner (M5.1 view 2).  Persistent, not a toast:
// the operator must never have to remember whether they paused.  Derived from
// the same /control query as the ControlPanel (shared queryKey → one poll),
// and it renders the stale state distinctly here too — a banner that says
// "HARNESS PAUSED" off a stale read is the exact failure §4a exists to prevent.

export function SystemStateBanner() {
  const ctl = useQuery({
    queryKey: ['control'],
    queryFn: api.getControl,
    refetchInterval: 5000, // Valkey-backed — cheap to poll (§4b)
    refetchIntervalInBackground: false,
    staleTime: 2000,
  });

  const phase =
    ctl.isLoading || ctl.isPending
      ? 'loading'
      : ctl.isError
        ? 'unreachable'
        : ctl.data?.stale
          ? 'stale'
          : 'live';

  const pausedPools = POOLS.filter(
    (p) => ctl.data?.[`${p}_paused` as keyof typeof ctl.data] === true,
  );
  const draining = pausedPools.length > 0;

  if (phase === 'loading') return null;

  return (
    <div
      className={cn(
        'border-b px-4 py-2 text-xs font-medium',
        phase === 'unreachable' &&
          'border-rose-200 bg-rose-50/70 text-rose-700 dark:border-rose-900 dark:bg-rose-950/30 dark:text-rose-400',
        phase === 'stale' &&
          'border-amber-200 bg-amber-50/70 text-amber-700 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-400',
        phase === 'live' &&
          draining &&
          'border-amber-200 bg-amber-50/70 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300',
        phase === 'live' &&
          !draining &&
          'border-emerald-200 bg-emerald-50/40 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/20 dark:text-emerald-400',
      )}
    >
      <div className="mx-auto flex max-w-7xl items-center gap-2">
        {phase === 'live' && draining && (
          <>
            <span className="font-semibold uppercase tracking-wide">
              {pausedPools.map((p, i) => (
                <span key={p}>
                  {i > 0 && ' · '}
                  {POOL_LABELS[p]} PAUSED
                </span>
              ))}
            </span>
            <span className="text-[11px] text-amber-600/80 dark:text-amber-500/80">
              — dispatch gates are closed; nothing new will start
            </span>
          </>
        )}
        {phase === 'live' && !draining && (
          <>
            <span className="font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-400">
              ALL RUNNING
            </span>
            <span className="text-[11px] text-zinc-500 dark:text-zinc-400">
              — no pool is paused
            </span>
          </>
        )}
        {phase === 'stale' && (
          <>
            <span className="font-semibold uppercase tracking-wide">
              CONTROL STATE STALE · FAIL-CLOSED
            </span>
            <span className="text-[11px]">
              — every pool reads paused because we cannot confirm otherwise. Do
              not act on this view.
            </span>
          </>
        )}
        {phase === 'unreachable' && (
          <>
            <span className="font-semibold uppercase tracking-wide">
              CONTROL UNREACHABLE
            </span>
            <span className="text-[11px]">
              — cannot confirm any control state.
            </span>
          </>
        )}
      </div>
    </div>
  );
}
