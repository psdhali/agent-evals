"""L1 call-start pacer — BUILDER4-HARNESS-AUTOSCALER-EXACT-DESIGN-2026-09-01.md §4.

Every LLM call across the whole fleet passes through here before its bytes go upstream. The
measured reality this enforces (design doc §1-§2, eleven+ live experiments): the shared provider
pools punish *simultaneous arrival* — token-weighted on Poolside (cold edge ~2.13M tokens
admitted at one instant, sagging to ~1.0M hot), request-weighted as a second independent axis
(laguna sustains ~1.85 call starts/s regardless of token volume; 100 tiny simultaneous calls
totalling 25K tokens were massacred) — while *paced* arrival at higher aggregate rate is
completely clean (4.9M tok/min sustained, zero 429s, flat latency).

Coordination model: shims never talk to each other. The shared Redis keys are the only
coordination, and the single Lua script below is the serialization point — Redis executes
scripts single-threaded, so there is no window where two shims both see and both take the same
budget. Refill time comes from **Redis server TIME inside the script**, never worker clocks
(150 Fargate tasks with skewed clocks must not each refill differently). A denied caller holds
NOTHING — budget is debited only on admission — it sleeps (jittered, no retry herd) and re-runs
the script.

Fail-closed: Redis unreachable → per-process local pacing (single-flight min-gap, jittered),
logged loudly. Never "no Redis → no pacing" — an unpaced fleet is the one configuration the
evidence says must not exist (design §8: even `autoscaler_enabled=false` keeps L1 on).

State (per model_alias — the alias is wrapped in a Redis-cluster hash tag, so every key of one
alias lands on ONE slot; the literal key is e.g. ``pacer:cfg:{laguna-xs-2.1-claude_code}``):
  pacer:cfg:{alias}       hash: c_burst, r_tok, k_inflight, c_req, r_qps  (L2 adjusts live)
  pacer:bucket:{alias}    hash: level, upd          — token bucket (arrival tokens)
  pacer:reqbucket:{alias} hash: level, upd          — request bucket (call starts)
  pacer:inflight:{alias}  hash: call_id -> "tokens:started_at"  (pruned in-script, TTL 300s)
  pacer:kappa:{alias}     chars-per-token EWMA (est calibration; initial 4.89, measured)
  pacer:waitq:{alias}     zset: call_id scored by priority  — head-of-line reservation
  pacer:waitest:{alias}   hash: call_id -> "est:first_denied_at"  (pruned in-script, TTL 300s)

Head-of-line fairness (BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03.md §2.4 — after run
01788405363237319353, where a 130K-token call was starved for 10 x 100s by 60-100K siblings that
kept admitting on partial refills): a denied caller registers once in the wait queue with
``score = first_denied_at - est / r_tok`` (a big call is treated as having started waiting
earlier by exactly the time its own need takes to refill — no more). The lowest score is the
HEAD; its need is reserved on every axis, so any other caller may only take the surplus above
it (``level >= est + head.est``, ``rlevel >= 2``, ``inflight + est + head.est <= k_inflight``).
A reservation is a claim on future refill, not a debit — "a denied caller holds nothing
upstream" still holds. Waiters leave the queue on admit, hold-cap timeout, cancellation, or
staleness.

The hash tag is load-bearing (2026-09-02): ElastiCache Serverless is CLUSTER-mode, and without
it the 4-key EVALSHA threw ``ClusterCrossSlotError`` on every call — every harness task silently
ran on the LOCAL fallback and the shared ledger never existed (seen live, run
01788315793731547650).  All readers/writers outside this module MUST build keys through the
``*_key`` helpers below, never with their own f-strings.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Conservative generic defaults, used ONLY when pacer:cfg:{alias} is absent — deliberately the
# tightest measured model's numbers with margin (design §8 table carries the per-model seeds;
# discovery/L2 write the real ones into pacer:cfg).
DEFAULT_C_BURST = 500_000  # tokens
DEFAULT_R_TOK = 20_000  # tokens/s
DEFAULT_K_INFLIGHT = 1_000_000  # tokens
DEFAULT_C_REQ = 30  # requests
DEFAULT_R_QPS = 1.5  # requests/s  (0.8 x laguna's measured 1.85/s — E16)

INFLIGHT_TTL_S = 300  # > hold cap (240s) + longest observed call — never reap a live call
DEFAULT_HOLD_CAP_S = 240.0  # total hold per call; must stay under the smallest CLI HTTP timeout
# Review F9: a waiter's prune horizon is its own hold cap plus this margin (a live waiter
# always times out and dequeues itself first; only a dead one reaches the prune).
_WAITER_TTL_MARGIN_S = 10.0
KAPPA_INITIAL = 4.89  # chars/token, measured (212,936 real tokens / 1,040,589 chars)
_KAPPA_ALPHA = 0.05
_KAPPA_MIN, _KAPPA_MAX = 2.0, 8.0

_FALLBACK_MIN_GAP_S = 2.5  # local single-flight pacing when Redis is unreachable

_LONG_WAIT_LOG_S = 5.0  # first WARNING once a call has waited this long at the pacer
_LONG_WAIT_LOG_EVERY_S = 30.0  # then again every this often — the during-load probe, built in

# One atomic admission decision. Returns
#   {1, 0,       '',   level_after, rlevel*1000, inflight_after, queue_len, head_est, is_head, r_tok, k_inf}
#   {0, wait_ms, axis, level,       rlevel*1000, inflight_sum,   queue_len, head_est, is_head, r_tok, k_inf}
# where axis names the FIRST failing condition ('tok' | 'req' | 'inflight'). The trailing
# fields are diagnostics — the bucket state DURING a deny is what the matplotlib investigation
# never had. Redis TIME inside the script — worker clocks are never trusted.
_ADMIT_LUA = """
local cfg_key, bucket_key, req_key, inflight_key = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local waitq_key, waitest_key = KEYS[5], KEYS[6]
local est      = tonumber(ARGV[1])
local call_id  = ARGV[2]
local ttl      = tonumber(ARGV[3])
local d_cburst = tonumber(ARGV[4])
local d_rtok   = tonumber(ARGV[5])
local d_kinf   = tonumber(ARGV[6])
local d_creq   = tonumber(ARGV[7])
local d_rqps   = tonumber(ARGV[8])
-- Review F9 (2026-09-03): waiters are pruned on their OWN horizon (the caller's hold cap plus
-- a margin), not the 300 s in-flight TTL — a SIGKILLed head must not pin its reservation for
-- a minute longer than any live waiter could exist.
local wttl     = tonumber(ARGV[9]) or ttl
-- F3 (2026-09-04): the WEIGHTED charge drawn from the arrival bucket — uncached tokens at full
-- price plus the estimated cached prefix x cached_weight (the shim computes it). The in-flight
-- reservation stays the full est (KV residency is the same whether or not the prefix hit).
local charge   = tonumber(ARGV[10]) or est

