import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LaunchScreen } from '../LaunchScreen';

// §7 of the launch-screen brief.  The launch body is a correctness contract:
// `budget_cap_usd` is the only run boundary (required, provider-minted),
// `context_window_tokens` and `max_tokens_per_instance` must never be sent
// (an explicit window set `context_window_source='run_config'` on every prior
// run and kept the live gateway resolution untested; a token cap aborts
// nothing by owner decision).  Each test asserts on the serialised payload
// captured by the mocked launch call — never on component state.

vi.mock('../../lib/api', () => ({
  api: {
    listDatasetInstances: vi.fn(),
    listHarnesses: vi.fn(),
    listModels: vi.fn(),
    launchRun: vi.fn(),
    validateImages: vi.fn(),
    instructionPresets: vi.fn(),
  },
}));

import { api, type RunLaunchRequest } from '../../lib/api';

beforeEach(() => {
  vi.clearAllMocks();
});

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    client,
    ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>),
  };
}

// 5 instances, one of them not launchable — enough to exercise the list.
const INSTANCES = [
  {
    instance_id: 'scikit-learn__scikit-learn-25102',
    repo: 'scikit-learn/scikit-learn',
    launchable: true,
  },
  {
    instance_id: 'astropy__astropy-12907',
    repo: 'astropy/astropy',
    launchable: true,
  },
  {
    instance_id: 'matplotlib__matplotlib-13989',
    repo: 'matplotlib/matplotlib',
    launchable: true,
  },
  {
    instance_id: 'django__django-10097',
    repo: 'django/django',
    launchable: true,
  },
  {
    instance_id: 'sphinx-doc__sphinx-10614',
    repo: 'sphinx-doc/sphinx',
    launchable: false,
  },
];

const HARNESSES = { harnesses: ['aider', 'claude_code', 'codex'] };
const MODELS = {
  items: [
    {
      alias: 'claude-code-model',
      max_input_tokens: 1_048_576,
      consistency_ratio: null,
      pacer_seeded_at: null,
    },
    // Item 14: a seeded pool whose arrival bucket binds first (0.42 < 0.5 = starvation).
    {
      alias: 'deepseek-flash',
      max_input_tokens: 1_048_576,
      consistency_ratio: 0.42,
      pacer_seeded_at: 1_700_000_000,
    },
  ],
};

function primeCatalog() {
  vi.mocked(api.listDatasetInstances).mockResolvedValue({
    items: INSTANCES,
    total: INSTANCES.length,
  });
  vi.mocked(api.listHarnesses).mockResolvedValue(HARNESSES);
  vi.mocked(api.listModels).mockResolvedValue(MODELS);
  vi.mocked(api.instructionPresets).mockResolvedValue({
    presets: [
      {
        id: 'opencode-laguna-efficiency-v1',
        name: 'opencode × Laguna efficiency rules v1',
        harness: 'opencode',
        text: '1. Locate, then read windows.\n2. Verify once, then stop.',
      },
    ],
    max_chars: 4000,
  });
}

/** Fill the required operator fields and pick a harness + model. */
async function fillRequired() {
  await userEvent.selectOptions(
    await screen.findByLabelText('harness'),
    'claude_code',
  );
  await userEvent.selectOptions(
    await screen.findByLabelText('model alias'),
    'deepseek-flash',
  );
  await userEvent.type(screen.getByLabelText('budget cap usd'), '25');
  await userEvent.click(
    screen.getByLabelText(/^scikit-learn__scikit-learn-25102$/),
  );
}

/** The request object the component sends — `JSON.stringify` of this *is* the
 * serialised payload, so asserting on it is asserting on the wire format. */
function lastPayload(): RunLaunchRequest {
  const calls = vi.mocked(api.launchRun).mock.calls;
  const last = calls[calls.length - 1];
  if (!last) throw new Error('launchRun was never called');
  return last[0];
}

describe('LaunchScreen — harness instructions (2026-09-09 efficiency prompt arm)', () => {
  it('an empty box sends harness_instructions: null — a plain run', async () => {
    primeCatalog();
    renderWithClient(<LaunchScreen onOpenRun={() => {}} />);
    await fillRequired();
    await userEvent.click(screen.getByRole('button', { name: 'Launch run' }));
    await waitFor(() => expect(api.launchRun).toHaveBeenCalled());
    expect(lastPayload().harness_instructions).toBeNull();
  });

  it('"load preset" fills the box and the trimmed text rides on the body', async () => {
    primeCatalog();
    renderWithClient(<LaunchScreen onOpenRun={() => {}} />);
    await fillRequired();
    await userEvent.click(
      await screen.findByRole('button', {
        name: /load preset: opencode-laguna-efficiency-v1/,
      }),
    );
    const box = screen.getByLabelText(
      'harness instructions',
    ) as HTMLTextAreaElement;
    expect(box.value).toContain('Verify once, then stop.');
    await userEvent.type(box, '  ');
    await userEvent.click(screen.getByRole('button', { name: 'Launch run' }));
    await waitFor(() => expect(api.launchRun).toHaveBeenCalled());
    expect(lastPayload().harness_instructions).toBe(
      '1. Locate, then read windows.\n2. Verify once, then stop.',
    );
  });
});

