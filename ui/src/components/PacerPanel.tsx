import { useQuery } from '@tanstack/react-query';
import { api, type AutoscalerDecision, type PacerAliasState } from '../lib/api';
import { fmtDuration, fmtNum, fmtTokens } from '../lib/format';
import { cn } from '../lib/utils';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  SectionLabel,
} from './ui-primitives';

// GET /runs/{run_id}/pacer — the live L1 pacer ledger, per alias, straight from the Redis
// keys every harness shim's admission script reads (BUILDER4-PACER-SEED-AND-FAIRNESS
// -2026-09-03.md §2.5). This is the view that did not exist when run 01788405363237319353's
// 130K call sat denied for 10 × 100 s: a waiting call is now a ROW — its size, how long it
// has waited — next to the bucket it is waiting on.
//
// Same house rules as LivePanel: the envelope `state` (ok / unknown) is Redis health and
// never renders as an empty list; a per-alias `measured=false` is "no pacer keys for this
// alias" and every number is null → '—', never a 0 that reads as idle-and-healthy.
//
// Layout (owner request 2026-09-05, "less dense"): one visual grammar for the whole panel —
// a label ABOVE a value in a spaced grid (Stat), status as a pill, the raw mono run-on lines
// gone. Numbers are rounded for reading (tok/s to the unit, big token counts compact, rates
// to 2 dp); the exact figures stay in the tooltip.

function pct(fill: number | null | undefined): string {
  return fill == null ? '—' : `${Math.round(fill * 100)}%`;
}

function whole(n: number | null | undefined): string {
  return n == null ? '—' : fmtNum(Math.round(n));
}

function compact(n: number | null | undefined): string {
  return n == null ? '—' : fmtTokens(Math.round(n));
}

function rate(n: number | null | undefined): string {
  return n == null ? '—' : n.toFixed(2);
}

type Tone = 'warn' | 'alarm' | 'muted' | 'ok';

const TONE_TEXT: Record<Tone, string> = {
  warn: 'text-amber-700 dark:text-amber-400',
  alarm: 'text-rose-600 dark:text-rose-400',
  muted: 'text-zinc-400 dark:text-zinc-500',
  ok: 'text-emerald-700 dark:text-emerald-400',
};

