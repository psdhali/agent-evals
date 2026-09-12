import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type ModelCeiling } from '../lib/api';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  SectionLabel,
} from './ui-primitives';

/**
 * Model ceiling discovery — BUILDER4-HARNESS-AUTOSCALER-EXACT-DESIGN-2026-09-01.md §6.
 *
 * MANUAL ONLY: this panel is the single trigger for the discovery probe. It is real spend
 * that deliberately drives a shared provider pool to its admission edge, so the Discover
 * button is confirm-gated behind the live cost estimate, per model, every time — a
 * per-invocation sign-off, never a blanket approval (reviewer F5, resolved structurally).
 *
 * The value display follows the house rule: unknown must never render as healthy — no
 * observation renders as "never measured", a stale one as an explicit stale state that a
 * run will NOT use (it falls back to the historical floor), never as a quiet number.
 */

const VALUE_LABELS: Record<string, string> = {
  burst_admission_tokens: 'burst edge (tok)',
  // BUILDER4-PACER-SEED-AND-FAIRNESS §2.1: the ramp's seed basis (what r_tok is seeded
  // from — the highest clean / 0.85x soft-strain / 1.0x top rate), the rate at which the
  // ramp saw strain (0 = none seen), and Phase A's measured max-context call latency (the
  // "one latency of service" the ramp starts from).
  paced_rate_tok_per_s: 'r_tok basis (tok/s)',
  ramp_strain_rate_tok_per_s: 'ramp strain rate (tok/s, 0 = none)',
  call_latency_ms_max_context: 'max-ctx call latency (ms)',
  req_burst_admitted: 'req burst admitted',
  req_qps_clean: 'clean QPS ×100',
  // F3 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): Phase D's cache-hit axis — the clean
  // rate for a mostly-cached stream (0 = not measured / the provider never proved its hits)
  // and the seeded cached-token weight ×1000 (1000 = cached tokens at full price).
  cached_rate_tok_per_s: 'cached-prefix clean rate (tok/s, 0 = n/a)',
  cached_weight_x1000: 'cached-token weight ×1000',
  tpm: 'legacy tpm',
};

function ago(iso: string | null | undefined): string {
  if (!iso) return '';
  const ms = Date.now() - new Date(iso).getTime();
  const days = Math.floor(ms / 86_400_000);
  if (days > 0) return `${days}d ago`;
  const hours = Math.floor(ms / 3_600_000);
  if (hours > 0) return `${hours}h ago`;
  return `${Math.max(0, Math.floor(ms / 60_000))}m ago`;
}

