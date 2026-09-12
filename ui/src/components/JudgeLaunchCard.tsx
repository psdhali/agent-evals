import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type JudgeLiveState } from '../lib/api';
import { noVerdictCount } from '../lib/judge';
import { fmtTime, fmtUsd } from '../lib/format';
import { cn } from '../lib/utils';
import { Markdown } from './Markdown';
import { Card, CardContent, CardHeader, CardTitle } from './ui-primitives';

/**
 * LLM judge launch control (offline-analysis-design.md §9.5/§10.1).
 *
 * Same real-spend trust tier as model-ceiling discovery: arm-then-confirm
 * behind a live cost estimate, never a bare button (ModelCeilings.tsx's
 * pattern, reused here rather than reinvented).
 *
 * `selectedInstanceIds` is owned by RunDetail — the same checkboxes
 * restart/regrade already use, not a second selector. Empty means "judge
 * all eligible", the §9.3 100%-coverage default; a non-empty selection
 * scopes the pass to just those instances.
 *
 * The pass-status banner (§3.2 of the 2026-09-02 review) is the load-
 * bearing part of this component: a pass the budget ceiling truncated must
 * not look like a pass that finished. `total_eligible` being null (a pass
 * from before pass-level reporting existed) renders as "unknown", never 0.
 */

const PRUNE_MODES = ['pruned', 'full', 'auto'] as const;
const WORKERS_MIN = 1;
const WORKERS_MAX = 100;
const WORKERS_DEFAULT = 24;

function clampWorkers(raw: string): number {
  const n = Math.floor(Number(raw));
  if (!Number.isFinite(n) || n < WORKERS_MIN) return WORKERS_DEFAULT;
  return Math.min(WORKERS_MAX, n);
}

function fmtDuration(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return '—';
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return m > 0 ? `${m}m ${r.toString().padStart(2, '0')}s` : `${r}s`;
}

/**
 * Live judge progress (2026-09-07): the pass in progress, from the TTL'd
 * Redis snapshot the pass publishes. A done/failed snapshot is shown as
 * such (never as still running); the persisted pass banner below remains
 * the record once it expires.
 */
