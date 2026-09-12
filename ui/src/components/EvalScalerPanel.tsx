import { useQuery } from '@tanstack/react-query';
import { api, type AutoscalerDecision } from '../lib/api';
import { fmtDuration } from '../lib/format';
import { cn } from '../lib/utils';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  SectionLabel,
} from './ui-primitives';

// GET /autoscaler/eval — the eval-side scaler's live decision record (F8 of
// BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04, owner request): what the eval fleet is being
// sized to and why, beside the harness-side PacerPanel. The record is published every tick
// by the run-supervisor's EvalAutoscaler (eval_autoscaler.py): queue depth + not-visible +
// the feed-forward on live harness tasks -> desired tasks -> hosts, the binding constraint,
// the ASG rail, and the scale-in damping countdown (F7: tasks wait ~5 min of lower ticks).
//
// Same honesty rules as PacerPanel's planner verdict: absent (no record / expired) and
// unknown (Redis unreachable) are distinct states, never an empty-looking "0 hosts".
//
// Layout (owner request 2026-09-05): the same grammar as the pacer panel — a status row of
// pills, then labelled figures in two spaced groups (what it wants / what it has) instead of
// one eight-column mono strip.

const HOLDING_BINDINGS = new Set([
  'paused',
  'asg_at_max',
  'no_hosts',
  'scale_in_blocked_busy',
  'at_max_workers',
]);

function Stat({
  label,
  value,
  title,
  tone,
}: {
  label: string;
  value: string;
  title?: string;
  tone?: 'warn' | 'alarm';
}) {
  return (
    <div className="min-w-0" title={title}>
      <div className="text-[11px] font-medium text-zinc-500 dark:text-zinc-400">
        {label}
      </div>
      <div
        className={cn(
          'mt-0.5 text-sm font-semibold tabular-nums text-zinc-900 dark:text-zinc-100',
          tone === 'warn' && 'text-amber-700 dark:text-amber-400',
          tone === 'alarm' && 'text-rose-600 dark:text-rose-400',
        )}
      >
        {value}
      </div>
    </div>
  );
}

function Pill({
  children,
  tone,
  title,
}: {
  children: React.ReactNode;
  tone?: 'warn' | 'ok';
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium',
        tone === 'warn'
          ? 'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300'
          : tone === 'ok'
            ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300'
            : 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300',
      )}
    >
      {children}
    </span>
  );
}

function num(v: unknown): number | null {
  const n = Number(v);
  return v == null || !Number.isFinite(n) ? null : n;
}

function show(v: number | null): string {
  return v == null ? '—' : String(v);
}

