import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState, type ReactNode } from 'react';
import { api, type JudgeResultItem } from '../lib/api';
import { IS_DEMO } from '../lib/dataMode';
import {
  fmtDuration,
  fmtNum,
  fmtPct,
  fmtTime,
  fmtTokens,
  fmtUsd,
  shortSha,
} from '../lib/format';
import {
  JUDGE_ISSUE_OPTIONS,
  contaminationStatus,
  findJudgeResult,
  judgeIssues,
  matchesJudgeIssue,
  type JudgeIssueKind,
} from '../lib/judge';
import {
  COLLAPSE_ABOVE_ROWS,
  narrowRows,
  repoOptions,
} from '../lib/instanceTable';
import { deriveRates } from '../lib/rates';
import { cn } from '../lib/utils';
import { ControlPanel } from './ControlPanel';
import { EvalScalerPanel } from './EvalScalerPanel';
import { Freshness } from './Freshness';
import { JudgeLaunchCard } from './JudgeLaunchCard';
import { LivePanel } from './LivePanel';
import { LimitsPanel } from './LimitsPanel';
import { PacerPanel } from './PacerPanel';
import { QueuePanels } from './QueuePanels';
import { RunCost } from './RunCost';
import { RunProgressPanel } from './RunProgressPanel';
import { StatusBadge } from './StatusBadge';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  SectionLabel,
} from './ui-primitives';

function KV({ k, v, mono }: { k: string; v: ReactNode; mono?: boolean }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
        {k}
      </span>
      <span
        className={cn(
          'text-xs text-zinc-700 dark:text-zinc-200',
          mono && 'font-mono',
        )}
      >
        {v}
      </span>
    </div>
  );
}

// offline-analysis-design.md §9.6 / review 2026-09-02 follow-up: the judge's
// contamination verdict, surfaced in the instances list — click-through to
// the full per-dimension detail inside InstanceDetail's Judge panel rather
// than a value with nowhere to go. "unjudged" renders as a dash, never a
// silent "no" — see lib/judge.ts's contaminationStatus for the four states.
function ContaminationCell({
  result,
  onOpen,
}: {
  result: JudgeResultItem | undefined;
  onOpen: () => void;
}) {
  const status = contaminationStatus(result);
  if (status === 'unjudged') {
    return <span className="text-xs text-zinc-400">—</span>;
  }
  const tone =
    status === 'yes'
      ? 'bad'
      : status === 'flagged_no_evidence'
        ? 'warn'
        : 'good';
  const text =
    status === 'yes'
      ? 'Yes'
      : status === 'flagged_no_evidence'
        ? 'Flagged'
        : 'No';
  return (
    <button
      type="button"
      onClick={onOpen}
      // The visible StatusBadge text ("Yes"/"No"/"Flagged") would otherwise
      // become the accessible name and shadow this — an explicit
      // aria-label keeps "open judge result" queryable regardless.
      aria-label="open judge result for this attempt"
      title="open judge result for this attempt"
    >
      <StatusBadge label={text} tone={tone} />
    </button>
  );
}

