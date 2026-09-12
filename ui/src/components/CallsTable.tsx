import { useQuery } from '@tanstack/react-query';
import { api, type InstanceCall } from '../lib/api';
import { fmtNum, fmtUsd } from '../lib/format';
import { cn } from '../lib/utils';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  SectionLabel,
} from './ui-primitives';

// GET /runs/{run_id}/instances/{instance_id}/{attempt}/calls — the attempt's llm_calls rows
// (BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03.md §2.6). One row per model call, with the
// complete wall-clock decomposition:
//
//   wall ≡ preflight + PACER WAIT + retried round-trips + backoff + final-attempt latency
//
// so "this instance was slow" resolves to "the provider was slow" vs "we held it at the
// pacer" vs "it burned 429 retries" — three different fixes. A 429 with error_type
// pacer_hold_cap_exceeded and latency 0 is OUR pacer refusing the call (it never went
// upstream); a provider 429 carries rate_limit_scope=provider. NULL renders as '—'.

function ms(v: number | null | undefined): string {
  if (v == null) return '—';
  if (v >= 10_000) return `${(v / 1000).toFixed(1)}s`;
  return `${v}ms`;
}

function Row({ c }: { c: InstanceCall }) {
  const paced = c.paced_wait_ms ?? 0;
  const status = c.http_status ?? 0;
  const pacerRefusal = c.error_type === 'pacer_hold_cap_exceeded';
  const tone = pacerRefusal
    ? 'bg-rose-50/60 dark:bg-rose-950/20'
    : status >= 400
      ? 'bg-amber-50/60 dark:bg-amber-950/20'
      : paced >= 5000
        ? 'bg-amber-50/30 dark:bg-amber-950/10'
        : '';
  return (
    <tr className={cn('border-t border-zinc-200/70 dark:border-zinc-800/70', tone)}>
      <td className="px-2 py-1 font-mono text-[11px] text-zinc-500">{c.call_index}</td>
      <td className="px-2 py-1 font-mono text-[11px] text-zinc-500">
        {c.started_at ? new Date(c.started_at).toLocaleTimeString() : '—'}
      </td>
      <td
        className={cn(
          'px-2 py-1 font-mono text-[11px]',
          status >= 400 && 'text-rose-600 dark:text-rose-400',
        )}
        title={c.error_type ?? undefined}
      >
        {c.http_status ?? '—'}
        {pacerRefusal && <span className="text-[9px] uppercase"> pacer</span>}
        {!pacerRefusal && c.rate_limit_scope && (
          <span className="text-[9px] uppercase text-zinc-400"> {c.rate_limit_scope}</span>
        )}
      </td>
      <td className="px-2 py-1 font-mono text-[11px]">{ms(c.shim_preflight_ms)}</td>
      <td
        className={cn(
          'px-2 py-1 font-mono text-[11px]',
          paced >= 5000 && 'font-medium text-amber-700 dark:text-amber-400',
        )}
        title={
          c.pacer_was_queued
            ? `denied at least once — last on the "${c.pacer_deny_axis ?? '?'}" axis, queue ${c.pacer_queue_len ?? '?'} at admission`
            : c.pacer_was_queued === false
              ? 'admitted without waiting'
              : undefined
        }
      >
        {ms(c.paced_wait_ms)}
        {c.pacer_was_queued && (
          <span className="text-zinc-400"> ·{c.pacer_deny_axis ?? 'q'}</span>
        )}
      </td>
      <td className="px-2 py-1 font-mono text-[11px]">
        {c.overload_retries == null ? '—' : c.overload_retries}
      </td>
      <td className="px-2 py-1 font-mono text-[11px]">{ms(c.overload_backoff_ms)}</td>
      <td className="px-2 py-1 font-mono text-[11px]">{ms(c.retry_upstream_ms)}</td>
      <td className="px-2 py-1 font-mono text-[11px]">{ms(c.ttft_ms)}</td>
      <td className="px-2 py-1 font-mono text-[11px]">{ms(c.latency_ms)}</td>
      <td className="px-2 py-1 font-mono text-[11px]" title={`cached ${fmtNum(c.cached_tokens)}`}>
        {fmtNum(c.input_tokens)}
      </td>
      <td className="px-2 py-1 font-mono text-[11px]">{fmtNum(c.output_tokens)}</td>
      <td className="px-2 py-1 font-mono text-[11px]">{fmtUsd(c.cost_usd)}</td>
    </tr>
  );
}

