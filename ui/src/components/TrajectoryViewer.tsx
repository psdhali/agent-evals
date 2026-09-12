import { useMemo, useState } from 'react';
import { cn } from '../lib/utils';
import { SectionLabel } from './ui-primitives';

// A lazy, collapsible reader for the harness trajectory.jsonl artifact.
//
// The trajectory is one JSON object per line (the normalized A2A event stream),
// each a "turn":
//   { "turn": n, "role": "user"|"assistant"|"tool"|"result",
//     "content": str, "ts": iso,
//     "tool_calls": [{ "id", "name", "input" }],   // assistant
//     "reasoning": str, "is_error": bool,           // assistant
//     "name": str, "output": str, "stdout": str, "stderr": str }  // tool
//
// We render it grouped by turn into collapsible blocks so a long run can be
// skimmed (turn number + what the agent did) and expanded to read the detail.
// Turns default collapsed and only the first few open — 400+ turns as an
// open monolith is unusable.

interface TrajMsg {
  turn?: number;
  role?: string;
  content?: string | null;
  ts?: string | null;
  // claude writes `input` (an object); the OpenAI-style harnesses
  // (custom_minimal, and any adapter that keeps the provider's tool_call
  // verbatim) write `arguments` — a JSON STRING. Read both (2026-09-07: an
  // `arguments`-only call crashed the row on `undefined.slice` and, with no
  // boundary above it, blanked the whole page).
  tool_calls?: Array<{
    id?: string;
    name?: string;
    input?: unknown;
    arguments?: unknown;
  }>;
  reasoning?: string | null;
  is_error?: boolean | null;
  name?: string | null;
  output?: string | null;
  stdout?: string | null;
  stderr?: string | null;
  // Cross-harness tool shape (BUILDER2 handover §D): custom_minimal, codex,
  // mini_swe_agent and opencode write the tool call on the TOOL event as
  // `normalized.command` (+ name), never as assistant `tool_calls`; opencode
  // additionally carries a `status` that can literally be "error". The old
  // viewer read none of these — every non-claude tool turn showed no command
  // and no error. Read both shapes.
  normalized?: { command?: string | null; [k: string]: unknown } | null;
  status?: string | null;
}

/** Error state across the harness shapes: claude's explicit `is_error`,
 *  opencode's `status === "error"`, or — for the harnesses that write neither —
 *  a non-zero `returncode` inside a JSON `output` blob. The last is a heuristic
 *  (a structured error field would be firmer), never a false clean. */
function inferError(m: TrajMsg): boolean {
  if (m.is_error) return true;
  if (m.status === 'error') return true;
  if (typeof m.output === 'string' && m.output.trim().startsWith('{')) {
    try {
      const o = JSON.parse(m.output) as { returncode?: unknown };
      if (typeof o.returncode === 'number' && o.returncode !== 0) return true;
    } catch {
      /* not a JSON blob — no signal */
    }
  }
  return false;
}

/** The shell command a tool turn ran, across shapes: `normalized.command`
 *  (mini/opencode/codex/custom_minimal) preferred, else nothing (claude puts
 *  it in the assistant `tool_calls.input`, rendered separately). */
function toolCommand(m: TrajMsg): string | null {
  const cmd = m.normalized?.command;
  return typeof cmd === 'string' && cmd.trim() ? cmd : null;
}

const ROLE_STYLE: Record<string, string> = {
  user: 'bg-sky-50 text-sky-700 ring-sky-600/20 dark:bg-sky-500/10 dark:text-sky-300',
  assistant: 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300',
  tool: 'bg-violet-50 text-violet-700 ring-violet-600/20 dark:bg-violet-500/10 dark:text-violet-300',
  result:
    'bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-500/10 dark:text-emerald-300',
};

const DEFAULT_OPEN = 3; // first N turns open so the head is readable immediately

function parseTrajectory(raw: string): TrajMsg[] {
  const out: TrajMsg[] = [];
  for (const line of raw.split('\n')) {
    const t = line.trim();
    if (!t) continue;
    try {
      out.push(JSON.parse(t) as TrajMsg);
    } catch {
      out.push({ role: 'result', content: line.slice(0, 400) });
    }
  }
  return out;
}

/** A short label for a turn: the tool it called (with its command), or the
 *  first words of text. Handles both the assistant `tool_calls` shape and the
 *  tool-event `name`/`normalized.command` shape. */
