import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api, type LiveInstance } from '../lib/api';
import { IS_DEMO } from '../lib/dataMode';
import { fmtDuration, fmtNum, fmtUsd } from '../lib/format';
import {
  COLLAPSE_ABOVE_ROWS,
  narrowRows,
  repoOptions,
} from '../lib/instanceTable';
import { StatusBadge } from './StatusBadge';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  SectionLabel,
} from './ui-primitives';

// GET /runs/{run_id}/live (F11) — per-instance in-flight progress, straight
// from Redis, over the Postgres attempt list. This is what closes F11's
// acceptance gap: the harness worker has no Postgres write path at all
// (ADR-0007), so Postgres only learns about an instance when it finishes —
// mid-run, this panel is the only place "what is actually happening right
// now" is visible at all (BUILDER2-LIVE-RUN-MONITORING-2026-08-31.md §2).
//
// Three states, three renderings, everywhere (§3):
//   - a per-instance `state` of running / pending / stale — never collapsed
//     into a bare number, because a missing key has at least three different
//     causes and they must not all read as "0 turns";
//   - a whole-response `state` of ok / unknown, when Redis itself is down —
//     an empty list must never be mistaken for "nothing is running";
//   - every numeric field is `None` on a missing key, never 0 — a `—` is
//     honest, a `0` is a lie that looks like data. Never coerce with `?? 0`
//     here; `fmtNum`/`fmtUsd`/`fmtDuration` already render null as `—`.
//
// `stale` does NOT mean dead (§3): the 300s TTL is ~13x the measured p90
// turn time, but a long tool call or compaction pass can still exceed it. A
// healthy-but-slow instance reporting `stale` and being read as "failed" is
// how an operator restarts a healthy instance and spends real inference
// money — so it renders as "no recent observation", with its own caption,
// never merely as an alarming badge.

// BUILDER4-PACER-SEED-AND-FAIRNESS §2.5: the instance's cumulative L1 pacer footprint,
// live — how long it has been held at the pacer in total, how many of its calls were
// queued, and (the alarm) how many hit the hold cap (each one surfaced a 429 to the
// CLI: run 01788405363237319353's matplotlib died of exactly ten of these, invisibly).
// `null` = the payload predates the field / not measured → '—', never 0.
function PacerCell({ it }: { it: LiveInstance }) {
  const held = it.paced_wait_ms_total;
  if (held == null) {
    return <td className="px-3 py-2 font-mono text-xs text-zinc-400">—</td>;
  }
  const timeouts = it.pacer_timeouts ?? 0;
  const queued = it.paced_calls ?? 0;
  // The total is the SUM of per-call admission round trips (~15 ms each on an
  // idle pacer), so it grows with the call count; amber only when calls were
  // actually queued (owner question 2026-09-07).
  const tone =
    timeouts > 0
      ? 'text-rose-600 dark:text-rose-400'
      : queued > 0 && held >= 5000
        ? 'text-amber-600 dark:text-amber-400'
        : 'text-zinc-600 dark:text-zinc-300';
  const title = [
    `${fmtDuration(held / 1000)} total pacer admission time (sum of per-call round trips, ~15 ms each when idle)`,
    `${queued} call(s) queued at least once`,
    `${timeouts} hold-cap timeout(s) (each surfaced a 429 to the CLI)`,
    it.overload_retries_total != null
      ? `${it.overload_retries_total} provider-429 retries absorbed`
      : null,
    it.pacer_last_deny_axis
      ? `last denied on the "${it.pacer_last_deny_axis}" axis behind a queue of ${it.pacer_last_queue_len ?? '?'}`
      : null,
  ]
    .filter(Boolean)
    .join(' · ');
  return (
    <td className={`px-3 py-2 font-mono text-xs ${tone}`} title={title}>
      {fmtDuration(held / 1000)}
      {queued > 0 && <span className="text-zinc-400"> · {queued}q</span>}
      {timeouts > 0 && <span> · {timeouts}✕</span>}
    </td>
  );
}

