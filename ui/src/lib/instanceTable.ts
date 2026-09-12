/** Client-side narrowing of the run page's instance table (owner request during
 * the first 500-run, 2026-09-06): a 500-instance run has ~1,000 rows (harness +
 * eval per attempt), which is unreadable as a flat list. The API's state /
 * error-category filters stay server-side; these three are pure functions over
 * the rows already fetched so they cost no round trip and compose with them. */

export interface TableRow {
  instance_id: string;
  attempt_number: number;
  phase: string;
  state: string;
}

export interface TableNarrowing {
  /** case-insensitive substring of the instance id */
  search: string;
  /** the SWE-bench repo prefix before `__` ("django", "sympy", …); '' = all */
  repo: string;
  /** 'harness' | 'eval' | '' = both */
  phase: string;
}

/** Rows above this count start collapsed — the operator opens the table on purpose. */
export const COLLAPSE_ABOVE_ROWS = 60;

export function repoOf(instanceId: string): string {
  const i = instanceId.indexOf('__');
  return i > 0 ? instanceId.slice(0, i) : instanceId;
}

export function repoOptions<T extends { instance_id: string }>(
  rows: readonly T[],
): string[] {
  return Array.from(new Set(rows.map((r) => repoOf(r.instance_id)))).sort();
}

export function narrowRows<T extends TableRow>(
  rows: readonly T[],
  n: TableNarrowing,
): T[] {
  const q = n.search.trim().toLowerCase();
  return rows.filter(
    (r) =>
      (!q || r.instance_id.toLowerCase().includes(q)) &&
      (!n.repo || repoOf(r.instance_id) === n.repo) &&
      (!n.phase || r.phase === n.phase),
  );
}
