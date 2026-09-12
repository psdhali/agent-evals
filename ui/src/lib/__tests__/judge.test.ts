import { describe, expect, it } from 'vitest';
import type { JudgeResultItem } from '../api';
import {
  TOOL_EFFICIENCY_REDUNDANT_RATIO,
  contaminationStatus,
  findJudgeResult,
  judgeIssues,
  matchesJudgeIssue,
} from '../judge';

// offline-analysis-design.md §3.3/§10.7, review 2026-09-02 §5 point 3:
// "unknown must never render as healthy" — evidence_missing and "never
// judged" must be distinguishable from a clean "no", and from each other.

function result(dimensions: JudgeResultItem['dimensions']): JudgeResultItem {
  return {
    instance_id: 'inst-1',
    attempt_number: 1,
    judged_at: '2026-09-02T00:00:00+00:00',
    judge_model_resolved: 'deepseek/deepseek-v4-flash-0731',
    rubric_version: '1',
    judge_prune_mode: 'pruned',
    input_truncated: false,
    tool_output_pruned: false,
    judge_parse_failed: false,
    summary: null,
    judge_cost_usd: 0.001,
    dimensions,
  };
}

function dim(overrides: Partial<JudgeResultItem['dimensions'][number]>) {
  return {
    dimension_id: 'contamination',
    scale_type: 'likert',
    score_numeric: null,
    score_secondary: null,
    flag: null,
    span_start_turn: null,
    span_end_turn: null,
    reasoning: null,
    evidence: [],
    evidence_missing: false,
    causes: [],
    ...overrides,
  };
}

describe('contaminationStatus', () => {
  it('is "unjudged" when no result exists at all', () => {
    expect(contaminationStatus(undefined)).toBe('unjudged');
  });

  it('is "unjudged" when the result has no contamination dimension', () => {
    expect(contaminationStatus(result([dim({ dimension_id: 'loop' })]))).toBe(
      'unjudged',
    );
  });

  it('is "no" for a clean baseline score with no evidence_missing flag', () => {
    expect(contaminationStatus(result([dim({ score_numeric: 0 })]))).toBe('no');
  });

  it('is "yes" for any score above baseline', () => {
    expect(contaminationStatus(result([dim({ score_numeric: 2 })]))).toBe(
      'yes',
    );
  });

  it('is "flagged_no_evidence", never "no", when evidence_missing is true', () => {
    // score_parsing.py demotes score_numeric to null exactly when
    // evidence_missing fires — this must not collapse to a clean "no".
    expect(
      contaminationStatus(
        result([dim({ score_numeric: null, evidence_missing: true })]),
      ),
    ).toBe('flagged_no_evidence');
  });

  it('is "unjudged", not "no", for a null score that is NOT evidence_missing (e.g. a parse failure)', () => {
    expect(
      contaminationStatus(
        result([dim({ score_numeric: null, evidence_missing: false })]),
      ),
    ).toBe('unjudged');
  });
});

describe('findJudgeResult', () => {
  it('matches on both instance_id and attempt_number', () => {
    const r1 = result([]);
    const r2 = { ...result([]), attempt_number: 2 };
    expect(findJudgeResult([r1, r2], 'inst-1', 2)).toBe(r2);
    expect(findJudgeResult([r1, r2], 'inst-1', 1)).toBe(r1);
    expect(findJudgeResult([r1, r2], 'inst-1', 3)).toBeUndefined();
  });

  it('returns undefined for an undefined results list', () => {
    expect(findJudgeResult(undefined, 'inst-1', 1)).toBeUndefined();
  });
});

