import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api } from '../lib/api';
import { fmtEpoch, POOL_LABELS } from '../lib/format';
import { cn, POOLS } from '../lib/utils';
import { Freshness } from './Freshness';
import { Card, CardContent, CardHeader, CardTitle } from './ui-primitives';

// Operator control (§4a).  The three state rendering is a correctness rule:
// a *stale* view must never render like a confident PAUSED (control/state.py
// fails closed — a dead Valkey reads as all pools paused), and a fetch error
// is its own third state.  Abort renders "draining" from the report, never
// "aborted" on the 200.

const QKEY = ['control'] as const;

export function ControlPanel({
  runId,
  onRunAborted,
  readyToClose,
  resolveRateDenominator,
  onRunClosed,
  gatewayKeyBlockedBy,
}: {
  runId?: string | null;
  onRunAborted?: (id: string) => void;
  // BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md §1/§3: derived, never
  // stored — computed by queries.get_run from live instance_results every
  // poll, so this stays honest through a concurrent /restart with nothing to
  // keep in sync.
  readyToClose?: boolean;
  resolveRateDenominator?: number;
  onRunClosed?: (id: string) => void;
  // BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §2: null/'global'/
  // 'operator' — read from GET /runs/{id}, same "derived, never stored on
  // the client" discipline as readyToClose above.
  gatewayKeyBlockedBy?: string | null;
}) {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string[]>(['harness']);
  const [reason, setReason] = useState('');
  const [actor, setActor] = useState('operator');
  const [abortScope, setAbortScope] = useState('harness');
  const [abortArmed, setAbortArmed] = useState(false);
  const [closeArmed, setCloseArmed] = useState(false);
  // Pausing this run's gateway key is the SAFE direction (stops spend,
  // reversible) — no confirm needed, same posture as the global Pause button
  // above. Resuming is what makes it spend money again, the same risk class
  // as restart's "launches real inference calls" — that's the one gated.
  const [gatewayResumeArmed, setGatewayResumeArmed] = useState(false);
  // F3 (implementation review, 2026-08-31): the GLOBAL resume button had no
  // confirm at all, and it's the higher-consequence action — it unblocks
  // EVERY active run's key at once, where the per-run one above unblocks
  // one. Before eb19b2a this button was a documented no-op, so nobody gated
  // it; a button that changed meaning needs its guard rails re-examined, not
  // inherited. Only gated when "gateway" is actually among the selected
  // pools — resuming harness/eval alone isn't a money control.
  const [globalResumeArmed, setGlobalResumeArmed] = useState(false);

  const ctl = useQuery({
    queryKey: QKEY,
    queryFn: api.getControl,
    refetchInterval: 5000, // Valkey-backed — cheap to poll (§4b)
    refetchIntervalInBackground: false,
    staleTime: 2000,
  });

  // three-way render: live | stale (fail-closed) | unreachable (fetch error)
  const phase =
    ctl.isLoading || ctl.isPending
      ? 'loading'
      : ctl.isError
        ? 'unreachable'
        : ctl.data?.stale
          ? 'stale'
          : 'live';

  const pause = useMutation({
    mutationFn: () =>
      api.pause(
        selected,
        reason.trim() || 'via dashboard',
        actor.trim() || 'operator',
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QKEY }),
  });
  const resume = useMutation({
    mutationFn: () => api.resume(selected, actor.trim() || 'operator'),
    onSuccess: () => {
      setGlobalResumeArmed(false);
      queryClient.invalidateQueries({ queryKey: QKEY });
    },
  });
  const abort = useMutation({
    mutationFn: () =>
      api.abort(runId ?? '', {
        scope: abortScope,
        reason: reason.trim() || 'operator abort via dashboard',
        actor: actor.trim() || 'operator',
      }),
    onSuccess: (report) => {
      queryClient.invalidateQueries({ queryKey: QKEY });
      if (onRunAborted && report.run_id) onRunAborted(report.run_id);
    },
  });
  const close = useMutation({
    mutationFn: () => api.closeRun(runId ?? ''),
    onSuccess: (report) => {
      queryClient.invalidateQueries({ queryKey: ['run', runId] });
      if (onRunClosed && report.run_id) onRunClosed(report.run_id);
    },
  });
  const pauseGateway = useMutation({
    mutationFn: () => api.pauseGateway(runId ?? '', actor.trim() || 'operator'),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['run', runId] }),
  });
  const resumeGateway = useMutation({
    mutationFn: () =>
      api.resumeGateway(runId ?? '', actor.trim() || 'operator'),
    onSuccess: () => {
      setGatewayResumeArmed(false);
      queryClient.invalidateQueries({ queryKey: ['run', runId] });
    },
  });

  const poolsPaused = useMemo(
    () => ({
      harness: ctl.data?.harness_paused,
      eval: ctl.data?.eval_paused,
      gateway: ctl.data?.gateway_paused,
    }),
    [ctl.data],
  );
  const anyPaused = useMemo(
    () =>
      Boolean(poolsPaused.harness || poolsPaused.eval || poolsPaused.gateway),
    [poolsPaused],
  );

  const toggle = (pool: string) => {
    setSelected((prev) =>
      prev.includes(pool) ? prev.filter((p) => p !== pool) : [...prev, pool],
    );
    setGlobalResumeArmed(false); // selection changed — the prior confirm no longer covers it
  };

  return (
    <Card
      className={cn(
        phase === 'unreachable' && 'border-rose-400 dark:border-rose-600',
        phase === 'stale' && 'border-amber-400 dark:border-amber-600',
      )}
    >
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Operator control
          {phase === 'live' && (
            <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-700 ring-1 ring-inset ring-emerald-600/20 dark:bg-emerald-500/10 dark:text-emerald-400 dark:ring-emerald-500/30">
              live
            </span>
          )}
          {phase === 'stale' && (
            <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700 ring-1 ring-inset ring-amber-600/20 dark:bg-amber-500/10 dark:text-amber-400 dark:ring-amber-500/30">
              stale · fail-closed
            </span>
          )}
          {phase === 'unreachable' && (
            <span className="inline-flex items-center gap-1 rounded-full bg-rose-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-rose-700 ring-1 ring-inset ring-rose-600/20 dark:bg-rose-500/10 dark:text-rose-400 dark:ring-rose-500/30">
              unreachable
            </span>
          )}
        </CardTitle>
        <div className="flex items-center justify-between gap-2 text-xs text-zinc-500 dark:text-zinc-400">
          <span>
            {phase === 'live' && (
              <>
                published{' '}
                {ctl.data?.published_at ? fmtEpoch(ctl.data.published_at) : '—'}
                {anyPaused && ' — something is paused'}
                {ctl.data?.updated_by && (
                  <>
                    {' · by '}
                    <span className="font-medium text-zinc-600 dark:text-zinc-300">
                      {ctl.data.updated_by}
                    </span>
                  </>
                )}
                {ctl.data?.reason && (
                  <>
                    {' · '}
                    <span className="italic">“{ctl.data.reason}”</span>
                  </>
                )}
              </>
            )}
            {phase === 'stale' &&
              'Valkey is unreachable / the read is stale. This is showing every pool paused because we cannot confirm otherwise — an operator must not resume off this view.'}
            {phase === 'unreachable' &&
              'GET /control failed — cannot confirm any control state. Pause/resume below may still work (they write Aurora directly), but the view is not authoritative.'}
          </span>
          <Freshness updatedAt={ctl.dataUpdatedAt} />
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* pool state rows */}
        <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-3">
          {POOLS.map((pool) => {
            const paused = poolsPaused[pool];
            const unknown = phase !== 'live';
            return (
              <div
                key={pool}
                className="flex items-center justify-between rounded-md border border-zinc-200 px-2.5 py-1.5 dark:border-zinc-800"
              >
                <span className="text-xs font-medium">{POOL_LABELS[pool]}</span>
                <span
                  className={cn(
                    'text-[11px] font-semibold',
                    unknown
                      ? 'text-amber-500'
                      : paused
                        ? 'text-rose-500'
                        : 'text-emerald-600',
                  )}
                >
                  {unknown ? 'unknown' : paused ? 'PAUSED' : 'running'}
                </span>
              </div>
            );
          })}
        </div>

        {/* pause/resume */}
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <div className="flex flex-wrap gap-1">
            {POOLS.map((pool) => (
              <button
                key={pool}
                type="button"
                onClick={() => toggle(pool)}
                className={cn(
                  'rounded-md border px-2 py-1 text-xs font-medium ring-1 ring-inset transition-colors',
                  selected.includes(pool)
                    ? 'border-sky-600/40 bg-sky-50 text-sky-700 ring-sky-600/20 dark:bg-sky-500/10 dark:text-sky-400 dark:ring-sky-500/30'
                    : 'border-zinc-200 bg-transparent text-zinc-500 ring-transparent hover:border-zinc-300 dark:border-zinc-800 dark:text-zinc-400',
                )}
              >
                {POOL_LABELS[pool]}
              </button>
            ))}
          </div>
          <input
            aria-label="reason"
            className="w-40 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
            placeholder="reason (audit)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <input
            aria-label="actor"
            className="w-28 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
            placeholder="actor"
            value={actor}
            onChange={(e) => setActor(e.target.value)}
          />
          <button
            type="button"
            disabled={
              selected.length === 0 ||
              pause.isPending ||
              phase === 'unreachable'
            }
            onClick={() => pause.mutate()}
            className="rounded-md bg-zinc-900 px-3 py-1 text-xs font-medium text-zinc-50 hover:bg-zinc-700 disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
          >
            Pause
          </button>
          {selected.includes('gateway') && (
            <label className="flex items-center gap-1 text-[11px] text-zinc-500">
              <input
                type="checkbox"
                checked={globalResumeArmed}
                onChange={(e) => setGlobalResumeArmed(e.target.checked)}
              />
              confirm resume — includes gateway, resumes real spend on every
              active run
            </label>
          )}
          <button
            type="button"
            disabled={
              selected.length === 0 ||
              (selected.includes('gateway') && !globalResumeArmed) ||
              resume.isPending ||
              phase === 'unreachable'
            }
            onClick={() => resume.mutate()}
            className="rounded-md border border-zinc-300 px-3 py-1 text-xs font-medium hover:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:hover:bg-zinc-900"
          >
            Resume
          </button>
          {(pause.isError || resume.isError) && (
            <span className="text-[11px] text-rose-500">mutation failed</span>
          )}
        </div>

        {/* abort — only meaningful with a run selected */}
        {runId && (
          <div className="rounded-md border border-rose-200 bg-rose-50/50 p-3 dark:border-rose-900 dark:bg-rose-950/20">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="text-xs font-semibold uppercase tracking-wide text-rose-500">
                Abort {runId.slice(0, 12)}
              </span>
              <select
                aria-label="abort scope"
                value={abortScope}
                onChange={(e) => {
                  setAbortScope(e.target.value);
                  setAbortArmed(false);
                }}
                className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
              >
                <option value="harness">harness</option>
                <option value="eval">eval</option>
                <option value="all">all</option>
              </select>
              <label className="flex items-center gap-1 text-[11px] text-zinc-500">
                <input
                  type="checkbox"
                  checked={abortArmed}
                  onChange={(e) => setAbortArmed(e.target.checked)}
                />
                confirm {abortScope === 'all' ? 'FULL abort' : 'abort'}
              </label>
              <button
                type="button"
                disabled={!abortArmed || abort.isPending}
                onClick={() => abort.mutate()}
                className="rounded-md bg-rose-600 px-3 py-1 text-xs font-medium text-white hover:bg-rose-500 disabled:opacity-40"
              >
                {abort.isPending ? 'Aborting…' : 'Abort'}
              </button>
            </div>

            {abort.isSuccess && (
              <div className="mt-2 rounded-md bg-white p-2 text-xs dark:bg-zinc-900">
                <div className="flex flex-wrap gap-x-4 gap-y-1 font-medium">
                  <span>
                    status{' '}
                    <span className="font-semibold text-rose-600">
                      {abort.data.status}
                    </span>
                  </span>
                  <span>
                    in-flight stopped{' '}
                    <span className="font-mono">
                      {abort.data.in_flight_stopped}
                    </span>
                  </span>
                  <span>
                    drained{' '}
                    <span className="font-mono">{abort.data.drained}</span>
                  </span>
                  <span>
                    settled{' '}
                    <span className="font-mono">
                      {String(Boolean(abort.data.settled))}
                    </span>
                  </span>
                </div>
                {!abort.data.settled && (
                  <p className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
                    draining — abort is bounded by stopTimeout + upload +
                    results-queue settle. Do not treat this 200 as “done”; keep
                    watching until <em>settled</em>.
                  </p>
                )}
                {abort.data.drain_skipped && (
                  <p className="mt-1 text-[11px] text-zinc-500">
                    drain skipped: {abort.data.drain_skip_reason}
                  </p>
                )}
              </div>
            )}
            {abort.isError && (
              <p className="mt-2 text-xs text-rose-500">
                abort request failed: {(abort.error as Error).message}
              </p>
            )}
          </div>
        )}

        {/* per-run gateway pause/resume (BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-
            DESIGN-2026-08-31.md §5) — blocks/unblocks THIS run's LiteLLM key,
            independent of the global "gateway" pool toggle above. §2's
            precedence table: an explicit action here always wins for this
            run, even while the pool is globally paused or resumed. */}
        {runId && (
          <div className="rounded-md border border-zinc-200 bg-zinc-50/50 p-3 dark:border-zinc-800 dark:bg-zinc-900/40">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                Gateway {runId.slice(0, 12)}
              </span>
              <span
                className={cn(
                  'text-[11px] font-semibold',
                  gatewayKeyBlockedBy ? 'text-rose-500' : 'text-emerald-600',
                )}
              >
                {gatewayKeyBlockedBy
                  ? `blocked (${gatewayKeyBlockedBy})`
                  : 'not blocked'}
              </span>
              <button
                type="button"
                disabled={
                  Boolean(gatewayKeyBlockedBy) || pauseGateway.isPending
                }
                onClick={() => pauseGateway.mutate()}
                className="rounded-md bg-zinc-900 px-3 py-1 text-xs font-medium text-zinc-50 hover:bg-zinc-700 disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
              >
                {pauseGateway.isPending ? 'Pausing…' : 'Pause gateway'}
              </button>
              <label className="flex items-center gap-1 text-[11px] text-zinc-500">
                <input
                  type="checkbox"
                  checked={gatewayResumeArmed}
                  disabled={!gatewayKeyBlockedBy}
                  onChange={(e) => setGatewayResumeArmed(e.target.checked)}
                />
                confirm resume — this run can spend again
              </label>
              <button
                type="button"
                disabled={
                  !gatewayKeyBlockedBy ||
                  !gatewayResumeArmed ||
                  resumeGateway.isPending
                }
                onClick={() => resumeGateway.mutate()}
                className="rounded-md border border-zinc-300 px-3 py-1 text-xs font-medium hover:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:hover:bg-zinc-900"
              >
                {resumeGateway.isPending ? 'Resuming…' : 'Resume gateway'}
              </button>
            </div>
            {gatewayKeyBlockedBy === 'global' && (
              <p className="mt-2 text-[11px] text-zinc-500">
                blocked by the global gateway pause, not a per-run action —
                resuming here carves this run out even while every other run
                stays blocked.
              </p>
            )}
            {(pauseGateway.isError || resumeGateway.isError) && (
              <p className="mt-2 text-xs text-rose-500">
                gateway pause request failed:{' '}
                {((pauseGateway.error ?? resumeGateway.error) as Error).message}
              </p>
            )}
          </div>
        )}

        {/* close — deliberate finalisation (BUILDER4-MANUAL-RESTART-DESIGN-V2-
            2026-08-29.md §1). Revokes both the LiteLLM and OpenRouter keys —
            never automatic, and never reversible from here. */}
        {runId && (
          <div className="rounded-md border border-zinc-200 bg-zinc-50/50 p-3 dark:border-zinc-800 dark:bg-zinc-900/40">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                Close {runId.slice(0, 12)}
              </span>
              <span className="text-[11px] text-zinc-500">
                {readyToClose
                  ? `ready — resolve-rate denominator ${resolveRateDenominator ?? '—'}`
                  : 'not ready — instances still in flight'}
              </span>
              <label className="flex items-center gap-1 text-[11px] text-zinc-500">
                <input
                  type="checkbox"
                  checked={closeArmed}
                  onChange={(e) => setCloseArmed(e.target.checked)}
                  disabled={!readyToClose}
                />
                confirm close — revokes both keys, cannot be undone from here
              </label>
              <button
                type="button"
                disabled={!readyToClose || !closeArmed || close.isPending}
                onClick={() => close.mutate()}
                className="rounded-md bg-zinc-700 px-3 py-1 text-xs font-medium text-white hover:bg-zinc-600 disabled:opacity-40 dark:bg-zinc-600 dark:hover:bg-zinc-500"
              >
                {close.isPending ? 'Closing…' : 'Close run'}
              </button>
            </div>
            {close.isSuccess && (
              <p className="mt-2 text-xs font-medium text-emerald-600 dark:text-emerald-400">
                closed — status {close.data.status}
              </p>
            )}
            {close.isError && (
              <p className="mt-2 text-xs text-rose-500">
                close failed: {(close.error as Error).message}
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
