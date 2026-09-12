import { useCallback, useEffect, useState } from 'react';
import { ControlPanel } from './components/ControlPanel';
import { RunList } from './components/RunList';
import { RunDetail } from './components/RunDetail';
import { InstanceDetail } from './components/InstanceDetail';
import { Capacity } from './components/Capacity';
import { LimitsPanel } from './components/LimitsPanel';
import { ModelCeilings } from './components/ModelCeilings';
import { LaunchScreen } from './components/LaunchScreen';
import { SystemStateBanner } from './components/SystemStateBanner';
import { DemoBar } from './components/demo/DemoBar';
import { IS_DEMO } from './lib/dataMode';
import { cn } from './lib/utils';

type View =
  | { name: 'runs' }
  | { name: 'launch' }
  | { name: 'run'; runId: string }
  | { name: 'instance'; runId: string; instanceId: string; attempt: number }
  | { name: 'capacity' };

// URL routing (BUILDER2 handover §E): the address bar reflects the open
// run/instance/tab so a link is copy-shareable and deep-links restore on load.
// Hash-based on purpose — it needs no server-side history fallback, so it works
// identically under Vite dev and behind any reverse proxy without extra config.
//   #/runs · #/launch · #/capacity
//   #/runs/<runId>
//   #/runs/<runId>/instances/<instanceId>/<attempt>
function parseHash(): View {
  const parts = window.location.hash
    .replace(/^#\/?/, '')
    .split('/')
    .filter(Boolean)
    .map((p) => decodeURIComponent(p));
  if (parts[0] === 'launch') return { name: 'launch' };
  if (parts[0] === 'capacity') return { name: 'capacity' };
  if (parts[0] === 'runs' && parts[1]) {
    const runId = parts[1];
    if (parts[2] === 'instances' && parts[3]) {
      const attempt = Number(parts[4] ?? '1');
      return {
        name: 'instance',
        runId,
        instanceId: parts[3],
        attempt: Number.isFinite(attempt) && attempt > 0 ? attempt : 1,
      };
    }
    return { name: 'run', runId };
  }
  return { name: 'runs' };
}

function viewToHash(v: View): string {
  switch (v.name) {
    case 'launch':
      return '#/launch';
    case 'capacity':
      return '#/capacity';
    case 'run':
      return `#/runs/${encodeURIComponent(v.runId)}`;
    case 'instance':
      return `#/runs/${encodeURIComponent(v.runId)}/instances/${encodeURIComponent(
        v.instanceId,
      )}/${v.attempt}`;
    default:
      return '#/runs';
  }
}

function useHashView(): [View, (v: View) => void] {
  const [view, setViewState] = useState<View>(() => parseHash());
  useEffect(() => {
    // hashchange fires for back/forward AND manual edits — the URL is the one
    // source of truth, so state simply follows it.
    const onHash = () => setViewState(parseHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const setView = useCallback((v: View) => {
    const next = viewToHash(v);
    if (window.location.hash === next)
      setViewState(v); // re-click same view
    else window.location.hash = next; // → hashchange → parseHash → state
  }, []);
  return [view, setView];
}

function App() {
  const [view, setView] = useHashView();

  return (
    <div className="min-h-screen bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      <header className="sticky top-0 z-10 border-b border-zinc-200 bg-white/90 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/90">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-4 px-4">
          <button
            type="button"
            onClick={() => setView({ name: 'runs' })}
            className="flex items-center gap-2 text-left"
          >
            <span className="flex h-7 w-7 items-center justify-center rounded-md bg-zinc-900 text-sm font-bold text-white dark:bg-zinc-100 dark:text-zinc-900">
              e
            </span>
            <span className="text-sm font-semibold tracking-tight">
              Eval Dashboard
            </span>
          </button>
          <nav className="ml-2 flex gap-1 text-sm">
            <button
              type="button"
              onClick={() => setView({ name: 'runs' })}
              className={cn(
                'rounded-md px-3 py-1.5 text-xs font-medium',
                view.name === 'runs' ||
                  view.name === 'run' ||
                  view.name === 'instance'
                  ? 'bg-zinc-100 text-zinc-900 dark:bg-zinc-800 dark:text-zinc-100'
                  : 'text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200',
              )}
            >
              Runs
            </button>
            <button
              type="button"
              onClick={() => setView({ name: 'capacity' })}
              className={cn(
                'rounded-md px-3 py-1.5 text-xs font-medium',
                view.name === 'capacity'
                  ? 'bg-zinc-100 text-zinc-900 dark:bg-zinc-800 dark:text-zinc-100'
                  : 'text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200',
              )}
            >
              Capacity
            </button>
            <button
              type="button"
              onClick={() => setView({ name: 'launch' })}
              className={cn(
                'rounded-md px-3 py-1.5 text-xs font-medium',
                view.name === 'launch'
                  ? 'bg-zinc-100 text-zinc-900 dark:bg-zinc-800 dark:text-zinc-100'
                  : 'text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200',
              )}
            >
              Launch
            </button>
          </nav>
          <div className="ml-auto hidden max-w-md md:block">
            <ControlPanel />
          </div>
        </div>
      </header>

      {/* Demo mode (public Explorer): the replay bar sits between the header and
          the banner, always visible — every number below is a real run's at
          the replay clock's t, every control simulates locally. */}
      {IS_DEMO && <DemoBar />}

      {/* Persistent system-state banner (M5.1 view 2) — always visible, never
          a toast, so the operator never has to remember whether they paused.
          Shares the /control query with the ControlPanel (same key → one poll). */}
      <SystemStateBanner />

      <main className="mx-auto max-w-7xl px-4 py-6">
        <div className="mb-4 md:hidden">
          <ControlPanel />
        </div>

        {view.name === 'runs' && (
          <RunList onOpenRun={(runId) => setView({ name: 'run', runId })} />
        )}
        {view.name === 'launch' && (
          <LaunchScreen
            onOpenRun={(runId) => setView({ name: 'run', runId })}
          />
        )}
        {view.name === 'run' && (
          <RunDetail
            runId={view.runId}
            onBack={() => setView({ name: 'runs' })}
            onOpenInstance={(instanceId, attempt) =>
              setView({
                name: 'instance',
                runId: view.runId,
                instanceId,
                attempt,
              })
            }
          />
        )}
        {view.name === 'instance' && (
          <InstanceDetail
            runId={view.runId}
            instanceId={view.instanceId}
            attempt={view.attempt}
            onBack={() => setView({ name: 'run', runId: view.runId })}
          />
        )}
        {view.name === 'capacity' && (
          <div className="space-y-6">
            <ModelCeilings />
            {/* Operator limits (2026-09-04) — here too, so the global planner / eval-scaler
                knobs and a pre-launch run override can be set with no run open. */}
            <LimitsPanel />
            <Capacity />
          </div>
        )}
      </main>
    </div>
  );
}

export default App;
