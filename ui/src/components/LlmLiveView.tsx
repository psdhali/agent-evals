import { useQuery } from '@tanstack/react-query';
import { Fragment, useEffect, useState } from 'react';
import { api, ApiError, type LlmLiveCall } from '../lib/api';
import { IS_DEMO } from '../lib/dataMode';
import { fmtNum } from '../lib/format';
import { cn } from '../lib/utils';
import { Card, CardContent, CardHeader, CardTitle } from './ui-primitives';

// Live LLM-call trajectory (BUILDER4-LITELLM-SPEND-DB-LIVE-VIEW-2026-09-02).
// The gateway writes one spend-log row per call (batched at 5 — seconds
// behind), and this panel reads them back through GET /runs/{id}/llm-live —
// the ONLY trajectory that exists while an attempt is still running (the S3
// trajectory artifacts land at attempt end). Facts the rendering leans on:
//   - `spend` is deliberately not surfaced by the API: LiteLLM prices our
//     custom models at 0.0 — cost truth is the ledger's cost_usd, elsewhere.
//   - text_tokens vs cached_tokens is the per-call cache split — the cheap
//     live health signal (a healthy agent loop reads ~99% from cache; a big
//     text_tokens spike mid-run means a cache miss / compaction rebuild).
//   - a 503 means the spend DB isn't wired/reachable. That renders as its
//     own degraded notice — an empty list must never impersonate it.
//
// Owner feedback (run 1, 2026-09-02): this panel lives on the INSTANCE detail
// screen (scoped to one attempt), auto-refresh pauses while a call is open so
// the row being read never scrolls away, and the conversation renders in the
// TrajectoryViewer's turn style rather than a raw dump.

const ROLE_STYLE: Record<string, string> = {
  user: 'bg-sky-50 text-sky-700 dark:bg-sky-500/10 dark:text-sky-300',
  assistant: 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300',
  system: 'bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300',
  tool: 'bg-violet-50 text-violet-700 dark:bg-violet-500/10 dark:text-violet-300',
};

// Messages carry either a plain string or provider content blocks; a call's
// message list is the WHOLE conversation snapshot, so the tail is the new part.
const OPEN_TAIL = 2; // last N messages open; the rest collapse to labels

function blockText(c: unknown): string {
  if (typeof c === 'string') return c;
  if (Array.isArray(c))
    return c
      .map((b) => {
        const blk = b as Record<string, unknown>;
        if (typeof blk.text === 'string') return blk.text;
        if (blk.type === 'tool_use')
          return `[tool_use: ${String(blk.name ?? '?')}] ${JSON.stringify(blk.input ?? {}).slice(0, 600)}`;
        if (blk.type === 'tool_result')
          return `[tool_result] ${blockText(blk.content)}`;
        if (blk.type === 'thinking')
          return `[thinking] ${String(blk.thinking ?? '')}`;
        return '';
      })
      .filter(Boolean)
      .join('\n');
  return '';
}

function msgLabel(content: unknown): string {
  const t = blockText(content).trim().replace(/\s+/g, ' ');
  return t.length > 110 ? `${t.slice(0, 110)}…` : t || '(empty)';
}

// Framework/harness-INJECTED tags that ride inside message content but are not
// the model's own prose (BUILDER2 handover 1b: a literal
// `<total_tokens>… tokens left</total_tokens>` sitting mid-conversation reads as
// noise). Dimmed inline so they're visibly structural, not deleted (still the
// honest record) and not mistaken for model output.
const INJECTED_TAGS = ['total_tokens', 'system-reminder', 'budget'];
const INJECTED_SPLIT = new RegExp(
  `(<(?:${INJECTED_TAGS.join('|')})>[\\s\\S]*?</(?:${INJECTED_TAGS.join('|')})>)`,
  'g',
);
function isInjected(s: string): boolean {
  return new RegExp(
    `^<(?:${INJECTED_TAGS.join('|')})>[\\s\\S]*</(?:${INJECTED_TAGS.join('|')})>$`,
  ).test(s);
}