// 2026-09-08 (owner): per-rubric findings behind the instances table's
// "filter judge findings" dropdown and its Judge findings column.
describe('judgeIssues / matchesJudgeIssue', () => {
  it('raises nothing for an unjudged attempt and never matches "none"', () => {
    expect(judgeIssues(undefined)).toEqual([]);
    expect(matchesJudgeIssue(undefined, 'none')).toBe(false);
    expect(matchesJudgeIssue(undefined, 'any')).toBe(false);
  });

  it('is clean at every baseline, and "none" matches it', () => {
    const r = result([
      dim({ dimension_id: 'contamination', score_numeric: 0 }),
      dim({
        dimension_id: 'hallucination',
        scale_type: 'count_and_severity',
        score_numeric: 0,
        score_secondary: 0,
      }),
      dim({
        dimension_id: 'tool_efficiency',
        scale_type: 'ratio',
        score_numeric: 2,
        score_secondary: 20,
      }),
      dim({
        dimension_id: 'loop',
        scale_type: 'boolean_with_span',
        flag: false,
      }),
      dim({
        dimension_id: 'environment_problem',
        scale_type: 'boolean_with_span',
        flag: false,
      }),
      dim({ dimension_id: 'gave_up_early', score_numeric: 0 }),
      dim({ dimension_id: 'test_gaming', score_numeric: 0 }),
      dim({ dimension_id: 'problem_misread', score_numeric: 0 }),
    ]);
    expect(judgeIssues(r)).toEqual([]);
    expect(matchesJudgeIssue(r, 'none')).toBe(true);
    expect(matchesJudgeIssue(r, 'any')).toBe(false);
    expect(matchesJudgeIssue(r, 'tool_efficiency')).toBe(false);
  });

  it('raises one finding per dimension above its baseline, in rubric order', () => {
    const r = result([
      dim({ dimension_id: 'contamination', score_numeric: 2 }),
      dim({
        dimension_id: 'hallucination',
        scale_type: 'count_and_severity',
        score_numeric: 3,
        score_secondary: 1,
      }),
      dim({
        dimension_id: 'tool_efficiency',
        scale_type: 'ratio',
        score_numeric: 5,
        score_secondary: 16,
      }),
      dim({
        dimension_id: 'loop',
        scale_type: 'boolean_with_span',
        flag: true,
      }),
      dim({
        dimension_id: 'environment_problem',
        scale_type: 'boolean_with_span',
        flag: true,
      }),
      dim({ dimension_id: 'gave_up_early', score_numeric: 1 }),
      dim({ dimension_id: 'test_gaming', score_numeric: 1 }),
      dim({ dimension_id: 'problem_misread', score_numeric: 3 }),
    ]);
    expect(judgeIssues(r).map((i) => `${i.kind}:${i.chip}`)).toEqual([
      'contamination:contam 2',
      'hallucination:halluc 3',
      'tool_efficiency:tools 31%',
      'loop:loop',
      'environment_problem:env',
      'gave_up_early:gave-up 1',
      'test_gaming:gaming 1',
      'problem_misread:misread 3',
    ]);
    expect(matchesJudgeIssue(r, 'contamination')).toBe(true);
    expect(matchesJudgeIssue(r, 'environment_problem')).toBe(true);
    expect(matchesJudgeIssue(r, 'any')).toBe(true);
    expect(matchesJudgeIssue(r, 'none')).toBe(false);
  });

  it('flags tool_efficiency only at or above the redundant-ratio threshold', () => {
    const at = result([
      dim({
        dimension_id: 'tool_efficiency',
        scale_type: 'ratio',
        score_numeric: 4,
        score_secondary: 16,
      }),
    ]);
    const below = result([
      dim({
        dimension_id: 'tool_efficiency',
        scale_type: 'ratio',
        score_numeric: 3,
        score_secondary: 16,
      }),
    ]);
    expect(TOOL_EFFICIENCY_REDUNDANT_RATIO).toBe(0.25);
    expect(matchesJudgeIssue(at, 'tool_efficiency')).toBe(true);
    expect(matchesJudgeIssue(below, 'tool_efficiency')).toBe(false);
  });

  it('reports evidence_missing as its own finding, not as the dimension', () => {
    const r = result([
      dim({
        dimension_id: 'contamination',
        score_numeric: null,
        evidence_missing: true,
      }),
    ]);
    expect(judgeIssues(r)).toEqual([
      { kind: 'no_evidence', chip: 'no evidence' },
    ]);
    expect(matchesJudgeIssue(r, 'contamination')).toBe(false);
    expect(matchesJudgeIssue(r, 'no_evidence')).toBe(true);
  });

  it('reports a parse failure first, whatever the (unreliable) scores say', () => {
    const r = {
      ...result([dim({ dimension_id: 'contamination', score_numeric: 3 })]),
      judge_parse_failed: true,
    };
    expect(judgeIssues(r).map((i) => i.kind)).toEqual([
      'parse_failed',
      'contamination',
    ]);
  });
});

// 2026-09-08 (owner): a judgment that hit the 10-min ceiling is recorded with
// judge_method "timeout" and every score null — it must read as "timed out",
// never as clean and never as unjudged-by-omission.
describe('judgeIssues — timed-out judgment', () => {
  it('is its own finding and matches only the timeout selection', () => {
    const r = {
      ...result([dim({ dimension_id: 'contamination', score_numeric: null })]),
      judge_method: 'timeout',
    };
    expect(judgeIssues(r)).toEqual([{ kind: 'timeout', chip: 'timed out' }]);
    expect(matchesJudgeIssue(r, 'timeout')).toBe(true);
    expect(matchesJudgeIssue(r, 'any')).toBe(true);
    expect(matchesJudgeIssue(r, 'none')).toBe(false);
    expect(contaminationStatus(r)).toBe('unjudged');
  });
});

// rubric v3 (2026-09-09): token_efficiency — the `causes` scale
describe('token_efficiency (causes scale)', () => {
  const base = {
    instance_id: 'x',
    attempt_number: 1,
    judged_at: 't',
    judge_model_resolved: 'm',
    rubric_version: '3',
    judge_prune_mode: 'pruned',
    input_truncated: false,
    tool_output_pruned: false,
    judge_parse_failed: false,
    judge_method: 'primary',
    judge_attempts: 1,
    summary: null,
    judge_cost_usd: 0,
  };
  const dim = (
    score: number | null,
    severity: number | null,
    evidence_missing = false,
  ): JudgeResultItem['dimensions'][number] => ({
    dimension_id: 'token_efficiency',
    scale_type: 'causes',
    score_numeric: score,
    score_secondary: severity,
    flag: null,
    span_start_turn: null,
    span_end_turn: null,
    reasoning: 'r',
    evidence: [],
    evidence_missing,
    causes: [{ cause: 'unbounded_file_reads', share: 0.7, recommendation: 'cap reads' }],
  });

  it('flags at an avoidable share of 25% or more, with a percent chip', () => {
    const r = { ...base, dimensions: [dim(0.6, 1)] } as never;
    expect(judgeIssues(r)).toEqual([{ kind: 'token_efficiency', chip: 'waste 60%' }]);
    expect(matchesJudgeIssue(r, 'token_efficiency')).toBe(true);
  });

  it('flags on severity 2+ even at a low share, and not below either bar', () => {
    expect(judgeIssues({ ...base, dimensions: [dim(0.1, 2)] } as never)).toEqual([
      { kind: 'token_efficiency', chip: 'waste 10%' },
    ]);
    expect(judgeIssues({ ...base, dimensions: [dim(0.1, 1)] } as never)).toEqual([]);
  });

  it('a demoted (no-evidence) causes score is the no_evidence finding, not waste', () => {
    expect(judgeIssues({ ...base, dimensions: [dim(null, null, true)] } as never)).toEqual([
      { kind: 'no_evidence', chip: 'no evidence' },
    ]);
  });
});