// 2026-09-08 (owner): the rubric findings the judge raised on this attempt,
// as short chips — the row-level answer to "which of the 500 had issues,
// and which kind" without opening each Judge panel. Thresholds live in
// lib/judge.ts's judgeIssues (one per rubric dimension). A judged attempt
// with nothing raised renders "clean"; unjudged renders a dash, never
// "clean".
function JudgeFindingsCell({
  result,
  onOpen,
}: {
  result: JudgeResultItem | undefined;
  onOpen: () => void;
}) {
  if (!result) return <span className="text-xs text-zinc-400">—</span>;
  const issues = judgeIssues(result);
  if (issues.length === 0) {
    return (
      <span className="text-[11px] text-emerald-600 dark:text-emerald-400">
        clean
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label="open judge findings for this attempt"
      title={issues.map((i) => i.chip).join(', ')}
      className="flex flex-wrap gap-1"
    >
      {issues.map((i) => (
        <StatusBadge
          key={i.kind}
          label={i.chip}
          tone={
            i.kind === 'contamination' || i.kind === 'test_gaming'
              ? 'bad'
              : i.kind === 'parse_failed' ||
                  i.kind === 'no_evidence' ||
                  i.kind === 'timeout'
                ? 'neutral'
                : 'warn'
          }
        />
      ))}
    </button>
  );
}

export function RunDetail({
  runId,
  onBack,
  onOpenInstance,
}: {
  runId: string;
  onBack: () => void;
  onOpenInstance: (instanceId: string, attempt: number) => void;
}) {
  const [stateFilter, setStateFilter] = useState('');
  const [errFilter, setErrFilter] = useState('');
  // Client-side narrowing of the (up to ~1,000-row) table — lib/instanceTable.ts.
  const [search, setSearch] = useState('');
  const [repoFilter, setRepoFilter] = useState('');
  const [phaseFilter, setPhaseFilter] = useState('');
  // 2026-09-08 (owner, first live judge pass): narrow the table to the rows
  // the LLM judge has / has not scored yet, so a running pass can be checked
  // row by row. Client-side — a judge result is keyed (instance, attempt),
  // the same lookup the Contamination column uses.
  const [judgedFilter, setJudgedFilter] = useState<'' | 'judged' | 'unjudged'>(
    '',
  );
  // 2026-09-08 (owner): narrow to the judged rows with a specific rubric
  // finding ("contamination: yes", "environment / tool problem", …), any
  // finding, or none — lib/judge.ts's judgeIssues decides per dimension.
  const [issueFilter, setIssueFilter] = useState<
    '' | JudgeIssueKind | 'any' | 'none'
  >('');
  // null = "not decided by the operator yet": collapsed iff the table is big.
  const [tableOpen, setTableOpen] = useState<boolean | null>(null);
  const [selectedInstances, setSelectedInstances] = useState<Set<string>>(
    new Set(),
  );
  const [restartActor, setRestartActor] = useState('operator');
  // Implementation review, 2026-08-31 (§E): restart is the only surface in
  // this system where a mis-click spends real inference money — needs a
  // confirm step the button can't be bypassed without, same trust level as
  // abort/close. Reset whenever the selection changes so "yes" from a
  // moment ago can never silently apply to a different, larger selection.
  const [restartArmed, setRestartArmed] = useState(false);
  const queryClient = useQueryClient();

  // BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md §2.2: allowed any time
  // the run isn't closed — there is no separate "ready to review" state to
  // gate this on, unlike close above. Only used client-side to disable
  // checkboxes for rows that are obviously still in flight; the backend is
  // the actual authority and reports a per-instance skip reason regardless.
  const activeStates = new Set([
    'PENDING',
    'DISPATCHED',
    'HARNESS_RUNNING',
    'EVAL_RUNNING',
  ]);

  const run = useQuery({
    queryKey: ['run', runId],
    queryFn: () => api.getRun(runId),
    refetchInterval: (q) => (q.state.data?.terminal ? false : 5000), // stop when terminal (§4b)
    refetchIntervalInBackground: false,
    staleTime: 3000,
  });

  const instances = useQuery({
    queryKey: [
      'instances',
      runId,
      {
        state: stateFilter || undefined,
        error_category: errFilter || undefined,
      },
    ],
    // Fetch EVERY row, not the API's default page of 50 (2026-09-05: a 41-run
    // showed 25 instances — two rows per instance, harness + eval — and the
    // turn/token totals below were summed over that first page only). The API
    // caps a page at 500, so walk offsets until `total` is covered.
    queryFn: async () => {
      const filters = {
        state: stateFilter || undefined,
        error_category: errFilter || undefined,
      };
      const pageSize = 500;
      const first = await api.listInstances(runId, {
        ...filters,
        limit: pageSize,
        offset: 0,
      });
      const items = [...first.items];
      let guard = 0;
      while (items.length < first.total && guard++ < 200) {
        const page = await api.listInstances(runId, {
          ...filters,
          limit: pageSize,
          offset: items.length,
        });
        if (page.items.length === 0) break;
        items.push(...page.items);
      }
      // The pages are read seconds apart while the run is live, so a row can
      // move across the page boundary between reads and arrive twice (seen
      // 2026-09-06 as React's duplicate-key warning on the instance table).
      // Keep the LAST copy of each (instance, attempt, phase): it is the newer
      // read.
      const byRow = new Map<string, (typeof items)[number]>();
      for (const i of items)
        byRow.set(`${i.instance_id}-${i.attempt_number}-${i.phase}`, i);
      const unique = Array.from(byRow.values());
      return { ...first, items: unique, limit: unique.length, offset: 0 };
    },
    refetchInterval: () => (run.data?.terminal ? false : 5000),
    refetchIntervalInBackground: false,
    enabled: Boolean(runId),
    staleTime: 3000,
  });

  // Same query key JudgeResultPanel/InstanceDetail use — shared cache, so
  // opening an instance from this table's contamination column doesn't
  // re-fetch results the list view already has.
  const judgeResults = useQuery({
    queryKey: ['judge-results', runId],
    queryFn: () => api.getJudgeResults(runId),
    enabled: Boolean(runId),
    staleTime: 10_000,
    // A judge pass runs AFTER the run is terminal, so the run's own
    // "stop polling when terminal" rule cannot gate this: keep the results
    // fresh while the page is open so newly judged rows show up (and the
    // judged/unjudged filter below tracks a live pass) without a reload.
    refetchInterval: 15_000,
    refetchIntervalInBackground: false,
  });

  const prov = run.data?.provenance;
  // Aggregates the flat run_summary blob doesn't carry (turns + tokens) —
  // summed from the harness-phase instance rows we already fetched, so the
  // Run Detail turn/token totals need no extra endpoint.
  const agg = useMemo(() => {
    const harnessRows = (instances.data?.items ?? []).filter(
      (i) => i.phase === 'harness',
    );
    const sumOf = (
      f: (i: (typeof harnessRows)[number]) => number | null | undefined,
    ) => harnessRows.reduce((a, i) => a + (f(i) ?? 0), 0);
    return {
      turns: sumOf((i) => i.turns_used),
      inputTokens: sumOf((i) => i.input_tokens),
      outputTokens: sumOf((i) => i.output_tokens),
    };
  }, [instances.data]);
  const errCats = useMemo(() => {
    const s = new Set(
      (instances.data?.items ?? [])
        .map((i) => i.error_category)
        .filter((x): x is string => Boolean(x)),
    );
    return Array.from(s);
  }, [instances.data]);

  // The table's rows after the client-side narrowing; a big table starts
  // collapsed unless the operator narrowed it or opened it explicitly.
  const visibleRows = useMemo(() => {
    const rows = narrowRows(instances.data?.items ?? [], {
      search,
      repo: repoFilter,
      phase: phaseFilter,
    });
    if (!judgedFilter && !issueFilter) return rows;
    const results = judgeResults.data?.results;
    return rows.filter((r) => {
      const result = findJudgeResult(results, r.instance_id, r.attempt_number);
      if (judgedFilter) {
        const judged = Boolean(result);
        if (judgedFilter === 'judged' ? !judged : judged) return false;
      }
      if (issueFilter && !matchesJudgeIssue(result, issueFilter)) return false;
      return true;
    });
  }, [
    instances.data,
    search,
    repoFilter,
    phaseFilter,
    judgedFilter,
    issueFilter,
    judgeResults.data,
  ]);
  const judgedCount = judgeResults.data?.results.length ?? 0;
  // Per-option counts over the judged ATTEMPTS (a result is per attempt, the
  // table shows one row per phase) so the dropdown reads "contamination: yes
  // (2)" before the operator picks it.
  const issueCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const opt of JUDGE_ISSUE_OPTIONS) counts.set(opt.value, 0);
    for (const r of judgeResults.data?.results ?? []) {
      for (const opt of JUDGE_ISSUE_OPTIONS) {
        if (matchesJudgeIssue(r, opt.value)) {
          counts.set(opt.value, (counts.get(opt.value) ?? 0) + 1);
        }
      }
    }
    return counts;
  }, [judgeResults.data]);
  const narrowed = Boolean(
    search.trim() || repoFilter || phaseFilter || judgedFilter || issueFilter,
  );
  // The public Explorer (demo mode) starts every table open: a visitor is here to see what
  // the rows look like, and the filters above are the way to narrow, not a closed table.
  const tableShown =
    tableOpen ??
    (IS_DEMO ||
      narrowed ||
      (instances.data?.items.length ?? 0) <= COLLAPSE_ABOVE_ROWS);

  const states = run.data?.states ?? [];
  // Per INSTANCE (latest attempt; eval row over harness row) — what the operator
  // means by "how many are where". The per-phase rows below are the detail.
  const instanceStates = run.data?.instance_states ?? [];
  const rates = useMemo(
    () =>
      deriveRates(
        run.data?.instance_states,
        run.data?.resolve_rate_denominator,
      ),
    [run.data],
  );
  // The top-card state buckets GROUP BY state over ALL instance_results rows, so
  // a mid-eval instance shows in two buckets (its harness row + its eval row) and
  // a bare "PENDING" is ambiguous — harness-pending or eval-pending? Re-derive the
  // buckets from the instance rows (already fetched) WITH their phase, so each
  // chip says which phase it belongs to. Falls back to the unlabeled backend
  // buckets until the instance list has loaded.
  const phaseStates = useMemo(() => {
    const its = instances.data?.items ?? [];
    if (its.length === 0) return null;
    const m = new Map<
      string,
      { phase: string; state: string; count: number }
    >();
    for (const i of its) {
      const key = `${i.phase}|${i.state}`;
      const e = m.get(key) ?? { phase: i.phase, state: i.state, count: 0 };
      e.count += 1;
      m.set(key, e);
    }
    const rank: Record<string, number> = { harness: 0, eval: 1 };
    return [...m.values()].sort(
      (a, b) =>
        (rank[a.phase] ?? 9) - (rank[b.phase] ?? 9) || b.count - a.count,
    );
  }, [instances.data]);

  const restart = useMutation({
    mutationFn: () =>
      api.restartInstances(
        runId,
        Array.from(selectedInstances),
        restartActor.trim() || 'operator',
      ),
    onSuccess: () => {
      setSelectedInstances(new Set());
      setRestartArmed(false);
      queryClient.invalidateQueries({ queryKey: ['run', runId] });
      queryClient.invalidateQueries({ queryKey: ['instances', runId] });
    },
  });

  // Regrade (2026-09-01): the EXISTING patch back through grading as an
  // eval-only attempt N+1 — the remedy for eval-side failures
  // (EVAL_OOM_KILLED / ABANDONED / dead-lettered) that restart can't give
  // without re-spending on inference. No arm step: a regrade spends no
  // model money (restart's confirm exists precisely because it does).
  const regrade = useMutation({
    mutationFn: () =>
      api.regradeInstances(
        runId,
        Array.from(selectedInstances),
        restartActor.trim() || 'operator',
      ),
    onSuccess: () => {
      setSelectedInstances(new Set());
      setRestartArmed(false);
      queryClient.invalidateQueries({ queryKey: ['run', runId] });
      queryClient.invalidateQueries({ queryKey: ['instances', runId] });
    },
  });

  // LLM judge (Pass B) reuses the same instance-selection checkboxes
  // restart/regrade already use, rather than a second selector — empty
  // means "judge all eligible" (§9.3's 100% default).
  const judgeInstanceIds = useMemo(
    () => Array.from(selectedInstances),
    [selectedInstances],
  );

  const toggleInstance = (instanceId: string) => {
    setSelectedInstances((prev) => {
      const next = new Set(prev);
      if (next.has(instanceId)) next.delete(instanceId);
      else next.add(instanceId);
      return next;
    });
    setRestartArmed(false); // selection changed — the prior confirm no longer covers it
    // JudgeLaunchCard re-arms itself on a selectedInstanceIds prop change —
    // same rule, owned inside the self-contained panel instead of here.
  };

  return (
    <div className="space-y-4">
      <button
        type="button"
        onClick={onBack}
        className="text-xs font-medium text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
      >
        ← back to runs
      </button>

      {run.isLoading && (
        <Card>
          <CardContent className="p-6 text-sm text-zinc-400">
            Loading run…
          </CardContent>
        </Card>
      )}
      {run.isError && (
        <Card>
          <CardContent className="p-6 text-sm text-rose-500">
            failed to load run: {(run.error as Error).message}
          </CardContent>
        </Card>
      )}

      {run.data && (
        <>
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 font-mono text-base">
                {run.data.run_id.slice(0, 24)}
                <StatusBadge label={run.data.status} />
                {run.data.terminal && (
                  <StatusBadge label="terminal" tone="neutral" />
                )}
                <span className="ml-auto flex items-center gap-2 text-[11px] font-normal text-zinc-400">
                  {run.data.instance_count ?? 0} instances
                  <Freshness updatedAt={run.dataUpdatedAt} />
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent>
              {/* provenance first — this is how an operator confirms what was
                  measured. Sourced from runs.config_snapshot (BUILDER2 handover
                  bucket A, option b), so it renders as soon as the run exists —
                  no dependency on the results-writer publishing a summary. */}
              <div className="mb-3 rounded-md border border-sky-200 bg-sky-50/50 p-3 dark:border-sky-900 dark:bg-sky-950/30">
                <SectionLabel>
                  provenance — what this run actually measured
                </SectionLabel>
                <div className="mt-2 grid grid-cols-2 gap-3 md:grid-cols-4">
                  <KV
                    k="framework sha"
                    v={
                      <span
                        title={prov?.framework_sha ?? undefined}
                        className="font-mono"
                      >
                        {prov?.framework_sha
                          ? shortSha(prov.framework_sha, 12)
                          : '—'}
                      </span>
                    }
                  />
                  <KV
                    k="harness image digest"
                    v={
                      <span
                        title={prov?.harness_image_digest ?? undefined}
                        className="font-mono"
                      >
                        {prov?.harness_image_digest
                          ? shortSha(prov.harness_image_digest, 14)
                          : '—'}
                      </span>
                    }
                  />
                  <KV
                    k="gateway config hash"
                    v={
                      <span
                        title={prov?.gateway_config_hash ?? undefined}
                        className="font-mono"
                      >
                        {prov?.gateway_config_hash
                          ? shortSha(prov.gateway_config_hash, 12)
                          : '—'}
                      </span>
                    }
                  />
                  <KV k="harness" v={run.data.harness ?? '—'} />
                  <KV
                    k="model"
                    v={
                      <span title={prov?.model_resolved ?? undefined}>
                        {run.data.model_alias ?? prov?.model_resolved ?? '—'}
                      </span>
                    }
                  />
                  <KV
                    k="dataset"
                    v={
                      <span title={prov?.image_digest_snapshot ?? undefined}>
                        {prov?.dataset_name ?? '—'}
                        {prov?.image_digest_snapshot
                          ? ' · digest snapshot ✓'
                          : ''}
                      </span>
                    }
                  />
                  <KV
                    k="dataset revision"
                    v={
                      prov?.dataset_revision
                        ? shortSha(prov.dataset_revision, 12)
                        : '—'
                    }
                  />
                  <KV k="swebench version" v={prov?.swebench_version ?? '—'} />
                  <KV
                    k="context window"
                    v={
                      prov?.context_window_tokens != null
                        ? fmtTokens(prov.context_window_tokens)
                        : '—'
                    }
                  />
                  <KV k="dispatched" v={fmtTime(run.data.dispatched_at)} />
                  <KV k="finalised" v={fmtTime(run.data.finalised_at)} />
                </div>
              </div>

              <SectionLabel>
                instances{' '}
                <span className="font-normal normal-case text-zinc-400">
                  (one bucket per instance — its latest attempt, the eval
                  verdict once graded)
                </span>
              </SectionLabel>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {instanceStates.length === 0 && (
                  <span className="text-xs text-zinc-400">
                    no instance rows yet
                  </span>
                )}
                {instanceStates.map((s) => (
                  <span
                    key={s.state}
                    className="rounded-md bg-zinc-100 px-2 py-1 font-mono text-xs dark:bg-zinc-800"
                  >
                    {s.state} <span className="font-semibold">{s.count}</span>
                  </span>
                ))}
              </div>

              <SectionLabel>
                phase rows{' '}
                <span className="font-normal normal-case text-zinc-400">
                  (per attempt × phase — an instance in eval has both a harness
                  and an eval row; a rerun adds rows)
                </span>
              </SectionLabel>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {phaseStates === null && states.length === 0 && (
                  <span className="text-xs text-zinc-400">
                    no instance rows yet
                  </span>
                )}
                {phaseStates
                  ? phaseStates.map((s) => (
                      <span
                        key={`${s.phase}|${s.state}`}
                        className="inline-flex items-center gap-1 rounded-md bg-zinc-100 px-2 py-1 font-mono text-xs dark:bg-zinc-800"
                      >
                        <span
                          className={cn(
                            'rounded px-1 text-[9px] font-semibold uppercase',
                            s.phase === 'eval'
                              ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300'
                              : 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300',
                          )}
                        >
                          {s.phase}
                        </span>
                        {s.state}{' '}
                        <span className="font-semibold">{s.count}</span>
                      </span>
                    ))
                  : states.map((s) => (
                      <span
                        key={s.state}
                        className="rounded-md bg-zinc-100 px-2 py-1 font-mono text-xs dark:bg-zinc-800"
                      >
                        {s.state}{' '}
                        <span className="font-semibold">{s.count}</span>
                      </span>
                    ))}
              </div>

              {/* totals: the two named denominators (ADR-0038 §4) are DERIVED
                  here from the per-instance state buckets (lib/rates.ts) —
                  not read from the flat run_summary blob, whose `gradeable`
                  silently dropped EMPTY_PATCH until the results-writer fix, and
                  which is never recomputed for a finished run. Empty patches
                  are model failures and stay in the denominator; harness
                  crashes / aborts / budget stops are named as excluded. Cost is
                  the summed actual inference spend; turns + tokens are
                  aggregated from the harness-phase instance rows. */}
              <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
                <KV k="resolved" v={fmtNum(rates.resolved)} />
                <KV
                  k="gradeable (verdicts + empty patches)"
                  v={fmtNum(rates.gradeable)}
                />
                <KV
                  k="resolve rate (gradeable)"
                  v={fmtPct(rates.rateGradeable)}
                />
                <KV
                  k="excluded (infra / cap)"
                  v={
                    rates.excluded > 0
                      ? `${fmtNum(rates.excluded)} · ${rates.excludedStates.join(', ')}`
                      : '0'
                  }
                />
                <KV
                  k="attempted (denominator rule)"
                  v={fmtNum(rates.attempted)}
                />
                <KV
                  k="resolve rate (attempted)"
                  v={fmtPct(rates.rateAttempted)}
                />
                <KV
                  k="in flight"
                  v={rates.inFlight > 0 ? fmtNum(rates.inFlight) : '0'}
                />
                <KV k="cost (inference)" v={fmtUsd(run.data.cost_usd_total)} />
                <KV k="turns" v={agg.turns ? fmtNum(agg.turns) : '—'} />
                <KV
                  k="tokens (in / out)"
                  v={`${fmtTokens(agg.inputTokens)} / ${fmtTokens(agg.outputTokens)}`}
                />
              </div>

              {(run.data.stop_requested_at || run.data.stop_scope) && (
                <div className="mt-4 rounded-md border border-amber-200 p-2 text-xs dark:border-amber-800">
                  <SectionLabel className="text-amber-500">
                    abort intent recorded
                  </SectionLabel>
                  <div className="mt-1 flex flex-wrap gap-4">
                    <KV k="scope" v={run.data.stop_scope ?? '—'} />
                    <KV
                      k="requested at"
                      v={fmtTime(run.data.stop_requested_at)}
                    />
                    <KV k="reason" v={run.data.stop_reason ?? '—'} />
                  </div>
                </div>
              )}
            </CardContent>
          </Card>

          {/* run monitor (M5.1 view 2) — cost vs budget, per-phase progress,
              and the work-queue panels an operator stares at during the smoke */}
          <RunCost
            actualCostUsd={run.data.cost_usd_total}
            estimatedCostUsd={run.data.estimated_cost_usd}
            tier={run.data.cost_confidence_tier}
            computeEstimatedUsd={run.data.compute_cost_estimated_usd}
            computeReconciledUsd={run.data.compute_cost_reconciled_usd}
            budgetCapUsd={run.data.budget_cap_usd}
          />
          <RunProgressPanel runId={runId} gradeable={rates.gradeable} />
          <LivePanel
            runId={runId}
            terminal={run.data.terminal}
            onOpenInstance={onOpenInstance}
          />
          {/* The live L1 pacer ledger per alias (§2.5) — the wait queue, bucket fill,
              in-flight volume and last-60s counters. Right under live progress: when an
              instance above shows a long pacer hold, this is what it is waiting on. */}
          <PacerPanel runId={runId} terminal={run.data.terminal} />
          {/* F8 (owner request, 2026-09-04): the eval-side scaling view — desired
              tasks/hosts, binding, queue/feed-forward inputs, the ASG rail and the
              F7 scale-in countdown — beside the harness-side pacer ledger. */}
          <EvalScalerPanel terminal={run.data.terminal} />
          {/* Operator limits (owner request 2026-09-04): the numbers the two panels above
              run on, editable mid-run — per-run cap, planner ceiling override, growth /
              cooldown, the global planner + eval-scaler knobs, and pacer:cfg per alias. */}
          {!run.data.terminal && <LimitsPanel runId={runId} />}
          {/* The live LLM-call panel moved to InstanceDetail (owner feedback,
              run 1): reading a trajectory belongs on the instance screen,
              scoped to one attempt, not on the run overview. */}
          <QueuePanels />

          <ControlPanel
            runId={runId}
            readyToClose={run.data.ready_to_close}
            resolveRateDenominator={run.data.resolve_rate_denominator}
            gatewayKeyBlockedBy={run.data.gateway_key_blocked_by}
          />

          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <div className="flex items-center gap-2">
                <CardTitle>Instances</CardTitle>
                <Freshness updatedAt={instances.dataUpdatedAt} />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <input
                  type="search"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="search instance id"
                  aria-label="search instances"
                  className="w-44 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
                />
                <select
                  value={repoFilter}
                  onChange={(e) => setRepoFilter(e.target.value)}
                  className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
                  aria-label="filter repo"
                >
                  <option value="">all repos</option>
                  {repoOptions(instances.data?.items ?? []).map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
                <select
                  value={phaseFilter}
                  onChange={(e) => setPhaseFilter(e.target.value)}
                  className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
                  aria-label="filter phase"
                >
                  <option value="">both phases</option>
                  <option value="harness">harness</option>
                  <option value="eval">eval</option>
                </select>
                <select
                  value={stateFilter}
                  onChange={(e) => setStateFilter(e.target.value)}
                  className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
                  aria-label="filter state"
                >
                  <option value="">all states</option>
                  {/* 2026-09-08: every state the RUN has (run.states, whole-run
                      counts), not just the states on the fetched page — the
                      filter is server-side, so a state absent from this page
                      must still be selectable. */}
                  {Array.from(
                    new Set([
                      ...states.map((s) => s.state),
                      ...(instances.data?.items ?? []).map((i) => i.state),
                    ]),
                  )
                    .sort()
                    .map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                </select>
                <select
                  value={errFilter}
                  onChange={(e) => setErrFilter(e.target.value)}
                  className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
                  aria-label="filter error category"
                >
                  <option value="">all error categories</option>
                  {errCats.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
                <select
                  value={judgedFilter}
                  onChange={(e) =>
                    setJudgedFilter(
                      e.target.value as '' | 'judged' | 'unjudged',
                    )
                  }
                  className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
                  aria-label="filter judged"
                  title="LLM judge (Pass B): rows the judge has scored — refreshes every 15 s while a pass runs"
                >
                  <option value="">judged or not</option>
                  <option value="judged">judged ({judgedCount})</option>
                  <option value="unjudged">not judged</option>
                </select>
                <select
                  value={issueFilter}
                  onChange={(e) =>
                    setIssueFilter(
                      e.target.value as '' | JudgeIssueKind | 'any' | 'none',
                    )
                  }
                  className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
                  aria-label="filter judge findings"
                  title="LLM judge (Pass B): judged rows with this rubric finding — counts are per judged attempt"
                >
                  <option value="">all judge findings</option>
                  {JUDGE_ISSUE_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label} ({issueCounts.get(opt.value) ?? 0})
                    </option>
                  ))}
                </select>
              </div>
            </CardHeader>

            {/* manual restart (BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md
                §2.2) — operator picks which failed instances relaunch; nothing
                here decides on its own. Allowed any time the run isn't closed,
                mid-run or fully idle. */}
            <div className="flex flex-wrap items-center gap-2 border-t border-zinc-200 px-3 py-2 text-xs dark:border-zinc-800">
              <span className="font-semibold uppercase tracking-wide text-zinc-500">
                {selectedInstances.size} selected
              </span>
              <input
                aria-label="restart actor"
                className="w-28 rounded-md border border-zinc-200 bg-white px-2 py-1 dark:border-zinc-800 dark:bg-zinc-900"
                placeholder="actor"
                value={restartActor}
                onChange={(e) => setRestartActor(e.target.value)}
              />
              <label className="flex items-center gap-1 text-[11px] text-zinc-500">
                <input
                  type="checkbox"
                  checked={restartArmed}
                  disabled={selectedInstances.size === 0}
                  onChange={(e) => setRestartArmed(e.target.checked)}
                />
                confirm restart — launches real inference calls
              </label>
              <button
                type="button"
                disabled={
                  selectedInstances.size === 0 ||
                  !restartArmed ||
                  restart.isPending
                }
                onClick={() => restart.mutate()}
                className="rounded-md bg-sky-600 px-3 py-1 font-medium text-white hover:bg-sky-500 disabled:opacity-40"
              >
                {restart.isPending
                  ? 'Restarting…'
                  : `Restart selected (${selectedInstances.size})`}
              </button>
              {restart.isError && (
                <span className="text-rose-500">
                  restart failed: {(restart.error as Error).message}
                </span>
              )}
              {restart.isSuccess && (
                <span className="text-zinc-500">
                  restarted{' '}
                  <span className="font-semibold text-emerald-600 dark:text-emerald-400">
                    {restart.data.restarted?.length ?? 0}
                  </span>
                  {(restart.data.skipped?.length ?? 0) > 0 && (
                    <>
                      {' '}
                      · skipped{' '}
                      <span className="font-semibold text-amber-600 dark:text-amber-400">
                        {restart.data.skipped?.length}
                      </span>{' '}
                      (
                      {restart.data.skipped
                        ?.map((s) => `${s.instance_id}: ${s.reason}`)
                        .join('; ')}
                      )
                    </>
                  )}
                </span>
              )}
              <button
                type="button"
                disabled={selectedInstances.size === 0 || regrade.isPending}
                onClick={() => regrade.mutate()}
                title="Grade the existing patch again (eval-only attempt N+1) — no model spend. For eval-side failures like EVAL_OOM_KILLED or ABANDONED."
                className="rounded-md border border-zinc-300 px-3 py-1 font-medium text-zinc-700 hover:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-800"
              >
                {regrade.isPending
                  ? 'Regrading…'
                  : `Regrade selected (${selectedInstances.size})`}
              </button>
              {regrade.isError && (
                <span className="text-rose-500">
                  regrade failed: {(regrade.error as Error).message}
                </span>
              )}
              {regrade.isSuccess && (
                <span className="text-zinc-500">
                  regraded{' '}
                  <span className="font-semibold text-emerald-600 dark:text-emerald-400">
                    {regrade.data.regraded?.length ?? 0}
                  </span>
                  {(regrade.data.skipped?.length ?? 0) > 0 && (
                    <>
                      {' '}
                      · skipped{' '}
                      <span className="font-semibold text-amber-600 dark:text-amber-400">
                        {regrade.data.skipped?.length}
                      </span>{' '}
                      (
                      {regrade.data.skipped
                        ?.map((s) => `${s.instance_id}: ${s.reason}`)
                        .join('; ')}
                      )
                    </>
                  )}
                </span>
              )}
            </div>
            <div className="border-t border-zinc-200 px-3 py-1.5 text-[11px] text-zinc-400 dark:border-zinc-800">
              If the harness or gateway pool is currently paused, a restarted
              instance stays <span className="font-mono">PENDING</span> — and
              the run can't be closed — until someone resumes it. That's
              expected, not stuck; check Operator control above.
            </div>

            <CardContent className="p-0">
              {instances.isLoading && (
                <div className="p-4 text-sm text-zinc-400">
                  Loading instances…
                </div>
              )}
              {instances.isError && (
                <div className="p-4 text-sm text-rose-500">
                  failed to load instances: {(instances.error as Error).message}
                </div>
              )}
              {instances.data && instances.data.items.length === 0 && (
                <div className="p-4 text-sm text-zinc-400">
                  No instances for this run yet.
                </div>
              )}
              {instances.data && instances.data.items.length > 0 && (
                <div className="flex flex-wrap items-center gap-3 border-t border-zinc-200 px-3 py-2 text-xs text-zinc-500 dark:border-zinc-800">
                  <span>
                    showing{' '}
                    <span className="font-mono font-semibold">
                      {visibleRows.length}
                    </span>{' '}
                    of {instances.data.items.length} rows
                    {visibleRows.length !== instances.data.items.length &&
                      ' (narrowed)'}
                  </span>
                  <button
                    type="button"
                    aria-label="toggle instance table"
                    onClick={() => setTableOpen(!tableShown)}
                    className="rounded-md border border-zinc-200 px-2 py-0.5 text-[11px] hover:bg-zinc-50 dark:border-zinc-800 dark:hover:bg-zinc-900"
                  >
                    {tableShown ? 'collapse' : 'expand'}
                  </button>
                  {!tableShown && (
                    <span className="text-[11px] text-zinc-400">
                      collapsed — large run; search or filter above, or expand
                    </span>
                  )}
                </div>
              )}
              {instances.data &&
                instances.data.items.length > 0 &&
                tableShown && (
                  <div className="overflow-x-auto">
                    <table className="w-full border-collapse">
                      <thead>
                        <tr className="border-b border-zinc-200 text-[11px] uppercase tracking-wide text-zinc-400 dark:border-zinc-800">
                          <th className="px-3 py-2 text-left">
                            <span className="sr-only">restart select</span>
                          </th>
                          <th className="px-3 py-2 text-left">Instance</th>
                          <th className="px-3 py-2 text-left">Attempt</th>
                          <th className="px-3 py-2 text-left">Phase</th>
                          <th className="px-3 py-2 text-left">State</th>
                          <th className="px-3 py-2 text-left">Verdict</th>
                          <th className="px-3 py-2 text-left">Contamination</th>
                          <th className="px-3 py-2 text-left">
                            Judge findings
                          </th>
                          <th className="px-3 py-2 text-left">Cost</th>
                          <th className="px-3 py-2 text-left">Agent time</th>
                          <th className="px-3 py-2 text-left">Error</th>
                          <th className="px-3 py-2 text-left">Retry</th>
                        </tr>
                      </thead>
                      <tbody>
                        {visibleRows.map((i) => {
                          const inFlight = activeStates.has(i.state);
                          return (
                            <tr
                              key={`${i.instance_id}-${i.attempt_number}-${i.phase}`}
                              className={cn(
                                'cursor-pointer border-t border-zinc-200/70 hover:bg-zinc-50 dark:border-zinc-800/70 dark:hover:bg-zinc-900/50',
                                i.phase === 'eval' &&
                                  'bg-zinc-50/50 dark:bg-zinc-900/40',
                              )}
                              onClick={() =>
                                onOpenInstance(i.instance_id, i.attempt_number)
                              }
                            >
                              <td
                                className="px-3 py-2"
                                onClick={(e) => e.stopPropagation()}
                              >
                                <input
                                  type="checkbox"
                                  aria-label={`select ${i.instance_id} for restart`}
                                  disabled={inFlight}
                                  checked={selectedInstances.has(i.instance_id)}
                                  onChange={() => toggleInstance(i.instance_id)}
                                  title={
                                    inFlight
                                      ? 'still in flight — not restart-eligible'
                                      : 'select for restart'
                                  }
                                />
                              </td>
                              <td className="px-3 py-2 font-mono text-xs text-sky-700 dark:text-sky-400">
                                {i.instance_id}
                              </td>
                              <td className="px-3 py-2 text-xs">
                                {i.attempt_number}
                              </td>
                              <td className="px-3 py-2 text-xs text-zinc-500">
                                {i.phase}
                              </td>
                              <td className="px-3 py-2">
                                <StatusBadge label={i.state} />
                              </td>
                              <td className="px-3 py-2">
                                <StatusBadge label={i.verdict} />
                              </td>
                              <td
                                className="px-3 py-2"
                                onClick={(e) => e.stopPropagation()}
                              >
                                <ContaminationCell
                                  result={findJudgeResult(
                                    judgeResults.data?.results,
                                    i.instance_id,
                                    i.attempt_number,
                                  )}
                                  onOpen={() =>
                                    onOpenInstance(
                                      i.instance_id,
                                      i.attempt_number,
                                    )
                                  }
                                />
                              </td>
                              <td
                                className="px-3 py-2"
                                onClick={(e) => e.stopPropagation()}
                              >
                                <JudgeFindingsCell
                                  result={findJudgeResult(
                                    judgeResults.data?.results,
                                    i.instance_id,
                                    i.attempt_number,
                                  )}
                                  onOpen={() =>
                                    onOpenInstance(
                                      i.instance_id,
                                      i.attempt_number,
                                    )
                                  }
                                />
                              </td>
                              <td className="px-3 py-2 font-mono text-xs">
                                {fmtUsd(i.cost_usd)}
                              </td>
                              <td className="px-3 py-2 text-xs">
                                {fmtDuration(i.agent_s)}
                              </td>
                              <td className="px-3 py-2 text-xs text-zinc-500">
                                {i.error_category ?? ''}
                              </td>
                              <td className="px-3 py-2">
                                {i.retry_reason && (
                                  <StatusBadge
                                    label={i.retry_reason}
                                    tone="neutral"
                                  />
                                )}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
            </CardContent>
          </Card>

          <JudgeLaunchCard
            runId={runId}
            terminal={Boolean(run.data.terminal)}
            selectedInstanceIds={judgeInstanceIds}
          />
        </>
      )}
    </div>
  );
}
