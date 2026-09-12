import { useEffect, useState } from 'react';
import { bundleIfLoaded } from '../../lib/demo/bundle';
import { SPEEDS, type JumpKind } from '../../lib/demo/store';
import { cn } from '../../lib/utils';
import { useDemo } from './useDemo';

// The always-visible demo bar (§8.2 "demo bar extras"): what is being
// replayed, at what speed, where the clock is, plus transport (play / pause /
// speed / scrub / jump-to-event / restart) and the two honesty stories —
// a simulated Valkey outage and a simulated provider-429 burst.

function mmss(t: number): string {
  const s = Math.max(0, Math.floor(t));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const p = (n: number) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${p(m)}:${p(sec)}` : `${p(m)}:${p(sec)}`;
}

const JUMPS: { kind: JumpKind; label: string }[] = [
  { kind: 'first_dispatched', label: 'first dispatched' },
  { kind: 'first_resolved', label: 'first resolved' },
  { kind: 'first_overload', label: 'first overload' },
  { kind: 'half_terminal', label: '50% terminal' },
  { kind: 'terminal', label: 'terminal' },
];

const btn =
  'rounded-md border border-zinc-300 px-2 py-0.5 text-[11px] font-medium hover:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:hover:bg-zinc-800';

export function DemoBar() {
  const store = useDemo();
  // the clock display ticks on its own; the store only notifies on changes
  const [, tick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 250);
    return () => clearInterval(id);
  }, []);

  const runId = store.runId;
  const end = runId ? store.endOf(runId) : null;
  const t = store.now();
  const loaded = runId ? bundleIfLoaded(runId) : undefined;
  const burst = runId ? store.hasSim(runId) && store.sim(runId).burst : null;
  const outage = store.global.valkeyOutage;

  const jump = (kind: JumpKind) => {
    const at = store.jump(kind);
    if (at == null)
      store.toast(
        `no "${JUMPS.find((j) => j.kind === kind)?.label}" event in this run's replay`,
      );
  };

  return (
    <div
      className="sticky top-14 z-10 border-b border-fuchsia-300 bg-fuchsia-50/95 text-fuchsia-950 backdrop-blur dark:border-fuchsia-800 dark:bg-fuchsia-950/80 dark:text-fuchsia-100"
      data-testid="demo-bar"
      role="region"
      aria-label="demo replay controls"
    >
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-1.5 text-xs">
        <span className="rounded bg-fuchsia-600 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-white">
          demo
        </span>
        <span className="font-medium">
          replaying run{' '}
          <span className="font-mono" title={runId ?? undefined}>
            {runId ? runId.slice(-8) : '…'}
          </span>{' '}
          at {store.speed}× · t = +{mmss(t)}
          {end != null && (
            <span className="text-fuchsia-700/70 dark:text-fuchsia-300/70">
              {' '}
              / {mmss(end)}
            </span>
          )}
          {loaded && (
            <span className="ml-1 text-fuchsia-700/70 dark:text-fuchsia-300/70">
              ({loaded.bundle.harness} × {loaded.bundle.model_alias})
            </span>
          )}{' '}
          · actions are simulated
        </span>

        <div className="flex items-center gap-1">
          <button
            type="button"
            className={btn}
            onClick={() => store.toggle()}
            aria-label={store.playing ? 'pause replay' : 'play replay'}
            disabled={!runId}
          >
            {store.playing ? '❚❚ pause' : '▶ play'}
          </button>
          <select
            aria-label="replay speed"
            value={store.speed}
            onChange={(e) => store.setSpeed(Number(e.target.value))}
            className="rounded-md border border-zinc-300 bg-white px-1 py-0.5 text-[11px] dark:border-zinc-700 dark:bg-zinc-900"
          >
            {SPEEDS.map((s) => (
              <option key={s} value={s}>
                {s}×
              </option>
            ))}
          </select>
          <input
            type="range"
            aria-label="scrub replay"
            min={0}
            max={Math.max(1, Math.floor(end ?? 1))}
            step={1}
            value={Math.floor(t)}
            onChange={(e) => store.seek(Number(e.target.value))}
            className="w-32 accent-fuchsia-600 md:w-48"
            disabled={end == null}
          />
          <select
            aria-label="jump to event"
            value=""
            onChange={(e) => {
              if (e.target.value) jump(e.target.value as JumpKind);
            }}
            className="rounded-md border border-zinc-300 bg-white px-1 py-0.5 text-[11px] dark:border-zinc-700 dark:bg-zinc-900"
            disabled={!loaded}
          >
            <option value="">jump to…</option>
            {JUMPS.map((j) => (
              <option key={j.kind} value={j.kind}>
                {j.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            className={btn}
            onClick={() => store.restart()}
            disabled={!runId}
            title="t = 0 and every simulated action of this run undone"
          >
            ↺ restart replay
          </button>
        </div>

        <div className="ml-auto flex items-center gap-1">
          <button
            type="button"
            className={cn(
              btn,
              outage &&
                'border-amber-500 bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200',
            )}
            onClick={() => {
              store.mutateGlobal((g) => {
                g.valkeyOutage = !g.valkeyOutage;
              });
              store.simulated(
                store.global.valkeyOutage
                  ? 'Valkey outage — live/pacer/planner/limits read unknown, control reads stale and fail-closed'
                  : 'Valkey back — live reads resume',
              );
            }}
            title="live → unknown, control → stale (every pool reads paused), planner + limits → unknown: the fail-closed story"
          >
            {outage ? 'end Valkey outage' : 'simulate Valkey outage'}
          </button>
          <button
            type="button"
            className={cn(
              btn,
              burst &&
                'border-rose-500 bg-rose-100 text-rose-900 dark:bg-rose-900/40 dark:text-rose-200',
            )}
            disabled={!runId}
            onClick={() => {
              if (!runId) return;
              const now = store.now();
              store.mutate(runId, (s) => {
                s.burst = s.burst ? null : { t: now };
              });
              store.simulated(
                store.sim(runId).burst
                  ? 'provider 429 burst — overloads climb for 90 s, the planner cuts r_qps ×0.85 and holds for the cooldown, then grows back +5 % per tick'
                  : 'provider 429 burst cleared',
              );
            }}
            title="pacer overloads climb, r_qps × 0.85 + cooldown, then +5 % growth: the AIMD story on this run's real budgets"
          >
            {burst ? 'clear 429 burst' : 'simulate provider 429 burst'}
          </button>
        </div>
      </div>

      {store.toasts.length > 0 && (
        <div
          className="pointer-events-none fixed bottom-4 right-4 z-50 flex max-w-md flex-col gap-2"
          aria-live="polite"
        >
          {store.toasts.map((tst) => (
            <div
              key={tst.id}
              className="rounded-md border border-fuchsia-300 bg-white/95 px-3 py-2 text-xs text-zinc-800 shadow-lg dark:border-fuchsia-700 dark:bg-zinc-900/95 dark:text-zinc-100"
            >
              {tst.text}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
