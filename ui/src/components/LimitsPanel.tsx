import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  api,
  type LimitField,
  type LimitSpec,
  type LimitsView,
  type PacerLimitRow,
} from '../lib/api';
import { fmtNum } from '../lib/format';
import { cn } from '../lib/utils';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  SectionLabel,
} from './ui-primitives';

// Operator limits (owner request 2026-09-04: "a way for me to up the various numbers, or
// bring them down, via the UI — this is the xth iteration and we still do not have it fully
// right"). Three scopes, one audit row per edit (operator_limit_edits):
//
//   run    — the run-overrides hash the dispatcher re-reads every tick: per-run cap, the
//            planner CEILING OVERRIDE (the honest "raise the ceiling" knob — raising the cap
//            alone does nothing while the projection binds), growth step, cooldown, gate.
//   global — operator:limits, read by the planner and the eval scaler every tick: the
//            borrowed-curve cap, the growth clamp, utilisation, eval max workers / scale-in.
//   pacer  — pacer:cfg fields per alias, live on the next admission; seed-relative fields
//            re-base their _seed; "also seed next launch" writes the pool + a seeds row.
//
// House rules: `state` unknown renders as "cannot read", never as defaults; every value shows
// its SOURCE (operator / run_launch / default) so a probe seed, a launch config and an
// operator's override are never confused; every edit is arm-gated and carries actor+reason.

const PACER_FIELDS = [
  'r_tok',
  'k_inflight',
  'r_qps',
  'c_burst',
  'c_req',
  'cached_weight',
];

function fmtValue(spec: LimitSpec, v: number | null | undefined): string {
  if (v == null) return '—';
  if (spec.kind === 'bool') return v ? 'on' : 'off';
  if (spec.kind === 'int') return fmtNum(Math.round(v));
  return Number.isInteger(v) ? fmtNum(v) : String(Math.round(v * 1000) / 1000);
}

function SourceChip({ field }: { field: LimitField }) {
  const tone =
    field.source === 'operator'
      ? 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300'
      : field.source === 'run_launch'
        ? 'bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-300'
        : 'bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300';
  return (
    <span
      className={cn('rounded px-1.5 py-0.5 text-[10px] font-medium', tone)}
      title={
        field.source === 'operator'
          ? `set by ${field.set_by ?? 'an operator'}`
          : field.source === 'run_launch'
            ? 'from the launch config'
            : 'default (env / constant / probe)'
      }
    >
      {field.source === 'run_launch' ? 'launch' : field.source}
    </span>
  );
}

