import type { JudgeResultItem } from './api';

/**
 * Reads the judge's `contamination` dimension out of one result and
 * classifies it into the four states the UI must distinguish (offline-
 * analysis-design.md §3.3/§10.7, review 2026-09-02 §5 point 3):
 *
 * - `unjudged`            — no judge_results row for this attempt at all,
 *                            OR the dimension's score is null for a reason
 *                            other than evidence_missing (e.g. a parse
 *                            failure) — genuinely unknown, never "no".
 * - `no`                  — scored at baseline (0) with no evidence_missing
 *                            flag — a clean negative.
 * - `yes`                 — scored above baseline (contamination's scale is
 *                            0-3: 0 none, 1 weak, 2 moderate, 3 strong).
 * - `flagged_no_evidence` — the judge wanted to score above baseline but
 *                            had no evidence to cite, so score_parsing.py
 *                            demoted it to null rather than keep a
 *                            confident score. This is NOT the same as "no"
 *                            — it must render as its own state, per the
 *                            review's "unknown must never render as
 *                            healthy" rule.
 */
export type ContaminationStatus =
  'unjudged' | 'no' | 'yes' | 'flagged_no_evidence';

export function contaminationStatus(
  result: JudgeResultItem | undefined,
): ContaminationStatus {
  if (!result) return 'unjudged';
  const dim = result.dimensions.find((d) => d.dimension_id === 'contamination');
  if (!dim) return 'unjudged';
  if (dim.evidence_missing) return 'flagged_no_evidence';
  if (dim.score_numeric == null) return 'unjudged';
  return dim.score_numeric > 0 ? 'yes' : 'no';
}

export function contaminationLabel(status: ContaminationStatus): string {
  switch (status) {
    case 'yes':
      return 'contamination: yes';
    case 'no':
      return 'contamination: no';
    case 'flagged_no_evidence':
      return 'contamination: flagged (no evidence)';
    case 'unjudged':
      return 'not judged';
  }
}

/**
 * 2026-09-08 (owner, after the first 500-attempt judge pass): "I want to
 * focus on the ones which had issues" — one finding kind per rubric
 * dimension (config/judge_rubric.yaml), plus the two honesty states that
 * are findings about the JUDGE rather than the agent. Each kind's "issue"
 * threshold is the dimension's own "nothing found" baseline from the
 * rubric, except tool_efficiency, which is a ratio with no baseline: on the
 * first pass 488/500 attempts had at least one redundant call, so "any
 * redundancy" would select the whole run — it flags at
 * TOOL_EFFICIENCY_REDUNDANT_RATIO (¼ of calls redundant) instead.
 */
export type JudgeIssueKind =
  | 'contamination'
  | 'hallucination'
  | 'tool_efficiency'
  | 'loop'
  | 'environment_problem'
  | 'gave_up_early'
  | 'test_gaming'
  | 'problem_misread'
  | 'token_efficiency'
  | 'no_evidence'
  | 'parse_failed'
  | 'timeout';

export const TOOL_EFFICIENCY_REDUNDANT_RATIO = 0.25;
/** rubric v3 (2026-09-09): token_efficiency (`causes` scale) flags at an
 * avoidable share of ≥ ¼ of the attempt's spend, or a severity of 2+. Same
 * thresholds as the synthesis digest and the site export. */
export const TOKEN_EFFICIENCY_AVOIDABLE_SHARE = 0.25;

export interface JudgeIssue {
  kind: JudgeIssueKind;
  /** short chip text for the instances table, e.g. "contam 2", "tools 31%" */
  chip: string;
}

/** Option list for the table's findings filter — value, human label; the
 * order is the rubric's order, then the judge-honesty kinds, then the two
 * synthetic selections. */
export const JUDGE_ISSUE_OPTIONS: ReadonlyArray<{
  value: JudgeIssueKind | 'any' | 'none';
  label: string;
}> = [
  { value: 'any', label: 'any finding' },
  { value: 'contamination', label: 'contamination: yes' },
  { value: 'hallucination', label: 'hallucination' },
  { value: 'environment_problem', label: 'environment / tool problem' },
  { value: 'loop', label: 'loop' },
  {
    value: 'tool_efficiency',
    label: `tool efficiency: ≥${Math.round(TOOL_EFFICIENCY_REDUNDANT_RATIO * 100)}% redundant`,
  },
  { value: 'gave_up_early', label: 'gave up early' },
  { value: 'test_gaming', label: 'test gaming' },
  { value: 'problem_misread', label: 'problem misread' },
  {
    value: 'token_efficiency',
    label: `token efficiency: ≥${Math.round(TOKEN_EFFICIENCY_AVOIDABLE_SHARE * 100)}% avoidable`,
  },
  { value: 'no_evidence', label: 'flagged without evidence' },
  { value: 'parse_failed', label: 'judge parse failed' },
  // 2026-09-08 (owner): the judge was still generating at the 10-min ceiling —
  // recorded as a judgment with no verdict, never "clean"
  { value: 'timeout', label: 'judge timed out (no verdict)' },
  { value: 'none', label: 'no findings' },
];