function turnLabel(m: TrajMsg): string {
  if (m.tool_calls?.length) {
    const names = m.tool_calls.map((tc) => tc.name ?? 'tool').join(', ');
    return `${names} call${m.tool_calls.length > 1 ? 's' : ''}`;
  }
  if (m.role === 'tool') {
    const name = m.name ?? 'tool';
    const cmd = toolCommand(m);
    const firstLine = cmd ? cmd.split('\n')[0].trim() : '';
    return firstLine ? `${name}: ${firstLine.slice(0, 72)}` : name;
  }
  const text = (m.content ?? m.reasoning ?? '').trim();
  const firstLine = text.split('\n')[0].trim();
  return firstLine ? firstLine.slice(0, 80) : (m.role ?? '');
}

/** The call's input across shapes: claude's `input` object, or the OpenAI
 *  `arguments` JSON string (parsed when it is JSON, kept verbatim when not).
 *  `undefined` when the call carries neither — the row then shows the name
 *  alone rather than throwing. */
function toolCallInput(
  call: NonNullable<TrajMsg['tool_calls']>[0],
): unknown {
  if (call.input !== undefined && call.input !== null) return call.input;
  const args = call.arguments;
  if (typeof args === 'string') {
    try {
      return JSON.parse(args) as unknown;
    } catch {
      return args;
    }
  }
  return args ?? undefined;
}

function ToolCallRow({
  call,
}: {
  call: NonNullable<TrajMsg['tool_calls']>[0];
}) {
  const [open, setOpen] = useState(false);
  const input = toolCallInput(call);
  const inputPreview =
    typeof input === 'string'
      ? input
      : input && typeof input === 'object'
        ? JSON.stringify(input, null, 2)
        : '';
  const inputOneLine =
    typeof input === 'string'
      ? input
      : input && typeof input === 'object'
        ? (JSON.stringify(input) ?? '')
        : '';
  return (
    <div className="rounded-md border border-violet-200 bg-white dark:border-violet-900/60 dark:bg-zinc-900">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-xs"
      >
        <span className="text-[10px] text-zinc-400">{open ? '▾' : '▸'}</span>
        <span className="rounded bg-violet-100 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-violet-700 dark:bg-violet-900/50 dark:text-violet-300">
          {call.name ?? 'tool'}
        </span>
        <span className="truncate text-zinc-500 dark:text-zinc-400">
          {inputOneLine.slice(0, 90)}
        </span>
      </button>
      {open && inputPreview && (
        <pre className="overflow-auto border-t border-violet-100 bg-zinc-50 p-2.5 font-mono text-[11px] leading-relaxed text-zinc-700 dark:border-violet-900/40 dark:bg-zinc-950 dark:text-zinc-300">
          {inputPreview}
        </pre>
      )}
    </div>
  );
}

function Collapse({
  title,
  children,
  defaultOpen = false,
}: {
  title: string;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-md border border-zinc-200 dark:border-zinc-800">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[11px] font-medium text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
      >
        <span className="text-[10px]">{open ? '▾' : '▸'}</span>
        {title}
      </button>
      {open && (
        <div className="border-t border-zinc-200 px-2.5 py-2 dark:border-zinc-800">
          {children}
        </div>
      )}
    </div>
  );
}

