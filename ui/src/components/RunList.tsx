import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api, flatSummary, type RunItem } from '../lib/api';
import { fmtTime, fmtUsd, relativeAge, shortSha } from '../lib/format';
import { isAnyRunActive, useAnyRunActive } from '../lib/queries';
import { cn } from '../lib/utils';
import { Freshness } from './Freshness';
import { StatusBadge } from './StatusBadge';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  SectionLabel,
} from './ui-primitives';

type SortKey = 'created_at' | 'run_id' | 'status' | 'cost_usd_total';
type SortDir = 'asc' | 'desc';

export function RunList({ onOpenRun }: { onOpenRun: (runId: string) => void }) {
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [harnessFilter, setHarnessFilter] = useState<string>('');
  const [modelFilter, setModelFilter] = useState<string>('');
  const [dateFrom, setDateFrom] = useState<string>('');
  const [dateTo, setDateTo] = useState<string>('');
  const [sortKey, setSortKey] = useState<SortKey>('created_at');
  const [sortDir, setSortDir] = useState<SortDir>('desc');
  const [limit, setLimit] = useState(50);

  // §1a: stop polling entirely when no run is active — a 20 s poll would
  // otherwise defeat Aurora auto-pause (min_capacity = 0, 300 s idle window).
  // "No run is active" is decided on the unfiltered list (a status filter
  // must not change whether we keep the fleet awake).
  const anyActive = useAnyRunActive();
  const runs = useQuery({
    queryKey: ['runs', { limit, offset: 0, status: statusFilter || undefined }],
    queryFn: () =>
      api.listRuns({ limit, offset: 0, status: statusFilter || undefined }),
    refetchInterval: isAnyRunActive(anyActive.data?.items) ? 20000 : false,
    refetchIntervalInBackground: false,
    staleTime: 10000,
  });

  // harness + date are filtered client-side over the fetched page (status is
  // the only server-side filter today); dev run counts are small enough that
  // this is honest and avoids a backend round-trip per filter change.
  const filtered = useMemo(() => {
    const items = runs.data?.items ?? [];
    const dir = sortDir === 'asc' ? 1 : -1;
    const fromMs = dateFrom ? Date.parse(dateFrom) : null;
    // dateTo is a calendar day → include the whole day (+24h).
    const toMs = dateTo ? Date.parse(dateTo) + 86_400_000 : null;
    return items
      .filter((r) => !harnessFilter || r.harness === harnessFilter)
      .filter((r) => !modelFilter || r.model_alias === modelFilter)
      .filter((r) => {
        if (!fromMs && !toMs) return true;
        const t = r.created_at ? Date.parse(r.created_at) : NaN;
        if (Number.isNaN(t)) return false;
        if (fromMs && t < fromMs) return false;
        if (toMs && t >= toMs) return false;
        return true;
      })
      .sort((a, b) => {
        if (sortKey === 'cost_usd_total') {
          return ((a.cost_usd_total ?? -1) - (b.cost_usd_total ?? -1)) * dir;
        }
        return (
          String(a[sortKey] ?? '').localeCompare(String(b[sortKey] ?? '')) * dir
        );
      });
  }, [
    runs.data,
    sortKey,
    sortDir,
    harnessFilter,
    modelFilter,
    dateFrom,
    dateTo,
  ]);

  const statuses = useMemo(() => {
    const s = new Set(
      (runs.data?.items ?? []).map((r) => r.status).filter(Boolean),
    );
    return ['running', 'aborted', ...Array.from(s)].filter(
      (v, i, a) => a.indexOf(v) === i,
    );
  }, [runs.data]);

  const harnesses = useMemo(() => {
    const s = new Set(
      (runs.data?.items ?? [])
        .map((r) => r.harness)
        .filter((h): h is string => !!h),
    );
    return Array.from(s).sort();
  }, [runs.data]);

  const models = useMemo(() => {
    const s = new Set(
      (runs.data?.items ?? [])
        .map((r) => r.model_alias)
        .filter((m): m is string => !!m),
    );
    return Array.from(s).sort();
  }, [runs.data]);

  const setSort = (key: SortKey) => {
    if (key === sortKey) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    else {
      setSortKey(key);
      setSortDir(
        key === 'cost_usd_total' || key === 'created_at' ? 'desc' : 'asc',
      );
    }
  };

  const Th = ({ children, k }: { children: string; k?: SortKey }) => (
    <th
      className={cn(
        'px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wide text-zinc-400',
        k &&
          'cursor-pointer select-none hover:text-zinc-600 dark:hover:text-zinc-300',
      )}
      onClick={k ? () => setSort(k) : undefined}
    >
      {children}
      {k === sortKey && (
        <span className="ml-1 text-[10px]">
          {sortDir === 'asc' ? '▲' : '▼'}
        </span>
      )}
    </th>
  );

  const Row = ({ run }: { run: RunItem }) => {
    const flat = flatSummary(run);
    const prov = run.provenance;
    return (
      <tr
        className="cursor-pointer border-t border-zinc-200/70 hover:bg-zinc-50 dark:border-zinc-800/70 dark:hover:bg-zinc-900/50"
        onClick={() => onOpenRun(run.run_id)}
      >
        <td className="px-3 py-2 font-mono text-xs text-sky-700 dark:text-sky-400">
          {run.run_id.slice(0, 16)}
        </td>
        <td className="px-3 py-2 text-xs text-zinc-600 dark:text-zinc-300">
          {fmtTime(run.created_at)}
        </td>
        <td className="px-3 py-2 text-xs text-zinc-400">
          {relativeAge(run.created_at)}
        </td>
        <td className="px-3 py-2">
          <StatusBadge label={run.status} />
        </td>
        <td className="px-3 py-2 text-xs text-zinc-600 dark:text-zinc-300">
          {run.harness ?? '—'}
        </td>
        <td
          className="px-3 py-2 text-xs text-zinc-500 dark:text-zinc-400"
          title={prov?.model_resolved ?? undefined}
        >
          {run.model_alias ?? '—'}
        </td>
        <td className="px-3 py-2 text-center font-mono text-xs text-zinc-500 dark:text-zinc-400">
          {run.instance_count ?? '—'}
        </td>
        {/* resolved over the INSTANCE count (what was launched), not the stored
            summary's `attempted`, which grows with every restart attempt (a
            41-run read 26/45 after four reruns). The list payload carries no
            per-instance state buckets, so the honest gradeable figure lives on
            the run page (lib/rates.ts); here the denominator is the one number
            that cannot drift. */}
        <td className="px-3 py-2 font-mono text-[11px] text-zinc-400">
          {flat.resolved != null || run.instance_count != null
            ? `${flat.resolved ?? 0}/${run.instance_count ?? 0}`
            : '—'}
        </td>
        <td className="px-3 py-2 font-mono text-xs text-zinc-600 dark:text-zinc-300">
          {fmtUsd(run.cost_usd_total)}
        </td>
        <td
          className="px-3 py-2 font-mono text-[11px] text-zinc-400"
          title={prov?.framework_sha ?? undefined}
        >
          {prov?.framework_sha ? shortSha(prov.framework_sha, 8) : '—'}
        </td>
      </tr>
    );
  };

  const selectCls =
    'rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900';

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
        <CardTitle>Runs</CardTitle>
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-[11px] text-zinc-400">status</label>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className={selectCls}
          >
            <option value="">all</option>
            {statuses.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <label className="text-[11px] text-zinc-400">harness</label>
          <select
            value={harnessFilter}
            onChange={(e) => setHarnessFilter(e.target.value)}
            className={selectCls}
          >
            <option value="">all</option>
            {harnesses.map((h) => (
              <option key={h} value={h}>
                {h}
              </option>
            ))}
          </select>
          <label className="text-[11px] text-zinc-400">model</label>
          <select
            value={modelFilter}
            onChange={(e) => setModelFilter(e.target.value)}
            className={selectCls}
          >
            <option value="">all</option>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          <label className="text-[11px] text-zinc-400">from</label>
          <input
            type="date"
            value={dateFrom}
            onChange={(e) => setDateFrom(e.target.value)}
            className={selectCls}
            aria-label="created from"
          />
          <label className="text-[11px] text-zinc-400">to</label>
          <input
            type="date"
            value={dateTo}
            onChange={(e) => setDateTo(e.target.value)}
            className={selectCls}
            aria-label="created to"
          />
          <select
            value={String(limit)}
            onChange={(e) => setLimit(Number(e.target.value))}
            className={selectCls}
            aria-label="page size"
          >
            {[20, 50, 100].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        {runs.isLoading && (
          <div className="p-6 text-sm text-zinc-400">Loading runs…</div>
        )}
        {runs.isError && (
          <div className="p-6 text-sm text-rose-500">
            failed to load runs: {(runs.error as Error).message}
          </div>
        )}
        {runs.data && filtered.length === 0 && (
          <div className="p-6 text-sm text-zinc-400">
            No runs match the current filters.
          </div>
        )}
        {runs.data && filtered.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr className="border-b border-zinc-200 dark:border-zinc-800">
                  <Th k="run_id">Run</Th>
                  <Th k="created_at">Created</Th>
                  <Th>Age</Th>
                  <Th k="status">Status</Th>
                  <Th>Harness</Th>
                  <Th>Model</Th>
                  <Th>Instances</Th>
                  <Th>Resolved</Th>
                  <Th k="cost_usd_total">Cost</Th>
                  <Th>Framework</Th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => (
                  <Row key={r.run_id} run={r} />
                ))}
              </tbody>
            </table>
            <div className="flex items-center justify-between border-t border-zinc-200 px-3 py-2 text-[11px] text-zinc-400 dark:border-zinc-800">
              <span>
                {filtered.length} shown · {runs.data.total} total
                {anyActive.data &&
                  isAnyRunActive(anyActive.data.items) &&
                  ' · live run active — polling on'}
              </span>
              <Freshness updatedAt={runs.dataUpdatedAt} />
            </div>
          </div>
        )}
      </CardContent>
      <div className="px-4 pb-3">
        <SectionLabel>Tip</SectionLabel>
        <p className="mt-0.5 text-[11px] text-zinc-400">
          Click a column header to sort; filter by status, harness, or created
          date. Click a row to open the run. Cost is actual inference spend
          summed across the run's instances.
        </p>
      </div>
    </Card>
  );
}