export function EvalScalerVerdict({ d }: { d: AutoscalerDecision }) {
  if (d.state !== 'ok' || !d.record) {
    return (
      <div
        className="rounded-lg border border-dashed border-zinc-300 px-4 py-3 text-xs text-zinc-500 dark:border-zinc-700"
        data-testid="eval-scaler-state"
      >
        eval scaler{' '}
        {d.state === 'unknown'
          ? 'unknown — Redis unreachable'
          : 'absent — no decision record (run-supervisor not running, scaler off, or its record expired)'}
      </div>
    );
  }
  const r = d.record as Record<string, unknown>;
  const mode = String(r.mode ?? '?');
  const binding = String(r.binding_constraint ?? '?');
  const tasks = num(r.desired_ceiling);
  const hosts = num(r.desired_hosts);
  const visible = num(r.visible);
  const notVisible = num(r.not_visible);
  const feedforward = num(r.feedforward);
  const runningTasks = num(r.running_tasks);
  const runningHosts = num(r.running_hosts);
  const idleHosts = num(r.idle_hosts);
  const asgDesired = num(r.asg_desired);
  const asgMax = num(r.asg_max);
  const pending = num(r.scale_in_pending_ticks) ?? 0;
  const needed = num(r.scale_in_ticks_needed);
  const tick = num(r.tick_interval_s);
  const wouldSet = (r.would_set ?? {}) as Record<string, unknown>;
  const holding = HOLDING_BINDINGS.has(binding);
  const damped = binding === 'scale_in_damped' || pending > 0;
  // F7 countdown: ticks still to go x the tick cadence. Null when the record predates the
  // fields (an older scaler) — shown as ticks only, never a fabricated number of seconds.
  const remainingS =
    needed != null && tick != null && tick > 0
      ? Math.max(0, (needed - pending) * tick)
      : null;
  const stale = (d.age_s ?? 0) > 90;
  return (
    <div
      className={cn(
        'rounded-lg border px-4 py-3',
        holding
          ? 'border-amber-200 bg-amber-50/50 dark:border-amber-900 dark:bg-amber-950/20'
          : 'border-zinc-200 bg-zinc-50/60 dark:border-zinc-800 dark:bg-zinc-900/40',
      )}
      data-testid="eval-scaler-verdict"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Eval scaler
        </span>
        <Pill tone={mode !== 'live' ? 'warn' : 'ok'} title="the scaler's mode">
          {mode}
          {mode !== 'live' && ' · publishes only, actuates nothing'}
        </Pill>
        <Pill tone={holding ? 'warn' : undefined} title="the binding constraint this tick">
          {binding}
        </Pill>
        {d.age_s != null && (
          <span
            className={cn(
              'ml-auto text-xs tabular-nums',
              stale
                ? 'text-amber-700 dark:text-amber-400'
                : 'text-zinc-400 dark:text-zinc-500',
            )}
          >
            {fmtDuration(d.age_s)} old{stale ? ' · stale' : ''}
          </span>
        )}
      </div>

      <div className="mt-3 grid gap-x-8 gap-y-4 md:grid-cols-2">
        <div>
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
            wants
          </div>
          <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
            <Stat
              label="desired tasks"
              value={show(tasks)}
              title="ceil(visible + not_visible + feed-forward), clamped to [min, max_workers]"
            />
            <Stat
              label="desired hosts"
              value={show(hosts)}
              title="ceil(desired tasks / tasks_per_host)"
            />
            <Stat
              label="queue visible"
              value={show(visible)}
              title="eval-jobs messages waiting"
            />
            <Stat
              label="grading now"
              value={show(notVisible)}
              title="grades in progress (a message is invisible for up to 300 s while a worker holds it)"
            />
          </div>
        </div>
        <div>
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
            has
          </div>
          <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
            <Stat
              label="running tasks"
              value={show(runningTasks)}
              title="the eval-worker service's runningCount"
            />
            <Stat
              label="hosts running / idle"
              value={`${show(runningHosts)} / ${show(idleHosts)}`}
              title="ECS container instances ACTIVE, and how many hold no task (the only scale-in victims)"
            />
            <Stat
              label="ASG desired / max"
              value={`${show(asgDesired)} / ${show(asgMax)}`}
              title="the host ASG's DesiredCapacity and its rail (MaxSize)"
              tone={
                asgDesired != null && asgMax != null && asgDesired >= asgMax
                  ? 'warn'
                  : undefined
              }
            />
            <Stat
              label="feed-forward"
              value={feedforward == null ? '—' : feedforward.toFixed(2)}
              title="beta x live harness tasks — patches expected within the host-boot horizon"
            />
          </div>
        </div>
      </div>

      {(damped || Object.keys(wouldSet).length > 0) && (
        <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 border-t border-zinc-200/70 pt-3 text-xs dark:border-zinc-800">
          {damped && (
            <span
              className="text-amber-700 dark:text-amber-400"
              title="scale-in damping: tasks need consecutive lower ticks before they go (F7: ~5 min); hosts two"
              data-testid="eval-scale-in-countdown"
            >
              <span className="font-medium text-zinc-500 dark:text-zinc-400">
                scale-in pending{' '}
              </span>
              {pending}
              {needed != null && ` / ${needed} ticks`}
              {remainingS != null && ` · ~${fmtDuration(remainingS)} to go`}
            </span>
          )}
          {Object.keys(wouldSet).length > 0 && (
            <span
              className={cn(
                'tabular-nums',
                mode !== 'live' && 'text-zinc-500 dark:text-zinc-400',
              )}
              title={
                mode === 'live'
                  ? 'actuated this tick'
                  : 'observe mode: intended, not applied'
              }
            >
              <span className="font-medium text-zinc-500 dark:text-zinc-400">
                {mode === 'live' ? 'set ' : 'would set '}
              </span>
              {Object.entries(wouldSet)
                .map(([k, v]) => `${k}=${String(v)}`)
                .join(' · ')}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

export function EvalScalerPanel({ terminal }: { terminal: boolean }) {
  const q = useQuery({
    queryKey: ['autoscaler-decision', 'eval'],
    queryFn: () => api.getAutoscalerDecision('eval'),
    refetchInterval: () => (terminal ? false : 5000),
    refetchIntervalInBackground: false,
    staleTime: 3000,
  });
  return (
    <Card>
      <CardHeader className="pb-1">
        <CardTitle>Eval scaler — hosts and grading tasks</CardTitle>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          The eval fleet follows patch arrivals: desired tasks = queue depth +
          grades in progress + a feed-forward on live harness tasks; hosts =
          tasks / tasks-per-host.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {q.isLoading && (
          <div className="text-sm text-zinc-400">
            Loading eval scaler state…
          </div>
        )}
        {q.isError && (
          <div className="text-sm text-rose-500">
            failed to load eval scaler state: {(q.error as Error).message}
          </div>
        )}
        {q.data && <EvalScalerVerdict d={q.data} />}
        <SectionLabel className="pt-1 font-normal normal-case tracking-normal">
          Scale-out is immediate; scale-in waits (tasks ~5 min of lower ticks,
          hosts two) and only ever terminates an idle host.
        </SectionLabel>
      </CardContent>
    </Card>
  );
}
