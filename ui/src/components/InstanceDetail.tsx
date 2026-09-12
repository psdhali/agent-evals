import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api, type InstanceItem } from '../lib/api';
import { fmtDuration, fmtNum, fmtPct, fmtTime, fmtUsd } from '../lib/format';
import { cn } from '../lib/utils';
import { ErrorBoundary } from './ErrorBoundary';
import { Freshness } from './Freshness';
import { JudgeResultPanel } from './JudgeResultPanel';
import { CallsTable } from './CallsTable';
import { LlmLiveView } from './LlmLiveView';
import { StatusBadge } from './StatusBadge';
import { TrajectoryViewer } from './TrajectoryViewer';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  SectionLabel,
} from './ui-primitives';

const ARTIFACT_KINDS = [
  { key: 'patch', label: 'Patch' },
  { key: 'trajectory', label: 'Trajectory' },
  { key: 'log', label: 'Harness log' },
  { key: 'test_output', label: 'Test output' },
  { key: 'run_log', label: 'Run log' },
  { key: 'native_trajectory', label: 'Native trajectory' },
  { key: 'report', label: 'Eval report' },
] as const;

/** SWE-bench harness report shape: {<instance_id>: {resolved, tests_status:
 *  {FAIL_TO_PASS:{success[],failure[]}, PASS_TO_PASS:{...}}}}. */
interface TestBucket {
  success?: string[];
  failure?: string[];
}
interface SwebenchReport {
  resolved?: boolean;
  patch_successfully_applied?: boolean;
  tests_status?: Record<string, TestBucket>;
}

/** Render the eval report structurally instead of a raw JSON dump (BUILDER2
 *  handover issue 5). Falls back to pretty-printed JSON for any shape it does
 *  not recognise — never blank, never a lie about the verdict. */
