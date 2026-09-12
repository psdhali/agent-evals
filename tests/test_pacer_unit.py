"""L1 pacer — no-Redis unit coverage: the fail-closed fallback and token estimation."""

from __future__ import annotations

import asyncio
import time

from swebench_eval.gateway.pacer import KAPPA_INITIAL, Pacer


class _DeadRedisPacer(Pacer):
    async def _redis(self):  # every Redis touch fails — the outage scenario
        raise ConnectionError("redis down (test)")


def test_redis_down_engages_local_fallback_pacing_not_unpaced() -> None:
    """Fail-closed: an unreachable Redis must DEGRADE pacing (per-process min-gap,
    single-flight), never disable it — an unpaced fleet is the one configuration the
    measured evidence forbids."""

    async def _go():
        p = _DeadRedisPacer()
        t0 = time.monotonic()
        a1 = await p.acquire("any-alias", 1000, hold_cap_s=30)
        a2 = await p.acquire("any-alias", 1000, hold_cap_s=30)
        return a1, a2, time.monotonic() - t0

    a1, a2, elapsed = asyncio.run(_go())
    assert a1.fallback is True and a2.fallback is True
    # Second acquire must have waited the local min-gap (2.5s x jitter >= 0.8) behind the first.
    assert elapsed >= 2.0


def test_fallback_release_is_a_safe_noop() -> None:
    async def _go():
        p = _DeadRedisPacer()
        adm = await p.acquire("any-alias", 1000, hold_cap_s=30)
        await p.release("any-alias", adm)  # nothing reserved on the shared ledger; must not raise

    asyncio.run(_go())


def test_estimate_uses_initial_kappa_when_redis_down() -> None:
    async def _go():
        p = _DeadRedisPacer()
        return await p.estimate_tokens("any-alias", 48_900)

    assert asyncio.run(_go()) == int(48_900 / KAPPA_INITIAL)
