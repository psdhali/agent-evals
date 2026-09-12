import { describe, expect, it } from 'vitest';

import {
  COLLAPSE_ABOVE_ROWS,
  narrowRows,
  repoOf,
  repoOptions,
} from '../instanceTable';

const rows = [
  {
    instance_id: 'django__django-10097',
    attempt_number: 1,
    phase: 'harness',
    state: 'PATCH_READY',
  },
  {
    instance_id: 'django__django-10097',
    attempt_number: 1,
    phase: 'eval',
    state: 'RESOLVED',
  },
  {
    instance_id: 'sympy__sympy-13031',
    attempt_number: 1,
    phase: 'harness',
    state: 'EMPTY_PATCH',
  },
  {
    instance_id: 'pylint-dev__pylint-4661',
    attempt_number: 2,
    phase: 'eval',
    state: 'UNRESOLVED',
  },
];

describe('instance table narrowing', () => {
  it('derives the repo prefix and the sorted option list', () => {
    expect(repoOf('django__django-10097')).toBe('django');
    expect(repoOf('pylint-dev__pylint-4661')).toBe('pylint-dev');
    expect(repoOf('weird-id')).toBe('weird-id');
    expect(repoOptions(rows)).toEqual(['django', 'pylint-dev', 'sympy']);
  });

  it('search is a case-insensitive substring of the instance id', () => {
    expect(
      narrowRows(rows, { search: 'SYMPY-130', repo: '', phase: '' }),
    ).toHaveLength(1);
    expect(
      narrowRows(rows, { search: '  10097 ', repo: '', phase: '' }),
    ).toHaveLength(2);
    expect(
      narrowRows(rows, { search: 'nope', repo: '', phase: '' }),
    ).toHaveLength(0);
  });

  it('repo and phase filters compose with search', () => {
    expect(
      narrowRows(rows, { search: '', repo: 'django', phase: 'eval' }),
    ).toEqual([rows[1]]);
    expect(
      narrowRows(rows, { search: '4661', repo: 'pylint-dev', phase: '' }),
    ).toEqual([rows[3]]);
    expect(narrowRows(rows, { search: '', repo: '', phase: '' })).toHaveLength(
      4,
    );
  });

  it('a 500-run collapses by default, a smoke run does not', () => {
    expect(COLLAPSE_ABOVE_ROWS).toBeLessThan(1000);
    expect(COLLAPSE_ABOVE_ROWS).toBeGreaterThan(12);
  });
});