function EvalReportView({
  raw,
  instanceId,
}: {
  raw: string;
  instanceId: string;
}) {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return <PreDump text={raw} />;
  }

  // The report is keyed by instance_id; some paths hand back the inner object
  // directly. Accept either.
  let report: SwebenchReport | undefined;
  if (parsed && typeof parsed === 'object') {
    const obj = parsed as Record<string, unknown>;
    const inner = obj[instanceId];
    if (inner && typeof inner === 'object') report = inner as SwebenchReport;
    else if ('tests_status' in obj || 'resolved' in obj)
      report = obj as SwebenchReport;
  }
  if (!report) return <PreDump text={pretty(raw)} />;

  const status = report.tests_status ?? {};
  const buckets = Object.entries(status);

  return (
    <div className="space-y-3 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge
          label={report.resolved ? 'RESOLVED' : 'UNRESOLVED'}
          tone={report.resolved ? undefined : 'neutral'}
        />
        {report.patch_successfully_applied === false && (
          <StatusBadge label="patch did not apply" tone="neutral" />
        )}
      </div>
      {buckets.length === 0 && (
        <p className="text-xs text-zinc-400">No tests_status in the report.</p>
      )}
      {buckets.map(([name, b]) => {
        const pass = b.success?.length ?? 0;
        const fail = b.failure?.length ?? 0;
        return (
          <div
            key={name}
            className="rounded-md border border-zinc-200 p-2 dark:border-zinc-800"
          >
            <div className="flex items-center gap-2">
              <SectionLabel>{name}</SectionLabel>
              <span className="font-mono text-xs text-emerald-600 dark:text-emerald-400">
                {pass} pass
              </span>
              <span
                className={cn(
                  'font-mono text-xs',
                  fail > 0
                    ? 'text-rose-600 dark:text-rose-400'
                    : 'text-zinc-400',
                )}
              >
                {fail} fail
              </span>
            </div>
            {fail > 0 && (
              <details className="mt-1">
                <summary className="cursor-pointer text-[11px] text-zinc-500">
                  show {fail} failing
                </summary>
                <ul className="mt-1 space-y-0.5">
                  {b.failure?.map((t) => (
                    <li key={t} className="font-mono text-[11px] text-rose-500">
                      {t}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        );
      })}
      <details>
        <summary className="cursor-pointer text-[11px] text-zinc-400">
          raw report JSON
        </summary>
        <PreDump text={pretty(raw)} />
      </details>
    </div>
  );
}

function PreDump({ text }: { text: string }) {
  return (
    <pre className="max-h-[480px] overflow-auto bg-zinc-50 p-3 font-mono text-[11px] leading-relaxed text-zinc-800 dark:bg-zinc-900 dark:text-zinc-200">
      {text}
    </pre>
  );
}

function pretty(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

export function InstanceDetail({
  runId,
  instanceId,
  attempt,
  onBack,
}: {
  runId: string;
  instanceId: string;
  attempt: number;
  onBack: () => void;
}) {
  const [tab, setTab] =
    useState<(typeof ARTIFACT_KINDS)[number]['key']>('patch');

  const inst = useQuery({
    queryKey: ['instance', runId, instanceId, attempt],
    queryFn: () => api.getInstance(runId, instanceId, attempt),
    refetchInterval: 10000,
    refetchIntervalInBackground: false,
  });

  // harness/model are per-RUN (run_targets), not per-instance — fetch the run
  // once (react-query dedupes with RunDetail's own ['run', runId] query, so
  // arriving from the run screen costs nothing) rather than adding a
  // per-instance column. BUILDER2 handover §B's cost-saving nuance.
  const run = useQuery({
    queryKey: ['run', runId],
    queryFn: () => api.getRun(runId),
    staleTime: 30_000,
  });

  // this instance is still in flight if any of its phase rows is active — gates
  // the live-progress poll (below) and the LlmLiveView refresh.
  const active = (inst.data?.rows ?? []).some((r) =>
    ['PENDING', 'DISPATCHED', 'HARNESS_RUNNING', 'EVAL_RUNNING'].includes(
      r.state,
    ),
  );

  // Per-instance live turn/cost/tokens from Redis (same source as the run-level
  // Live progress panel), filtered to THIS instance — a header for the live
  // section, deliberately NOT tied to any single spend-log call row.
  const live = useQuery({
    queryKey: ['run-live', runId],
    queryFn: () => api.getRunLive(runId),
    refetchInterval: () => (active ? 5000 : false),
    refetchIntervalInBackground: false,
    staleTime: 3000,
    enabled: Boolean(runId),
  });
  const liveItem = (live.data?.items ?? []).find(
    (i) => i.instance_id === instanceId && i.attempt_number === attempt,
  );

  const artifact = useQuery({
    queryKey: ['artifact', runId, instanceId, attempt, tab],
    queryFn: () => api.artifact(runId, instanceId, attempt, tab),
    enabled: Boolean(inst.data?.rows.length),
    staleTime: 60_000,
    retry: false,
  });

  const harnessRow = inst.data?.rows.find((r) => r.phase === 'harness');
  const evalRow = inst.data?.rows.find((r) => r.phase === 'eval');

  // Booleans and lists carry the same NULL-means-unmeasured rule as the
  // numeric fields (ADR-0037/0038) — `null` renders as `—`, not as `false`
  // or an empty list, both of which would silently claim "measured, and
  // clean" for something never checked at all.
  const fmtBool = (b: boolean | null | undefined): string =>
    b === null || b === undefined ? '—' : String(b);
  const fmtList = (xs: string[] | null | undefined): string =>
    xs === null || xs === undefined
      ? '—'
      : xs.length === 0
        ? 'none'
        : xs.join(', ');

  const PhaseRow = ({ row, label }: { row?: InstanceItem; label: string }) => {
    if (!row) return null;
    const isEval = row.phase === 'eval';
    const wallClock = isEval ? row.wall_clock_eval_s : row.wall_clock_harness_s;
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {label} <StatusBadge label={row.state} />{' '}
            <StatusBadge label={row.verdict} />
          </CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {/* Cost, cost source, tokens and turns are HARNESS-phase concepts —
              eval_worker (grading) writes none of them, so they'd render a
              misleading row of "—" on the Eval card. Hidden there entirely
              (BUILDER2 handover issue 5), shown on Harness. */}
          {!isEval && (
            <>
              <div>
                <SectionLabel>cost</SectionLabel>
                <span className="font-mono text-xs">
                  {fmtUsd(row.cost_usd)}
                </span>
              </div>
              <div>
                {/* Which meter produced cost_usd: 'provider' is the gateway's
                    authoritative figure; anything else (local_pricing) is a
                    fallback ESTIMATE and renders amber so it can't pass as
                    metered truth. NULL = no cost recorded at all. */}
                <SectionLabel>cost source</SectionLabel>
                <span
                  className={cn(
                    'font-mono text-xs',
                    row.cost_source !== null &&
                      row.cost_source !== undefined &&
                      row.cost_source !== 'provider' &&
                      'text-amber-600 dark:text-amber-400',
                  )}
                >
                  {row.cost_source ?? '—'}
                </span>
              </div>
              <div>
                <SectionLabel>input tokens</SectionLabel>
                <span className="font-mono text-xs">
                  {fmtNum(row.input_tokens)}
                </span>
              </div>
              <div>
                <SectionLabel>output tokens</SectionLabel>
                <span className="font-mono text-xs">
                  {fmtNum(row.output_tokens)}
                </span>
              </div>
              <div>
                <SectionLabel>turns</SectionLabel>
                <span className="font-mono text-xs">
                  {fmtNum(row.turns_used)}
                </span>
              </div>
            </>
          )}
          <div>
            <SectionLabel>agent time</SectionLabel>
            <span className="font-mono text-xs">
              {fmtDuration(row.agent_s)}
            </span>
          </div>
          <div>
            <SectionLabel>task observed</SectionLabel>
            <span className="font-mono text-xs">
              {fmtDuration(row.task_observed_s)}
            </span>
          </div>
          <div>
            <SectionLabel>task billed</SectionLabel>
            <span className="font-mono text-xs">
              {fmtDuration(row.task_billed_s)}
            </span>
          </div>
          <div>
            <SectionLabel>{row.phase} wall clock</SectionLabel>
            <span className="font-mono text-xs">{fmtDuration(wallClock)}</span>
          </div>
          <div>
            <SectionLabel>repo prep</SectionLabel>
            <span className="font-mono text-xs">
              {fmtDuration(row.repo_prep_s)}
            </span>
          </div>
          {row.phase === 'eval' && (
            <div>
              <SectionLabel>eval test time</SectionLabel>
              <span className="font-mono text-xs">
                {fmtDuration(row.eval_test_s)}
              </span>
            </div>
          )}
          <div>
            <SectionLabel>touches test files</SectionLabel>
            <span className="font-mono text-xs">
              {fmtBool(row.touches_test_files)}
            </span>
          </div>
          <div>
            <SectionLabel>leak detectable</SectionLabel>
            <span className="font-mono text-xs">
              {fmtBool(row.leak_detectable)}
            </span>
          </div>
          <div>
            <SectionLabel>gold patch similarity</SectionLabel>
            <span className="font-mono text-xs">
              {fmtPct(row.gold_patch_similarity)}
            </span>
          </div>
          <div>
            <SectionLabel>grade invalid</SectionLabel>
            <span className="font-mono text-xs">
              {fmtBool(row.grade_invalid)}
            </span>
          </div>
          <div className="col-span-2">
            <SectionLabel>leaked node ids</SectionLabel>
            <span className="font-mono text-xs">
              {fmtList(row.leaked_node_ids)}
            </span>
          </div>
          <div className="col-span-2">
            <SectionLabel>stripped test paths</SectionLabel>
            <span className="font-mono text-xs">
              {fmtList(row.stripped_test_paths)}
            </span>
          </div>

          {/* ADR-0037 timing breakdown — where this phase's wall clock
              actually went (queueing vs pulls vs agent/test work), per row's
              own phase. Same NULL rule as everything above: — means "not
              measured", never zero, and the cache/cold flags render — rather
              than a silently-invented false. */}
          <div className="col-span-2 rounded-md border border-zinc-200 p-2 dark:border-zinc-800 md:col-span-4">
            <SectionLabel>timing breakdown</SectionLabel>
            <div className="mt-1 grid grid-cols-2 gap-3 md:grid-cols-4">
              {(row.phase === 'eval'
                ? ([
                    ['queue wait', fmtDuration(row.eval_queue_wait_s)],
                    ['patch fetch', fmtDuration(row.eval_patch_fetch_s)],
                    ['image pull', fmtDuration(row.eval_image_pull_s)],
                    ['log upload', fmtDuration(row.eval_log_upload_s)],
                    ['image pull cold', fmtBool(row.eval_image_pull_cold)],
                  ] as const)
                : ([
                    ['queue wait', fmtDuration(row.queue_wait_s)],
                    ['provision', fmtDuration(row.provision_s)],
                    ['image pull', fmtDuration(row.image_pull_s)],
                    ['worker boot', fmtDuration(row.worker_boot_s)],
                    ['patch extract', fmtDuration(row.patch_extract_s)],
                    ['artifact upload', fmtDuration(row.artifact_upload_s)],
                    ['repo prep cache hit', fmtBool(row.repo_prep_cache_hit)],
                    ['image pull cold', fmtBool(row.image_pull_cold)],
                    // §2.5: the L1 pacer's rollup for this attempt. The total is the
                    // SUM of every call's admission round trip (~15 ms each even on an
                    // idle pacer — owner question 2026-09-07: "pacer hold 2s" on a
                    // 157-turn attempt that was never queued), so it is shown with the
                    // per-call average; only "calls queued" is an actual hold.
                    [
                      'pacer admission (sum)',
                      row.paced_wait_ms_total == null
                        ? '—'
                        : `${fmtDuration(row.paced_wait_ms_total / 1000)}${
                            row.turns_used
                              ? ` · ~${Math.round(row.paced_wait_ms_total / row.turns_used)} ms/call`
                              : ''
                          }`,
                    ],
                    ['calls queued at pacer', fmtNum(row.paced_calls)],
                    ['pacer timeouts', fmtNum(row.pacer_timeouts)],
                    ['overload retries', fmtNum(row.overload_retries_total)],
                  ] as const)
              ).map(([tLabel, value]) => (
                <div key={tLabel}>
                  <span className="text-[10px] text-zinc-400">{tLabel}</span>
                  <div className="font-mono text-xs">{value}</div>
                </div>
              ))}
            </div>
          </div>

          {/* Adapter-reported tokens/cost block removed (owner, 2026-09-02): the
              adapter figures are unreliable and only invited confusion next to
              the shim's authoritative meter (ADR-0019). The shim's numbers above
              are the truth; there is no second cross-check worth rendering. */}

          {row.error_category && (
            <div className="col-span-2">
              <SectionLabel>error</SectionLabel>
              <span className="text-xs text-rose-500">
                {row.error_category}
              </span>
              {row.error_detail && (
                <p className="mt-0.5 text-[11px] text-zinc-500">
                  {row.error_detail}
                </p>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    );
  };

  return (
    <div className="space-y-4">
      <button
        type="button"
        onClick={onBack}
        className="text-xs font-medium text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
      >
        ← back to run
      </button>

      <Card>
        <CardHeader>
          <CardTitle className="font-mono text-base">
            {instanceId}{' '}
            <span className="text-sm text-zinc-400">· attempt {attempt}</span>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-zinc-500">
          <span>
            run{' '}
            <span className="font-mono text-zinc-600 dark:text-zinc-300">
              {runId.slice(0, 16)}
            </span>
          </span>
          <span>
            harness{' '}
            <span className="font-mono text-zinc-600 dark:text-zinc-300">
              {run.data?.harness ?? '—'}
            </span>
          </span>
          <span title={run.data?.provenance?.model_resolved ?? undefined}>
            model{' '}
            <span className="font-mono text-zinc-600 dark:text-zinc-300">
              {run.data?.model_alias ?? '—'}
            </span>
          </span>
          <span>created {fmtTime(inst.data?.rows[0]?.created_at)}</span>
          <Freshness updatedAt={inst.dataUpdatedAt} />
        </CardContent>
      </Card>

      <PhaseRow row={harnessRow} label="Harness" />
      <PhaseRow row={evalRow} label="Eval" />

      {/* offline-analysis-design.md §9.6/§11 — all 8 rubric dimensions plus
          the approve/deny calibration controls. Placed here, prominently,
          rather than buried after Artifacts: this is where RunDetail's
          contamination-column link lands. */}
      <JudgeResultPanel
        runId={runId}
        instanceId={instanceId}
        attempt={attempt}
      />

      {/* Per-instance live progress (Redis) — turn + cost + tokens for THIS
          instance, a header for the live section. Only while in flight; the
          final numbers live on the phase cards above once it completes. */}
      {liveItem && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle>Live progress — this instance</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-x-6 gap-y-1 font-mono text-xs">
            <span>
              <span className="text-[10px] uppercase text-zinc-400">turn </span>
              {fmtNum(liveItem.turn_number)}
            </span>
            <span>
              <span className="text-[10px] uppercase text-zinc-400">
                cost so far{' '}
              </span>
              {fmtUsd(liveItem.cost_usd)}
            </span>
            <span title={`cached ${fmtNum(liveItem.cached_tokens)}`}>
              <span className="text-[10px] uppercase text-zinc-400">
                input tok{' '}
              </span>
              {fmtNum(liveItem.input_tokens)}
            </span>
            <span>
              <span className="text-[10px] uppercase text-zinc-400">
                output tok{' '}
              </span>
              {fmtNum(liveItem.output_tokens)}
            </span>
            {/* §2.5: the pacer's cumulative footprint on THIS instance, live — total
                hold, calls queued, hold-cap timeouts (each surfaced a 429 to the CLI),
                and what it was last denied on. null = not in the payload → '—'. */}
            {liveItem.paced_wait_ms_total != null && (
              <span
                className={
                  (liveItem.pacer_timeouts ?? 0) > 0
                    ? 'text-rose-600 dark:text-rose-400'
                    : (liveItem.paced_calls ?? 0) > 0 &&
                        liveItem.paced_wait_ms_total >= 5000
                      ? 'text-amber-600 dark:text-amber-400'
                      : undefined
                }
                title={
                  liveItem.pacer_last_deny_axis
                    ? `last denied on the "${liveItem.pacer_last_deny_axis}" axis behind a queue of ${liveItem.pacer_last_queue_len ?? '?'}`
                    : 'never denied at the pacer — the total is the sum of per-call admission round trips (~15 ms each), not time spent queued'
                }
              >
                <span className="text-[10px] uppercase text-zinc-400">
                  pacer admission{' '}
                </span>
                {fmtDuration(liveItem.paced_wait_ms_total / 1000)}
                {liveItem.turn_number
                  ? ` (~${Math.round(liveItem.paced_wait_ms_total / liveItem.turn_number)} ms/call)`
                  : ''}
                <span className="text-zinc-400">
                  {' '}
                  · {liveItem.paced_calls ?? 0} queued ·{' '}
                  {liveItem.pacer_timeouts ?? 0} timeouts ·{' '}
                  {liveItem.overload_retries_total ?? 0} 429 retries
                </span>
              </span>
            )}
            <span className="text-zinc-500">
              {liveItem.age_s === null || liveItem.age_s === undefined
                ? '—'
                : `${fmtDuration(liveItem.age_s)} old`}
            </span>
          </CardContent>
        </Card>
      )}

      {/* The live per-call trajectory, scoped to THIS instance (moved here
          from the run screen — owner feedback, run 1). `terminal` gates the
          10s refresh: once no phase row is active, rows can't change. */}
      <LlmLiveView
        runId={runId}
        instanceId={instanceId}
        attempt={attempt}
        terminal={!active}
      />

      {/* §2.6: the durable per-call wall-clock decomposition from llm_calls — preflight /
          pacer wait / retried round-trips / backoff / final latency, plus the pacer's
          per-call diagnostics. Lands once the attempt's llm_calls.jsonl is ingested. */}
      <CallsTable
        runId={runId}
        instanceId={instanceId}
        attempt={attempt}
        active={active}
      />

      <Card>
        <CardHeader className="flex-row flex-wrap items-center gap-2">
          <CardTitle>Artifacts</CardTitle>
          <div className="flex flex-wrap gap-1">
            {ARTIFACT_KINDS.map((k) => (
              <button
                key={k.key}
                type="button"
                onClick={() => setTab(k.key)}
                className={cn(
                  'rounded-md px-2 py-1 text-xs font-medium',
                  tab === k.key
                    ? 'bg-zinc-900 text-zinc-50 dark:bg-zinc-100 dark:text-zinc-900'
                    : 'text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-900',
                )}
              >
                {k.label}
              </button>
            ))}
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {artifact.isLoading && (
            <div className="p-4 text-sm text-zinc-400">Loading artifact…</div>
          )}
          {artifact.isError && (
            <div className="p-4 text-sm text-amber-600 dark:text-amber-400">
              {(artifact.error as Error).message}
            </div>
          )}
          {/* Each viewer is fenced: a render error inside one (a tool call in
              an unexpected shape, say) shows in place instead of unmounting
              the whole app — see ErrorBoundary. Keyed by tab so switching
              tabs resets a tripped boundary. */}
          {artifact.data !== undefined && tab === 'trajectory' && (
            <ErrorBoundary key="trajectory" label="trajectory viewer">
              <TrajectoryViewer raw={artifact.data} />
            </ErrorBoundary>
          )}
          {artifact.data !== undefined && tab === 'report' && (
            <ErrorBoundary key="report" label="eval report viewer">
              <EvalReportView raw={artifact.data} instanceId={instanceId} />
            </ErrorBoundary>
          )}
          {artifact.data !== undefined &&
            tab !== 'trajectory' &&
            tab !== 'report' && (
              <ErrorBoundary key={tab} label="artifact viewer">
                <PreDump text={artifact.data} />
              </ErrorBoundary>
            )}
        </CardContent>
      </Card>
    </div>
  );
}