function Turn({ m, index }: { m: TrajMsg; index: number }) {
  const [open, setOpen] = useState(index < DEFAULT_OPEN);
  const role = m.role ?? 'result';
  const isError = inferError(m);
  const command = toolCommand(m);

  return (
    <div
      className={cn(
        'rounded-md border dark:border-zinc-800',
        isError ? 'border-rose-300 dark:border-rose-900' : 'border-zinc-200',
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={cn(
          'flex w-full items-center gap-2 px-3 py-2 text-left',
          isError
            ? 'bg-rose-50/60 dark:bg-rose-950/30'
            : 'bg-zinc-50/60 dark:bg-zinc-900/40',
        )}
      >
        <span className="text-[10px] text-zinc-400">{open ? '▾' : '▸'}</span>
        <span className="w-12 shrink-0 font-mono text-[11px] text-zinc-400">
          #{m.turn ?? index}
        </span>
        <span
          className={cn(
            'rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ring-1 ring-inset',
            ROLE_STYLE[role] ?? ROLE_STYLE.result,
          )}
        >
          {role}
        </span>
        <span className="truncate text-xs text-zinc-500 dark:text-zinc-400">
          {turnLabel(m)}
        </span>
        {isError && (
          <span className="ml-auto rounded-full bg-rose-100 px-2 py-0.5 text-[10px] font-semibold text-rose-600 dark:bg-rose-900/50 dark:text-rose-300">
            error
          </span>
        )}
      </button>

      {open && (
        <div className="space-y-2 border-t border-zinc-200 px-3 py-2.5 dark:border-zinc-800">
          {typeof m.content === 'string' && m.content.trim() && (
            <div>
              <SectionLabel>content</SectionLabel>
              <pre className="mt-0.5 whitespace-pre-wrap rounded-md bg-zinc-50 p-2.5 font-mono text-[11px] leading-relaxed text-zinc-700 dark:bg-zinc-950 dark:text-zinc-300">
                {m.content}
              </pre>
            </div>
          )}
          {m.reasoning && (
            <Collapse title="reasoning">
              <pre className="whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-zinc-600 dark:text-zinc-300">
                {m.reasoning}
              </pre>
            </Collapse>
          )}
          {m.tool_calls && m.tool_calls.length > 0 && (
            <div>
              <SectionLabel>tool calls</SectionLabel>
              <div className="mt-1 space-y-1">
                {m.tool_calls.map((tc, i) => (
                  <ToolCallRow key={tc.id ?? i} call={tc} />
                ))}
              </div>
            </div>
          )}
          {role === 'tool' && (
            <div className="space-y-1">
              {m.name && (
                <div className="flex items-center gap-2">
                  <SectionLabel>tool</SectionLabel>
                  <span className="font-mono text-xs text-violet-600 dark:text-violet-300">
                    {m.name}
                  </span>
                  {m.status && (
                    <span
                      className={cn(
                        'font-mono text-[10px]',
                        m.status === 'error'
                          ? 'text-rose-500'
                          : 'text-zinc-400',
                      )}
                    >
                      {m.status}
                    </span>
                  )}
                </div>
              )}
              {command && (
                <div>
                  <SectionLabel>command</SectionLabel>
                  <pre className="mt-0.5 whitespace-pre-wrap rounded-md bg-zinc-50 p-2.5 font-mono text-[11px] leading-relaxed text-zinc-800 dark:bg-zinc-950 dark:text-zinc-200">
                    {command}
                  </pre>
                </div>
              )}
              {m.stdout != null && Boolean(m.stdout) && (
                <Collapse title="stdout">
                  <pre className="whitespace-pre-wrap font-mono text-[11px] text-zinc-600 dark:text-zinc-300">
                    {m.stdout}
                  </pre>
                </Collapse>
              )}
              {m.stderr != null && Boolean(m.stderr) && (
                <Collapse title="stderr">
                  <pre className="whitespace-pre-wrap font-mono text-[11px] text-rose-600 dark:text-rose-300">
                    {m.stderr}
                  </pre>
                </Collapse>
              )}
              {m.output != null && Boolean(m.output) && (
                <Collapse title="output">
                  <pre className="whitespace-pre-wrap font-mono text-[11px] text-zinc-600 dark:text-zinc-300">
                    {m.output}
                  </pre>
                </Collapse>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function TrajectoryViewer({ raw }: { raw: string }) {
  const turns = useMemo(() => parseTrajectory(raw), [raw]);
  const [rawView, setRawView] = useState(false);

  if (rawView) {
    return (
      <div>
        <div className="p-3">
          <RawToggle rawView onRawToggle={() => setRawView(false)} />
        </div>
        <pre className="max-h-[70vh] overflow-auto bg-zinc-50 p-3 font-mono text-[11px] leading-relaxed text-zinc-700 dark:bg-zinc-900 dark:text-zinc-200">
          {raw}
        </pre>
      </div>
    );
  }

  return (
    <div className="space-y-2 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[11px] text-zinc-400">
          {turns.length} turns — click a turn to expand · first {DEFAULT_OPEN}{' '}
          open
        </div>
        <RawToggle rawView={false} onRawToggle={() => setRawView(true)} />
      </div>
      {turns.map((m, i) => (
        <Turn key={`${m.turn ?? i}-${i}`} m={m} index={i} />
      ))}
    </div>
  );
}

function RawToggle({
  rawView,
  onRawToggle,
}: {
  rawView: boolean;
  onRawToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onRawToggle}
      className="rounded-md border border-zinc-300 px-2 py-1 text-[11px] font-medium text-zinc-500 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-800"
    >
      {rawView ? '← structured view' : 'raw JSON'}
    </button>
  );
}