local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local cfg = {}
local raw = redis.call('HGETALL', cfg_key)
for i = 1, #raw, 2 do cfg[raw[i]] = tonumber(raw[i + 1]) end
local c_burst = cfg['c_burst'] or d_cburst
local r_tok   = cfg['r_tok'] or d_rtok
local k_inf   = cfg['k_inflight'] or d_kinf
local c_req   = cfg['c_req'] or d_creq
local r_qps   = cfg['r_qps'] or d_rqps

-- prune stale in-flight members (crashed workers must not leak budget), sum the live ones
local inflight_sum = 0
local inf = redis.call('HGETALL', inflight_key)
for i = 1, #inf, 2 do
  local sep = string.find(inf[i + 1], ':')
  local tokens = tonumber(string.sub(inf[i + 1], 1, sep - 1))
  local started = tonumber(string.sub(inf[i + 1], sep + 1))
  if now - started > ttl then
    redis.call('HDEL', inflight_key, inf[i])
  else
    inflight_sum = inflight_sum + tokens
  end
end

-- prune stale waiters (a crashed/cancelled shim must not hold the head slot forever)
local we = redis.call('HGETALL', waitest_key)
for i = 1, #we, 2 do
  local sep = string.find(we[i + 1], ':')
  local registered = tonumber(string.sub(we[i + 1], sep + 1))
  if now - registered > wttl then
    redis.call('HDEL', waitest_key, we[i])
    redis.call('ZREM', waitq_key, we[i])
  end
