/** The honest resolve rates, derived client-side from the per-instance state
 * buckets (ADR-0038 §4), so the run card is right for EVERY run — including
 * runs whose stored `run_summary` predates the results-writer fix that counts
 * EMPTY_PATCH in `gradeable` (owner request 2026-09-06, first 500-run).
 *
 * `instance_states` is one bucket per instance at its LATEST attempt (eval row
 * over harness row), so restarts collapse naturally: an instance restarted
 * after a harness crash is counted once, by what its rerun did.
 *
 *  - gradeable  = RESOLVED + UNRESOLVED + EMPTY_PATCH — every instance the model
 *                 got a fair shot at. An empty submission is a model failure and
 *                 stays in the denominator (ADR-0038 only excludes infrastructure
 *                 categories and invalid grades).
 *  - excluded   = terminal states that are NOT a model verdict: harness crashes,
 *                 abandoned/aborted/never-dispatched, budget stops, timeouts.
 *  - inFlight   = everything else (still running).
 *  - attempted  = the API's `resolve_rate_denominator` (distinct attempts with
 *                 operator infra retries collapsed) — the other named denominator.
 */

export interface StateBucket {
  state: string;
  count: number;
}

export interface DerivedRates {
  resolved: number;
  unresolved: number;
  emptyPatch: number;
  gradeable: number;
  excluded: number;
  excludedStates: string[];
  inFlight: number;
  attempted: number | null;
  rateGradeable: number | null;
  rateAttempted: number | null;
}

const IN_FLIGHT = new Set([
  'PENDING',
  'DISPATCHED',
  'HARNESS_RUNNING',
  'PATCH_READY',
  'EVAL_PENDING',
  'EVAL_RUNNING',
]);

export function deriveRates(
  buckets: readonly StateBucket[] | null | undefined,
  attemptedDenominator: number | null | undefined,
): DerivedRates {
  let resolved = 0;
  let unresolved = 0;
  let emptyPatch = 0;
  let excluded = 0;
  let inFlight = 0;
  const excludedStates: string[] = [];
  for (const b of buckets ?? []) {
    const n = b.count ?? 0;
    if (b.state === 'RESOLVED') resolved += n;
    else if (b.state === 'UNRESOLVED') unresolved += n;
    else if (b.state === 'EMPTY_PATCH') emptyPatch += n;
    else if (IN_FLIGHT.has(b.state)) inFlight += n;
    else {
      excluded += n;
      if (n > 0) excludedStates.push(`${b.state} ${n}`);
    }
  }
  const gradeable = resolved + unresolved + emptyPatch;
  const attempted =
    attemptedDenominator === null || attemptedDenominator === undefined
      ? null
      : attemptedDenominator;
  return {
    resolved,
    unresolved,
    emptyPatch,
    gradeable,
    excluded,
    excludedStates,
    inFlight,
    attempted,
    rateGradeable: gradeable > 0 ? resolved / gradeable : null,
    rateAttempted: attempted && attempted > 0 ? resolved / attempted : null,
  };
}