export function judgeIssues(result: JudgeResultItem | undefined): JudgeIssue[] {
  if (!result) return [];
  if (result.judge_method === 'timeout') {
    return [{ kind: 'timeout', chip: 'timed out' }];
  }
  const issues: JudgeIssue[] = [];
  if (result.judge_parse_failed) {
    issues.push({ kind: 'parse_failed', chip: 'parse failed' });
  }
  let noEvidence = false;
  for (const d of result.dimensions) {
    if (d.evidence_missing) {
      noEvidence = true;
      continue;
    }
    const n = d.score_numeric;
    switch (d.dimension_id) {
      case 'contamination':
        if (n != null && n > 0)
          issues.push({ kind: 'contamination', chip: `contam ${n}` });
        break;
      case 'hallucination':
        if (n != null && n > 0)
          issues.push({ kind: 'hallucination', chip: `halluc ${n}` });
        break;
      case 'tool_efficiency': {
        const total = d.score_secondary;
        if (n != null && total != null && total > 0) {
          const ratio = n / total;
          if (ratio >= TOOL_EFFICIENCY_REDUNDANT_RATIO) {
            issues.push({
              kind: 'tool_efficiency',
              chip: `tools ${Math.round(ratio * 100)}%`,
            });
          }
        }
        break;
      }
      case 'loop':
        if (d.flag) issues.push({ kind: 'loop', chip: 'loop' });
        break;
      case 'environment_problem':
        if (d.flag) issues.push({ kind: 'environment_problem', chip: 'env' });
        break;
      case 'gave_up_early':
        if (n != null && n > 0)
          issues.push({ kind: 'gave_up_early', chip: `gave-up ${n}` });
        break;
      case 'test_gaming':
        if (n != null && n > 0)
          issues.push({ kind: 'test_gaming', chip: `gaming ${n}` });
        break;
      case 'problem_misread':
        if (n != null && n > 0)
          issues.push({ kind: 'problem_misread', chip: `misread ${n}` });
        break;
      case 'token_efficiency': {
        const severity = d.score_secondary;
        if (
          (n != null && n >= TOKEN_EFFICIENCY_AVOIDABLE_SHARE) ||
          (severity != null && severity >= 2)
        ) {
          issues.push({
            kind: 'token_efficiency',
            chip: n != null ? `waste ${Math.round(n * 100)}%` : 'waste',
          });
        }
        break;
      }
      default:
        break;
    }
  }
  if (noEvidence) issues.push({ kind: 'no_evidence', chip: 'no evidence' });
  return issues;
}

/** How many judged attempts have NO verdict — the judge call timed out at the
 * ceiling, or its answer failed to parse. The Judge card's retry count. */
export function noVerdictCount(results: JudgeResultItem[] | undefined): number {
  return (results ?? []).filter(
    (r) => r.judge_method === 'timeout' || r.judge_parse_failed,
  ).length;
}

/** True when the result matches the filter selection: a kind, 'any'
 * (at least one finding) or 'none' (judged, zero findings). Unjudged is
 * never a match — an unjudged row is not "no findings". */
export function matchesJudgeIssue(
  result: JudgeResultItem | undefined,
  selection: JudgeIssueKind | 'any' | 'none',
): boolean {
  if (!result) return false;
  const issues = judgeIssues(result);
  if (selection === 'any') return issues.length > 0;
  if (selection === 'none') return issues.length === 0;
  return issues.some((i) => i.kind === selection);
}

/** Finds the one result for a specific (instance, attempt) out of a run's
 * full judge/results response — that endpoint isn't per-instance-scoped. */
export function findJudgeResult(
  results: JudgeResultItem[] | undefined,
  instanceId: string,
  attemptNumber: number,
): JudgeResultItem | undefined {
  return results?.find(
    (r) => r.instance_id === instanceId && r.attempt_number === attemptNumber,
  );
}