export function LivePanel({
  runId,
  terminal,
  onOpenInstance,
}: {
  runId: string;
  terminal: boolean;
  onOpenInstance?: (instanceId: string, attempt: number) => void;
}) {
  const live = useQuery({
    queryKey: ['run-live', runId],
    queryFn: () => api.getRunLive(runId),
    refetchInterval: () => (terminal ? false : 5000),
    refetchIntervalInBackground: false,
    staleTime: 3000,
    enabled: Boolean(runId),
  });

  const data = live.data;
  const items = useMemo(() => data?.items ?? [], [data]);

  // Owner request during the first 500-run (2026-09-06): at 145 concurrent
  // agents this list is as unreadable as the instance table, so it narrows
  // the same way (search / repo / phase, plus the live state) and starts
  // collapsed above COLLAPSE_ABOVE_ROWS rows. Pure client-side — the 5 s
  // poll is unchanged. A harness payload predates the `phase` field, so a
  // missing phase IS the harness phase.
  const [search, setSearch] = useState('');
  const [repoFilter, setRepoFilter] = useState('');
  const [phaseFilter, setPhaseFilter] = useState('');
  const [stateFilter, setStateFilter] = useState('');
  // null = "not decided by the operator yet": collapsed iff the list is big.
  const [listOpen, setListOpen] = useState<boolean | null>(null);

  const visible = useMemo(() => {
    const withPhase = items.map((it) => ({
      ...it,
      phase: it.phase ?? 'harness',
    }));
    const narrowed = narrowRows(withPhase, {
      search,
      repo: repoFilter,
      phase: phaseFilter,
    });
    return stateFilter
      ? narrowed.filter((it) => it.state === stateFilter)
      : narrowed;
  }, [items, search, repoFilter, phaseFilter, stateFilter]);
  const narrowed = Boolean(
    search.trim() || repoFilter || phaseFilter || stateFilter,
  );
  // The public Explorer (demo mode) starts the list open; see RunDetail's tableShown.
  const listShown =
    listOpen ?? (IS_DEMO || narrowed || items.length <= COLLAPSE_ABOVE_ROWS);

  const CAPTION: Record<string, string> = {
    pending: 'not started',
    stale: 'no recent observation — may still be healthy, not necessarily dead',
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>Live progress</CardTitle>
          {items.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="search instance id"
                aria-label="search live instances"
                className="w-44 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
              />
              <select
                value={repoFilter}
                onChange={(e) => setRepoFilter(e.target.value)}
                className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
                aria-label="filter live repo"
              >
                <option value="">all repos</option>
                {repoOptions(items).map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
              <select
                value={phaseFilter}
                onChange={(e) => setPhaseFilter(e.target.value)}
                className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
                aria-label="filter live phase"
              >
                <option value="">both phases</option>
                <option value="harness">harness (agent)</option>
                <option value="eval">eval (grading)</option>
              </select>
              <select
                value={stateFilter}
                onChange={(e) => setStateFilter(e.target.value)}
                className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900"
                aria-label="filter live state"
              >
                <option value="">all states</option>
                {Array.from(new Set(items.map((i) => i.state))).map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {live.isLoading && (
          <div className="text-sm text-zinc-400">Loading live progress…</div>
        )}
        {live.isError && (
          <div className="text-sm text-rose-500">
            failed to load live progress: {(live.error as Error).message}
          </div>
        )}

        {/* whole-response health: Redis itself unreachable. Never render this
            as an empty table — that reads as "nothing is running". */}
        {data && data.state === 'unknown' && (
          <div className="rounded-md border border-amber-200 bg-amber-50/50 p-3 text-xs text-amber-700 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-400">
            Live progress unknown ({data.reason || 'reason not given'}) — Redis
            is unreachable right now. This says nothing about whether instances
            are actually running; it means this panel can't see them.
          </div>
        )}

        {data && data.state !== 'unknown' && items.length === 0 && (
          <div className="text-sm text-zinc-400">
            No instances in flight right now.
          </div>
        )}

        {data && data.state !== 'unknown' && items.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-500">
            <span>
              showing {visible.length} of {items.length} in flight
              {narrowed && ' (narrowed)'}
            </span>
            <button
              type="button"
              aria-label="toggle live progress list"
              onClick={() => setListOpen(!listShown)}
              className="rounded-md border border-zinc-200 px-2 py-0.5 text-[11px] hover:bg-zinc-50 dark:border-zinc-800 dark:hover:bg-zinc-900"
            >
              {listShown ? 'collapse' : 'expand'}
            </button>
            {!listShown && (
              <span className="text-[11px] text-zinc-400">
                collapsed — many in flight; search or filter above, or expand
              </span>
            )}
          </div>
        )}

        {data && data.state !== 'unknown' && items.length > 0 && listShown && (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr className="border-b border-zinc-200 text-[11px] uppercase tracking-wide text-zinc-400 dark:border-zinc-800">
                  <th className="px-3 py-2 text-left">Instance</th>
                  <th className="px-3 py-2 text-left">Attempt</th>
                  <th className="px-3 py-2 text-left">State</th>
                  <th className="px-3 py-2 text-left">Turn</th>
                  <th className="px-3 py-2 text-left">Input tok</th>
                  <th className="px-3 py-2 text-left">Output tok</th>
                  <th className="px-3 py-2 text-left">Cost so far</th>
                  <th className="px-3 py-2 text-left">Pacer</th>
                  <th className="px-3 py-2 text-left">Age</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((it) => (
                  <tr
                    key={`${it.instance_id}-${it.attempt_number}`}
                    className={
                      onOpenInstance
                        ? 'cursor-pointer border-t border-zinc-200/70 hover:bg-zinc-50 dark:border-zinc-800/70 dark:hover:bg-zinc-900/50'
                        : 'border-t border-zinc-200/70 dark:border-zinc-800/70'
                    }
                    onClick={
                      onOpenInstance
                        ? () =>
                            onOpenInstance(it.instance_id, it.attempt_number)
                        : undefined
                    }
                  >
                    <td className="px-3 py-2 font-mono text-xs text-sky-700 underline-offset-2 hover:underline dark:text-sky-400">
                      {it.instance_id}
                    </td>
                    <td className="px-3 py-2 text-xs">{it.attempt_number}</td>
                    <td className="px-3 py-2">
                      <div className="flex flex-col gap-0.5">
                        <StatusBadge label={it.state} />
                        {CAPTION[it.state] && (
                          <span className="text-[10px] text-zinc-400">
                            {CAPTION[it.state]}
                          </span>
                        )}
                      </div>
                    </td>
                    {/* eval-phase payload (the grade's exec tee, 2026-09-06):
                        the turn/token columns carry the grade's progress —
                        lines of test output so far and the last line seen —
                        so a 67-minute suite is visibly alive, not "stale". */}
                    <td
                      className="px-3 py-2 font-mono text-xs"
                      title={
                        it.phase === 'eval' && it.eval_last_line
                          ? `last line: ${it.eval_last_line}`
                          : undefined
                      }
                    >
                      {it.phase === 'eval'
                        ? `grading · ${fmtNum(it.eval_lines)} lines${
                            it.eval_elapsed_s != null
                              ? ` · ${fmtDuration(it.eval_elapsed_s)}`
                              : ''
                          }`
                        : fmtNum(it.turn_number)}
                    </td>
                    <td
                      className="px-3 py-2 font-mono text-xs"
                      title={
                        it.phase === 'eval'
                          ? undefined
                          : `cached ${fmtNum(it.cached_tokens)} · reasoning ${fmtNum(it.reasoning_tokens)}`
                      }
                    >
                      {it.phase === 'eval' ? '—' : fmtNum(it.input_tokens)}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {fmtNum(it.output_tokens)}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {fmtUsd(it.cost_usd)}
                    </td>
                    <PacerCell it={it} />
                    <td className="px-3 py-2 font-mono text-xs text-zinc-500">
                      {it.age_s === null ? '—' : `${fmtDuration(it.age_s)} old`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {!live.isLoading && !live.isError && !data && (
          <div className="text-sm text-zinc-400">No live data yet.</div>
        )}

        <SectionLabel className="pt-1">
          Redis-backed, best-effort, TTL'd at 300s — this is a supplement to the
          instance table below, not a replacement. It shows what Postgres can't:
          instances that are still running.
        </SectionLabel>
      </CardContent>
    </Card>
  );
}
