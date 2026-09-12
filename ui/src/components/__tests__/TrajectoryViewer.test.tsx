import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { TrajectoryViewer } from '../TrajectoryViewer';

// Builders against the real normalized trajectory.jsonl shape (see the
// harness trajectory export) — one JSON object per line, roles
// user/assistant/tool, no fabricated fields the production path doesn't write.

const RAW = [
  JSON.stringify({
    turn: 0,
    role: 'user',
    content: 'Fix the bug in sort.py',
    ts: '2026-08-28T10:00:00+00:00',
  }),
  JSON.stringify({
    turn: 1,
    role: 'assistant',
    content: '',
    tool_calls: [
      { id: 'tc-1', name: 'Agent', input: 'Read sort.py' },
      { id: 'tc-2', name: 'Bash', input: 'ls -la' },
    ],
    reasoning: 'Let me look at the file first.',
    ts: '2026-08-28T10:00:01+00:00',
  }),
  JSON.stringify({
    turn: 2,
    role: 'tool',
    tool_call_id: 'tc-1',
    name: 'Agent',
    output: 'file contents',
    stdout: 'stdout-line',
    stderr: '',
    ts: '2026-08-28T10:00:02+00:00',
  }),
  JSON.stringify({
    turn: 3,
    role: 'assistant',
    content: '',
    tool_calls: [{ id: 'tc-3', name: 'Edit', input: 'Apply the fix' }],
    reasoning: 'Write the patch.',
    ts: '2026-08-28T10:00:03+00:00',
  }),
].join('\n');

describe('TrajectoryViewer — structured, collapsible turns', () => {
  it('splits the trajectory into one collapsible block per turn', () => {
    render(<TrajectoryViewer raw={RAW} />);
    // The four turn headers render (turn numbers).
    expect(screen.getByText('#0')).toBeInTheDocument();
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(screen.getByText('#2')).toBeInTheDocument();
    expect(screen.getByText('#3')).toBeInTheDocument();
    // Count summary.
    expect(screen.getByText(/4 turns/)).toBeInTheDocument();
  });

  it('expanding a collapsed assistant turn reveals its tool calls and reasoning', async () => {
    render(<TrajectoryViewer raw={RAW} />);
    // First 3 turns are open by default; turn 3 (assistant w/ Edit tool call)
    // is collapsed.  Expand it and its tool-call chip + reasoning appear.
    expect(screen.queryByText('Edit')).not.toBeInTheDocument();
    await userEvent.click(screen.getByText('#3'));
    // Tool-call chip renders with the tool name.
    expect(screen.getByText('Edit')).toBeInTheDocument();
    // A reasoning collapsible is present once an assistant turn is open
    // (turn 1 is also open by default, so it may appear more than once).
    expect(screen.getAllByText('reasoning').length).toBeGreaterThan(0);
  });

  it('a tool turn exposes its stdout/output (turns 0-2 open by default)', () => {
    render(<TrajectoryViewer raw={RAW} />);
    expect(screen.getByText('stdout')).toBeInTheDocument();
    expect(screen.getByText('output')).toBeInTheDocument();
  });

  it('can switch to the raw JSON view and back', async () => {
    render(<TrajectoryViewer raw={RAW} />);
    await userEvent.click(screen.getByRole('button', { name: 'raw JSON' }));
    // The structured turn header is gone; the raw button now says "back".
    expect(screen.queryByText('#0')).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '← structured view' }),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole('button', { name: '← structured view' }),
    );
    expect(screen.getByText('#0')).toBeInTheDocument();
  });
});

// 2026-09-07: custom_minimal (and any adapter keeping the provider's tool_call
// verbatim) writes assistant tool calls as OpenAI `{id, name, arguments}` with
// `arguments` a JSON STRING, not claude's `{id, name, input}`. Expanding such a
// turn used to throw `undefined.slice` in ToolCallRow and, with no boundary,
// blank the whole page.
const RAW_OPENAI_SHAPE = [
  JSON.stringify({ turn: 0, role: 'system', content: 'You are an engineer.' }),
  JSON.stringify({ turn: 1, role: 'user', content: 'Fix it.' }),
  JSON.stringify({ turn: 2, role: 'assistant', content: 'Looking.' }),
  JSON.stringify({ turn: 3, role: 'tool', name: 'bash', output: 'x' }),
  JSON.stringify({
    turn: 4,
    role: 'assistant',
    content: '',
    tool_calls: [
      {
        id: 'call_1',
        name: 'bash',
        arguments: '{"command": "find /testbed -name \\"*.py\\" | head"}',
      },
      {
        id: 'call_2',
        name: 'str_replace_editor',
        arguments: 'not json at all',
      },
      { id: 'call_3', name: 'bare' },
    ],
  }),
].join('\n');

describe('TrajectoryViewer — OpenAI-shaped tool calls (arguments as a JSON string)', () => {
  it('expands an assistant turn whose calls carry `arguments` instead of `input`', async () => {
    render(<TrajectoryViewer raw={RAW_OPENAI_SHAPE} />);
    // turn 4 is collapsed by default (only the first 3 open)
    await userEvent.click(
      screen.getByText('bash, str_replace_editor, bare calls'),
    );
    // parsed JSON arguments preview the command; a non-JSON string is shown verbatim;
    // a call with neither renders its name and nothing else — no crash.
    expect(screen.getByText(/find \/testbed -name/)).toBeInTheDocument();
    expect(screen.getByText('not json at all')).toBeInTheDocument();
    expect(screen.getByText('bare')).toBeInTheDocument();
  });
});