end

-- head-of-line: the lowest score waits with the highest priority. Its need is reserved on
-- every axis for everyone else; the head itself (or a caller with no one ahead) needs only
-- its own est.
local head_est = 0
local is_head = 1
-- Item 19 (2026-09-04): an orphan head (a ZSET member with no est record) is dropped and the
-- NEXT head is read, bounded — the old single read returned as if THIS call were the head,
-- letting it jump every real waiter behind the orphan.
for _ = 1, 16 do
  local head = redis.call('ZRANGE', waitq_key, 0, 0)
  if head[1] == nil or head[1] == call_id then break end
  local hv = redis.call('HGET', waitest_key, head[1])
  if hv then
    local sep = string.find(hv, ':')
    head_est = tonumber(string.sub(hv, 1, sep - 1))
    is_head = 0
    break
  end
  redis.call('ZREM', waitq_key, head[1])  -- orphan without an est record: drop, re-read
end
local queue_len = redis.call('ZCARD', waitq_key)

-- refill both buckets from server time (missing bucket state = full, the safe start)
local function refill(key, cap, rate)
  local level = tonumber(redis.call('HGET', key, 'level'))
  local upd = tonumber(redis.call('HGET', key, 'upd'))
  if level == nil or upd == nil then return cap end
  local l = level + (now - upd) * rate
  if l > cap then l = cap end
  return l
end
local level = refill(bucket_key, c_burst, r_tok)
local rlevel = refill(req_key, c_req, r_qps)

