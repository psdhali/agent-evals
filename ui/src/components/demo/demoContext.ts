import { createContext } from 'react';
import type { DemoStore } from '../../lib/demo/store';

/** The replay store, provided by DemoProvider (demo mode only). */
export const DemoContext = createContext<DemoStore | null>(null);
