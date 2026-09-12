import { fmtUsd } from '../lib/format';
import { cn } from '../lib/utils';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  SectionLabel,
} from './ui-primitives';

// Live cost against max_budget (M5.1 view 2).  RunItem already carries the
// estimate, the confidence tier, and both compute figures.  The tier sits next
// to the estimate, and an estimate must never look like a reconciled figure —
// different colors and labels so an operator cannot conflate an extrapolation
// with an invoice.

const TIER_STYLE: Record<string, string> = {
  estimated:
    'bg-sky-50 text-sky-700 ring-sky-600/20 dark:bg-sky-500/10 dark:text-sky-400 dark:ring-sky-500/30',
  provisional:
    'bg-amber-50 text-amber-700 ring-amber-600/20 dark:bg-amber-500/10 dark:text-amber-400 dark:ring-amber-500/30',
  reconciled:
    'bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-500/10 dark:text-emerald-400 dark:ring-emerald-500/30',
};

export function RunCost({
  actualCostUsd,
  estimatedCostUsd,
  tier,
  computeEstimatedUsd,
  computeReconciledUsd,
  budgetCapUsd,
}: {
  actualCostUsd?: number | null;
  estimatedCostUsd?: number | null;
  tier?: string | null;
  computeEstimatedUsd?: number | null;
  computeReconciledUsd?: number | null;
  budgetCapUsd?: number | null;
}) {
  // Drive the budget bar off ACTUAL spend when we have it (the real inference
  // total summed across the run's instances), falling back to the pre-launch
  // estimate. The estimate/compute figures (BUILDER2 handover bucket C) have no
  // writer yet and render as "—" until cost_estimator.py / Cost Explorer land.
  const against = actualCostUsd ?? estimatedCostUsd;
  const pct =
    budgetCapUsd && budgetCapUsd > 0 && against != null
      ? (against / budgetCapUsd) * 100
      : null;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle>Cost vs budget</CardTitle>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <div>
          <SectionLabel>actual · inference</SectionLabel>
          <span className="font-mono text-lg font-semibold text-emerald-700 dark:text-emerald-400">
            {fmtUsd(actualCostUsd)}
          </span>
        </div>
        <div>
          <SectionLabel>estimated</SectionLabel>
          <div className="flex items-center gap-1.5">
            <span className="font-mono text-lg font-semibold text-zinc-800 dark:text-zinc-100">
              {fmtUsd(estimatedCostUsd)}
            </span>
            {tier && (
              <span
                className={cn(
                  'rounded-full px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ring-1 ring-inset',
                  TIER_STYLE[tier] ?? TIER_STYLE.provisional,
                )}
              >
                {tier}
              </span>
            )}
          </div>
        </div>
        <div>
          <SectionLabel>compute · estimated</SectionLabel>
          <span className="font-mono text-sm italic text-zinc-500 dark:text-zinc-400">
            {fmtUsd(computeEstimatedUsd)}
          </span>
        </div>
        <div>
          <SectionLabel>compute · reconciled</SectionLabel>
          <span className="font-mono text-sm font-semibold text-emerald-600 dark:text-emerald-400">
            {fmtUsd(computeReconciledUsd)}
          </span>
        </div>
        <div>
          <SectionLabel>max budget</SectionLabel>
          <span className="font-mono text-sm text-zinc-700 dark:text-zinc-200">
            {fmtUsd(budgetCapUsd)}
          </span>
          {pct !== null && (
            <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
              <div
                className={cn(
                  'h-full rounded-full',
                  pct >= 100
                    ? 'bg-rose-500'
                    : pct >= 80
                      ? 'bg-amber-500'
                      : 'bg-emerald-500',
                )}
                style={{ width: `${Math.min(pct, 100)}%` }}
              />
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
