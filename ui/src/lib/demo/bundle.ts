import { inflate } from 'pako';
import { ApiError } from '../apiError';
import { laneKey, type LoadedBundle, type ReplayBundle } from './types';

// Static-file loading for demo mode. Everything is a RELATIVE URL under
// `data/` (the Explorer is served from the domain root, hash-routed, so the
// document URL never changes and `data/…` always resolves beside index.html).
// Gzipped files (`*.gz`) are inflated here with pako — Cloudflare Pages serves
// them as opaque bytes, never transparently decoded.

const DATA_BASE = 'data/';

const jsonCache = new Map<string, Promise<unknown>>();
const textCache = new Map<string, Promise<string>>();
const bundleCache = new Map<string, Promise<LoadedBundle>>();
const loadedBundles = new Map<string, LoadedBundle>();

async function fetchBytes(rel: string): Promise<Uint8Array> {
  const res = await fetch(DATA_BASE + rel);
  if (!res.ok) {
    throw new ApiError(
      res.status,
      res.status === 404
        ? `not in this snapshot: ${rel}`
        : `snapshot fetch failed (${res.status}) for ${rel}`,
    );
  }
  return new Uint8Array(await res.arrayBuffer());
}

function inflateText(bytes: Uint8Array): string {
  // A host that sets Content-Encoding: gzip on a .gz file hands the browser
  // already-inflated bytes; only inflate what still carries the gzip magic.
  if (bytes.length >= 2 && bytes[0] === 0x1f && bytes[1] === 0x8b) {
    return inflate(bytes, { toText: true });
  }
  return new TextDecoder().decode(bytes);
}

/** A plain JSON file under data/ (cached forever — snapshots never change). */
export function loadJson<T>(rel: string): Promise<T> {
  let p = jsonCache.get(rel);
  if (!p) {
    p = fetch(DATA_BASE + rel).then(async (res) => {
      if (!res.ok) {
        throw new ApiError(
          res.status,
          res.status === 404
            ? `not in this snapshot: ${rel}`
            : `snapshot fetch failed (${res.status}) for ${rel}`,
        );
      }
      return (await res.json()) as unknown;
    });
    jsonCache.set(rel, p);
    p.catch(() => jsonCache.delete(rel)); // let a transient failure retry
  }
  return p as Promise<T>;
}

/** A gzipped text file under data/, inflated (cached). */
export function loadGzText(rel: string): Promise<string> {
  let p = textCache.get(rel);
  if (!p) {
    p = fetchBytes(rel).then(inflateText);
    textCache.set(rel, p);
    p.catch(() => textCache.delete(rel));
  }
  return p;
}

/** A gzipped JSON file under data/ (cached). */
export async function loadGzJson<T>(rel: string): Promise<T> {
  return JSON.parse(await loadGzText(rel)) as T;
}

function index(bundle: ReplayBundle): LoadedBundle {
  const callsByLane = new Map<number, ReplayBundle['calls']>();
  for (const c of bundle.calls) {
    const arr = callsByLane.get(c[0]);
    if (arr) arr.push(c);
    else callsByLane.set(c[0], [c]);
  }
  for (const arr of callsByLane.values()) arr.sort((a, b) => a[1] - b[1]);
  const laneIndex = new Map<string, number>();
  bundle.lanes.forEach((l, i) =>
    laneIndex.set(laneKey(l.instance_id, l.attempt), i),
  );
  return {
    bundle,
    startEpoch: Date.parse(bundle.window.start) / 1000,
    callsByLane,
    laneIndex,
  };
}

/** The replay bundle for a run, fetched + inflated + indexed once. */
export function loadBundle(runId: string): Promise<LoadedBundle> {
  let p = bundleCache.get(runId);
  if (!p) {
    p = loadGzJson<ReplayBundle>(
      `runs/${encodeURIComponent(runId)}/replay.json.gz`,
    ).then((b) => {
      const loaded = index(b);
      loadedBundles.set(runId, loaded);
      return loaded;
    });
    bundleCache.set(runId, p);
    p.catch(() => bundleCache.delete(runId));
  }
  return p;
}

/** Synchronous access to an already-loaded bundle (the clock needs the window
 * length without awaiting); undefined until loadBundle resolved. */
export function bundleIfLoaded(runId: string): LoadedBundle | undefined {
  return loadedBundles.get(runId);
}

/** Test hook: register a bundle without fetching. */
export function primeBundle(bundle: ReplayBundle): LoadedBundle {
  const loaded = index(bundle);
  loadedBundles.set(bundle.run_id, loaded);
  bundleCache.set(bundle.run_id, Promise.resolve(loaded));
  return loaded;
}