function JudgeLiveProgress({ live }: { live: JudgeLiveState }) {
  const done =
    live.judged +
    live.skipped_over_budget +
    live.skipped_artifacts +
    (live.call_failed ?? 0) +
    (live.timed_out ?? 0);
  const pct =
    live.selected > 0 ? Math.min(100, (done / live.selected) * 100) : 0;
  const now = Date.now() / 1000;
  // 2026-09-08: after the last judgment the pass writes its report (one more
  // judge call over every recorded judgment) — still active, not yet done.
  const running = live.status === 'running' || live.status === 'synthesizing';
  const tone =
    live.status === 'failed'
      ? 'text-rose-600 dark:text-rose-400'
      : live.status === 'stopped'
        ? 'text-amber-600 dark:text-amber-400'
        : live.status === 'done'
          ? 'text-emerald-600 dark:text-emerald-400'
          : 'text-zinc-700 dark:text-zinc-200';
  return (
    <div
      className="rounded-md border border-zinc-200 p-2 text-xs dark:border-zinc-800"
      data-testid="judge-live"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className={cn('font-medium', tone)}>
          {live.status === 'running' && 'judge pass running'}
          {live.status === 'synthesizing' &&
            'judge pass writing its report (all judgments recorded)'}
          {live.status === 'done' && 'judge pass finished'}
          {live.status === 'stopped' &&
            'judge pass stopped by the operator (judged rows kept — relaunch resumes)'}
          {live.status === 'failed' && 'judge pass FAILED'}
          {' — '}
          {live.judged} of {live.selected} judged
          {live.in_flight_count > 0 && ` · ${live.in_flight_count} in flight`}
          {live.skipped_over_budget > 0 &&
            ` · ${live.skipped_over_budget} skipped at the budget ceiling`}
          {live.parse_failed > 0 && ` · ${live.parse_failed} parse failures`}
          {live.skipped_artifacts > 0 &&
            ` · ${live.skipped_artifacts} skipped (artifact fetch)`}
          {(live.call_failed ?? 0) > 0 &&
            ` · ${live.call_failed} call failures (not judged — relaunch resumes them)`}
          {(live.timed_out ?? 0) > 0 &&
            ` · ${live.timed_out} timed out at 10 min (recorded, no verdict)`}
          {(live.already_judged ?? 0) > 0 &&
            ` · ${live.already_judged} already judged, skipped`}
        </span>
        <span className="font-mono text-zinc-500">
          {fmtUsd(live.spend_usd)} / {fmtUsd(live.max_spend_usd)} ·{' '}
          {live.workers} workers · {fmtDuration(live.elapsed_s)} elapsed
          {running &&
            live.eta_s != null &&
            ` · ~${fmtDuration(live.eta_s)} left`}
        </span>
      </div>
      <div
        className="mt-1.5 h-1.5 w-full overflow-hidden rounded bg-zinc-100 dark:bg-zinc-800"
        role="progressbar"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="judge pass progress"
      >
        <div
          className={cn(
            'h-full rounded',
            live.status === 'failed' ? 'bg-rose-500' : 'bg-emerald-500',
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
      {live.last_error && (
        <div className="mt-1 text-rose-600 dark:text-rose-400">
          {live.last_error}
        </div>
      )}
      {running && live.in_flight.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[11px] text-zinc-500">
          {live.in_flight.map((f) => (
            <span key={`${f.instance_id}/${f.attempt_number}`}>
              {f.instance_id}/{f.attempt_number}{' '}
              <span className="text-zinc-400">
                {fmtDuration(Math.max(0, now - f.started_at))}
              </span>
            </span>
          ))}
        </div>
      )}
      <div className="mt-1 text-zinc-400">
        pass <span className="font-mono">{live.pass_id}</span> · live snapshot,
        TTL'd — the pass history below is the record
      </div>
    </div>
  );
}

export function JudgeLaunchCard({
  runId,
  terminal,
  selectedInstanceIds,
}: {
  runId: string;
  terminal: boolean;
  selectedInstanceIds: string[];
}) {
  const [pruneMode, setPruneMode] = useState<string>('pruned');
  const [maxSpend, setMaxSpend] = useState('25');
  // 2026-09-07: concurrent judge calls. 24 default; the API clamps to 1..100
  // (the deepseek pool's discovery seeds take 100 comfortably, but a pass's
  // finish time is its slowest calls, and a pool-wide 429 would reset the
  // planner's r_tok for every deepseek run — raise per pass, deliberately).
  const [workers, setWorkers] = useState('24');
  // 2026-09-08: a relaunch RESUMES by default — candidates that already have a
  // judgment are skipped (a failed / truncated pass costs only what is left).
  // Tick to judge everything selected again (new rows; the old ones stay).
  const [rejudge, setRejudge] = useState(false);
  // 2026-09-08 (owner): a judgment that timed out at the 10-min ceiling or failed to
  // parse is a ROW (a resume skips it) — this includes those rows again without a
  // full re-judge, from the card, no table filtering needed.
  const [retryNoVerdict, setRetryNoVerdict] = useState(false);
  const [armed, setArmed] = useState(false);
  const qc = useQueryClient();

  // The pass in progress — judge_sampling is only written when a pass ENDS,
  // so without this a running pass is invisible for its whole duration.
  // Polled while a snapshot says "running"; a done/failed snapshot lingers
  // ~10 min server-side, then the pass history below is the record.
  const live = useQuery({
    queryKey: ['judge-live', runId],
    queryFn: () => api.getJudgeLive(runId),
    enabled: Boolean(runId),
    refetchInterval: (q) =>
      q.state.data?.live?.status === 'running' ||
      q.state.data?.live?.status === 'synthesizing'
        ? 5000
        : 30000,
    refetchIntervalInBackground: false,
  });

  // Same rule restart's confirm gate uses: a stale "yes" must never cover a
  // selection the operator changed after arming.
  useEffect(() => {
    setArmed(false);
  }, [selectedInstanceIds]);

  const passes = useQuery({
    queryKey: ['judge-passes', runId],
    queryFn: () => api.listJudgePasses(runId),
    enabled: Boolean(runId),
    // A pass runs AFTER the run is terminal, so "stop polling when terminal"
    // would leave its banner + report stale until a reload: keep polling
    // while a live snapshot says a pass is active.
    refetchInterval: () =>
      !terminal ||
      live.data?.live?.status === 'running' ||
      live.data?.live?.status === 'synthesizing'
        ? 10000
        : false,
    refetchIntervalInBackground: false,
  });

  // Live estimate only once armed — never fetched, let alone spent, from a
  // bare page load (ModelCeilings.tsx's pattern).
  const estimate = useQuery({
    queryKey: [
      'judge-estimate',
      runId,
      pruneMode,
      selectedInstanceIds,
      rejudge,
      retryNoVerdict,
    ],
    queryFn: () =>
      api.judgeEstimate(
        runId,
        pruneMode,
        selectedInstanceIds,
        rejudge,
        retryNoVerdict,
      ),
    enabled: armed,
    staleTime: 60_000,
  });

  // The judgments that exist — same query key RunDetail's table uses (shared
  // cache). The banner reads these too: a pass that died writes no ledger row,
  // but its judgments are real and must not read as "no judge pass has run".
  const results = useQuery({
    queryKey: ['judge-results', runId],
    queryFn: () => api.getJudgeResults(runId),
    enabled: Boolean(runId),
    staleTime: 10_000,
    refetchInterval: 15_000,
    refetchIntervalInBackground: false,
  });
  const judgedRows = results.data?.results.length ?? 0;
  const noVerdict = noVerdictCount(results.data?.results);

  const launch = useMutation({
    mutationFn: () =>
      api.launchJudge(runId, {
        instance_ids: selectedInstanceIds.length
          ? selectedInstanceIds
          : undefined,
        prune_mode: pruneMode,
        max_spend_usd: Number(maxSpend) || 25,
        workers: clampWorkers(workers),
        rejudge,
        retry_no_verdict: retryNoVerdict,
        triggered_by: 'operator',
      }),
    onSuccess: () => {
      setArmed(false);
      qc.invalidateQueries({ queryKey: ['judge-passes', runId] });
      qc.invalidateQueries({ queryKey: ['judge-live', runId] });
    },
  });

  // 2026-09-08: a "regenerate report" pass judges nothing by design — the
  // banner reads the latest JUDGING pass, the report the latest pass that
  // has one (or a reason it does not).
  const latest =
    passes.data?.passes.find((p) => !p.synthesis_only) ??
    passes.data?.passes[0];
  const latestReport = passes.data?.passes.find(
    (p) => p.synthesis || p.synthesis_error,
  );
  const liveState = live.data?.live ?? null;
  const liveActive =
    liveState?.status === 'running' || liveState?.status === 'synthesizing';

  // Regenerate the pass report only (owner, 2026-09-08): one judge call over
  // every recorded judgment, no re-judging possible. Real spend (cents), so
  // the same arm-then-confirm shape as a launch, in miniature.
  const [regenArmed, setRegenArmed] = useState(false);
  const regenerate = useMutation({
    mutationFn: () =>
      api.launchJudge(runId, {
        prune_mode: pruneMode,
        max_spend_usd: 1,
        workers: 1,
        synthesis_only: true,
        triggered_by: 'operator',
      }),
    onSuccess: () => {
      setRegenArmed(false);
      qc.invalidateQueries({ queryKey: ['judge-passes', runId] });
      qc.invalidateQueries({ queryKey: ['judge-live', runId] });
    },
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>LLM Judge (Pass B)</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {liveState && <JudgeLiveProgress live={liveState} />}
        {/* pass-status banner — §3.2: a truncated pass must not read as a
            finished one. */}
        {passes.isLoading && (
          <p className="text-xs text-zinc-400">loading judge pass history…</p>
        )}
        {passes.isError && (
          <p className="text-xs text-rose-500">
            could not load judge pass history — state unknown, not healthy
          </p>
        )}
        {passes.data && !latest && (
          <p className="text-xs text-zinc-500">
            {judgedRows > 0
              ? `no completed judge pass yet — ${judgedRows} attempt(s) judged by an earlier pass that did not finish (a relaunch resumes from them)`
              : 'no judge pass has run yet for this run.'}
          </p>
        )}
        {noVerdict > 0 && (
          <p
            className="text-xs text-amber-600 dark:text-amber-400"
            data-testid="judge-no-verdict"
          >
            {noVerdict} attempt(s) have a judgment with no verdict (timed out at
            the 10-min ceiling or unparseable) — tick "retry attempts with no
            verdict" below to judge them again, ideally at fewer workers.
          </p>
        )}
        {latest && (
          <div
            className="rounded-md border p-2 text-xs"
            data-testid="judge-pass-banner"
          >
            {latest.total_eligible == null ? (
              <span className="text-zinc-500">
                pass <span className="font-mono">{latest.pass_id}</span> —
                judged {latest.total_judged ?? '—'} (eligible/skipped counts
                unknown — pre-dates pass-level reporting)
              </span>
            ) : (latest.total_skipped_over_budget ?? 0) > 0 ? (
              <span className="font-medium text-amber-600 dark:text-amber-400">
                judged {latest.total_judged ?? 0} of {latest.total_eligible}{' '}
                eligible — {latest.total_skipped_over_budget} skipped at the
                budget ceiling
              </span>
            ) : (
              <span className="font-medium text-emerald-600 dark:text-emerald-400">
                judged {latest.total_judged ?? 0} of {latest.total_eligible}{' '}
                eligible
              </span>
            )}
            {(latest.total_parse_failed ?? 0) > 0 && (
              <span className="ml-1 text-amber-600 dark:text-amber-400">
                · {latest.total_parse_failed} parse failures
              </span>
            )}
            <div className="mt-1 text-zinc-400">
              pass <span className="font-mono">{latest.pass_id}</span> ·{' '}
              {fmtTime(latest.created_at)}
            </div>
          </div>
        )}

        {/* 2026-09-08 (owner): the pass report — the judge model's synthesis
            over EVERY recorded judgment for the run ("two attempts showed
            contamination because …, a recurring environment problem was …").
            Written at the end of the pass; absent with a reason when it was
            skipped or failed (the judgments themselves are unaffected). */}
        {latestReport && (
          <details
            open={Boolean(latestReport.synthesis)}
            className="rounded-md border border-zinc-200 p-2 text-xs dark:border-zinc-800"
            data-testid="judge-pass-report"
          >
            <summary className="cursor-pointer font-medium">
              pass report
              {latestReport.synthesis ? (
                <span className="ml-1 font-normal text-zinc-400">
                  by {latestReport.synthesis_model_resolved ?? 'judge model'}
                  {latestReport.synthesis_cost_usd != null &&
                    ` · ${fmtUsd(latestReport.synthesis_cost_usd)}`}
                  {' · '}
                  {fmtTime(latestReport.created_at)}
                </span>
              ) : (
                <span className="ml-1 font-normal text-amber-600 dark:text-amber-400">
                  not written — {latestReport.synthesis_error}
                </span>
              )}
            </summary>
            {latestReport.synthesis && (
              <Markdown
                source={latestReport.synthesis}
                className="mt-2 text-zinc-700 dark:text-zinc-200"
              />
            )}
          </details>
        )}

        {/* regenerate the report — only meaningful once something is judged;
            never while a pass is active (it would race the pass's own report) */}
        {latest && (latest.total_judged ?? 0) > 0 && (
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {!regenArmed ? (
              <button
                type="button"
                disabled={liveActive || regenerate.isPending}
                onClick={() => setRegenArmed(true)}
                className="rounded-md border border-zinc-200 px-2 py-1 text-xs disabled:opacity-40 dark:border-zinc-700"
                title="one judge call over every recorded judgment for this run — judges nothing"
              >
                regenerate report
              </button>
            ) : (
              <>
                <span className="text-zinc-500">
                  one judge call over all recorded judgments (~$0.02), nothing
                  is re-judged
                </span>
                <button
                  type="button"
                  disabled={regenerate.isPending}
                  onClick={() => regenerate.mutate()}
                  className="rounded-md bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white disabled:opacity-40"
                >
                  confirm regenerate
                </button>
                <button
                  type="button"
                  onClick={() => setRegenArmed(false)}
                  className="rounded-md border border-zinc-200 px-2 py-1 text-xs dark:border-zinc-700"
                >
                  cancel
                </button>
              </>
            )}
            {regenerate.isSuccess && (
              <span className="text-emerald-600 dark:text-emerald-400">
                report pass {regenerate.data.pass_id} started
              </span>
            )}
            {regenerate.isError && (
              <span className="text-rose-500">
                {(regenerate.error as Error).message}
              </span>
            )}
          </div>
        )}

        {/* launch controls */}
        <div className="flex flex-wrap items-center gap-3 border-t border-zinc-100 pt-3 dark:border-zinc-800">
          <span className="text-xs text-zinc-500">
            {selectedInstanceIds.length === 0
              ? 'judging all eligible instances (default)'
              : `judging ${selectedInstanceIds.length} selected instance(s)`}
          </span>

          <label className="flex items-center gap-1.5 text-xs">
            prune mode
            <select
              value={pruneMode}
              onChange={(e) => {
                setPruneMode(e.target.value);
                setArmed(false);
              }}
              className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-700 dark:bg-zinc-900"
            >
              {PRUNE_MODES.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>

          <label className="flex items-center gap-1.5 text-xs">
            max spend ($)
            <input
              type="number"
              min="0"
              step="1"
              value={maxSpend}
              onChange={(e) => {
                setMaxSpend(e.target.value);
                setArmed(false);
              }}
              className="w-20 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-700 dark:bg-zinc-900"
            />
          </label>

          <label
            className="flex items-center gap-1.5 text-xs"
            title="concurrent judge calls (1–100). 24 by default: well inside the deepseek pool's discovered limits; the pass's finish time is its slowest calls, not its throughput"
          >
            workers
            <input
              type="number"
              min={WORKERS_MIN}
              max={WORKERS_MAX}
              step="1"
              value={workers}
              aria-label="judge workers"
              onChange={(e) => {
                setWorkers(e.target.value);
                setArmed(false);
              }}
              className="w-16 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-700 dark:bg-zinc-900"
            />
          </label>

          <label
            className="flex items-center gap-1.5 text-xs"
            title="off (default): a relaunch RESUMES — candidates that already have a judgment are skipped, so a failed or truncated pass costs only what is left. On: judge everything selected again (new rows; the old ones stay)."
          >
            <input
              type="checkbox"
              checked={rejudge}
              aria-label="re-judge already judged"
              onChange={(e) => {
                setRejudge(e.target.checked);
                setArmed(false);
              }}
            />
            re-judge already judged
          </label>
          <label className="flex items-center gap-1.5 text-xs">
            <input
              type="checkbox"
              checked={retryNoVerdict}
              aria-label="retry attempts with no verdict"
              onChange={(e) => {
                setRetryNoVerdict(e.target.checked);
                setArmed(false);
              }}
            />
            retry attempts with no verdict
            {noVerdict > 0 && ` (${noVerdict})`}
          </label>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs">
            <input
              type="checkbox"
              checked={armed}
              aria-label="arm judge pass"
              onChange={(e) => setArmed(e.target.checked)}
            />
            <span>
              {armed && estimate.isLoading && 'estimating…'}
              {armed && estimate.isError && 'could not load estimate'}
              {armed && estimate.data
                ? `confirm ~${fmtUsd(estimate.data.estimated_cost_usd)} for ${estimate.data.candidate_count} candidate(s) — real spend`
                : !armed && 'arm judge pass (real spend)'}
            </span>
          </label>
          <button
            type="button"
            disabled={
              !armed || estimate.isLoading || !estimate.data || launch.isPending
            }
            onClick={() => launch.mutate()}
            className="rounded-md bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900"
          >
            {launch.isPending ? 'starting…' : 'Launch judge pass'}
          </button>
          {launch.isSuccess && (
            <span className="text-xs text-emerald-600 dark:text-emerald-400">
              started — pass{' '}
              <span className="font-mono">{launch.data.pass_id}</span>
            </span>
          )}
          {launch.isError && (
            <span className="text-xs text-rose-500">
              {(launch.error as Error).message}
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
