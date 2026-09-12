/** formatting helpers shared by the views — numbers, money, durations, times */

export function fmtUsd(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  if (n === 0) return '$0.00';
  if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

export function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return n.toLocaleString('en-US');
}

export function fmtTokens(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

/** seconds → "1h 2m 3s"; null → — */
export function fmtDuration(s: number | null | undefined): string {
  if (s === null || s === undefined || Number.isNaN(s)) return '—';
  const neg = s < 0;
  const a = Math.round(Math.abs(s));
  const h = Math.floor(a / 3600);
  const m = Math.floor((a % 3600) / 60);
  const sec = a % 60;
  const parts =
    h > 0
      ? [`${h}h`, `${m}m`, `${sec}s`]
      : m > 0
        ? [`${m}m`, `${sec}s`]
        : [`${sec}s`];
  return (neg ? '−' : '') + parts.join(' ');
}

/** ISO timestamp → local "2026-08-21 10:42"; null → — */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (x: number) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** epoch float (seconds or ms) → local time string */
export function fmtEpoch(epoch: number | null | undefined): string {
  if (epoch === null || epoch === undefined) return '—';
  const ms = epoch < 1e12 ? epoch * 1000 : epoch;
  return fmtTime(new Date(ms).toISOString());
}

export function fmtPct(p: number | null | undefined): string {
  if (p === null || p === undefined || Number.isNaN(p)) return '—';
  return `${(p * 100).toFixed(1)}%`;
}

export function shortSha(sha: string | undefined, n = 10): string {
  if (!sha) return '—';
  return sha.length > n ? sha.slice(0, n) : sha;
}

export function relativeAge(iso: string | null | undefined): string {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export const POOL_LABELS: Record<string, string> = {
  harness: 'Harness',
  eval: 'Eval',
  gateway: 'Gateway',
};