export function CallsTable({
  runId,
  instanceId,
  attempt,
  active,
}: {
  runId: string;
  instanceId: string;
  attempt: number;
  /** while the attempt is in flight the rows cannot exist yet — poll slowly, say so. */
  active: boolean;
}) {
  const q = useQuery({
    queryKey: ['instance-calls', runId, instanceId, attempt],
    queryFn: () => api.getInstanceCalls(runId, instanceId, attempt),
    refetchInterval: () => (active ? 30_000 : false),
    refetchIntervalInBackground: false,
    staleTime: 10_000,
    enabled: Boolean(runId && instanceId),
  });
  const items = q.data?.items ?? [];
  const totals = items.reduce(
    (t, c) => ({
      paced: t.paced + (c.paced_wait_ms ?? 0),
      backoff: t.backoff + (c.overload_backoff_ms ?? 0),
      retryUp: t.retryUp + (c.retry_upstream_ms ?? 0),
      latency: t.latency + (c.latency_ms ?? 0),
      refusals: t.refusals + (c.error_type === 'pacer_hold_cap_exceeded' ? 1 : 0),
    }),
    { paced: 0, backoff: 0, retryUp: 0, latency: 0, refusals: 0 },
  );

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle>Calls — wall-clock decomposition</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {q.isLoading && <div className="text-sm text-zinc-400">Loading calls…</div>}
        {q.isError && (
          <div className="text-sm text-rose-500">
            failed to load calls: {(q.error as Error).message}
          </div>
        )}
        {q.data && items.length === 0 && (
          <div className="text-sm text-zinc-400">
            {active
              ? 'No call rows yet — they land after the attempt finishes (see Live progress and the pacer ledger for now).'
              : 'No llm_calls rows for this attempt.'}
          </div>
        )}
        {items.length > 0 && (
          <>
            <div className="flex flex-wrap gap-x-5 gap-y-1 font-mono text-xs">
              <span>
                <span className="text-[10px] uppercase text-zinc-400">calls </span>
                {items.length}
              </span>
              <span
                className={cn(totals.paced >= 5000 && 'text-amber-700 dark:text-amber-400')}
              >
                <span className="text-[10px] uppercase text-zinc-400">pacer wait Σ </span>
                {ms(totals.paced)}
              </span>
              <span>
                <span className="text-[10px] uppercase text-zinc-400">429 backoff Σ </span>
                {ms(totals.backoff)}
              </span>
              <span>
                <span className="text-[10px] uppercase text-zinc-400">retried round-trips Σ </span>
                {ms(totals.retryUp)}
              </span>
              <span>
                <span className="text-[10px] uppercase text-zinc-400">upstream latency Σ </span>
                {ms(totals.latency)}
              </span>
              {totals.refusals > 0 && (
                <span className="text-rose-600 dark:text-rose-400">
                  <span className="text-[10px] uppercase">pacer refusals </span>
                  {totals.refusals}
                </span>
              )}
            </div>
            <div className="overflow-x-auto">
              <table className="w-full border-collapse">
                <thead>
                  <tr className="border-b border-zinc-200 text-[10px] uppercase tracking-wide text-zinc-400 dark:border-zinc-800">
                    <th className="px-2 py-1 text-left">#</th>
                    <th className="px-2 py-1 text-left">started</th>
                    <th className="px-2 py-1 text-left">status</th>
                    <th className="px-2 py-1 text-left">preflight</th>
                    <th className="px-2 py-1 text-left">pacer wait</th>
                    <th className="px-2 py-1 text-left">429 retries</th>
                    <th className="px-2 py-1 text-left">backoff</th>
                    <th className="px-2 py-1 text-left">retried rtt</th>
                    <th className="px-2 py-1 text-left">ttft</th>
                    <th className="px-2 py-1 text-left">latency</th>
                    <th className="px-2 py-1 text-left">in tok</th>
                    <th className="px-2 py-1 text-left">out tok</th>
                    <th className="px-2 py-1 text-left">cost</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((c) => (
                    <Row key={c.call_index} c={c} />
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
        <SectionLabel className="pt-1">
          wall ≡ preflight + pacer wait + retried round-trips + backoff + latency (final
          attempt). A "pacer" 429 with latency 0 never reached the provider — that is our
          own admission control refusing it at the hold cap.
        </SectionLabel>
      </CardContent>
    </Card>
  );
}