function CeilingRow({ row }: { row: ModelCeiling }) {
  const qc = useQueryClient();
  const [armed, setArmed] = useState(false);
  const [manualValue, setManualValue] = useState('');
  const target = 150; // the fleet_target the request-axis phase sizes against (§6.7 default)
  // 2026-09-05 (owner): target-first ramp. The probe's first paced step offers the rate
  // `targetTasks` agent tasks need; a clean step ends the ramp, strain steps down 25 %.
  // 60 = twice the borrowed-curve cap — the most one 60-call step can offer.
  const [targetTasks, setTargetTasks] = useState(60);
  const [rampMode, setRampMode] = useState<'target_first' | 'bottom_up'>(
    'target_first',
  );

  const estimate = useQuery({
    queryKey: ['ceiling-estimate', row.model_alias, targetTasks, rampMode],
    queryFn: () =>
      api.estimateDiscovery(row.model_alias, target, targetTasks, rampMode),
    enabled: armed, // only fetch the preview once the operator starts arming
    staleTime: 300_000,
  });

  const discover = useMutation({
    mutationFn: () =>
      api.discoverCeiling(row.model_alias, target, targetTasks, rampMode),
    onSuccess: () => {
      setArmed(false);
      // The completion signal IS a fresh discovered_at — poll the list.
      qc.invalidateQueries({ queryKey: ['model-ceilings'] });
    },
  });

  const manual = useMutation({
    mutationFn: () => api.manualCeiling(row.model_alias, Number(manualValue)),
    onSuccess: () => {
      setManualValue('');
      qc.invalidateQueries({ queryKey: ['model-ceilings'] });
    },
  });

  const hasValue = row.discovered_tpm != null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <span>{row.model_alias}</span>
          {row.provider && (
            <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
              {row.provider}
            </span>
          )}
          {row.is_stale && (
            <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
              stale — not in use, re-discover recommended
            </span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {hasValue ? (
          <div>
            <div className="flex items-baseline gap-2">
              <span className="text-lg font-semibold tabular-nums">
                {row.discovered_tpm?.toLocaleString()}
              </span>
              <span className="text-xs text-zinc-500">
                burst-admission tokens · {row.ceiling_source} ·{' '}
                {ago(row.discovered_at)}
              </span>
            </div>
            {row.values && Object.keys(row.values).length > 1 && (
              <dl className="mt-1 grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs text-zinc-500 sm:grid-cols-4">
                {Object.entries(row.values)
                  .filter(([k]) => k !== 'burst_admission_tokens')
                  .map(([k, v]) => (
                    <div key={k} className="flex justify-between gap-2">
                      <dt>{VALUE_LABELS[k] ?? k}</dt>
                      <dd className="tabular-nums">{v.toLocaleString()}</dd>
                    </div>
                  ))}
              </dl>
            )}
          </div>
        ) : (
          <p className="text-xs text-zinc-500">
            never measured — runs fall back to the historical floor until
            discovery runs
          </p>
        )}

        <div className="flex flex-wrap items-center gap-3 border-t border-zinc-100 pt-3 dark:border-zinc-800">
          <label className="flex items-center gap-1.5 text-xs">
            <span>target tasks</span>
            <input
              type="number"
              min={1}
              max={500}
              aria-label={`${row.model_alias} target tasks`}
              value={targetTasks}
              onChange={(e) =>
                setTargetTasks(Math.max(1, Number(e.target.value) || 1))
              }
              className="w-16 rounded border border-zinc-300 px-1 py-0.5 text-xs tabular-nums dark:border-zinc-700 dark:bg-zinc-900"
            />
          </label>
          <label className="flex items-center gap-1.5 text-xs">
            <span>ramp</span>
            <select
              aria-label={`${row.model_alias} ramp mode`}
              value={rampMode}
              onChange={(e) =>
                setRampMode(e.target.value as 'target_first' | 'bottom_up')
              }
              className="rounded border border-zinc-300 px-1 py-0.5 text-xs dark:border-zinc-700 dark:bg-zinc-900"
            >
              <option value="target_first">target-first (step down on strain)</option>
              <option value="bottom_up">bottom-up (×1.5 per step)</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-xs">
            <input
              type="checkbox"
              checked={armed}
              onChange={(e) => setArmed(e.target.checked)}
            />
            <span>
              {armed && estimate.data
                ? `confirm ~$${estimate.data.estimated_cost_usd.toFixed(2)} of real probe spend`
                : 'arm discovery (real spend)'}
            </span>
          </label>
          <button
            type="button"
            disabled={
              !armed || discover.isPending || (armed && estimate.isLoading)
            }
            onClick={() => discover.mutate()}
            className="rounded-md bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900"
          >
            {discover.isPending
              ? 'starting…'
              : hasValue
                ? 'Re-discover (force)'
                : 'Discover'}
          </button>
          {discover.isSuccess && (
            <span className="text-xs text-emerald-600 dark:text-emerald-400">
              started — a fresh timestamp above is the completion signal
            </span>
          )}
          {discover.isError && (
            <span className="text-xs text-red-600">
              {String(discover.error)}
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <input
            type="number"
            placeholder="manual burst-edge tokens"
            value={manualValue}
            onChange={(e) => setManualValue(e.target.value)}
            className="w-52 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs dark:border-zinc-700 dark:bg-zinc-900"
          />
          <button
            type="button"
            disabled={!manualValue || manual.isPending}
            onClick={() => manual.mutate()}
            className="rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium disabled:opacity-40 dark:border-zinc-700"
          >
            Save manual value
          </button>
          {manual.isError && (
            <span className="text-xs text-red-600">{String(manual.error)}</span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export function ModelCeilings() {
  const ceilings = useQuery({
    queryKey: ['model-ceilings'],
    queryFn: () => api.listModelCeilings(),
    refetchInterval: 30_000, // a fresh discovered_at is discovery's completion signal
  });

  return (
    <section className="space-y-3">
      <SectionLabel>Model ceilings (discovery is manual-only)</SectionLabel>
      {ceilings.isLoading && (
        <p className="text-xs text-zinc-500">loading ceilings…</p>
      )}
      {ceilings.isError && (
        <p className="text-xs text-red-600">
          could not load model ceilings — state unknown, not healthy
        </p>
      )}
      {ceilings.data?.map((row) => (
        <CeilingRow key={row.model_alias} row={row} />
      ))}
    </section>
  );
}