/** A label above a value. The value is one element so tests can match its text. */
function Stat({
  label,
  children,
  tone,
  title,
  className,
}: {
  label: string;
  children: React.ReactNode;
  tone?: Tone;
  title?: string;
  className?: string;
}) {
  return (
    <div className={cn('min-w-0', className)} title={title}>
      <div className="text-[11px] font-medium text-zinc-500 dark:text-zinc-400">
        {label}
      </div>
      <div
        className={cn(
          'mt-0.5 text-sm font-semibold tabular-nums text-zinc-900 dark:text-zinc-100',
          tone && TONE_TEXT[tone],
        )}
      >
        {children}
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
  tone?: Tone;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium',
        tone === 'alarm'
          ? 'bg-rose-100 text-rose-700 dark:bg-rose-950/50 dark:text-rose-300'
          : tone === 'warn'
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

function Bar({
  label,
  fill,
  detail,
  invert = false,
}: {
  label: string;
  fill: number | null | undefined;
  detail: string;
  /** true when HIGH is the pressure signal (in-flight); false when LOW is (buckets). */
  invert?: boolean;
}) {
  const f = fill == null ? null : Math.max(0, Math.min(1, fill));
  const pressure = f == null ? false : invert ? f >= 0.85 : f <= 0.15;
  return (
    <div className="min-w-0">
      <div className="flex items-baseline justify-between">
        <span className="text-[11px] font-medium text-zinc-500 dark:text-zinc-400">
          {label}
        </span>
        <span
          className={cn(
            'text-xs font-semibold tabular-nums',
            pressure
              ? 'text-amber-700 dark:text-amber-400'
              : 'text-zinc-700 dark:text-zinc-300',
          )}
        >
          {pct(f)}
        </span>
      </div>
      <div className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
        {f != null && (
          <div
            className={cn(
              'h-full rounded-full transition-[width]',
              pressure ? 'bg-amber-500' : 'bg-sky-500',
            )}
            style={{ width: `${Math.round(f * 100)}%` }}
          />
        )}
      </div>
      <div className="mt-1 text-xs tabular-nums text-zinc-500 dark:text-zinc-400">
        {detail}
      </div>
    </div>
  );
}

// The planner's per-alias verdict, from the live decision record (record.aliases[alias]).
type AliasVerdict = {
  tasks?: number;
  booting?: number;
  ceiling?: number;
  binding?: string;
  curve_source?: string;
  budgets_source?: string;
};

// Forecast review 2026-09-03: the dispatcher's live verdict — the answer to "why is it
// (not) launching right now". Absent (no planner record / expired) and unknown (Redis
// down) are distinct, and an old record says how old it is.
function PlannerVerdict({ d }: { d: AutoscalerDecision }) {
  if (d.state !== 'ok' || !d.record) {
    return (
      <div className="rounded-lg border border-dashed border-zinc-300 px-4 py-3 text-xs text-zinc-500 dark:border-zinc-700">
        planner verdict{' '}
        {d.state === 'unknown'
          ? 'unknown — Redis unreachable'
          : 'absent — no decision record (dispatcher not running, or its record expired)'}
      </div>
    );
  }
  const r = d.record as Record<string, unknown>;
  const mode = String(r.mode ?? '?');
  const ceiling = Number(r.desired_ceiling ?? NaN);
  const inFlight = Number(r.in_flight_tasks ?? NaN);
  const binding = String(r.binding_constraint ?? '?');
  const bindingAlias = String(r.binding_alias ?? '');
  const booting = Number(r.booting_tasks ?? 0);
  const timeouts = Number(r.paced_timeouts ?? 0);
  const queue = Number(r.queue_len ?? 0);
  const headWait = Number(r.queue_head_wait_s ?? 0);
  // Scaling review 2026-09-03 (F5/F6/F2): the hard cap in force, and the two budget events
  // a tick can carry — growth refused by the seed-relative clamp, or an overload RECOVERY
  // that lowered r_tok/r_qps.
  const staticCap = Number(r.static_cap ?? NaN);
  const growthClamped = Boolean(r.growth_clamped);
  const recovery = (r.recovery_set ?? {}) as Record<string, unknown>;
  const recovered = Object.keys(recovery).length > 0;
  const holding =
    binding === 'cooldown' ||
    binding === 'paced' ||
    (Number.isFinite(ceiling) &&
      Number.isFinite(inFlight) &&
      inFlight >= ceiling);
  const stale = (d.age_s ?? 0) > 60;
  return (
    <div
      className={cn(
        'rounded-lg border px-4 py-3',
        holding
          ? 'border-amber-200 bg-amber-50/50 dark:border-amber-900 dark:bg-amber-950/20'
          : 'border-zinc-200 bg-zinc-50/60 dark:border-zinc-800 dark:bg-zinc-900/40',
      )}
      data-testid="planner-verdict"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Planner
        </span>
        <Pill tone={mode !== 'live' ? 'warn' : 'ok'} title="the L2 planner's mode">
          {mode}
          {mode !== 'live' && ' · publishes only, never blocks'}
        </Pill>
        <Pill tone={holding ? 'warn' : 'ok'}>
          {holding ? 'HOLDING' : 'launching'} — {binding}
          {bindingAlias && ` (${bindingAlias})`}
        </Pill>
        {growthClamped && (
          <Pill
            tone="warn"
            title="+5% growth refused: r_tok would exceed 1.5x the discovered seed"
          >
            growth clamped
          </Pill>
        )}
        {recovered && (
          <Pill
            tone="alarm"
            title={`overload recovery lowered budgets: ${JSON.stringify(recovery)}`}
          >
            recovery ↓ {Object.keys(recovery).join(', ')}
          </Pill>
        )}
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
      <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
        <Stat
          label="in flight / ceiling"
          title="ECS in-flight harness tasks vs the planner's desired ceiling; cap = static env cap min per-run cap (min-wins)"
        >
          {Number.isFinite(inFlight) ? inFlight : '—'} /{' '}
          {Number.isFinite(ceiling) ? ceiling : '—'}
          {Number.isFinite(staticCap) && (
            <span className="ml-1.5 text-xs font-normal text-zinc-400">
              cap {staticCap}
            </span>
          )}
        </Stat>
        <Stat
          label="booting"
          title="tasks ECS counts that have not written progress yet — modelled as fresh"
        >
          {booting}
        </Stat>
        <Stat
          label="wait queue"
          tone={queue > 0 ? 'warn' : undefined}
          title="calls denied admission fleet-wide right now"
        >
          {queue}
          {queue > 0 && (
            <span className="ml-1.5 text-xs font-normal text-zinc-500">
              head {fmtDuration(headWait)}
            </span>
          )}
        </Stat>
        <Stat
          label="hold-cap timeouts / 60 s"
          tone={timeouts > 0 ? 'alarm' : undefined}
        >
          {timeouts}
        </Stat>
      </div>
    </div>
  );
}

function AliasCard({
  a,
  verdict,
}: {
  a: PacerAliasState;
  verdict?: AliasVerdict;
}) {
  const queueLen = a.queue_len ?? 0;
  const overloads = a.overloads_60s ?? 0;
  const headWait = a.head_waiting_s ?? 0;
  const alarm = overloads > 0 || headWait >= 30;
  const warn = !alarm && (queueLen > 0 || (a.over_2s_60s ?? 0) > 0);
  return (
    <div
      className={cn(
        'rounded-lg border p-4',
        alarm
          ? 'border-rose-200 bg-rose-50/40 dark:border-rose-900 dark:bg-rose-950/20'
          : warn
            ? 'border-amber-200 bg-amber-50/40 dark:border-amber-900 dark:bg-amber-950/20'
            : 'border-zinc-200 dark:border-zinc-800',
      )}
    >
      {/* header: alias + harness, status at the right */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          {a.alias}
        </span>
        <Pill>{a.harness}</Pill>
        {a.measured && a.seeded_at == null && (
          <Pill tone="warn" title="pacer:cfg carries no discovery seed — defaults in force">
            unseeded · defaults
          </Pill>
        )}
        {a.measured ? (
          <Pill tone={alarm ? 'alarm' : warn ? 'warn' : 'ok'}>
            {alarm
              ? overloads > 0
                ? 'provider pushing back'
                : 'head waiting 30 s+'
              : warn
                ? 'queueing'
                : 'flowing'}
          </Pill>
        ) : (
          <span className="text-xs italic text-zinc-400">
            not measured — no pacer keys for this alias (no traffic yet, or
            expired)
          </span>
        )}
      </div>

      {a.measured && (
        // The cfg in force — every admission is judged against these constants (discovery
        // seeds, grown by the planner). One text per cell so the exact-value tooltip and the
        // test's `r_tok 50,000/s` both hold.
        <div
          className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-zinc-500 dark:text-zinc-400"
          title="the pacer:cfg constants every admission is judged against (discovery seeds, grown by the planner)"
        >
          <span className="tabular-nums" title={`r_tok = ${a.r_tok ?? '—'} tok/s`}>
            r_tok {whole(a.r_tok)}/s
          </span>
          <span className="tabular-nums" title={`k_inflight = ${a.k_inflight ?? '—'} tok`}>
            k_inflight {compact(a.k_inflight)}
          </span>
          <span className="tabular-nums" title={`c_burst = ${a.c_burst ?? '—'} tok`}>
            c_burst {compact(a.c_burst)}
          </span>
          <span className="tabular-nums" title={`r_qps = ${a.r_qps ?? '—'} req/s`}>
            r_qps {rate(a.r_qps)}
          </span>
        </div>
      )}

      {verdict && (
        <div
          className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-zinc-500 dark:text-zinc-400"
          title="the L2 planner's verdict for this alias (live decision record)"
        >
          <span className="tabular-nums">
            planner {verdict.tasks ?? '—'} tasks
            {(verdict.booting ?? 0) > 0 && ` +${verdict.booting} booting`}
          </span>
          <span className="tabular-nums">ceiling {verdict.ceiling ?? '—'}</span>
          <span
            className={cn(
              verdict.binding &&
                verdict.binding !== 'none' &&
                'font-medium text-amber-700 dark:text-amber-400',
            )}
          >
            {verdict.binding ?? '—'}
          </span>
          <span>curve {verdict.curve_source ?? '—'}</span>
          <span
            className={cn(
              verdict.budgets_source === 'defaults' &&
                'font-medium text-rose-600 dark:text-rose-400',
              verdict.budgets_source === 'pacer_cfg_stale' &&
                'font-medium text-amber-700 dark:text-amber-400',
            )}
          >
            budgets {verdict.budgets_source ?? '—'}
          </span>
        </div>
      )}

      {a.measured && (
        <>
          <div className="mt-4 grid grid-cols-1 gap-x-8 gap-y-4 sm:grid-cols-3">
            <Bar
              label="token bucket"
              fill={a.bucket_fill}
              detail={`${compact(a.bucket_level)} of ${compact(a.c_burst)} tok`}
            />
            <Bar
              label="request bucket"
              fill={a.req_fill}
              detail={`${a.req_level == null ? '—' : Math.round(a.req_level)} of ${a.c_req ?? '—'} req`}
            />
            <Bar
              label="in flight"
              fill={a.inflight_fill}
              invert
              detail={`${whole(a.inflight_calls)} calls · ${compact(a.inflight_tokens)} of ${compact(a.k_inflight)} tok`}
            />
          </div>

          <div className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 border-t border-zinc-200/70 pt-3 sm:grid-cols-5 dark:border-zinc-800">
            <Stat
              label="wait queue"
              tone={
                headWait >= 30 ? 'alarm' : queueLen > 0 ? 'warn' : undefined
              }
              title="calls currently DENIED admission, head of line first — the head's need is reserved on every axis"
            >
              {a.queue_len ?? '—'}
            </Stat>
            <Stat label="admits / 60 s" title="admissions in the last 60s">
              {whole(a.admits_60s)}
            </Stat>
            <Stat
              label="waits>2s / 60 s"
              tone={(a.over_2s_60s ?? 0) > 0 ? 'warn' : undefined}
              title="admissions that waited > 2s in the last 60s — the design's p95 back-pressure proxy"
            >
              {whole(a.over_2s_60s)}
            </Stat>
            <Stat label="mean wait" title="mean admission wait over the last 60s">
              {a.mean_wait_ms_60s == null
                ? '—'
                : `${Math.round(a.mean_wait_ms_60s)} ms`}
            </Stat>
            <Stat
              label="provider 429s / 60 s"
              tone={overloads > 0 ? 'alarm' : undefined}
              title="REAL provider 429s (no retry-after) observed in the last 60s — the pool itself pushing back, not our pacer"
            >
              {whole(a.overloads_60s)}
            </Stat>
          </div>

          {queueLen > 0 && (
            <div className="mt-3 text-xs text-zinc-600 dark:text-zinc-300">
              head {fmtNum(a.head_est_tokens)} tok waiting{' '}
              {fmtDuration(a.head_waiting_s ?? null)}
            </div>
          )}

          {a.waiters.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {a.waiters.slice(0, 12).map((w, i) => (
                <span
                  key={i}
                  className={cn(
                    'rounded-md border px-2 py-0.5 text-xs tabular-nums',
                    i === 0
                      ? 'border-amber-300 bg-amber-100 text-amber-800 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-300'
                      : 'border-zinc-200 text-zinc-600 dark:border-zinc-700 dark:text-zinc-300',
                  )}
                  title={
                    i === 0
                      ? 'head of line — its need is reserved on every axis'
                      : undefined
                  }
                >
                  {fmtNum(w.est_tokens)} tok · {fmtDuration(w.waiting_s)}
                </span>
              ))}
              {a.waiters.length > 12 && (
                <span className="text-xs text-zinc-400">
                  +{a.waiters.length - 12} more
                </span>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function PacerPanel({
  runId,
  terminal,
}: {
  runId: string;
  terminal: boolean;
}) {
  const q = useQuery({
    queryKey: ['run-pacer', runId],
    queryFn: () => api.getRunPacer(runId),
    refetchInterval: () => (terminal ? false : 5000),
    refetchIntervalInBackground: false,
    staleTime: 3000,
    enabled: Boolean(runId),
  });
  const data = q.data;
  const items = data?.items ?? [];
  const planner = useQuery({
    queryKey: ['autoscaler-decision', 'harness'],
    queryFn: () => api.getAutoscalerDecision('harness'),
    refetchInterval: () => (terminal ? false : 5000),
    refetchIntervalInBackground: false,
    staleTime: 3000,
  });
  const verdicts = (
    planner.data?.record as
      { aliases?: Record<string, AliasVerdict> } | undefined
  )?.aliases;

  return (
    <Card>
      <CardHeader className="pb-1">
        <CardTitle>Pacer — live admission ledger</CardTitle>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Every harness call passes this ledger before its bytes go upstream.
          A wait queue is pressure from our pacer; provider 429s are the pool
          itself.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {planner.data && <PlannerVerdict d={planner.data} />}
        {q.isLoading && (
          <div className="text-sm text-zinc-400">Loading pacer state…</div>
        )}
        {q.isError && (
          <div className="text-sm text-rose-500">
            failed to load pacer state: {(q.error as Error).message}
          </div>
        )}
        {data && data.state === 'unknown' && (
          <div className="rounded-lg border border-amber-200 bg-amber-50/50 p-3 text-xs text-amber-700 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-400">
            Pacer state unknown ({data.reason || 'reason not given'}) — Redis is
            unreachable right now. This says nothing about pressure on the pool;
            it means this panel can't see the ledger.
          </div>
        )}
        {data && data.state !== 'unknown' && items.length === 0 && (
          <div className="text-sm text-zinc-400">
            No pacer aliases for this run (no run targets).
          </div>
        )}
        {data && data.state !== 'unknown' && items.length > 0 && (
          <div className="space-y-3">
            {items.map((a) => (
              <AliasCard
                key={`${a.harness}-${a.alias}`}
                a={a}
                verdict={verdicts?.[a.alias]}
              />
            ))}
          </div>
        )}
        <SectionLabel className="pt-1 font-normal normal-case tracking-normal">
          Bucket levels are extrapolated to now with the cfg refill rate.
          Exact values are in each figure's tooltip.
        </SectionLabel>
      </CardContent>
    </Card>
  );
}
