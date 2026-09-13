import { describe, expect, it } from 'vitest';
import { messagesForCall } from '../demoApi';

// The call-detail panel's conversation is rebuilt from the trajectory artifact.
// Three harness conventions are covered: turns that carry `content` (Claude Code,
// Codex, custom_minimal) must come through unchanged; a turn whose only text is
// `reasoning` (mini-swe-agent, OpenCode before a tool call) shows that text as
// the message; OpenCode's usage-only `result` turns are call boundaries, not
// messages.

describe('messagesForCall', () => {
  it('leaves content-carrying trajectories unchanged', () => {
    const turns = [
      { turn: 0, role: 'user', content: 'fix the bug' },
      {
        turn: 1,
        role: 'assistant',
        content: 'Looking at the file.',
        reasoning: 'I should read it first',
        tool_calls: [{ id: 'c1', name: 'bash', arguments: '{"cmd":"cat a.py"}' }],
      },
      { turn: 2, role: 'tool', name: 'bash', output: 'print(1)' },
      { turn: 3, role: 'assistant', content: 'Done.' },
    ];
    expect(messagesForCall(turns, 1)).toEqual([
      { role: 'user', content: 'fix the bug' },
      {
        role: 'assistant',
        content: 'Looking at the file.',
        reasoning_content: 'I should read it first',
        tool_calls: [
          {
            id: 'c1',
            type: 'function',
            function: { name: 'bash', arguments: '{"cmd":"cat a.py"}' },
          },
        ],
      },
    ]);
    const second = messagesForCall(turns, 2);
    expect(second).toHaveLength(4);
    expect(second[2]).toEqual({ role: 'tool', content: '[bash]\nprint(1)' });
    expect(second[3]).toEqual({ role: 'assistant', content: 'Done.' });
  });

  it('shows reasoning as the message when content is empty (mini-swe-agent)', () => {
    const turns = [
      { turn: 0, role: 'user', content: 'task' },
      { turn: 1, role: 'assistant', content: '', reasoning: 'THOUGHT: list files' },
      {
        turn: 2,
        role: 'tool',
        name: 'bash',
        normalized: { command: 'ls' },
        output: 'a.py',
      },
      { turn: 3, role: 'assistant', content: '', reasoning: 'THOUGHT: read a.py' },
    ];
    const msgs = messagesForCall(turns, 2);
    expect(msgs[1]).toEqual({ role: 'assistant', content: 'THOUGHT: list files' });
    expect(msgs[1]).not.toHaveProperty('reasoning_content');
    expect(msgs[2]).toEqual({ role: 'tool', content: '[bash] ls\na.py' });
    expect(msgs[3]).toEqual({ role: 'assistant', content: 'THOUGHT: read a.py' });
  });

  it('drops usage-only result turns but keeps them as call boundaries (OpenCode)', () => {
    const turns = [
      { turn: 0, role: 'user', content: 'task' },
      { turn: 1, role: 'assistant', content: '', reasoning: 'look around' },
      { turn: 2, role: 'tool', name: 'bash', normalized: { command: 'ls' }, output: 'a.py' },
      { turn: 3, role: 'result', usage: { input: 10, output: 2 } },
      { turn: 4, role: 'assistant', content: 'Here is the fix.' },
      { turn: 5, role: 'result', usage: { input: 12, output: 3 } },
    ];
    // call 1 = turns 1-2; the result at 3 ends it; call 2 = turn 4
    expect(messagesForCall(turns, 1)).toEqual([
      { role: 'user', content: 'task' },
      { role: 'assistant', content: 'look around' },
    ]);
    const all = messagesForCall(turns, 2);
    expect(all.map((m) => m.role)).toEqual(['user', 'assistant', 'tool', 'assistant']);
    expect(all.some((m) => m.role === 'tool' && m.content === '')).toBe(false);
    // a result that does carry output is still a tool message
    const withBody = [
      { turn: 0, role: 'user', content: 'task' },
      { turn: 1, role: 'assistant', content: 'run it' },
      { turn: 2, role: 'result', output: 'ok' },
      { turn: 3, role: 'assistant', content: 'done' },
    ];
    expect(messagesForCall(withBody, 2)[2]).toEqual({ role: 'tool', content: '\nok' });
  });
});
