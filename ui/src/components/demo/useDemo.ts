import { useContext, useSyncExternalStore } from 'react';
import type { DemoStore } from '../../lib/demo/store';
import { DemoContext } from './demoContext';

/** The demo store, re-rendering the caller on every store change. */
export function useDemo(): DemoStore {
  const s = useContext(DemoContext);
  if (!s) throw new Error('useDemo outside DemoProvider');
  useSyncExternalStore(s.subscribe, s.getVersion);
  return s;
}