describe('LaunchScreen — paste ids (2026-09-09)', () => {
  it('pasted ids become the explicit id list; unknown ids are reported, not sent', async () => {
    primeCatalog();
    renderWithClient(<LaunchScreen onOpenRun={() => {}} />);
    await userEvent.selectOptions(
      await screen.findByLabelText('harness'),
      'codex',
    );
    await userEvent.selectOptions(
      await screen.findByLabelText('model alias'),
      'deepseek-flash',
    );
    await userEvent.type(screen.getByLabelText('budget cap usd'), '25');
    await userEvent.type(
      screen.getByLabelText('paste instance ids'),
      'astropy__astropy-12907\ndjango__django-10097, nope__nope-1\nastropy__astropy-12907',
    );
    await userEvent.click(
      screen.getByRole('button', { name: 'select pasted' }),
    );
    expect(
      screen.getByText(/2 selected; 1 unknown id\(s\) ignored: nope__nope-1/),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Launch run' }));
    await waitFor(() => expect(api.launchRun).toHaveBeenCalled());
    expect(lastPayload().instance_ids).toEqual([
      'astropy__astropy-12907',
      'django__django-10097',
    ]);
  });
});

describe('LaunchScreen — §7 launch-body correctness rules', () => {
  it('"all" is sent as the literal string, not a 500-element array', async () => {
    primeCatalog();
    renderWithClient(<LaunchScreen onOpenRun={() => {}} />);
    await fillRequired();

    await userEvent.click(screen.getByRole('checkbox', { name: /all/i }));
    await userEvent.click(screen.getByRole('button', { name: 'Launch run' }));

    await waitFor(() => expect(api.launchRun).toHaveBeenCalled());
    const body = lastPayload();
    expect(typeof body.instance_ids).toBe('string');
    expect(body.instance_ids).toBe('all');
  });

  it('context_window_tokens is absent from the body when not overridden', async () => {
    primeCatalog();
    renderWithClient(<LaunchScreen onOpenRun={() => {}} />);
    await fillRequired();
    await userEvent.click(screen.getByRole('button', { name: 'Launch run' }));

    await waitFor(() => expect(api.launchRun).toHaveBeenCalled());
    const body = lastPayload();
    expect(body).not.toHaveProperty('context_window_tokens');
  });

  it('max_tokens_per_instance is absent from the body', async () => {
    primeCatalog();
    renderWithClient(<LaunchScreen onOpenRun={() => {}} />);
    await fillRequired();
    await userEvent.click(screen.getByRole('button', { name: 'Launch run' }));

    await waitFor(() => expect(api.launchRun).toHaveBeenCalled());
    const body = lastPayload();
    expect(body).not.toHaveProperty('max_tokens_per_instance');
  });

  it('budget_cap_usd cannot be submitted empty', async () => {
    primeCatalog();
    renderWithClient(<LaunchScreen onOpenRun={() => {}} />);
    await userEvent.selectOptions(
      await screen.findByLabelText('harness'),
      'codex',
    );
    await userEvent.selectOptions(
      await screen.findByLabelText('model alias'),
      'deepseek-flash',
    );
    await userEvent.click(screen.getByLabelText(/^astropy__astropy-12907$/));

    // Budget left blank → the submit button must be disabled (and launch never called).
    const submit = screen.getByRole('button', {
      name: 'Launch run',
    }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    expect(api.launchRun).not.toHaveBeenCalled();
  });

  it('budget_cap_usd of 0 or negative is rejected (the check must not be removable)', async () => {
    // 0 and negatives are finite, so the empty-string check alone would let
    // them through — and they reach _provision_keys as max_budget/limit_usd.
    // A NON-positive budget must disable submit (a 0/negative ceiling is the
    // input that can strand a claim when provisioning then fails).
    primeCatalog();
    renderWithClient(<LaunchScreen onOpenRun={() => {}} />);
    await userEvent.selectOptions(
      await screen.findByLabelText('harness'),
      'codex',
    );
    await userEvent.selectOptions(
      await screen.findByLabelText('model alias'),
      'deepseek-flash',
    );
    await userEvent.click(screen.getByLabelText(/^astropy__astropy-12907$/));

    for (const bad of ['0', '-5']) {
      const input = screen.getByLabelText('budget cap usd');
      await userEvent.clear(input);
      await userEvent.type(input, bad);
      const submit = screen.getByRole('button', {
        name: 'Launch run',
      }) as HTMLButtonElement;
      expect(submit.disabled, `budget ${bad} must disable submit`).toBe(true);
    }
    expect(api.launchRun).not.toHaveBeenCalled();
  });

  it('a 409 renders the returned run_id and offers "open that run"', async () => {
    primeCatalog();
    vi.mocked(api.launchRun).mockResolvedValue({
      kind: 'duplicate',
      data: {
        status: 'duplicate',
        run_id: '1787000000000',
        harness: 'claude_code',
        model_alias: 'deepseek-flash',
        message: 'a run is already in progress: 1787000000000',
      },
    });
    const onOpenRun = vi.fn();
    renderWithClient(<LaunchScreen onOpenRun={onOpenRun} />);
    await fillRequired();
    await userEvent.click(screen.getByRole('button', { name: 'Launch run' }));

    await waitFor(() =>
      expect(screen.getByText('1787000000000')).toBeInTheDocument(),
    );
    await userEvent.click(
      screen.getByRole('button', { name: 'Open that run' }),
    );
    expect(onOpenRun).toHaveBeenCalledWith('1787000000000');
  });

  it('a 201 keeps the form mounted: run_id banner shows, selection survives', async () => {
    // The e2e is five runs over the same six instances.  A successful launch
    // must NOT unmount the form (which would drop the selection) — it shows
    // the run_id with an open-action, and the previously-picked instance stays
    // checked so the next harness can relaunch the same set.
    primeCatalog();
    vi.mocked(api.launchRun).mockResolvedValue({
      kind: 'launched',
      data: {
        status: 'launched',
        run_id: 'e2e-run-0001',
        dispatched: 6,
        seeded: 6,
      },
    });
    const onOpenRun = vi.fn();
    renderWithClient(<LaunchScreen onOpenRun={onOpenRun} />);
    await fillRequired();
    await userEvent.click(screen.getByRole('button', { name: 'Launch run' }));

    // The banner shows the run_id + dispatched/seeded, and offers open.
    await waitFor(() =>
      expect(screen.getByText('e2e-run-0001')).toBeInTheDocument(),
    );
    expect(screen.getByText(/dispatched 6 · seeded 6/i)).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole('button', { name: 'Open this run' }),
    );
    expect(onOpenRun).toHaveBeenCalledWith('e2e-run-0001');

    // The form is still mounted and the selection is retained: the harness
    // dropdown + the checked instance are still present (fillRequired picked
    // scikit-learn__scikit-learn-25102, which stays checked).
    expect(screen.getByLabelText('harness')).toBeInTheDocument();
    expect(
      screen.getByLabelText(/^scikit-learn__scikit-learn-25102$/),
    ).toBeChecked();
  });

  it("shows the selected pool's pacer consistency ratio and warns when it binds first", async () => {
    primeCatalog();
    renderWithClient(<LaunchScreen onOpenRun={() => {}} />);
    await screen.findByRole('option', { name: 'deepseek-flash' });
    await userEvent.selectOptions(
      screen.getByLabelText('model alias'),
      'deepseek-flash',
    );
    const note = await screen.findByTestId('pacer-consistency');
    expect(note).toHaveTextContent('= 0.42');
    expect(note).toHaveTextContent(/WARNING/);
    expect(note).toHaveTextContent(/starvation/);
    // An unseeded pool says so — never a blank that reads as healthy.
    await userEvent.selectOptions(
      screen.getByLabelText('model alias'),
      'claude-code-model',
    );
    expect(screen.getByTestId('pacer-consistency')).toHaveTextContent(
      /no seeded cfg/,
    );
  });

  it('a 503 from /models renders as gateway reachable-failure, never an empty list', async () => {
    vi.mocked(api.listDatasetInstances).mockResolvedValue({
      items: INSTANCES,
      total: INSTANCES.length,
    });
    vi.mocked(api.listHarnesses).mockResolvedValue(HARNESSES);
    vi.mocked(api.listModels).mockRejectedValue(new Error('gateway down'));

    renderWithClient(<LaunchScreen onOpenRun={() => {}} />);

    // The gateway-failure message is the honest render; an empty dropdown —
    // which would read "no models exist" — must not appear.
    await waitFor(() =>
      expect(
        screen.getByText(/gateway unreachable — models can't be loaded/i),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole('combobox', { name: 'model alias' }),
    ).not.toBeInTheDocument();
  });

  it('"with image only" filters the instance list to launchable instances', async () => {
    primeCatalog();
    renderWithClient(<LaunchScreen onOpenRun={() => {}} />);

    // By default all five are listed (incl. the non-launchable sphinx).
    await waitFor(() =>
      expect(
        screen.getByLabelText(/sphinx-doc__sphinx-10614/),
      ).toBeInTheDocument(),
    );

    // Toggling "with image only" hides the non-launchable instance.
    await userEvent.click(
      screen.getByRole('checkbox', { name: /with image only/i }),
    );
    expect(
      screen.queryByLabelText(/sphinx-doc__sphinx-10614/),
    ).not.toBeInTheDocument();
    // The launchable ones remain selectable.
    expect(
      screen.getByLabelText(/scikit-learn__scikit-learn-25102/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/astropy__astropy-12907/)).toBeInTheDocument();
    // And the count chip reflects 4 launchable (exact text — the "select
    // shown (4)" button also carries the number).
    expect(screen.getByText(/^\(4\)$/)).toBeInTheDocument();
  });

  it('"select shown" selects exactly the filtered instances as an explicit id list', async () => {
    primeCatalog();
    renderWithClient(<LaunchScreen onOpenRun={() => {}} />);
    await waitFor(() =>
      expect(
        screen.getByLabelText(/sphinx-doc__sphinx-10614/),
      ).toBeInTheDocument(),
    );

    // Nothing selected, so "clear shown" is inert and "select shown" offers all 5.
    expect(screen.getByRole('button', { name: /clear shown/i })).toBeDisabled();
    expect(
      screen.getByRole('button', { name: /select shown \(5\)/i }),
    ).toBeEnabled();

    // Narrow to the built images, then select everything shown.
    await userEvent.click(
      screen.getByRole('checkbox', { name: /with image only/i }),
    );
    await userEvent.click(
      screen.getByRole('button', { name: /select shown \(4\)/i }),
    );
    expect(screen.getByText(/4 selected/)).toBeInTheDocument();
    // The "all" checkbox stays OFF — the selection is an explicit list.
    expect(
      screen.getByRole('checkbox', { name: /^all \(5\)/i }),
    ).not.toBeChecked();
    // Every shown row is checked; the hidden non-launchable one was not added.
    expect(
      screen.getByLabelText(/scikit-learn__scikit-learn-25102/),
    ).toBeChecked();
    await userEvent.click(
      screen.getByRole('checkbox', { name: /with image only/i }),
    );
    expect(screen.getByLabelText(/sphinx-doc__sphinx-10614/)).not.toBeChecked();
    expect(screen.getByText(/4 selected/)).toBeInTheDocument();

    // "clear shown" drops what is currently listed (all 5 shown, 4 selected).
    await userEvent.click(screen.getByRole('button', { name: /clear shown/i }));
    expect(screen.getByText(/0 selected/)).toBeInTheDocument();
  });

  it('"gold-validate selected" sends the explicit selection to /images/validate and reports the run', async () => {
    primeCatalog();
    vi.mocked(api.validateImages).mockResolvedValue({
      run_id: 'image-validation-20260905T070000Z-ab12',
      validated: [
        { instance_id: 'scikit-learn__scikit-learn-25102', attempt_number: 1 },
      ],
      skipped: [
        {
          instance_id: 'django__django-10097',
          reason: 'validation still in flight',
        },
      ],
    });
    renderWithClient(<LaunchScreen onOpenRun={() => {}} />);
    await waitFor(() =>
      expect(
        screen.getByLabelText(/scikit-learn__scikit-learn-25102/),
      ).toBeInTheDocument(),
    );

    // Nothing selected → the button is inert (no accidental whole-split grade).
    expect(
      screen.getByRole('button', { name: /gold-validate selected \(0\)/i }),
    ).toBeDisabled();

    await userEvent.click(
      screen.getByLabelText(/scikit-learn__scikit-learn-25102/),
    );
    await userEvent.click(
      screen.getByRole('button', { name: /gold-validate selected \(1\)/i }),
    );
    await waitFor(() => expect(api.validateImages).toHaveBeenCalled());
    expect(vi.mocked(api.validateImages).mock.calls[0][0]).toEqual([
      'scikit-learn__scikit-learn-25102',
    ]);
    // No inference is launched by a validation.
    expect(api.launchRun).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/gold grades enqueued: 1/),
    ).toBeInTheDocument();
    expect(screen.getByText(/skipped 1/)).toBeInTheDocument();
    expect(
      screen.getByText('image-validation-20260905T070000Z-ab12'),
    ).toBeInTheDocument();
  });
});
