// Where the dashboard's numbers come from (SITE-REDESIGN-AND-PUBLICATION-PLAN
// 2026-09-10 §8). `live` is the operator UI against the orchestrator API;
// `demo` is the public Explorer: the SAME screens and controls, answered from
// exported run snapshots replayed on a clock, every action simulated locally.
//
// Kept in its own module (not api.ts) so components can read it without
// touching the `api` object the 17 component tests mock wholesale.
export type DataMode = 'live' | 'demo';

export const DATA_MODE: DataMode =
  import.meta.env.VITE_DATA_MODE === 'demo' ? 'demo' : 'live';

export const IS_DEMO = DATA_MODE === 'demo';
