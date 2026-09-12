import { cn } from '../lib/utils';

// Status → colour mapping for instance states, run statuses and verdicts.
// Terminal-good states are emerald; working states are sky; anything that
// ended badly is amber/rose.  ``null`` (not-yet-known) is neutral zinc.

const TONES = {
  good: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-500/10 dark:text-emerald-400 dark:ring-emerald-500/30',
  warn: 'bg-amber-50 text-amber-700 ring-amber-600/20 dark:bg-amber-500/10 dark:text-amber-400 dark:ring-amber-500/30',
  bad: 'bg-rose-50 text-rose-700 ring-rose-600/20 dark:bg-rose-500/10 dark:text-rose-400 dark:ring-rose-500/30',
  active:
    'bg-sky-50 text-sky-700 ring-sky-600/20 dark:bg-sky-500/10 dark:text-sky-400 dark:ring-sky-500/30',
  neutral:
    'bg-zinc-100 text-zinc-700 ring-zinc-600/20 dark:bg-zinc-800 dark:text-zinc-300 dark:ring-zinc-500/30',
} as const;

type Tone = keyof typeof TONES;

const STATE_TONE: Record<string, Tone> = {
  PENDING: 'neutral',
  QUEUED: 'neutral',
  PROVISIONING: 'active',
  HARNESS_RUNNING: 'active',
  EVAL_RUNNING: 'active',
  PATCH_READY: 'warn',
  RESOLVED: 'good',
  FAILED: 'bad',
  ERROR: 'bad',
  ABORTED: 'bad',
  TIMEOUT: 'bad',
  aborted: 'bad',
  running: 'active',
  pending: 'neutral',
  // GET /runs/{run_id}/live: a missing Redis key mid-run, not a failure — the
  // instance may well be healthy and just slow (BUILDER2-LIVE-RUN-MONITORING-
  // 2026-08-31.md §3). Amber ("uncertain"), never rose ("dead") — a `stale`
  // rendered as failed is how an operator restarts a healthy run and spends
  // real inference money on it.
  stale: 'warn',
};

const VERDICT_TONE: Record<string, Tone> = {
  resolved: 'good',
  unresolved: 'neutral',
  invalid: 'warn',
  error: 'bad',
};

function toneFor(thing: string | null | undefined): Tone {
  if (!thing) return 'neutral';
  return STATE_TONE[thing] ?? VERDICT_TONE[thing] ?? 'neutral';
}

export function StatusBadge({
  label,
  tone,
  className,
}: {
  label: string | null | undefined;
  tone?: Tone;
  className?: string;
}) {
  const t = tone ?? toneFor(label);
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset',
        TONES[t],
        className,
      )}
    >
      {label ?? '—'}
    </span>
  );
}
