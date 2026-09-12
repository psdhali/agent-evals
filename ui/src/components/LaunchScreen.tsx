import { useMutation, useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import {
  api,
  type LaunchResult,
  type RunLaunchRequest,
  type RunLaunchResponse,
} from '../lib/api';
import { cn } from '../lib/utils';
import { Freshness } from './Freshness';
import { Card, CardContent, CardHeader, CardTitle } from './ui-primitives';

// The run-launch form (§2 of the brief).  Two hard rules govern the request
// body, both owned by the backend contract (builder 4's schemas.py):
//   - budget_cap_usd has NO default — the operator must choose it.  It is the
//     only thing that bounds a run: it is minted onto the per-run LiteLLM key
//     (`max_budget`) and the per-run OpenRouter key (`limit_usd`), i.e.
//     enforced by the provider, not by our arithmetic.
//   - never send `context_window_tokens` unless the operator deliberately
//     overrides it, and never send `max_tokens_per_instance` at all.  An
//     explicit context window is exactly what set `context_window_source =
//     'run_config'` on every prior run (the live gateway resolution has never
//     run in production); a token cap aborts nothing by owner decision.

// The instance field is a real list from day one, but not 500-row UX — a
// filterable multi-select plus the literal "all" option is enough (phase 2
// adds filters, saved sets, "select all in repo").  `"all"` is sent as the
// literal string and resolved server-side; we never expand it client-side.

type InstanceSelection = { mode: 'all' } | { mode: 'ids'; ids: string[] };

interface LaunchScreenProps {
  onOpenRun: (runId: string) => void;
}

// This screen keeps the form MOUNTED after a successful launch.  The e2e runs
// five harnesses over the same six instances; auto-navigating to the run on
// launch (App's onLaunched → setView) unmounts it and loses the selection,
// forcing the operator to re-pick the same six ids by hand each time — a live
// chance to mis-select one and silently break cross-harness comparability.
// So a launch shows the run_id here with an explicit "open this run" action;
// the form (and its selection) survives until the operator navigates.
export function LaunchScreen({ onOpenRun }: LaunchScreenProps) {
  // ---- catalog queries (all live; /models 503 → gateway unreachable) ----
  const instances = useQuery({
    queryKey: ['dataset', 'instances'],
    queryFn: () => api.listDatasetInstances({ limit: 1000, offset: 0 }),
    staleTime: 60_000,
  });
  const harnesses = useQuery({
    queryKey: ['harnesses'],
    queryFn: api.listHarnesses,
    staleTime: 60_000,
  });
  const models = useQuery({
    queryKey: ['models'],
    queryFn: api.listModels,
    staleTime: 30_000,
    // No retry — a gateway 503 must fail loud (render "unreachable"), not sit
    // in `pending` until a retry gives up.  An empty dropdown reads as "no
    // models exist", which is the wrong and dangerous statement.
  });

  // ---- form state ----
  const [instanceSel, setInstanceSel] = useState<InstanceSelection>({
    mode: 'ids',
    ids: [],
  });
  const [harness, setHarness] = useState('');
  const [modelAlias, setModelAlias] = useState('');
  const [budgetCap, setBudgetCap] = useState('');
  const [maxCostPerInst, setMaxCostPerInst] = useState('5.0');
  const [maxTurns, setMaxTurns] = useState('500');
  const [attempts, setAttempts] = useState('1');
  const [timeoutSec, setTimeoutSec] = useState('5400');
  // Autoscaler per-run overrides (harness-autoscaler exact-design §8 / §6.7). Defaults
  // mirror run_config.py; ramp step above 5 is clamped server-side (owner-fixed max).
  const [maxParallel, setMaxParallel] = useState('150');
  const [rampStepPct, setRampStepPct] = useState('5');
  const [cooldownSec, setCooldownSec] = useState('60');
  // 2026-09-09 efficiency prompt arm: operator text the dispatcher appends to every job's
  // problem statement (advice, not a limit; recorded on the run). Empty = plain run.
  const [harnessInstructions, setHarnessInstructions] = useState('');
  const presets = useQuery({
    queryKey: ['instruction-presets'],
    queryFn: api.instructionPresets,
    staleTime: Infinity,
  });
  const [autoscalerEnabled, setAutoscalerEnabled] = useState(true);
  const [filter, setFilter] = useState('');
  // Only show instances that have a built image (launchable = true).  The
  // e2e picks from the few built images, and only those actually dispatch —
  // this cuts the 500-row list down to the ones that will run.
  const [launchableOnly, setLaunchableOnly] = useState(false);
  // The most recent 201 — rendered as a banner with an "open this run" action
  // rather than navigating away, so the form's selection survives a launch
  // (§2: the e2e is five runs over the same six instances).
  const [lastLaunch, setLastLaunch] = useState<RunLaunchResponse | null>(null);

  const allInstances = useMemo(
    () => instances.data?.items ?? [],
    [instances.data],
  );
  const nonLaunchable = useMemo(
    () => allInstances.filter((i) => !i.launchable),
    [allInstances],
  );

  // client-side filter by repo or instance_id substring (phase 2 adds the
  // richer controls; this is the usable single/multi entry).  When
  // `launchableOnly` is on, also hide instances with no built image.
  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return allInstances.filter((i) => {
      if (launchableOnly && !i.launchable) return false;
      if (!q) return true;
      return (
        i.instance_id.toLowerCase().includes(q) ||
        i.repo.toLowerCase().includes(q)
      );
    });
  }, [allInstances, filter, launchableOnly]);

  const toggleInstance = (id: string) => {
    setInstanceSel((sel) => {
      if (sel.mode === 'all') return sel; // selecting while in "all" mode is a no-op
      const has = sel.ids.includes(id);
      return {
        mode: 'ids',
        ids: has ? sel.ids.filter((x) => x !== id) : [...sel.ids, id],
      };
    });
  };

  // "select shown" / "clear shown" act on the CURRENT filtered view (text
  // filter + "with image only"), so the operator can pick e.g. the 41 built
  // images in one click and still send an explicit id list — never the
  // literal "all", which would dispatch the image-less rows too (owner
  // request 2026-09-05).
  const selectShown = () => {
    const ids = filtered.map((i) => i.instance_id);
    setInstanceSel((sel) => {
      if (sel.mode === 'all') return sel;
      const merged = new Set([...sel.ids, ...ids]);
      return { mode: 'ids', ids: [...merged] };
    });
  };
  const clearShown = () => {
    const shown = new Set(filtered.map((i) => i.instance_id));
    setInstanceSel((sel) => {
      if (sel.mode === 'all') return sel;
      return { mode: 'ids', ids: sel.ids.filter((id) => !shown.has(id)) };
    });
  };
  // 2026-09-09: "paste ids" — an explicit list pasted from a file (the efficiency
  // experiments re-run the SAME 78 pilot instances several times; clicking 78 rows
  // per launch is a mis-selection waiting to happen). Whitespace/comma separated;
  // unknown ids are reported, never silently dropped into the body.
  const [pasteIds, setPasteIds] = useState('');
  const [pasteReport, setPasteReport] = useState<string | null>(null);
  const selectPasted = () => {
    const tokens = pasteIds
      .split(/[\s,]+/)
      .map((t) => t.trim())
      .filter(Boolean);
    const known = new Set(allInstances.map((i) => i.instance_id));
    const found = [...new Set(tokens.filter((t) => known.has(t)))];
    const unknown = [...new Set(tokens.filter((t) => !known.has(t)))];
    setInstanceSel((sel) => {
      if (sel.mode === 'all') return sel;
      return { mode: 'ids', ids: [...new Set([...sel.ids, ...found])] };
    });
    setPasteReport(
      `${found.length} selected` +
        (unknown.length
          ? `; ${unknown.length} unknown id(s) ignored: ${unknown.slice(0, 5).join(', ')}${unknown.length > 5 ? '…' : ''}`
          : ''),
    );
  };
  const shownSelected = filtered.filter((i) =>
    instanceSel.mode === 'ids' ? instanceSel.ids.includes(i.instance_id) : true,
  ).length;

  const selectedCount =
    instanceSel.mode === 'all' ? allInstances.length : instanceSel.ids.length;

  // ---- gold validation (image-parity Part B) ----
  // Grades the dataset's GOLD patch in each selected instance's -inst image
  // under the synthetic run "image-validation" — the gate an image must pass
  // before it is trusted for a scored run. No inference, no confirm step.
  const validate = useMutation({
    mutationFn: () =>
      api.validateImages(
        instanceSel.mode === 'all'
          ? allInstances.map((i) => i.instance_id)
          : instanceSel.ids,
        'ui',
      ),
  });

  // ---- submit ----
  const launch = useMutation({
    mutationFn: () => {
      const body: RunLaunchRequest = {
        instance_ids: instanceSel.mode === 'all' ? 'all' : instanceSel.ids,
        harness,
        model_alias: modelAlias,
        budget_cap_usd: Number(budgetCap),
        max_cost_usd_per_instance: Number(maxCostPerInst),
        max_turns_per_instance: maxTurns === '' ? null : Number(maxTurns),
        attempts_per_instance: Number(attempts),
        timeout_seconds: Number(timeoutSec),
        max_parallel_harness_tasks: Number(maxParallel),
        ramp_step_pct: Number(rampStepPct),
        ramp_cooldown_seconds: Number(cooldownSec),
        autoscaler_enabled: autoscalerEnabled,
        harness_instructions: harnessInstructions.trim() || null,
      };
      // initial_budget_override is deliberately ABSENT (API-only, advanced —
      // it rewrites the pacer's measured constants; the discovery flow is the
      // supported way to set them).
      // context_window_tokens and max_tokens_per_instance are deliberately
      // ABSENT — see the module comment.
      return api.launchRun(body);
    },
    onSuccess: (res: LaunchResult) => {
      if (res.kind === 'launched') setLastLaunch(res.data);
    },
  });

  // budget_cap_usd is the run's provider-minted ceiling (max_budget on the
  // LiteLLM key, limit_usd on the OpenRouter key).  0 and negatives are finite
  // but meaningless ceilings — and chosing one is exactly the input that can
  // strand a claim: launch_run has no rollback after `_claim`, so a
  // PROVISION failure leaves the run holding active_key and blocks every
  // further run for that (harness, model_alias, set) until an abort.  So the
  // operator must pick a strictly positive number.
  const budgetInvalid =
    budgetCap.trim() === '' ||
    !Number.isFinite(Number(budgetCap)) ||
    Number(budgetCap) <= 0;

  const modelsUnreachable = models.isError;

  // 409 result hoisted out of the render so the narrowing survives the click
  // handler's closure (TS drops it on a re-read of launch.data inside a fn).
  const duplicate =
    launch.isSuccess && launch.data.kind === 'duplicate'
      ? launch.data.data
      : null;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>Launch a run</CardTitle>
        <div className="flex items-center gap-2 text-xs text-zinc-400">
          catalog
          <Freshness
            updatedAt={Math.max(
              instances.dataUpdatedAt,
              harnesses.dataUpdatedAt,
              models.dataUpdatedAt,
            )}
          />
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* gate: non-launchable instances */}
        {allInstances.length > 0 && nonLaunchable.length > 0 && (
          <div className="rounded-md border border-amber-200 bg-amber-50/60 p-3 text-xs text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-300">
            {nonLaunchable.length} of {allInstances.length} instances have no
            built image and will be refused by dispatch. Only instances marked
            as launchable will actually run.
          </div>
        )}

        {/* instances */}
        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-zinc-600 dark:text-zinc-300">
            Instances
          </label>
          {instances.isError && (
            <div className="rounded-md border border-rose-200 bg-rose-50/50 p-3 text-xs text-rose-600 dark:border-rose-900 dark:bg-rose-950/20 dark:text-rose-400">
              failed to load instances: {(instances.error as Error).message}
            </div>
          )}
          {instances.isSuccess && (
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <label className="flex items-center gap-1.5 text-xs">
                  <input
                    type="checkbox"
                    checked={instanceSel.mode === 'all'}
                    onChange={(e) =>
                      setInstanceSel(
                        e.target.checked
                          ? { mode: 'all' }
                          : { mode: 'ids', ids: [] },
                      )
                    }
                  />
                  <span className="font-medium">
                    all ({allInstances.length})
                  </span>
                  <span className="text-[10px] text-zinc-400">
                    — sent as the literal &quot;all&quot;, resolved server-side
                  </span>
                </label>
                <span className="ml-auto text-[11px] text-zinc-400">
                  {selectedCount} selected
                </span>
              </div>

              {instanceSel.mode === 'ids' && (
                <>
                  <div className="flex flex-wrap items-center gap-3">
                    <input
                      aria-label="filter instances"
                      value={filter}
                      onChange={(e) => setFilter(e.target.value)}
                      placeholder="filter by repo or instance id…"
                      className="w-56 rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-xs dark:border-zinc-800 dark:bg-zinc-900"
                    />
                    <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-zinc-600 dark:text-zinc-300">
                      <input
                        type="checkbox"
                        checked={launchableOnly}
                        onChange={(e) => setLaunchableOnly(e.target.checked)}
                      />
                      with image only
                      <span className="text-[10px] text-zinc-400">
                        ({allInstances.filter((i) => i.launchable).length})
                      </span>
                    </label>
                    <span className="ml-auto flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={selectShown}
                        disabled={
                          filtered.length === 0 ||
                          shownSelected === filtered.length
                        }
                        className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-[11px] text-zinc-700 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-200 dark:hover:bg-zinc-800"
                      >
                        select shown ({filtered.length})
                      </button>
                      <button
                        type="button"
                        onClick={clearShown}
                        disabled={shownSelected === 0}
                        className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-[11px] text-zinc-500 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800"
                      >
                        clear shown
                      </button>
                      <button
                        type="button"
                        onClick={() => validate.mutate()}
                        disabled={selectedCount === 0 || validate.isPending}
                        title="Grade the dataset's gold patch in each selected instance's image (run: image-validation). No model spend."
                        className="rounded-md border border-emerald-300 bg-emerald-50 px-2 py-1 text-[11px] text-emerald-800 hover:bg-emerald-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200 dark:hover:bg-emerald-900/40"
                      >
                        {validate.isPending
                          ? 'validating…'
                          : `gold-validate selected (${selectedCount})`}
                      </button>
                    </span>
                  </div>
                  <div className="flex items-start gap-2">
                    <textarea
                      aria-label="paste instance ids"
                      rows={pasteIds ? 3 : 1}
                      value={pasteIds}
                      onChange={(e) => setPasteIds(e.target.value)}
                      placeholder="paste instance ids (one per line or comma-separated) → select pasted"
                      className="w-full rounded-md border border-zinc-200 bg-white px-2 py-1 font-mono text-[11px] leading-snug dark:border-zinc-800 dark:bg-zinc-900"
                    />
                    <button
                      type="button"
                      onClick={selectPasted}
                      disabled={pasteIds.trim() === ''}
                      className="shrink-0 rounded-md border border-zinc-200 bg-white px-2 py-1 text-[11px] text-zinc-700 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-200 dark:hover:bg-zinc-800"
                    >
                      select pasted
                    </button>
                  </div>
                  {pasteReport && (
                    <p className="text-[11px] text-zinc-500">{pasteReport}</p>
                  )}
                  {validate.isError && (
                    <p className="text-[11px] text-rose-500">
                      validation failed: {(validate.error as Error).message}
                    </p>
                  )}
                  {validate.data && (
                    <p className="text-[11px] text-emerald-700 dark:text-emerald-300">
                      gold grades enqueued: {validate.data.validated.length}
                      {validate.data.skipped.length > 0 &&
                        ` (skipped ${validate.data.skipped.length}: ${validate.data.skipped
                          .map((s) => `${s.instance_id} — ${s.reason}`)
                          .join('; ')})`}{' '}
                      — run{' '}
                      <span className="font-mono">{validate.data.run_id}</span>{' '}
                      <button
                        type="button"
                        onClick={() => onOpenRun(validate.data!.run_id)}
                        className="underline hover:text-emerald-900 dark:hover:text-emerald-100"
                      >
                        open that run
                      </button>
                    </p>
                  )}
                  <div className="max-h-48 space-y-0.5 overflow-y-auto rounded-md border border-zinc-200 p-1.5 dark:border-zinc-800">
                    {filtered.length === 0 && (
                      <div className="px-2 py-1 text-[11px] text-zinc-400">
                        {launchableOnly && !filter.trim()
                          ? 'no instances have a built image'
                          : `no instances match &quot;${filter}&quot;`}
                      </div>
                    )}
                    {filtered.map((i) => (
                      <label
                        key={i.instance_id}
                        className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs hover:bg-zinc-50 dark:hover:bg-zinc-900"
                      >
                        <input
                          type="checkbox"
                          checked={instanceSel.ids.includes(i.instance_id)}
                          onChange={() => toggleInstance(i.instance_id)}
                        />
                        <span className="font-mono text-[11px] text-zinc-700 dark:text-zinc-200">
                          {i.instance_id}
                        </span>
                        {!i.launchable && (
                          <span className="ml-auto rounded-full bg-amber-100 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-amber-700 dark:bg-amber-900/50 dark:text-amber-300">
                            no image
                          </span>
                        )}
                      </label>
                    ))}
                  </div>
                </>
              )}
            </div>
          )}
        </div>

        {/* harness */}
        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-zinc-600 dark:text-zinc-300">
            Harness
          </label>
          {harnesses.isError && (
            <div className="rounded-md border border-rose-200 bg-rose-50/50 p-3 text-xs text-rose-600 dark:border-rose-900 dark:bg-rose-950/20 dark:text-rose-400">
              failed to load harnesses
            </div>
          )}
          {harnesses.data && (
            <select
              aria-label="harness"
              value={harness}
              onChange={(e) => setHarness(e.target.value)}
              className="w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-xs dark:border-zinc-800 dark:bg-zinc-900"
            >
              <option value="">select a harness…</option>
              {harnesses.data.harnesses.map((h) => (
                <option key={h} value={h}>
                  {h}
                </option>
              ))}
            </select>
          )}
        </div>

        {/* model alias — live from the gateway; 503 renders as unreachable */}
        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-zinc-600 dark:text-zinc-300">
            Model alias
          </label>
          {modelsUnreachable ? (
            <div className="rounded-md border border-rose-200 bg-rose-50/50 p-3 text-xs font-medium text-rose-600 dark:border-rose-900 dark:bg-rose-950/20 dark:text-rose-400">
              gateway unreachable — models can&apos;t be loaded. This is not an
              empty model list; the gateway is down.
            </div>
          ) : (
            <select
              aria-label="model alias"
              value={modelAlias}
              onChange={(e) => setModelAlias(e.target.value)}
              disabled={!models.data}
              className="w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-xs dark:border-zinc-800 dark:bg-zinc-900 disabled:opacity-50"
            >
              {!models.data && <option>loading…</option>}
              {models.data?.items.map((m) => (
                <option key={m.alias} value={m.alias}>
                  {m.alias}
                </option>
              ))}
            </select>
          )}
          {/* Item 14 (reviewer suggestion, owner-approved 2026-09-04): the pool's pacer
              consistency ratio r_tok / (k_inflight / L_A) from its last discovery. Below 1
              the arrival bucket refills slower than the in-flight cap turns over and binds
              first; below ~0.5 that is the starvation mode of run 01788405363237319353.
              No seeded cfg is said out loud — never rendered as a healthy blank. */}
          {(() => {
            const sel = models.data?.items.find((m) => m.alias === modelAlias);
            if (!sel) return null;
            const ratio = sel.consistency_ratio;
            if (ratio == null) {
              return (
                <div
                  className="text-[11px] text-amber-700 dark:text-amber-400"
                  data-testid="pacer-consistency"
                >
                  pacer: no seeded cfg for this pool
                  {sel.pacer_seeded_at != null && ' (seed incomplete)'} — run
                  discovery (Model ceilings) before a real run, or the pacer
                  runs on defaults.
                </div>
              );
            }
            const starving = ratio < 0.5;
            const binds = ratio < 1;
            return (
              <div
                className={cn(
                  'text-[11px]',
                  starving
                    ? 'text-rose-600 dark:text-rose-400'
                    : binds
                      ? 'text-amber-700 dark:text-amber-400'
                      : 'text-zinc-500',
                )}
                data-testid="pacer-consistency"
              >
                pacer consistency r_tok / (k_inflight / L_A) ={' '}
                {ratio.toFixed(2)}
                {starving
                  ? ' — WARNING: the arrival bucket binds long before the in-flight cap (starvation mode); re-probe before launching'
                  : binds
                    ? ' — WARNING: the arrival bucket refills slower than the in-flight cap turns over, so it binds first'
                    : ' — in-flight cap is the binding constraint, as intended'}
              </div>
            );
          })()}
        </div>

        {/* operator-specified limits */}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="space-y-1">
            <label
              className={cn(
                'block text-xs font-medium',
                budgetInvalid
                  ? 'text-rose-600 dark:text-rose-400'
                  : 'text-zinc-600 dark:text-zinc-300',
              )}
            >
              Budget cap (USD) *
            </label>
            <input
              aria-label="budget cap usd"
              type="number"
              min="0"
              step="any"
              value={budgetCap}
              onChange={(e) => setBudgetCap(e.target.value)}
              placeholder="required"
              className={cn(
                'w-full rounded-md border bg-white px-2 py-1.5 text-xs dark:bg-zinc-900',
                budgetInvalid
                  ? 'border-rose-300 dark:border-rose-700'
                  : 'border-zinc-200 dark:border-zinc-800',
              )}
            />
            <p className="text-[10px] text-zinc-400">
              the only run-wide ceiling — minted onto the provider key
            </p>
          </div>
          <div className="space-y-1">
            <label className="block text-xs font-medium text-zinc-600 dark:text-zinc-300">
              Max cost / instance
            </label>
            <input
              aria-label="max cost per instance"
              type="number"
              min="0"
              step="any"
              value={maxCostPerInst}
              onChange={(e) => setMaxCostPerInst(e.target.value)}
              className="w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-xs dark:border-zinc-800 dark:bg-zinc-900"
            />
          </div>
          <div className="space-y-1">
            <label className="block text-xs font-medium text-zinc-600 dark:text-zinc-300">
              Max turns / instance
            </label>
            <input
              aria-label="max turns per instance"
              type="number"
              min="0"
              step="1"
              value={maxTurns}
              onChange={(e) => setMaxTurns(e.target.value)}
              className="w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-xs dark:border-zinc-800 dark:bg-zinc-900"
            />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="space-y-1">
            <label className="block text-xs font-medium text-zinc-600 dark:text-zinc-300">
              Attempts / instance
            </label>
            <input
              aria-label="attempts per instance"
              type="number"
              min="1"
              step="1"
              value={attempts}
              onChange={(e) => setAttempts(e.target.value)}
              className="w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-xs dark:border-zinc-800 dark:bg-zinc-900"
            />
          </div>
          <div className="space-y-1">
            <label className="block text-xs font-medium text-zinc-600 dark:text-zinc-300">
              Timeout (s)
            </label>
            <input
              aria-label="timeout seconds"
              type="number"
              min="1"
              step="1"
              value={timeoutSec}
              onChange={(e) => setTimeoutSec(e.target.value)}
              className="w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-xs dark:border-zinc-800 dark:bg-zinc-900"
            />
          </div>
        </div>

        {/* autoscaler per-run overrides (§8): all four tighten-only — the static
            dispatcher ceiling and the L1 pacer stay in force regardless */}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="space-y-1">
            <label className="block text-xs font-medium text-zinc-600 dark:text-zinc-300">
              Max parallel tasks
            </label>
            <input
              aria-label="max parallel harness tasks"
              type="number"
              min="1"
              step="1"
              value={maxParallel}
              onChange={(e) => setMaxParallel(e.target.value)}
              className="w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-xs dark:border-zinc-800 dark:bg-zinc-900"
            />
            <p className="text-[10px] text-zinc-400">
              min-wins vs the fleet ceiling
            </p>
          </div>
          <div className="space-y-1">
            <label className="block text-xs font-medium text-zinc-600 dark:text-zinc-300">
              Ramp step (%)
            </label>
            <input
              aria-label="ramp step percent"
              type="number"
              min="0"
              max="5"
              step="1"
              value={rampStepPct}
              onChange={(e) => setRampStepPct(e.target.value)}
              className="w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-xs dark:border-zinc-800 dark:bg-zinc-900"
            />
            <p className="text-[10px] text-zinc-400">5 is the hard max</p>
          </div>
          <div className="space-y-1">
            <label className="block text-xs font-medium text-zinc-600 dark:text-zinc-300">
              Ramp cooldown (s)
            </label>
            <input
              aria-label="ramp cooldown seconds"
              type="number"
              min="1"
              step="1"
              value={cooldownSec}
              onChange={(e) => setCooldownSec(e.target.value)}
              className="w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-xs dark:border-zinc-800 dark:bg-zinc-900"
            />
          </div>
          <div className="space-y-1">
            <label className="block text-xs font-medium text-zinc-600 dark:text-zinc-300">
              Autoscaler
            </label>
            <label className="flex items-center gap-2 rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-xs dark:border-zinc-800 dark:bg-zinc-900">
              <input
                aria-label="autoscaler enabled"
                type="checkbox"
                checked={autoscalerEnabled}
                onChange={(e) => setAutoscalerEnabled(e.target.checked)}
              />
              dynamic ceiling
            </label>
            <p className="text-[10px] text-zinc-400">
              off = static ceiling only; pacing always on
            </p>
          </div>
        </div>

        {/* harness instructions (2026-09-09 efficiency prompt arm): appended to every
            job's problem statement at dispatch under a fixed heading. Advice the model may
            ignore — recorded on the run so a run with rules is never read as a plain one. */}
        <div className="space-y-1">
          <div className="flex items-center justify-between gap-2">
            <label
              htmlFor="harness-instructions"
              className="block text-xs font-medium text-zinc-600 dark:text-zinc-300"
            >
              Harness instructions{' '}
              <span className="font-normal text-zinc-400">
                (appended to the task prompt; advice, not a limit)
              </span>
            </label>
            <div className="flex items-center gap-2">
              {(presets.data?.presets ?? []).map((p) => (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => setHarnessInstructions(p.text)}
                  title={p.name}
                  className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-[11px] text-zinc-700 hover:bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-200"
                >
                  load preset: {p.id}
                </button>
              ))}
              {harnessInstructions !== '' && (
                <button
                  type="button"
                  onClick={() => setHarnessInstructions('')}
                  className="rounded-md border border-zinc-200 bg-white px-2 py-1 text-[11px] text-zinc-500 hover:bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-900"
                >
                  clear
                </button>
              )}
            </div>
          </div>
          <textarea
            id="harness-instructions"
            aria-label="harness instructions"
            rows={harnessInstructions ? 10 : 3}
            maxLength={presets.data?.max_chars ?? 4000}
            value={harnessInstructions}
            onChange={(e) => setHarnessInstructions(e.target.value)}
            placeholder="empty = plain run (the prompt the harness has always sent)"
            className="w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 font-mono text-[11px] leading-snug dark:border-zinc-800 dark:bg-zinc-900"
          />
          <p className="text-[10px] text-zinc-400">
            {harnessInstructions.length} / {presets.data?.max_chars ?? 4000}{' '}
            chars
            {harnessInstructions
              ? ' · shows in turn 0 of every trajectory and in the run’s launch record'
              : ''}
          </p>
        </div>

        {/* submit + outcomes */}
        <div className="space-y-2 pt-1">
          <button
            type="button"
            disabled={
              launch.isPending ||
              budgetInvalid ||
              selectedCount === 0 ||
              !harness ||
              !modelAlias ||
              modelsUnreachable
            }
            onClick={() => launch.mutate()}
            className="rounded-md bg-zinc-900 px-4 py-2 text-xs font-semibold text-zinc-50 hover:bg-zinc-700 disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
          >
            {launch.isPending ? 'Launching…' : 'Launch run'}
          </button>

          {launch.isError && (
            <p className="text-xs text-rose-500">
              launch failed: {(launch.error as Error).message}
            </p>
          )}

          {/* 409 — the run_id is the payload; never an error toast */}
          {duplicate && (
            <div className="rounded-md border border-sky-200 bg-sky-50/60 p-3 text-xs text-sky-900 dark:border-sky-900/60 dark:bg-sky-950/30 dark:text-sky-200">
              <p className="font-semibold">
                A run with this harness + model is already in progress.
              </p>
              <p className="mt-1 font-mono text-sm">{duplicate.run_id}</p>
              <p className="mt-1 text-[11px] text-sky-700/80 dark:text-sky-300/80">
                {duplicate.message}
              </p>
              <button
                type="button"
                onClick={() => onOpenRun(duplicate.run_id)}
                className="mt-2 rounded-md border border-sky-300 bg-white px-3 py-1 text-xs font-medium text-sky-700 hover:bg-sky-50 dark:border-sky-800 dark:bg-zinc-900 dark:text-sky-300"
              >
                Open that run
              </button>
            </div>
          )}

          {/* 503 — provisioning key unavailable (fail closed, D4) */}
          {launch.isSuccess && launch.data.kind === 'refused' && (
            <p className="text-xs text-rose-500">
              launch refused: {launch.data.message}
            </p>
          )}

          {/* 201 — launched.  We stay mounted (the form's selection survives,
              so the e2e's five runs over the same six instances each relaunch
              without re-picking the set) and offer an explicit open action. */}
          {lastLaunch && (
            <div className="rounded-md border border-emerald-200 bg-emerald-50/60 p-3 text-xs text-emerald-900 dark:border-emerald-900/60 dark:bg-emerald-950/30 dark:text-emerald-200">
              <p className="font-semibold">Run launched.</p>
              <p className="mt-1 font-mono text-sm">{lastLaunch.run_id}</p>
              <p className="mt-1 text-[11px] text-emerald-700/80 dark:text-emerald-300/80">
                dispatched {lastLaunch.dispatched} · seeded {lastLaunch.seeded}{' '}
                — your selection is retained below to launch the next harness on
                the same instances.
              </p>
              <button
                type="button"
                onClick={() => onOpenRun(lastLaunch.run_id)}
                className="mt-2 rounded-md border border-emerald-300 bg-white px-3 py-1 text-xs font-medium text-emerald-700 hover:bg-emerald-50 dark:border-emerald-800 dark:bg-zinc-900 dark:text-emerald-300"
              >
                Open this run
              </button>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
