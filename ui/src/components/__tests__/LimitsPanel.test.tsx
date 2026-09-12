import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { LimitsPanel } from '../LimitsPanel';

// Operator limits (2026-09-04): every knob shows its value + SOURCE, edits are arm-gated and
// carry actor + reason, a pacer edit carries the alias and the also_pool flag, and the
// unknown state never renders as defaults.

vi.mock('../../lib/api', () => ({
  api: {
    getLimits: vi.fn(),
    setRunLimit: vi.fn(),
    setGlobalLimit: vi.fn(),
    setPacerLimit: vi.fn(),
  },
}));

import { api } from '../../lib/api';

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
  );
}

const SPECS = [
  {
    field: 'ceiling_override',
    scope: 'run',
    kind: 'int',
    label: 'planner ceiling override',
    description: 'replaces the projection',
    lo: 0,
    hi: 5000,
    default: null,
    default_note: 'unset = the projection',
    unit: 'tasks',
    read_by: 'dispatcher planner, every tick',
  },
  {
    field: 'borrowed_curve_cap',
    scope: 'global',
    kind: 'int',
    label: 'borrowed-curve cap',
    description: 'cap under a borrowed curve',
    lo: 1,
    hi: 5000,
    default: null,
    default_note: 'env (30)',
    unit: 'tasks',
    read_by: 'dispatcher planner, every tick',
  },
  {
    field: 'r_tok',
    scope: 'pacer',
    kind: 'float',
    label: 'r_tok',
    description: 'refill',
    lo: 1,
    hi: null,
    default: null,
    default_note: '',
    unit: 'tok/s',
    read_by: 'pacer',
  },
];

const VIEW = {
  state: 'ok',
  run: {
    run_id: 'run-9',
    set_at: 1000,
    fields: {
      ceiling_override: { value: 13, source: 'operator', set_by: 'preet' },
    },
  },
  global_: {
    fields: {
      borrowed_curve_cap: { value: null, source: 'default', set_by: null },
    },
  },
  static: {
    max_concurrent_harness_tasks: 145,
    eval_asg_max: 16,
    eval_max_workers_env: 64,
  },
  pacer: [
    {
      harness: 'mini_swe_agent',
      alias: 'deepseek-v4-flash-0731-mini',
      pool: 'deepseek-v4-flash-0731',
      cfg: { r_tok: 244608, r_tok_seed: 244608, cached_weight: 1 },
      pool_cfg: { r_tok: 244608 },
    },
  ],
  specs: SPECS,
};

describe('LimitsPanel', () => {
  it('renders each knob with its value and source, and the static rails', async () => {
    vi.mocked(api.getLimits).mockResolvedValue(VIEW as never);
    renderWithQuery(<LimitsPanel runId="run-9" />);
    await waitFor(() =>
      expect(screen.getByText('planner ceiling override')).toBeTruthy(),
    );
    expect(screen.getByText('13')).toBeTruthy();
    expect(screen.getByText('operator')).toBeTruthy(); // the source chip
    expect(screen.getByText('borrowed-curve cap')).toBeTruthy();
    expect(screen.getByText('default')).toBeTruthy();
    expect(screen.getByText('145')).toBeTruthy(); // the static harness cap
    expect(api.getLimits).toHaveBeenCalledWith('run-9');
  });

  it('a run edit is arm-gated and carries actor + reason', async () => {
    vi.mocked(api.getLimits).mockResolvedValue(VIEW as never);
    vi.mocked(api.setRunLimit).mockResolvedValue({
      scope: 'run',
      target: 'run-9',
      field: 'ceiling_override',
      old: '13',
      new: '20',
      pool: null,
    });
    renderWithQuery(<LimitsPanel runId="run-9" />);
    await waitFor(() =>
      expect(screen.getByText('planner ceiling override')).toBeTruthy(),
    );
    const input = screen.getByLabelText('ceiling_override value');
    fireEvent.change(input, { target: { value: '20' } });
    const setButtons = screen.getAllByRole('button', { name: 'set' });
    expect((setButtons[0] as HTMLButtonElement).disabled).toBe(true); // not armed
    fireEvent.click(screen.getByLabelText('arm limit edits'));
    fireEvent.change(screen.getByLabelText('limits reason'), {
      target: { value: '13 queued' },
    });
    fireEvent.click(screen.getAllByRole('button', { name: 'set' })[0]);
    await waitFor(() =>
      expect(api.setRunLimit).toHaveBeenCalledWith(
        'ceiling_override',
        20,
        'operator',
        '13 queued',
      ),
    );
    await waitFor(() =>
      expect(screen.getByText(/last edit: run ceiling_override/)).toBeTruthy(),
    );
  });

  it('clear sends null; a pacer edit carries the alias and also_pool', async () => {
    vi.mocked(api.getLimits).mockResolvedValue(VIEW as never);
    vi.mocked(api.setRunLimit).mockResolvedValue({
      scope: 'run',
      target: 'run-9',
      field: 'ceiling_override',
      old: '13',
      new: null,
      pool: null,
    });
    vi.mocked(api.setPacerLimit).mockResolvedValue({
      scope: 'pacer',
      target: 'deepseek-v4-flash-0731-mini',
      field: 'r_tok',
      old: '244608.0',
      new: '600000.0',
      pool: 'deepseek-v4-flash-0731',
    });
    renderWithQuery(<LimitsPanel runId="run-9" />);
    await waitFor(() =>
      expect(screen.getByText('planner ceiling override')).toBeTruthy(),
    );
    fireEvent.click(screen.getByLabelText('arm limit edits'));
    fireEvent.click(screen.getByRole('button', { name: 'clear' }));
    await waitFor(() =>
      expect(api.setRunLimit).toHaveBeenCalledWith(
        'ceiling_override',
        null,
        'operator',
        '',
      ),
    );
    fireEvent.change(
      screen.getByLabelText('deepseek-v4-flash-0731-mini r_tok value'),
      {
        target: { value: '600000' },
      },
    );
    const sets = screen.getAllByRole('button', { name: 'set' });
    const pacerSet = sets[sets.length - 1];
    fireEvent.click(pacerSet);
    await waitFor(() =>
      expect(api.setPacerLimit).toHaveBeenCalledWith(
        'deepseek-v4-flash-0731-mini',
        'r_tok',
        600000,
        'operator',
        '',
        true,
      ),
    );
  });

  it('the unknown state never renders values', async () => {
    vi.mocked(api.getLimits).mockResolvedValue({
      state: 'unknown',
      run: null,
      global_: null,
      static: {},
      pacer: [],
      specs: [],
    } as never);
    renderWithQuery(<LimitsPanel />);
    await waitFor(() => expect(screen.getByText(/cannot read/)).toBeTruthy());
    expect(screen.queryByText('planner ceiling override')).toBeNull();
  });
});
