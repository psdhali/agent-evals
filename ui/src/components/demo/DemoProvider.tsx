import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useSyncExternalStore, type ReactNode } from 'react';
import { loadBundle } from '../../lib/demo/bundle';
import { defaultRunId, publishedIds } from '../../lib/demo/demoApi';
import { demoStore } from '../../lib/demo/store';
import { DemoContext } from './demoContext';

// The replay clock in React: one context over the module-level demoStore.
// Two jobs beyond exposing it — (1) follow the hash so opening a run's page
// makes it the replayed run, (2) invalidate every react-query on a clock
// discontinuity (scrub, jump, restart, switch) or a simulated action, so the
// panels re-answer immediately instead of at their next poll.

function runIdFromHash(): string | null {
  const parts = window.location.hash
    .replace(/^#\/?/, '')
    .split('/')
    .filter(Boolean)
    .map((p) => decodeURIComponent(p));
  return parts[0] === 'runs' && parts[1] ? parts[1] : null;
}

export function DemoProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const version = useSyncExternalStore(
    demoStore.subscribe,
    demoStore.getVersion,
  );
  const lastEpoch = useRef(demoStore.epoch);

  // discontinuity → every poller re-asks the adapter now
  useEffect(() => {
    if (demoStore.epoch !== lastEpoch.current) {
      lastEpoch.current = demoStore.epoch;
      void qc.invalidateQueries();
    }
  }, [version, qc]);

  // the hash decides which run is replayed
  useEffect(() => {
    let cancelled = false;
    const sync = async () => {
      const fromHash = runIdFromHash();
      let target = fromHash;
      if (target) {
        const ids = await publishedIds();
        if (!ids.includes(target)) target = null;
      }
      if (!target && !demoStore.runId) target = await defaultRunId();
      if (!target || target === demoStore.runId || cancelled) return;
      await loadBundle(target); // the clock needs the window length first
      if (!cancelled) demoStore.switchRun(target);
    };
    void sync();
    const onHash = () => void sync();
    window.addEventListener('hashchange', onHash);
    return () => {
      cancelled = true;
      window.removeEventListener('hashchange', onHash);
    };
  }, []);

  return (
    <DemoContext.Provider value={demoStore}>{children}</DemoContext.Provider>
  );
}