-- Review F1 (2026-09-03): a need larger than the bucket itself can NEVER be met (level is
-- capped at c_burst; a call with est > c_burst was denied for its whole hold cap and, as head,
-- froze every sibling behind its reservation). Clamp the need to the bucket: a call that
-- cannot fit is admitted alone against a FULL bucket and drives the level negative, so the
-- refill it owes is still paid by everyone after it — the debt is honoured, not waived. Same
-- on the in-flight axis against k_inf.
-- head_est is registered in CHARGE units (the head's own weighted need), so the reservation
-- and this call's draw are on the same scale.
local need_tok = math.min(charge + head_est, c_burst)
local need_req = 1
if is_head == 0 then need_req = 2 end
local need_inf = inflight_sum + math.min(est + head_est, k_inf)
local axis = ''
if level < need_tok then axis = 'tok'
elseif rlevel < need_req then axis = 'req'
elseif need_inf > k_inf then axis = 'inflight'
end

if axis == '' then
  redis.call('HSET', bucket_key, 'level', tostring(level - charge), 'upd', tostring(now))
  redis.call('HSET', req_key, 'level', tostring(rlevel - 1), 'upd', tostring(now))
  redis.call('HSET', inflight_key, call_id, tostring(est) .. ':' .. tostring(now))
  redis.call('ZREM', waitq_key, call_id)
  redis.call('HDEL', waitest_key, call_id)
  redis.call('EXPIRE', bucket_key, 3600)
  redis.call('EXPIRE', req_key, 3600)
  redis.call('EXPIRE', inflight_key, 3600)
  return {1, 0, '', math.floor(level - charge), math.floor(rlevel * 1000),
          math.floor(inflight_sum + est), queue_len, math.floor(head_est), is_head,
          math.floor(r_tok), math.floor(k_inf)}
end

-- deny: persist the refilled levels (so drain is observable), register the waiter ONCE with
-- its priority (first_denied_at - charge / r_tok), hand back a wait hint that includes the
-- head's reservation.
redis.call('HSET', bucket_key, 'level', tostring(level), 'upd', tostring(now))
redis.call('HSET', req_key, 'level', tostring(rlevel), 'upd', tostring(now))
if redis.call('HEXISTS', waitest_key, call_id) == 0 then
  redis.call('HSET', waitest_key, call_id, tostring(charge) .. ':' .. tostring(now))
  local prio = now
  if r_tok > 0 then prio = now - charge / r_tok end
  redis.call('ZADD', waitq_key, 'NX', tostring(prio), call_id)
  queue_len = queue_len + 1
end
redis.call('EXPIRE', bucket_key, 3600)
redis.call('EXPIRE', req_key, 3600)
redis.call('EXPIRE', waitq_key, 3600)
redis.call('EXPIRE', waitest_key, 3600)
local wait_s = 0.25
if need_tok > level and r_tok > 0 then
  local w = (need_tok - level) / r_tok
  if w > wait_s then wait_s = w end
end
if rlevel < need_req and r_qps > 0 then
  local w = (need_req - rlevel) / r_qps
  if w > wait_s then wait_s = w end
end
return {0, math.ceil(wait_s * 1000), axis, math.floor(level), math.floor(rlevel * 1000),
        math.floor(inflight_sum), queue_len, math.floor(head_est), is_head,
        math.floor(r_tok), math.floor(k_inf)}
"""


class PacerTimeout(Exception):
    """Total hold cap exceeded — the caller surfaces the pressure honestly (a 429/error),
    it does not wait past the point where the CLI's own HTTP timeout would fire first."""


@dataclass(frozen=True)
class Admission:
    call_id: str
    est_tokens: int
    paced_wait_ms: int
    fallback: bool  # True when granted by the local (Redis-down) fallback, not the shared ledger
    # Diagnostics (design doc §2.5) — what the shim writes onto the call record so a wait is
    # explainable after the fact: was this call ever denied, how deep was the queue when it
    # finally admitted, and which axis denied it last.
    was_queued: bool = False
    queue_len_at_admit: int = 0
    deny_axis_last: str | None = None
    # F3 (2026-09-04): the weighted amount actually drawn from the arrival bucket (0 = an
    # older admission that drew est_tokens). release() settles the real usage against it.
    charge_tokens: int = 0


@dataclass(frozen=True)
class _Verdict:
    """One Lua admission result, decoded. Every field past ``wait_ms`` is the bucket state the
    script saw — on a deny, exactly the during-load measurement the original investigation
    lacked."""

    admitted: bool
    wait_ms: int
    axis: str  # '' on admit; 'tok' | 'req' | 'inflight' names the first failing condition
    level: int
    rlevel_x1000: int
    inflight_sum: int
    queue_len: int
    head_est: int
    is_head: bool
    r_tok: int
    k_inflight: int

    def describe(self) -> str:
        return (
            f"level={self.level} rlevel={self.rlevel_x1000 / 1000:.2f} "
            f"inflight={self.inflight_sum}/{self.k_inflight} r_tok={self.r_tok}/s "
            f"queue_len={self.queue_len} head_est={self.head_est} "
            f"is_head={int(self.is_head)} axis={self.axis or '-'}"
        )


CACHED_WEIGHT_MIN = 0.05
CACHED_WEIGHT_DEFAULT = 1.0  # full price until a Phase D probe writes pacer:cfg cached_weight


def weighted_charge(est_tokens: int, cached_est_tokens: int, cached_weight: float) -> int:
    """F3: the arrival-bucket draw for a prompt of *est_tokens* of which *cached_est_tokens*
    are expected cache hits — ``uncached + cached x w``, never below 1."""
    cached = max(0, min(int(cached_est_tokens), int(est_tokens)))
    w = min(1.0, max(CACHED_WEIGHT_MIN, float(cached_weight)))
    return max(1, round((est_tokens - cached) + cached * w))


def pacer_cfg_key(alias: str) -> str:
    """``pacer:cfg:{alias}`` with the cluster hash tag — the ONE builder every
    cfg reader/writer (dispatcher, run_launch, ceiling_discovery,
    capacity_observer) must use."""
    return f"pacer:cfg:{{{alias}}}"


def pacer_bucket_key(alias: str) -> str:
    return f"pacer:bucket:{{{alias}}}"


def kappa_key(alias: str) -> str:
    return f"pacer:kappa:{{{alias}}}"


def overload_key(alias: str, bucket: int) -> str:
    return f"overload:{{{alias}}}:{bucket}"


def paced_key(alias: str, bucket: int) -> str:
    return f"paced:{{{alias}}}:{bucket}"


def alias_from_bucket_key(key: str) -> str | None:
    """Inverse of :func:`pacer_bucket_key` for scan_iter consumers — strips the
    hash-tag braces.  Returns None for a key that is not a pacer bucket."""
    if not key.startswith("pacer:bucket:"):
        return None
    alias = key.split("pacer:bucket:", 1)[1]
    if alias.startswith("{") and alias.endswith("}"):
        alias = alias[1:-1]
    return alias


def pacer_reqbucket_key(alias: str) -> str:
    return f"pacer:reqbucket:{{{alias}}}"


def pacer_inflight_key(alias: str) -> str:
    return f"pacer:inflight:{{{alias}}}"


def pacer_waitq_key(alias: str) -> str:
    return f"pacer:waitq:{{{alias}}}"


def pacer_waitest_key(alias: str) -> str:
    return f"pacer:waitest:{{{alias}}}"


def _keys(alias: str) -> tuple[str, str, str, str, str, str]:
    return (
        pacer_cfg_key(alias),
        pacer_bucket_key(alias),
        pacer_reqbucket_key(alias),
        pacer_inflight_key(alias),
        pacer_waitq_key(alias),
        pacer_waitest_key(alias),
    )


class Pacer:
    """One instance per shim process. All methods are safe under Redis failure (fallback)."""

    def __init__(self, redis_url: str | None = None) -> None:
        self._redis_url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        self._client: Any = None  # lazy — the shim must come up even with Redis down
        self._script_sha: str | None = None
        # Local fallback state (per process): single-flight min-gap pacing.
        self._fallback_lock = asyncio.Lock()
        self._fallback_last_start = 0.0
        self._kappa_cache: dict[str, tuple[float, float]] = {}  # alias -> (kappa, fetched_at)
        self._weight_cache: dict[str, tuple[float, float]] = {}  # alias -> (weight, fetched_at)

    async def _redis(self) -> Any:
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.Redis.from_url(self._redis_url)
        return self._client

    async def _admit_once(
        self,
        alias: str,
        est: int,
        call_id: str,
        waiter_ttl_s: float = DEFAULT_HOLD_CAP_S + _WAITER_TTL_MARGIN_S,
        charge: int | None = None,
    ) -> _Verdict:
        client = await self._redis()
        if self._script_sha is None:
            self._script_sha = await client.script_load(_ADMIT_LUA)
        args = (
            est,
            call_id,
            INFLIGHT_TTL_S,
            DEFAULT_C_BURST,
            DEFAULT_R_TOK,
            DEFAULT_K_INFLIGHT,
            DEFAULT_C_REQ,
            DEFAULT_R_QPS,
            waiter_ttl_s,
            charge if charge is not None else est,
        )
        try:
            res = await client.evalsha(self._script_sha, 6, *_keys(alias), *args)
        except Exception as exc:  # NOSCRIPT after a Redis restart — reload once
            if "NOSCRIPT" in str(exc):
                self._script_sha = await client.script_load(_ADMIT_LUA)
                res = await client.evalsha(self._script_sha, 6, *_keys(alias), *args)
            else:
                raise
        axis = res[2]
        if isinstance(axis, bytes):
            axis = axis.decode()
        return _Verdict(
            admitted=bool(int(res[0])),
            wait_ms=int(res[1]),
            axis=str(axis or ""),
            level=int(res[3]),
            rlevel_x1000=int(res[4]),
            inflight_sum=int(res[5]),
            queue_len=int(res[6]),
            head_est=int(res[7]),
            is_head=bool(int(res[8])),
            r_tok=int(res[9]),
            k_inflight=int(res[10]),
        )

    async def _dequeue(self, alias: str, call_id: str) -> None:
        """Leave the wait queue on every non-admit exit (hold-cap timeout, cancellation) so a
        dead waiter never holds the head reservation. Best-effort; the in-script TTL prune is
        the backstop."""
        try:
            client = await self._redis()
            pipe = client.pipeline()
            pipe.zrem(pacer_waitq_key(alias), call_id)
            pipe.hdel(pacer_waitest_key(alias), call_id)
            await pipe.execute()
        except Exception:
            logger.debug("pacer: dequeue failed (TTL prune is the backstop)", exc_info=True)

    async def acquire(
        self,
        alias: str,
        est_tokens: int,
        *,
        hold_cap_s: float = DEFAULT_HOLD_CAP_S,
        charge_tokens: int | None = None,
    ) -> Admission:
        """Block (async) until admitted or the hold cap expires. A waiting call reserves
        nothing upstream; budget is debited only at the moment of admission. A denied call
        holds a head-of-line RESERVATION (a claim on future refill) so it cannot be starved by
        smaller siblings — see the module docstring.

        ``charge_tokens`` (F3): the WEIGHTED draw from the arrival bucket (see
        :func:`weighted_charge`); None draws the full estimate. The in-flight reservation is
        always the full *est_tokens*."""
        charge = charge_tokens if charge_tokens is not None else est_tokens
        call_id = uuid.uuid4().hex
        deadline = time.monotonic() + hold_cap_s
        started = time.monotonic()
        last: _Verdict | None = None
        polls = 0
        next_log_at = started + _LONG_WAIT_LOG_S
        waiter_ttl_s = hold_cap_s + _WAITER_TTL_MARGIN_S
        try:
            while True:
                try:
                    verdict = await self._admit_once(
                        alias, est_tokens, call_id, waiter_ttl_s, charge=charge
                    )
                except Exception:
                    logger.warning(
                        "pacer: Redis unreachable — LOCAL FALLBACK pacing engaged (fail-closed)",
                        exc_info=True,
                    )
                    return await self._acquire_fallback(call_id, est_tokens, started)
                polls += 1
                if verdict.admitted:
                    wait_ms_total = round((time.monotonic() - started) * 1000)
                    # The measured-arrival ledger is in the same weighted units as r_tok.
                    await self._publish_wait_stats(alias, wait_ms_total, est_tokens=charge)
                    if last is not None and wait_ms_total >= _LONG_WAIT_LOG_S * 1000:
                        logger.info(
                            "pacer: %s admitted est=%d after %.1fs / %d polls (last deny: %s)",
                            alias,
                            est_tokens,
                            wait_ms_total / 1000,
                            polls,
                            last.describe(),
                        )
                    return Admission(
                        call_id=call_id,
                        est_tokens=est_tokens,
                        paced_wait_ms=wait_ms_total,
                        fallback=False,
                        was_queued=last is not None,
                        queue_len_at_admit=verdict.queue_len,
                        deny_axis_last=last.axis if last is not None else None,
                        charge_tokens=charge,
                    )
                last = verdict
                now = time.monotonic()
                if now >= next_log_at:
                    # The during-load probe: bucket state WHILE a call is being denied.
                    logger.warning(
                        "pacer: %s still waiting est=%d after %.1fs / %d polls — %s",
                        alias,
                        est_tokens,
                        now - started,
                        polls,
                        verdict.describe(),
                    )
                    next_log_at = now + _LONG_WAIT_LOG_EVERY_S
                if now >= deadline:
                    await self._dequeue(alias, call_id)
                    # Forecast review §3: a timeout is the WORST wait and used to be invisible
                    # to the planner's paced signal (only admissions published). Count it.
                    await self._publish_wait_stats(
                        alias, round((now - started) * 1000), timeout=True
                    )
                    logger.warning(
                        "pacer: HOLD CAP %.0fs exceeded for %s est=%d after %d polls — "
                        "last state: %s",
                        hold_cap_s,
                        alias,
                        est_tokens,
                        polls,
                        verdict.describe(),
                    )
                    raise PacerTimeout(
                        f"pacer hold cap {hold_cap_s:.0f}s exceeded for {alias} "
                        f"(est {est_tokens} tokens; last deny {verdict.describe()}) — "
                        "surfacing pressure instead of out-waiting the CLI's own HTTP timeout"
                    )
                sleep_s = min(max(0.25, verdict.wait_ms / 1000.0), 5.0) * random.uniform(0.75, 1.25)
                sleep_s = min(sleep_s, max(0.05, deadline - time.monotonic()))
                await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            # Client disconnected mid-wait (the shim's handler task is cancelled): leave the
            # queue immediately so the CLI's own retry re-enters cleanly behind nobody.
            if last is not None:
                await asyncio.shield(self._dequeue(alias, call_id))
            raise

    async def _acquire_fallback(self, call_id: str, est: int, started: float) -> Admission:
        """Redis-down path: per-process single-flight min-gap. Fleet-wide arrival is then
        bounded by task count × 1/gap even with zero coordination — degraded, never unpaced."""
        async with self._fallback_lock:
            now = time.monotonic()
            gap = _FALLBACK_MIN_GAP_S * random.uniform(0.8, 1.4)
            wait = self._fallback_last_start + gap - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._fallback_last_start = time.monotonic()
        return Admission(
            call_id=call_id,
            est_tokens=est,
            paced_wait_ms=round((time.monotonic() - started) * 1000),
            fallback=True,
        )

    async def release(
        self,
        alias: str,
        admission: Admission,
        *,
        real_prompt_tokens: int | None = None,
        request_chars: int | None = None,
        real_cached_tokens: int | None = None,
    ) -> None:
        """Free the in-flight reservation (every terminal path: success, error, client
        disconnect). Also folds the real usage back into the κ estimate when available, and
        (F3) SETTLES the arrival bucket: the admission drew an estimated weighted charge; the
        provider's reported prompt/cached split gives the true one, and the difference is
        applied to the bucket level (a refund when the prefix hit more than estimated, a
        further debit when it hit less). The refill clamp keeps a refund from overfilling."""
        if admission.fallback:
            return  # nothing was reserved on the shared ledger
        try:
            client = await self._redis()
            await client.hdel(_keys(alias)[3], admission.call_id)
            if real_prompt_tokens and real_prompt_tokens > 0 and admission.charge_tokens > 0:
                weight = await self.get_cached_weight(alias)
                actual = weighted_charge(real_prompt_tokens, int(real_cached_tokens or 0), weight)
                delta = actual - admission.charge_tokens
                if delta:
                    await client.hincrbyfloat(_keys(alias)[1], "level", -float(delta))
            if real_prompt_tokens and request_chars and real_prompt_tokens > 0:
                observed = request_chars / real_prompt_tokens
                kappa = await self.get_kappa(alias)
                updated = max(
                    _KAPPA_MIN,
                    min(_KAPPA_MAX, kappa * (1 - _KAPPA_ALPHA) + observed * _KAPPA_ALPHA),
                )
                await client.set(kappa_key(alias), repr(updated), ex=7 * 24 * 3600)
                self._kappa_cache[alias] = (updated, time.monotonic())
        except Exception:
            logger.warning(
                "pacer: release/kappa update failed (harmless — TTL prunes)", exc_info=True
            )

    async def _publish_wait_stats(
        self, alias: str, wait_ms: int, *, timeout: bool = False, est_tokens: int = 0
    ) -> None:
        """Wait telemetry for L2's pacer-empty back-pressure (design §5): per-10s bucket counts
        of admissions and of waits over 2s — enough to approximate 'p95 wait > 2s' as
        n_over_2s/n > 0.05 without shipping every sample — plus, since the forecast review,
        ``n_timeout`` for calls that hit the hold cap (they never admit, so they were never
        counted: the worst waits were invisible), and ``tok`` (review F2, 2026-09-03): the
        admitted token estimate, so the planner can read the MEASURED arrival rate of the last
        60 s (Σtok/60) rather than only its own projection. Fire-and-forget."""
        try:
            client = await self._redis()
            bucket = int(time.time() // 10)
            key = paced_key(alias, bucket)
            pipe = client.pipeline()
            if timeout:
                pipe.hincrby(key, "n_timeout", 1)
            else:
                pipe.hincrby(key, "n", 1)
                if wait_ms > 2_000:
                    pipe.hincrby(key, "n_over_2s", 1)
                pipe.hincrby(key, "sum_ms", wait_ms)
                if est_tokens > 0:
                    pipe.hincrby(key, "tok", int(est_tokens))
            pipe.expire(key, 120)
            await pipe.execute()
        except Exception:
            logger.debug("pacer: wait-stats publish failed", exc_info=True)

    async def record_overload(self, alias: str) -> None:
        """§2.3 congestion counter: ``overload:{alias}:{now // 10}`` INCR + unconditional
        EXPIRE 120 (idempotent, self-healing). ``//`` is floor division, NOT modulo — the index
        rises forever; ``% 10`` would wrap every 100s and mix ancient counts into the window
        (the original spec calls this out as the obvious 'simplification' that is wrong).
        Fire-and-forget: a counter failure must never affect the call path."""
        try:
            client = await self._redis()
            bucket = int(time.time() // 10)
            key = overload_key(alias, bucket)
            pipe = client.pipeline()
            pipe.incr(key)
            pipe.expire(key, 120)
            await pipe.execute()
        except Exception:
            logger.warning(
                "pacer: overload counter publish failed (fire-and-forget)", exc_info=True
            )

    async def get_cached_weight(self, alias: str) -> float:
        """F3: ``pacer:cfg:{alias}`` ``cached_weight`` (a Phase D probe writes it), clamped to
        [CACHED_WEIGHT_MIN, 1.0]; 1.0 (full price) when unset or unreadable. Cached 60 s."""
        cached = self._weight_cache.get(alias)
        if cached and time.monotonic() - cached[1] < 60:
            return cached[0]
        weight = CACHED_WEIGHT_DEFAULT
        try:
            client = await self._redis()
            raw = await client.hget(pacer_cfg_key(alias), "cached_weight")
            if raw:
                weight = min(1.0, max(CACHED_WEIGHT_MIN, float(raw)))
        except Exception:  # noqa: BLE001 — any Redis failure reads as full price
            weight = CACHED_WEIGHT_DEFAULT
        self._weight_cache[alias] = (weight, time.monotonic())
        return weight

    async def get_kappa(self, alias: str) -> float:
        cached = self._kappa_cache.get(alias)
        if cached and time.monotonic() - cached[1] < 60:
            return cached[0]
        try:
            client = await self._redis()
            raw = await client.get(kappa_key(alias))
            kappa = float(raw) if raw else KAPPA_INITIAL
        except Exception:  # noqa: BLE001 — any Redis failure degrades to the measured initial
            kappa = KAPPA_INITIAL
        self._kappa_cache[alias] = (kappa, time.monotonic())
        return kappa

    async def estimate_tokens(self, alias: str, request_chars: int) -> int:
        return max(1, int(request_chars / await self.get_kappa(alias)))
