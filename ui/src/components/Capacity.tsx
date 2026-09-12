import { useQuery } from '@tanstack/react-query';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { api, type CapacityPoint } from '../lib/api';
import { isAnyRunActive, useAnyRunActive } from '../lib/queries';
import { cn } from '../lib/utils';
import { Freshness } from './Freshness';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  SectionLabel,
} from './ui-primitives';

const POOL_COLORS: Record<string, { line: string; label: string }> = {
  harness: { line: '#0ea5e9', label: 'Harness' },
  eval: { line: '#8b5cf6', label: 'Eval' },
};

// ETAs are median–p90 RANGES, labelled estimates, never points (capacity-view design §5:
// every duration was measured at concurrency ~1, so a fleet-wide figure is a 5–6×
// extrapolation — a confident point would get planned against).
function fmtEtaRange(lowS: number | null | undefined, highS: number | null | undefined) {
  if (lowS == null || highS == null) return null;
  const toMin = (s: number) => Math.max(1, Math.round(s / 60));
  return `${toMin(lowS)}–${toMin(highS)} min (estimated)`;
}

function fmtAge(ageS: number | null | undefined) {
  if (ageS == null) return null;
  if (ageS < 90) return `${Math.round(ageS)}s ago`;
  return `${Math.round(ageS / 60)} min ago`;
}

/** The latest tick's status line for one pool. Unknown must never render as healthy:
 * a null is "not measured", an old decision record says how old it is. */
function PoolStatus({ latest }: { latest: CapacityPoint }) {
  const eta = fmtEtaRange(latest.eta_low_s, latest.eta_high_s);
  const age = fmtAge(latest.decision_age_s);
  const decisionStale = (latest.decision_age_s ?? 0) > 120;
  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-500 dark:text-zinc-400">
      {latest.binding_constraint != null ? (
        <span className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono dark:bg-zinc-800">
          {latest.binding_constraint}
        </span>
      ) : (
        <span className="italic">no autoscaler decision</span>
      )}
      {age != null && (
        <span className={cn(decisionStale && 'text-amber-500')}>
          decision {age}
          {decisionStale && ' (stale)'}
        </span>
      )}
      {latest.ceiling_utilization != null && (
        <span>ceiling {Math.round(latest.ceiling_utilization * 100)}% used</span>
      )}
      {/* §2.5 pacer pressure at the tick: wait-queue depth (max over active aliases) and
          the share of last-60s admissions that waited > 2s. null = not measured. */}
      {latest.pacer_queue_len != null && (
        <span
          className={cn(latest.pacer_queue_len > 0 && 'text-amber-500')}
          title="calls currently denied at the L1 pacer (max over active aliases)"
        >
          pacer queue {latest.pacer_queue_len}
        </span>
      )}
      {latest.paced_over_2s_share != null && (
        <span
          className={cn(latest.paced_over_2s_share > 0.05 && 'text-amber-500')}
          title="share of the last 60s of admissions that waited > 2s (p95 back-pressure proxy)"
        >
          waits&gt;2s {Math.round(latest.paced_over_2s_share * 100)}%
        </span>
      )}
      {/* §2.4: decisions computed from unmeasured constants must LOOK degraded —
          they cannot support the observe→live comparison */}
      {latest.constants_source != null && latest.constants_source !== 'pacer_cfg' && (
        <span
          className={cn(
            'rounded px-1.5 py-0.5 font-medium',
            latest.constants_source === 'defaults'
              ? 'bg-rose-100 text-rose-700 dark:bg-rose-950 dark:text-rose-400'
              : 'bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-400',
          )}
        >
          constants: {latest.constants_source === 'defaults' ? 'GENERIC DEFAULTS (unmeasured)' : latest.constants_source}
        </span>
      )}
      {latest.not_visible != null && latest.not_visible > 0 && (
        <span>{latest.not_visible} in flight (not visible)</span>
      )}
      <span>
        ETA {eta ?? <span className="italic">not measurable</span>}
      </span>
    </div>
  );
}