/** Prose with injected framework tags dimmed apart from the model's text. */
function Prose({ text }: { text: string }) {
  const parts = text.slice(0, 20_000).split(INJECTED_SPLIT);
  return (
    <div className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-zinc-700 dark:text-zinc-300">
      {parts.map((p, i) =>
        isInjected(p) ? (
          <span
            key={i}
            className="rounded bg-zinc-100 px-1 text-[10px] text-zinc-400 dark:bg-zinc-800 dark:text-zinc-500"
            title="framework-injected, not model output"
          >
            {p}
          </span>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </div>
  );
}

/** Give thinking / tool_use / tool_result their own treatment (closer to the
 *  TrajectoryViewer's turn style) instead of one undifferentiated dump. */
function ContentBlocks({ content }: { content: unknown }) {
  if (typeof content === 'string') return <Prose text={content} />;
  if (!Array.isArray(content)) return <Prose text={blockText(content)} />;
  return (
    <div className="space-y-1.5">
      {content.map((b, i) => {
        const blk = b as Record<string, unknown>;
        const type = blk.type;
        if (type === 'thinking') {
          return (
            <details key={i} className="rounded bg-zinc-50 dark:bg-zinc-950">
              <summary className="cursor-pointer px-2 py-1 text-[10px] text-zinc-400">
                thinking
              </summary>
              <div className="px-2 pb-2">
                <Prose text={String(blk.thinking ?? '')} />
              </div>
            </details>
          );
        }
        if (type === 'tool_use') {
          return (
            <div
              key={i}
              className="rounded border border-violet-200 bg-violet-50/50 p-1.5 dark:border-violet-900/60 dark:bg-violet-950/20"
            >
              <span className="font-mono text-[10px] font-semibold text-violet-700 dark:text-violet-300">
                tool_use · {String(blk.name ?? '?')}
              </span>
              <Prose
                text={JSON.stringify(blk.input ?? {}, null, 2).slice(0, 4000)}
              />
            </div>
          );
        }
        if (type === 'tool_result') {
          return (
            <div
              key={i}
              className="rounded border border-emerald-200 bg-emerald-50/40 p-1.5 dark:border-emerald-900/50 dark:bg-emerald-950/20"
            >
              <span className="font-mono text-[10px] font-semibold text-emerald-700 dark:text-emerald-300">
                tool_result
              </span>
              <Prose text={blockText(blk.content)} />
            </div>
          );
        }
        const t = typeof blk.text === 'string' ? blk.text : '';
        return t ? <Prose key={i} text={t} /> : null;
      })}
    </div>
  );
}

// OpenAI-shaped tool call: {id, type:'function', function:{name, arguments}}.
// opencode/codex/mini put the command HERE, on the assistant message, not in a
// content block — so a content-only renderer (fine for Anthropic) shows the
// prose but drops the command. `arguments` is a JSON string; pretty-print it.
function OpenAIToolCall({ call }: { call: Record<string, unknown> }) {
  const fn = (call.function ?? {}) as Record<string, unknown>;
  const rawArgs = fn.arguments;
  let pretty =
    typeof rawArgs === 'string'
      ? rawArgs
      : JSON.stringify(rawArgs ?? {}, null, 2);
  if (typeof rawArgs === 'string') {
    try {
      pretty = JSON.stringify(JSON.parse(rawArgs), null, 2);
    } catch {
      /* leave the raw string */
    }
  }
  return (
    <div className="rounded border border-violet-200 bg-violet-50/50 p-1.5 dark:border-violet-900/60 dark:bg-violet-950/20">
      <span className="font-mono text-[10px] font-semibold text-violet-700 dark:text-violet-300">
        tool_call · {String(fn.name ?? '?')}
      </span>
      <Prose text={pretty.slice(0, 4000)} />
    </div>
  );
}

function Message({
  msg,
  open,
}: {
  msg: Record<string, unknown>;
  open: boolean;
}) {
  const [expanded, setExpanded] = useState(open);
  const role = String(msg.role ?? '?');
  const content = msg.content;
  // reasoning arrives under either name (LiteLLM/deepseek); tool_calls is the
  // OpenAI function-call array. Both are message-level siblings of content.
  const reasoning =
    typeof msg.reasoning_content === 'string'
      ? msg.reasoning_content
      : typeof msg.reasoning === 'string'
        ? msg.reasoning
        : '';
  const toolCalls = Array.isArray(msg.tool_calls)
    ? (msg.tool_calls as Record<string, unknown>[])
    : [];
  const label = toolCalls.length
    ? `${toolCalls
        .map((t) =>
          String((t.function as Record<string, unknown>)?.name ?? 'tool'),
        )
        .join(', ')} call${toolCalls.length > 1 ? 's' : ''}`
    : msgLabel(content);
  const hasContent =
    typeof content === 'string' ? content.trim().length > 0 : Boolean(content);
  return (
    <div className="rounded-md border border-zinc-200 dark:border-zinc-800">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center gap-2 p-1.5 text-left"
      >
        <span
          className={cn(
            'rounded px-1.5 py-0.5 font-mono text-[10px]',
            ROLE_STYLE[role] ?? ROLE_STYLE.tool,
          )}
        >
          {role}
        </span>
        {!expanded && (
          <span className="truncate text-[11px] text-zinc-500">{label}</span>
        )}
      </button>
      {expanded && (
        <div className="max-h-72 space-y-1.5 overflow-y-auto border-t border-zinc-100 p-2 dark:border-zinc-800">
          {reasoning && (
            <details className="rounded bg-zinc-50 dark:bg-zinc-950">
              <summary className="cursor-pointer px-2 py-1 text-[10px] text-zinc-400">
                reasoning
              </summary>
              <div className="px-2 pb-2">
                <Prose text={reasoning} />
              </div>
            </details>
          )}
          {hasContent && <ContentBlocks content={content} />}
          {toolCalls.map((tc, i) => (
            <OpenAIToolCall key={i} call={tc} />
          ))}
          {!reasoning && !hasContent && toolCalls.length === 0 && (
            <span className="text-[11px] text-zinc-400">(empty)</span>
          )}
        </div>
      )}
    </div>
  );
}

function respText(blocks: unknown): string {
  if (typeof blocks === 'string') return blocks;
  if (!Array.isArray(blocks)) return '';
  return blocks
    .map((b) =>
      typeof (b as Record<string, unknown>)?.text === 'string'
        ? String((b as Record<string, unknown>).text)
        : '',
    )
    .filter(Boolean)
    .join('\n');
}

// One item of the OpenAI Responses API `response.output` (codex). Unlike
// chat/completions, the conversation is a list of typed items: function_call
// (the tool call), message (assistant text), reasoning. The spend row captures
// only THIS call's output — the request `input` (history) isn't stored — so
// this shows what the call produced, not the whole conversation.
function ResponseItem({ item }: { item: Record<string, unknown> }) {
  const type = String(item.type ?? '');
  if (type === 'function_call') {
    return (
      <OpenAIToolCall
        call={{ function: { name: item.name, arguments: item.arguments } }}
      />
    );
  }
  if (type === 'message') {
    const role = String(item.role ?? 'assistant');
    return (
      <div className="rounded-md border border-zinc-200 p-1.5 dark:border-zinc-800">
        <span
          className={cn(
            'rounded px-1.5 py-0.5 font-mono text-[10px]',
            ROLE_STYLE[role] ?? ROLE_STYLE.assistant,
          )}
        >
          {role}
        </span>
        <Prose text={respText(item.content)} />
      </div>
    );
  }
  if (type === 'reasoning') {
    const txt = respText(item.content) || respText(item.summary);
    return txt ? (
      <details className="rounded bg-zinc-50 dark:bg-zinc-950">
        <summary className="cursor-pointer px-2 py-1 text-[10px] text-zinc-400">
          reasoning
        </summary>
        <div className="px-2 pb-2">
          <Prose text={txt} />
        </div>
      </details>
    ) : null;
  }
  return <div className="text-[10px] text-zinc-400">[{type || 'item'}]</div>;
}

function CallDetail({
  runId,
  requestId,
}: {
  runId: string;
  requestId: string;
}) {
  const detail = useQuery({
    queryKey: ['llm-live-detail', runId, requestId],
    queryFn: () => api.llmLiveDetail(runId, requestId),
    staleTime: 60_000, // a written row never changes
  });
  if (detail.isLoading)
    return <div className="p-3 text-sm opacity-70">loading call…</div>;
  if (detail.error || !detail.data)
    return (
      <div className="p-3 text-sm text-red-500">could not load call detail</div>
    );
  const d = detail.data;
  const msgs = d.messages ?? [];
  // codex uses the Responses API: no `messages`, the output is response.output.
  const respOut = (
    Array.isArray(d.response?.output) ? d.response!.output : []
  ) as Record<string, unknown>[];
  // 2026-09-07: the API now normalises a Responses-API `input` into `messages`
  // (the history), so a codex call renders its history AND its own output;
  // only a row whose body stored no input falls back to output-only.
  const responsesApi = respOut.length > 0;
  const historyMissing = responsesApi && msgs.length === 0;
  return (
    <div className="border-t p-3 space-y-1.5 text-sm max-h-[32rem] overflow-y-auto bg-muted/30">
      <div className="text-xs opacity-70">
        {d.model} · {d.tools_count} tools · session {d.session_id ?? '—'} ·{' '}
        {historyMissing
          ? "Responses API — this call's output only (the spend row stored no request input)"
          : responsesApi
            ? 'Responses API — the request history, then what this call produced'
            : 'this call carries the whole conversation so far — the tail is what’s new'}
      </div>
      {d.system ? (
        <Message msg={{ role: 'system', content: d.system }} open={false} />
      ) : null}
      {msgs.map((m, i) => (
        <Message
          key={i}
          msg={m as Record<string, unknown>}
          open={i >= msgs.length - OPEN_TAIL}
        />
      ))}
      {responsesApi && !historyMissing && (
        <div className="pt-1 text-[10px] uppercase tracking-wide text-zinc-400">
          this call&apos;s output
        </div>
      )}
      {responsesApi &&
        respOut.map((it, i) => <ResponseItem key={i} item={it} />)}
      {msgs.length === 0 && respOut.length === 0 && !d.system && (
        <div className="text-xs opacity-70">
          no conversation captured for this call.
        </div>
      )}
    </div>
  );
}

export function LlmLiveView({
  runId,
  terminal,
  instanceId,
  attempt,
}: {
  runId: string;
  terminal: boolean;
  instanceId?: string;
  /** Scope to ONE attempt of the instance (the instance page is per attempt). */
  attempt?: number;
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const calls = useQuery({
    queryKey: ['llm-live', runId, instanceId ?? null, attempt ?? null],
    queryFn: () => api.llmLiveCalls(runId, { limit: 50, instanceId, attempt }),
    // paused while a call is open — the owner is READING; rows shifting under
    // an expanded conversation was run-1 feedback, not a hypothetical.
    refetchInterval: () => (terminal || openId ? false : 10_000),
    refetchIntervalInBackground: false,
    staleTime: 5000,
    enabled: Boolean(runId),
    retry: (count, err) =>
      // a 503 is a stable degraded state (spend DB not wired), not a blip —
      // retrying it every few seconds just hammers a known-down dependency.
      err instanceof ApiError && err.status === 503 ? false : count < 2,
  });

  // Instance scoping degrades honestly: rows written by a gateway that stored
  // no tie-back coordinates (the 1.99 header regression) can't match the
  // instance filter, so when the scoped list is empty we probe the run level
  // to tell "no calls yet" apart from "calls exist but are unlabeled".
  const scopedEmpty =
    Boolean(instanceId) && !calls.isLoading && calls.data?.items.length === 0;
  const fallback = useQuery({
    queryKey: ['llm-live', runId, '__unscoped-probe'],
    queryFn: () => api.llmLiveCalls(runId, { limit: 50 }),
    enabled: scopedEmpty,
    refetchInterval: () => (terminal || openId ? false : 10_000),
    refetchIntervalInBackground: false,
    staleTime: 5000,
    retry: false,
  });
  // 2026-09-06: the run-level list is shown in place of the scoped one ONLY
  // when the gateway really stored no labels — i.e. no run-level row carries
  // an instance id. A run whose rows ARE labelled (just none for this attempt
  // yet, or only failed calls — LiteLLM logs a provider-rejected call as a
  // `failure` row with no coordinates) must not have another instance's calls
  // rendered on this page as if they were its own.
  const probeRows = fallback.data?.items ?? [];
  const probeLabelled = probeRows.filter((c) => c.instance_id).length;
  const probeFailed = probeRows.filter((c) => c.status === 'failure').length;
  const usingFallback =
    scopedEmpty && probeRows.length > 0 && probeLabelled === 0;
  const labelledElsewhere = scopedEmpty && probeLabelled > 0;

  const unavailable =
    calls.error instanceof ApiError && calls.error.status === 503;
  const newest: LlmLiveCall[] = usingFallback
    ? (fallback.data?.items ?? [])
    : (calls.data?.items ?? []);

  // Older pages (owner, 2026-09-07: a 200-call attempt showed only its newest
  // 50 — "does not show the full trajectory"). The live query keeps refreshing
  // the newest page; older pages are fetched once through the API's keyset
  // cursor (`before` = the oldest started_at shown) and appended below it.
  // They reset whenever the scope changes.
  const [older, setOlder] = useState<LlmLiveCall[]>([]);
  const [olderDone, setOlderDone] = useState(false);
  const [olderLoading, setOlderLoading] = useState(false);
  const [olderError, setOlderError] = useState<string | null>(null);
  useEffect(() => {
    setOlder([]);
    setOlderDone(false);
    setOlderError(null);
  }, [runId, instanceId, attempt]);

  const seen = new Set<string>();
  const items: LlmLiveCall[] = [];
  for (const c of [...newest, ...older]) {
    if (seen.has(c.request_id)) continue;
    seen.add(c.request_id);
    items.push(c);
  }
  const PAGE = 50;
  // Nothing older can exist when the first page came back short.
  const mayHaveOlder = newest.length >= PAGE && !olderDone && !usingFallback;

  const loadOlder = async () => {
    const oldest = items[items.length - 1];
    if (!oldest || olderLoading) return;
    setOlderLoading(true);
    setOlderError(null);
    try {
      const page = await api.llmLiveCalls(runId, {
        limit: PAGE,
        instanceId,
        attempt,
        before: oldest.started_at,
      });
      setOlder((prev) => [...prev, ...page.items]);
      if (page.items.length < PAGE) setOlderDone(true);
    } catch (err) {
      setOlderError(err instanceof Error ? err.message : String(err));
    } finally {
      setOlderLoading(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Live LLM calls
          <span className="ml-2 text-xs font-normal opacity-60">
            {instanceId
              ? `for ${instanceId}${attempt != null ? ` #${attempt}` : ''} · `
              : ''}
            {IS_DEMO
              ? 'RECONSTRUCTED from the captured calls file + trajectory artifacts, newest first — not the gateway spend log'
              : 'from the gateway spend log, newest first — seconds behind the wire'}
          </span>
          {openId && !terminal ? (
            <span className="ml-2 rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-normal text-amber-700 dark:bg-amber-500/10 dark:text-amber-300">
              refresh paused while reading — close the call to resume
            </span>
          ) : null}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {unavailable ? (
          <div className="text-sm text-amber-600">
            live call view unavailable — the spend DB is not configured or not
            reachable (LITELLM_SPEND_DATABASE_URL). The run itself is
            unaffected.
          </div>
        ) : calls.isLoading ? (
          <div className="text-sm opacity-70">loading…</div>
        ) : calls.error ? (
          <div className="text-sm text-red-500">could not load live calls</div>
        ) : items.length === 0 ? (
          <div className="text-sm opacity-70">
            {labelledElsewhere ? (
              <>
                no calls for this {attempt != null ? 'attempt' : 'instance'} yet
                — {probeLabelled} labelled call{probeLabelled === 1 ? '' : 's'}{' '}
                on other instances of this run
                {probeFailed > 0
                  ? `, ${probeFailed} failed call${probeFailed === 1 ? '' : 's'} (provider-rejected, no instance label)`
                  : ''}
              </>
            ) : (
              <>
                no calls recorded yet — rows land a few seconds after the first
                model call (batch write at 5)
              </>
            )}
          </div>
        ) : (
          <div className="overflow-x-auto">
            {usingFallback ? (
              <div className="mb-2 text-xs text-amber-600 dark:text-amber-400">
                calls on this run carry no instance labels (gateway rows without
                tie-back coordinates) — showing every call in the run instead
              </div>
            ) : null}
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="text-left text-xs opacity-60">
                  <th className="p-1">time</th>
                  <th className="p-1">instance / attempt</th>
                  <th className="p-1">harness</th>
                  <th className="p-1 text-right">uncached in</th>
                  <th className="p-1 text-right">cached</th>
                  <th className="p-1 text-right">out</th>
                </tr>
              </thead>
              <tbody>
                {items.map((c) => (
                  <Fragment key={c.request_id}>
                    <tr
                      className="cursor-pointer border-t hover:bg-muted/40"
                      onClick={() =>
                        setOpenId(openId === c.request_id ? null : c.request_id)
                      }
                    >
                      <td className="p-1 font-mono text-xs">
                        {c.started_at.slice(11, 19)}
                      </td>
                      <td className="p-1">
                        {c.status === 'failure' ? (
                          <span
                            className="mr-1 rounded bg-red-50 px-1 text-[10px] text-red-600 dark:bg-red-500/10 dark:text-red-300"
                            title="the provider rejected this call (LiteLLM logged it as a failure; such rows carry no instance label)"
                          >
                            failed call
                          </span>
                        ) : null}
                        {c.instance_id ?? (c.status === 'failure' ? '' : '—')}
                        {c.attempt != null ? ` · #${c.attempt}` : ''}
                      </td>
                      <td className="p-1">{c.harness ?? '—'}</td>
                      <td className="p-1 text-right">
                        {fmtNum(c.text_tokens)}
                      </td>
                      <td className="p-1 text-right">
                        {fmtNum(c.cached_tokens)}
                      </td>
                      <td className="p-1 text-right">
                        {fmtNum(c.completion_tokens)}
                      </td>
                    </tr>
                    {openId === c.request_id ? (
                      <tr>
                        <td colSpan={6}>
                          <CallDetail runId={runId} requestId={c.request_id} />
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                ))}
              </tbody>
            </table>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs opacity-70">
              <span>
                showing {items.length} call{items.length === 1 ? '' : 's'}
                {olderDone || !mayHaveOlder
                  ? ' — all recorded calls'
                  : ' — newest first'}
              </span>
              {mayHaveOlder ? (
                <button
                  type="button"
                  onClick={() => void loadOlder()}
                  disabled={olderLoading}
                  aria-label="load older calls"
                  className="rounded-md border border-zinc-200 px-2 py-0.5 text-[11px] hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-800 dark:hover:bg-zinc-900"
                >
                  {olderLoading ? 'loading…' : `load ${PAGE} older`}
                </button>
              ) : null}
              {olderError ? (
                <span className="text-red-500">
                  could not load older calls: {olderError}
                </span>
              ) : null}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