function KnobRow({
  spec,
  field,
  armed,
  busy,
  onSet,
  onClear,
}: {
  spec: LimitSpec;
  field: LimitField;
  armed: boolean;
  busy: boolean;
  onSet: (value: number | boolean) => void;
  onClear: () => void;
}) {
  const [draft, setDraft] = useState('');
  const defaultText =
    spec.default != null
      ? `default ${fmtValue(spec, spec.default)}`
      : spec.default_note || '';
  const range =
    spec.lo != null || spec.hi != null
      ? `${spec.lo != null ? spec.lo : ''}…${spec.hi != null ? spec.hi : ''}`
      : '';
  return (
    <tr className="border-t border-zinc-100 align-top dark:border-zinc-800">
      <td className="py-1.5 pr-3">
        <div className="font-medium">{spec.label}</div>
        <div className="max-w-md text-[11px] leading-snug text-zinc-500">
          {spec.description}
          {spec.read_by && (
            <span className="block text-zinc-400">read by {spec.read_by}</span>
          )}
        </div>
      </td>
      <td className="whitespace-nowrap py-1.5 pr-3 font-mono tabular-nums">
        {fmtValue(spec, field.value)}
        {spec.unit && (
          <span className="ml-1 text-[10px] text-zinc-400">{spec.unit}</span>
        )}
        <div className="mt-0.5 flex items-center gap-1">
          <SourceChip field={field} />
          {defaultText && (
            <span
              className="text-[10px] text-zinc-400"
              title="what applies when cleared"
            >
              {defaultText}
            </span>
          )}
        </div>
      </td>
      <td className="whitespace-nowrap py-1.5">
        <div className="flex items-center gap-1">
          {spec.kind === 'bool' ? (
            <select
              aria-label={`${spec.field} value`}
              className="rounded border border-zinc-300 bg-white px-1 py-0.5 text-xs dark:border-zinc-700 dark:bg-zinc-900"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
            >
              <option value="">—</option>
              <option value="1">on</option>
              <option value="0">off</option>
            </select>
          ) : (
            <input
              aria-label={`${spec.field} value`}
              className="w-28 rounded border border-zinc-300 bg-white px-1 py-0.5 font-mono text-xs dark:border-zinc-700 dark:bg-zinc-900"
              placeholder={range}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              inputMode="decimal"
            />
          )}
          <button
            type="button"
            className="rounded border border-zinc-300 px-2 py-0.5 text-xs disabled:opacity-40 dark:border-zinc-700"
            disabled={
              !armed || busy || draft === '' || Number.isNaN(Number(draft))
            }
            onClick={() => {
              onSet(spec.kind === 'bool' ? draft === '1' : Number(draft));
              setDraft('');
            }}
          >
            set
          </button>
          {field.source !== 'default' && (
            <button
              type="button"
              className="rounded border border-zinc-300 px-2 py-0.5 text-xs disabled:opacity-40 dark:border-zinc-700"
              disabled={!armed || busy}
              title="remove the override; the default applies again"
              onClick={onClear}
            >
              clear
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}

function PacerRows({
  row,
  specs,
  armed,
  busy,
  onSet,
}: {
  row: PacerLimitRow;
  specs: LimitSpec[];
  armed: boolean;
  busy: boolean;
  onSet: (
    alias: string,
    field: string,
    value: number,
    alsoPool: boolean,
  ) => void;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [alsoPool, setAlsoPool] = useState(true);
  return (
    <div className="rounded border border-zinc-200 p-2 dark:border-zinc-800">
      <div className="flex flex-wrap items-baseline gap-2 text-xs">
        <span className="font-medium">
          {row.harness} · <span className="font-mono">{row.alias}</span>
        </span>
        {row.pool && (
          <span className="text-zinc-500">
            pool <span className="font-mono">{row.pool}</span>
          </span>
        )}
        <label className="ml-auto flex items-center gap-1 text-[11px] text-zinc-600 dark:text-zinc-300">
          <input
            type="checkbox"
            aria-label={`also seed next launch for ${row.alias}`}
            checked={alsoPool}
            onChange={(e) => setAlsoPool(e.target.checked)}
            disabled={!row.pool}
          />
          also seed next launch (writes the pool + a pacer_cfg_seeds row)
        </label>
      </div>
      <table className="mt-1 w-full text-xs">
        <thead className="text-[10px] uppercase tracking-wide text-zinc-400">
          <tr>
            <th className="py-1 text-left font-normal">field</th>
            <th className="py-1 text-left font-normal">alias (live)</th>
            <th className="py-1 text-left font-normal">seed</th>
            <th className="py-1 text-left font-normal">pool</th>
            <th className="py-1 text-left font-normal">new value</th>
          </tr>
        </thead>
        <tbody>
          {PACER_FIELDS.map((f) => {
            const spec = specs.find(
              (s) => s.scope === 'pacer' && s.field === f,
            );
            if (!spec) return null;
            const seed = row.cfg[`${f}_seed`];
            const draft = drafts[f] ?? '';
            return (
              <tr
                key={f}
                className="border-t border-zinc-100 dark:border-zinc-800"
              >
                <td className="py-1 pr-2" title={spec.description}>
                  <span className="font-mono">{f}</span>
                  {spec.unit && (
                    <span className="ml-1 text-[10px] text-zinc-400">
                      {spec.unit}
                    </span>
                  )}
                </td>
                <td className="py-1 pr-2 font-mono tabular-nums">
                  {fmtValue(spec, row.cfg[f])}
                </td>
                <td className="py-1 pr-2 font-mono tabular-nums text-zinc-500">
                  {seed === undefined ? '' : fmtValue(spec, seed)}
                </td>
                <td className="py-1 pr-2 font-mono tabular-nums text-zinc-500">
                  {row.pool_cfg ? fmtValue(spec, row.pool_cfg[f]) : '—'}
                </td>
                <td className="py-1">
                  <div className="flex items-center gap-1">
                    <input
                      aria-label={`${row.alias} ${f} value`}
                      className="w-28 rounded border border-zinc-300 bg-white px-1 py-0.5 font-mono text-xs dark:border-zinc-700 dark:bg-zinc-900"
                      value={draft}
                      onChange={(e) =>
                        setDrafts({ ...drafts, [f]: e.target.value })
                      }
                      inputMode="decimal"
                    />
                    <button
                      type="button"
                      className="rounded border border-zinc-300 px-2 py-0.5 text-xs disabled:opacity-40 dark:border-zinc-700"
                      disabled={
                        !armed ||
                        busy ||
                        draft === '' ||
                        Number.isNaN(Number(draft))
                      }
                      onClick={() => {
                        onSet(
                          row.alias,
                          f,
                          Number(draft),
                          alsoPool && !!row.pool,
                        );
                        setDrafts({ ...drafts, [f]: '' });
                      }}
                    >
                      set
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function LimitsPanel({ runId }: { runId?: string }) {
  const qc = useQueryClient();
  const [actor, setActor] = useState('operator');
  const [reason, setReason] = useState('');
  const [armed, setArmed] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  const [lastEdit, setLastEdit] = useState<string | null>(null);

  const limits = useQuery({
    queryKey: ['limits', runId ?? null],
    queryFn: () => api.getLimits(runId),
    refetchInterval: 10_000,
    refetchIntervalInBackground: false,
  });

  const afterEdit = (what: string) => {
    setLastError(null);
    setLastEdit(what);
    qc.invalidateQueries({ queryKey: ['limits'] });
    qc.invalidateQueries({ queryKey: ['run-pacer'] });
    qc.invalidateQueries({ queryKey: ['autoscaler'] });
  };
  const onError = (e: unknown) =>
    setLastError(e instanceof Error ? e.message : String(e));

  const setRun = useMutation({
    mutationFn: ({
      field,
      value,
    }: {
      field: string;
      value: number | boolean | null;
    }) => api.setRunLimit(field, value, actor.trim() || 'operator', reason),
    onSuccess: (r) =>
      afterEdit(`run ${r.field}: ${r.old ?? '—'} → ${r.new ?? 'cleared'}`),
    onError,
  });
  const setGlobal = useMutation({
    mutationFn: ({
      field,
      value,
    }: {
      field: string;
      value: number | boolean | null;
    }) => api.setGlobalLimit(field, value, actor.trim() || 'operator', reason),
    onSuccess: (r) =>
      afterEdit(`global ${r.field}: ${r.old ?? '—'} → ${r.new ?? 'cleared'}`),
    onError,
  });
  const setPacer = useMutation({
    mutationFn: ({
      alias,
      field,
      value,
      alsoPool,
    }: {
      alias: string;
      field: string;
      value: number;
      alsoPool: boolean;
    }) =>
      api.setPacerLimit(
        alias,
        field,
        value,
        actor.trim() || 'operator',
        reason,
        alsoPool,
      ),
    onSuccess: (r) =>
      afterEdit(
        `pacer ${r.target} ${r.field}: ${r.old ?? '—'} → ${r.new}${r.pool ? ` (+ pool ${r.pool})` : ''}`,
      ),
    onError,
  });
  const busy = setRun.isPending || setGlobal.isPending || setPacer.isPending;

  const view: LimitsView | undefined = limits.data;
  const specs = view?.specs ?? [];
  const runSpecs = specs.filter((s) => s.scope === 'run');
  const globalSpecs = specs.filter((s) => s.scope === 'global');

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>
          Operator limits{' '}
          <span className="text-xs font-normal text-zinc-500">
            {view?.state === 'ok'
              ? 'live — edits land within one tick'
              : view?.state === 'unknown'
                ? 'cannot read (Redis unreachable)'
                : limits.isError
                  ? 'unavailable'
                  : 'loading'}
          </span>
        </CardTitle>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <input
            aria-label="limits actor"
            className="w-24 rounded border border-zinc-300 bg-white px-1 py-0.5 dark:border-zinc-700 dark:bg-zinc-900"
            value={actor}
            onChange={(e) => setActor(e.target.value)}
            placeholder="actor"
          />
          <input
            aria-label="limits reason"
            className="w-56 rounded border border-zinc-300 bg-white px-1 py-0.5 dark:border-zinc-700 dark:bg-zinc-900"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="reason (audit)"
          />
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              aria-label="arm limit edits"
              checked={armed}
              onChange={(e) => setArmed(e.target.checked)}
            />
            arm edits
          </label>
        </div>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {lastError && (
          <div className="rounded border border-red-300 bg-red-50 px-2 py-1 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300">
            {lastError}
          </div>
        )}
        {lastEdit && !lastError && (
          <div className="text-xs text-zinc-500">last edit: {lastEdit}</div>
        )}

        {view?.state === 'ok' && view.run && view.global_ && (
          <>
            <div>
              <SectionLabel>
                Run{' '}
                <span className="font-mono normal-case">
                  {view.run.run_id ?? '(no launch has published overrides yet)'}
                </span>
              </SectionLabel>
              <table className="mt-1 w-full text-xs">
                <tbody>
                  {runSpecs.map((spec) => (
                    <KnobRow
                      key={spec.field}
                      spec={spec}
                      field={
                        view.run!.fields[spec.field] ?? {
                          value: null,
                          source: 'default',
                        }
                      }
                      armed={armed}
                      busy={busy}
                      onSet={(value) =>
                        setRun.mutate({ field: spec.field, value })
                      }
                      onClear={() =>
                        setRun.mutate({ field: spec.field, value: null })
                      }
                    />
                  ))}
                </tbody>
              </table>
            </div>

            <div>
              <SectionLabel>Planner and eval scaler (global)</SectionLabel>
              <table className="mt-1 w-full text-xs">
                <tbody>
                  {globalSpecs.map((spec) => (
                    <KnobRow
                      key={spec.field}
                      spec={spec}
                      field={
                        view.global_!.fields[spec.field] ?? {
                          value: null,
                          source: 'default',
                        }
                      }
                      armed={armed}
                      busy={busy}
                      onSet={(value) =>
                        setGlobal.mutate({ field: spec.field, value })
                      }
                      onClear={() =>
                        setGlobal.mutate({ field: spec.field, value: null })
                      }
                    />
                  ))}
                </tbody>
              </table>
            </div>

            {view.pacer.length > 0 && (
              <div className="space-y-2">
                <SectionLabel>
                  Pacer (pacer:cfg per alias — live on the next admission)
                </SectionLabel>
                {view.pacer.map((row) => (
                  <PacerRows
                    key={row.alias}
                    row={row}
                    specs={specs}
                    armed={armed}
                    busy={busy}
                    onSet={(alias, field, value, alsoPool) =>
                      setPacer.mutate({ alias, field, value, alsoPool })
                    }
                  />
                ))}
              </div>
            )}

            <div className="text-[11px] text-zinc-500">
              <span className="font-medium">
                Static rails (terraform only):
              </span>{' '}
              harness hard cap{' '}
              <span className="font-mono">
                {fmtNum(
                  view.static.max_concurrent_harness_tasks as number | null,
                )}
              </span>{' '}
              · eval ASG max{' '}
              <span className="font-mono">
                {fmtNum(view.static.eval_asg_max as number | null)}
              </span>{' '}
              · eval max workers (env){' '}
              <span className="font-mono">
                {fmtNum(view.static.eval_max_workers_env as number | null)}
              </span>
              . Every edit writes one audit row (actor, reason, old → new).
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
