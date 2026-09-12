"""Live L1 pacer state for a run — GET /runs/{run_id}/pacer
(BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03.md §2.5).

Reads ONLY what the pacer already keeps in Redis — cfg, both buckets, the in-flight members,
the wait queue with every waiter's ``est`` and ``first_denied_at``, the 10 s wait-stat and
overload counters — and shapes it per alias. Nothing here is a second source of truth: it is
the same ledger every shim's Lua script reads, rendered. Advisory + best-effort like ``/live``:
Redis unreachable → the WHOLE response is ``unknown``; an alias with no pacer keys at all is
``measured=False`` with every number None — never zeros that read as "idle and healthy".

The wait queue is the point. Run 01788405363237319353's 130K call sat denied for 10 x 100 s
and nothing showed it; with this, a waiter is a row: its size, how long it has waited, and
what the bucket looks like right now.
"""

from __future__ import annotations

import time
from typing import Any

from swebench_eval.gateway.pacer import (
    INFLIGHT_TTL_S,
    overload_key,
    paced_key,
    pacer_bucket_key,
    pacer_cfg_key,
    pacer_inflight_key,
    pacer_reqbucket_key,
    pacer_waitest_key,
    pacer_waitq_key,
)

_STATS_WINDOW_BUCKETS = 6  # 6 x 10 s = the last 60 s of wait-stat / overload counters


def _s(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _hash(client: Any, key: str) -> dict[str, str]:
    raw = client.hgetall(key) or {}
    return {_s(k): _s(v) for k, v in raw.items()}


def _f(d: dict[str, str], k: str) -> float | None:
    v = d.get(k)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def resolve_pacer_aliases(targets: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``(harness, run_targets.model_alias)`` → ``(harness, the alias the pacer keys on)``.

    The pacer keys on the per-harness rotatable alias (``rotatable_models``: ``f"{prefix}-
    {harness}"``). A run target may already carry that alias or only the family prefix;
    resolve whichever the registry knows, and fall back to the target's own name so an
    unknown alias still renders (as not-measured) rather than vanishing.
    """
    from swebench_eval.gateway.rotatable_models import ROTATABLE_MODELS

    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for harness, model_alias in targets:
        candidate = f"{model_alias}-{harness}"
        if model_alias in ROTATABLE_MODELS:
            alias = model_alias
        elif candidate in ROTATABLE_MODELS:
            alias = candidate
        else:
            alias = model_alias
        if (harness, alias) not in seen:
            seen.add((harness, alias))
            out.append((harness, alias))
    return out


def read_alias_state(
    client: Any, alias: str, harness: str, *, now: float | None = None
) -> dict[str, Any]:
    """One alias's live pacer state, from the ledger the shims share. Bucket levels are
    extrapolated to *now* with the cfg refill rate (the script itself refills lazily on
    the next call, so the stored level is as of the last admission attempt)."""
    now = time.time() if now is None else now
    cfg = _hash(client, pacer_cfg_key(alias))
    c_burst, r_tok = _f(cfg, "c_burst"), _f(cfg, "r_tok")
    k_inf, c_req, r_qps = _f(cfg, "k_inflight"), _f(cfg, "c_req"), _f(cfg, "r_qps")

    def _bucket(
        key: str, cap: float | None, rate: float | None
    ) -> tuple[float | None, float | None]:
        b = _hash(client, key)
        level, upd = _f(b, "level"), _f(b, "upd")
        if level is None:
            return None, None
        if upd is not None and rate:
            level = level + max(0.0, now - upd) * rate
        if cap:
            level = min(level, cap)
        fill = (level / cap) if cap else None
        return round(level, 1), (round(fill, 4) if fill is not None else None)

    bucket_level, bucket_fill = _bucket(pacer_bucket_key(alias), c_burst, r_tok)
    req_level, req_fill = _bucket(pacer_reqbucket_key(alias), c_req, r_qps)

    inflight_calls: int | None = None
    inflight_tokens: int | None = None
    inflight = _hash(client, pacer_inflight_key(alias))
    if inflight or cfg:
        inflight_calls, inflight_tokens = 0, 0
        for v in inflight.values():
            try:
                tokens_s, started_s = v.split(":", 1)
                if now - float(started_s) > INFLIGHT_TTL_S:
                    continue  # the script would prune it; do not count a dead member
                inflight_calls += 1
                inflight_tokens += int(float(tokens_s))
            except (ValueError, TypeError):
                continue
    inflight_fill = (
        round(inflight_tokens / k_inf, 4) if inflight_tokens is not None and k_inf else None
    )

    waiters: list[dict[str, Any]] = []
    try:
        members = client.zrange(pacer_waitq_key(alias), 0, -1, withscores=True) or []
    except Exception:  # noqa: BLE001 — a client without ZSET support reads as "no queue seen"
        members = []
    waitest = _hash(client, pacer_waitest_key(alias)) if members else {}
    for member, _score in members:
        raw = waitest.get(_s(member))
        if not raw:
            continue
        try:
            est_s, first_s = raw.split(":", 1)
            waiters.append(
                {"est_tokens": int(float(est_s)), "waiting_s": round(now - float(first_s), 1)}
            )
        except (ValueError, TypeError):
            continue

    n = over = sum_ms = overloads = 0
    stats_seen = False
    b = int(now // 10)
    for i in range(_STATS_WINDOW_BUCKETS):
        h = _hash(client, paced_key(alias, b - i))
        if h:
            stats_seen = True
            n += int(float(h.get("n", 0)))
            over += int(float(h.get("n_over_2s", 0)))
            sum_ms += int(float(h.get("sum_ms", 0)))
        raw_ov = client.get(overload_key(alias, b - i))
        if raw_ov is not None:
            stats_seen = True
            try:
                overloads += int(float(_s(raw_ov)))
            except (ValueError, TypeError):
                pass

    measured = bool(cfg) or bucket_level is not None or bool(waiters) or stats_seen
    return {
        "alias": alias,
        "harness": harness,
        "measured": measured,
        "c_burst": c_burst,
        "r_tok": r_tok,
        "k_inflight": k_inf,
        "c_req": c_req,
        "r_qps": r_qps,
        "seeded_at": _f(cfg, "seeded_at"),
        "bucket_level": bucket_level,
        "bucket_fill": bucket_fill,
        "req_level": req_level,
        "req_fill": req_fill,
        "inflight_calls": inflight_calls,
        "inflight_tokens": inflight_tokens,
        "inflight_fill": inflight_fill,
        "queue_len": len(waiters) if measured else None,
        "head_est_tokens": waiters[0]["est_tokens"] if waiters else None,
        "head_waiting_s": waiters[0]["waiting_s"] if waiters else None,
        "waiters": waiters,
        "admits_60s": n if stats_seen else None,
        "over_2s_60s": over if stats_seen else None,
        "mean_wait_ms_60s": (round(sum_ms / n, 1) if stats_seen and n > 0 else None),
        "overloads_60s": overloads if stats_seen else None,
        "observed_at": now,
    }