export function Capacity() {
  // §1a: stop polling entirely when no run is active — a 20 s poll would
  // otherwise defeat Aurora auto-pause (min_capacity = 0, 300 s idle window).
  const anyActive = useAnyRunActive();
  const cap = useQuery({
    queryKey: ['capacity'],
    queryFn: () => api.listCapacity({ limit: 500 }),
    refetchInterval: isAnyRunActive(anyActive.data?.items) ? 20000 : false,
    refetchIntervalInBackground: false,
    staleTime: 15000,
  });

  const points = cap.data?.items ?? [];
  const byPool: Record<string, CapacityPoint[]> = {};
  for (const p of points) {
    if (!byPool[p.pool]) byPool[p.pool] = [];
    byPool[p.pool].push(p);
  }

  // Overall = max of the stages, never a sum (design §2: eval overlaps harness);
  // dominance is the single fact that says where capacity is needed.
  const latestByPool = Object.fromEntries(
    Object.entries(byPool).map(([pool, rows]) => [pool, rows[rows.length - 1]]),
  );
  const stageEtas = Object.entries(latestByPool)
    .filter(([, r]) => r?.eta_high_s != null && r?.eta_low_s != null)
    .map(([pool, r]) => ({ pool, low: r.eta_low_s!, high: r.eta_high_s! }));
  const dominant =
    stageEtas.length === 2
      ? stageEtas.reduce((a, b) => (b.high > a.high ? b : a))
      : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Capacity</CardTitle>
        <p className="flex items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400">
          {points.length} snapshot ticks
          <Freshness updatedAt={cap.dataUpdatedAt} />
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {cap.isLoading && (
          <div className="p-4 text-sm text-zinc-400">Loading capacity…</div>
        )}
        {cap.isError && (
          <div className="p-4 text-sm text-rose-500">
            failed to load capacity: {(cap.error as Error).message}
          </div>
        )}
        {cap.data && points.length === 0 && (
          <div className="p-4 text-sm text-zinc-400">
            No capacity ticks yet (the observation tick in the run-supervisor
            hasn’t written any — it records only while there is activity).
          </div>
        )}
        {cap.data && points.length > 0 && (
          <div className="space-y-6">
            {dominant && (
              <div className="text-xs text-zinc-500 dark:text-zinc-400">
                Overall ETA{' '}
                <span className="font-medium text-zinc-700 dark:text-zinc-300">
                  {fmtEtaRange(dominant.low, dominant.high)}
                </span>{' '}
                — the {POOL_COLORS[dominant.pool]?.label ?? dominant.pool} stage
                dominates (overall is the max of the stages, not their sum)
              </div>
            )}
            {Object.entries(byPool).map(([pool, rows]) => {
              const color = POOL_COLORS[pool]?.line ?? '#64748b';
              const latest = rows[rows.length - 1];
              const data = rows.map((r) => ({
                time: new Date(r.ts).toLocaleTimeString([], {
                  hour: '2-digit',
                  minute: '2-digit',
                }),
                queueDepth: r.queue_depth,
                notVisible: r.not_visible,
                workers: r.current_workers,
                desired: r.desired,
              }));
              return (
                <div key={pool}>
                  <SectionLabel>
                    {POOL_COLORS[pool]?.label ?? pool} pool
                  </SectionLabel>
                  {latest && <PoolStatus latest={latest} />}
                  <div className={cn('mt-1 h-56 w-full')}>
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart
                        data={data}
                        margin={{ top: 5, right: 8, left: -18, bottom: 0 }}
                      >
                        <CartesianGrid
                          strokeDasharray="3 3"
                          stroke="#27272a"
                          strokeOpacity={0.4}
                        />
                        <XAxis dataKey="time" fontSize={10} stroke="#71717a" />
                        <YAxis
                          fontSize={10}
                          stroke="#71717a"
                          allowDecimals={false}
                        />
                        <Tooltip
                          contentStyle={{
                            background: '#18181b',
                            border: '1px solid #3f3f46',
                            borderRadius: 8,
                            fontSize: 12,
                          }}
                        />
                        <Legend wrapperStyle={{ fontSize: 11 }} />
                        <Line
                          type="stepAfter"
                          dataKey="queueDepth"
                          name="queue depth"
                          stroke={color}
                          strokeWidth={2}
                          dot={false}
                        />
                        <Line
                          type="stepAfter"
                          dataKey="notVisible"
                          name="in flight (not visible)"
                          stroke="#10b981"
                          strokeWidth={1.5}
                          dot={false}
                        />
                        <Line
                          type="stepAfter"
                          dataKey="workers"
                          name="current workers"
                          stroke="#f59e0b"
                          strokeWidth={2}
                          dot={false}
                        />
                        <Line
                          type="stepAfter"
                          dataKey="desired"
                          name="desired"
                          stroke="#a1a1aa"
                          strokeWidth={1.5}
                          strokeDasharray="4 3"
                          dot={false}
                        />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
