"""L1 pacer against a real Redis — BUILDER4-HARNESS-AUTOSCALER-EXACT-DESIGN-2026-09-01.md §4.

Real Redis (the local compose valkey), because the whole point of the Lua script is atomicity
under genuine concurrency — a fake that serializes in Python would test nothing. Marked
integration alongside the real-Postgres suites.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from swebench_eval.gateway.pacer import Admission, Pacer, PacerTimeout

pytestmark = pytest.mark.integration

_ALIAS = "pacer-test-alias"


def _sync_redis():
    import os

    import redis

    return redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))


@pytest.fixture(autouse=True)
def _clean_keys():
    r = _sync_redis()

    def _clean():
        for key in r.scan_iter(f"pacer:*:{{{_ALIAS}}}"):
            r.delete(key)

    _clean()
    yield
    _clean()
    r.close()


def _set_cfg(**cfg: float) -> None:
    r = _sync_redis()
    r.hset(f"pacer:cfg:{{{_ALIAS}}}", mapping={k: repr(v) for k, v in cfg.items()})
    r.close()


def _run(coro):
    return asyncio.run(coro)


class TestAdmission:
    def test_admits_and_debits_both_buckets_and_records_inflight(self) -> None:
        _set_cfg(c_burst=100_000, r_tok=1_000, k_inflight=500_000, c_req=10, r_qps=1)

        async def _go():
            p = Pacer()
            adm = await p.acquire(_ALIAS, 40_000, hold_cap_s=2)
            return adm

        adm = _run(_go())
        assert isinstance(adm, Admission)
        assert adm.fallback is False

        r = _sync_redis()
        level = float(r.hget(f"pacer:bucket:{{{_ALIAS}}}", "level"))
        rlevel = float(r.hget(f"pacer:reqbucket:{{{_ALIAS}}}", "level"))
        inflight = r.hgetall(f"pacer:inflight:{{{_ALIAS}}}")
        r.close()
        assert level == pytest.approx(60_000, abs=1_500)  # 100K - 40K (small refill drift ok)
        assert rlevel == pytest.approx(9, abs=0.1)
        assert len(inflight) == 1
        assert next(iter(inflight.values())).decode().startswith("40000:")

    def test_token_bucket_denies_until_refill(self) -> None:
        # Tiny bucket, fast refill: first call drains it; second must WAIT ~est/r_tok, then pass.
        _set_cfg(c_burst=10_000, r_tok=20_000, k_inflight=1_000_000, c_req=100, r_qps=100)

        async def _go():
            p = Pacer()
            await p.acquire(_ALIAS, 10_000, hold_cap_s=5)
            t0 = time.monotonic()
            adm2 = await p.acquire(_ALIAS, 10_000, hold_cap_s=5)
            return time.monotonic() - t0, adm2

        waited, adm2 = _run(_go())
        assert waited >= 0.20  # ~0.5s of refill needed; jittered polling
        assert adm2.paced_wait_ms >= 200

    def test_request_bucket_is_independent_of_token_volume(self) -> None:
        """E12-E16: the request axis binds even when token volume is trivial."""
        _set_cfg(c_burst=10_000_000, r_tok=1_000_000, k_inflight=50_000_000, c_req=3, r_qps=0.001)

        async def _go():
            p = Pacer()
            results = await asyncio.gather(
                *(p.acquire(_ALIAS, 10, hold_cap_s=0.6) for _ in range(6)),
                return_exceptions=True,
            )
            return results

        results = _run(_go())
        admitted = [x for x in results if isinstance(x, Admission)]
        timed_out = [x for x in results if isinstance(x, PacerTimeout)]
        assert len(admitted) == 3  # exactly c_req — tokens were abundant, requests were not
        assert len(timed_out) == 3

    def test_inflight_cap_blocks_then_release_unblocks(self) -> None:
        _set_cfg(c_burst=10_000_000, r_tok=1_000_000, k_inflight=50_000, c_req=100, r_qps=100)

        async def _go():
            p = Pacer()
            a1 = await p.acquire(_ALIAS, 40_000, hold_cap_s=2)
            # 40K in flight; another 40K would exceed k_inflight=50K.
            with pytest.raises(PacerTimeout):
                await p.acquire(_ALIAS, 40_000, hold_cap_s=0.8)
            await p.release(_ALIAS, a1)
            a2 = await p.acquire(_ALIAS, 40_000, hold_cap_s=2)
            return a2

        assert isinstance(_run(_go()), Admission)

    def test_stale_inflight_member_is_pruned_not_counted(self) -> None:
        """A crashed worker's reservation must not leak budget forever."""
        _set_cfg(c_burst=10_000_000, r_tok=1_000_000, k_inflight=50_000, c_req=100, r_qps=100)
        r = _sync_redis()
        # A 40K member started 10 minutes ago (>> TTL 300s).
        r.hset(f"pacer:inflight:{{{_ALIAS}}}", "dead-call", f"40000:{time.time() - 600}")
        r.close()

        async def _go():
            p = Pacer()
            return await p.acquire(_ALIAS, 40_000, hold_cap_s=2)

        assert isinstance(_run(_go()), Admission)  # stale 40K did not count against the cap
        r = _sync_redis()
        assert r.hget(f"pacer:inflight:{{{_ALIAS}}}", "dead-call") is None  # and was deleted
        r.close()

    def test_concurrent_acquires_never_exceed_the_request_cap(self) -> None:
        """The atomicity property itself: 40 simultaneous acquires against c_req=10 must admit
        EXACTLY 10 — no double-spend window, however unlucky the interleaving."""
        _set_cfg(c_burst=100_000_000, r_tok=1, k_inflight=500_000_000, c_req=10, r_qps=0.001)

        async def _go():
            p = Pacer()
            results = await asyncio.gather(
                *(p.acquire(_ALIAS, 100, hold_cap_s=0.5) for _ in range(40)),
                return_exceptions=True,
            )
            return sum(1 for x in results if isinstance(x, Admission))

        assert _run(_go()) == 10

    def test_kappa_updates_from_real_usage_on_release(self) -> None:
        _set_cfg(c_burst=1_000_000, r_tok=100_000, k_inflight=5_000_000, c_req=100, r_qps=100)

        async def _go():
            p = Pacer()
            k0 = await p.get_kappa(_ALIAS)
            adm = await p.acquire(_ALIAS, 1000, hold_cap_s=2)
            # Observed: 10,000 chars / 1,250 real tokens = 8.0 chars/token (vs initial 4.89).
            await p.release(_ALIAS, adm, real_prompt_tokens=1_250, request_chars=10_000)
            p._kappa_cache.clear()
            k1 = await p.get_kappa(_ALIAS)
            return k0, k1

        k0, k1 = _run(_go())
        assert k1 > k0  # moved toward the observed ratio
        assert k1 == pytest.approx(k0 * 0.95 + 8.0 * 0.05, rel=1e-3)


class TestCachedWeight:
    """F3 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): the Lua draws the WEIGHTED charge from
    the arrival bucket while the in-flight reservation stays the full estimate, and release()
    settles the estimated charge against the provider's real prompt/cached split."""

    def test_admission_draws_the_charge_not_the_estimate(self) -> None:
        _set_cfg(c_burst=100_000, r_tok=1_000, k_inflight=500_000, c_req=10, r_qps=1)

        async def _go():
            p = Pacer()
            return await p.acquire(_ALIAS, 40_000, hold_cap_s=2, charge_tokens=4_000)

        adm = _run(_go())
        assert adm.charge_tokens == 4_000
        r = _sync_redis()
        level = float(r.hget(f"pacer:bucket:{{{_ALIAS}}}", "level"))
        inflight = r.hgetall(f"pacer:inflight:{{{_ALIAS}}}")
        r.close()
        assert level == pytest.approx(96_000, abs=1_500)  # 100K - the 4K CHARGE, not 40K
        assert next(iter(inflight.values())).decode().startswith("40000:")  # full est held

    def test_a_weighted_stream_admits_where_full_price_would_wait(self) -> None:
        """Ten 10K-token calls against a 10K bucket: at full price the second waits on the
        refill; at weight 0.05 all ten fit in one burst (10 x 500 = 5K)."""
        _set_cfg(c_burst=10_000, r_tok=100, k_inflight=10_000_000, c_req=100, r_qps=100)

        async def _go():
            p = Pacer()
            t0 = time.monotonic()
            for _ in range(10):
                await p.acquire(_ALIAS, 10_000, hold_cap_s=5, charge_tokens=500)
            return time.monotonic() - t0

        assert _run(_go()) < 1.0

    def test_release_settles_the_real_split_against_the_charge(self) -> None:
        _set_cfg(
            c_burst=100_000, r_tok=0.001, k_inflight=500_000, c_req=10, r_qps=1, cached_weight=0.1
        )

        async def _go():
            p = Pacer()
            # Estimated: 40K prompt of which 30K cached -> charge 10K + 3K = 13K.
            adm = await p.acquire(_ALIAS, 40_000, hold_cap_s=2, charge_tokens=13_000)
            # Real: 40K prompt of which 39K cached -> actual 1K + 3.9K = 4.9K. Refund 8.1K.
            await p.release(
                _ALIAS, adm, real_prompt_tokens=40_000, request_chars=1, real_cached_tokens=39_000
            )
            return adm

        _run(_go())
        r = _sync_redis()
        level = float(r.hget(f"pacer:bucket:{{{_ALIAS}}}", "level"))
        r.close()
        assert level == pytest.approx(100_000 - 13_000 + 8_100, abs=50)

    def test_release_debits_when_the_prefix_hit_less_than_estimated(self) -> None:
        _set_cfg(
            c_burst=100_000, r_tok=0.001, k_inflight=500_000, c_req=10, r_qps=1, cached_weight=0.1
        )

        async def _go():
            p = Pacer()
            adm = await p.acquire(_ALIAS, 40_000, hold_cap_s=2, charge_tokens=4_000)
            # Nothing hit: actual = 40K full price -> a further 36K debit.
            await p.release(
                _ALIAS, adm, real_prompt_tokens=40_000, request_chars=1, real_cached_tokens=0
            )

        _run(_go())
        r = _sync_redis()
        level = float(r.hget(f"pacer:bucket:{{{_ALIAS}}}", "level"))
        r.close()
        assert level == pytest.approx(100_000 - 40_000, abs=50)

    def test_get_cached_weight_reads_cfg_and_defaults_to_full_price(self) -> None:
        async def _go():
            p = Pacer()
            before = await p.get_cached_weight(_ALIAS)
            _set_cfg(cached_weight=0.25)
            p._weight_cache.clear()
            after = await p.get_cached_weight(_ALIAS)
            _set_cfg(cached_weight=0.0)  # below the floor -> clamped
            p._weight_cache.clear()
            floored = await p.get_cached_weight(_ALIAS)
            return before, after, floored

        before, after, floored = _run(_go())
        assert before == 1.0
        assert after == pytest.approx(0.25)
        assert floored == pytest.approx(0.05)


class TestOrphanHead:
    def test_orphan_heads_are_dropped_and_the_real_head_is_reserved(self) -> None:
        """Item 19 (2026-09-04): two orphaned ZSET members (no est record) sit ahead of a real
        waiter reserving 90K. A new 20K call must see THAT reservation (need 110K -> clamped
        to the 100K bucket, level 50K -> denied), not treat itself as head and jump the queue;
        and the orphans are gone afterwards."""
        _set_cfg(c_burst=100_000, r_tok=1_000, k_inflight=10_000_000, c_req=100, r_qps=100)
        r = _sync_redis()
        now = time.time()
        r.hset(f"pacer:bucket:{{{_ALIAS}}}", mapping={"level": "50000", "upd": repr(now)})
        # Scores as the Lua would have written them (first_denied - charge / r_tok): the real
        # 90K waiter sits at now - 100, well ahead of where a new 20K call lands (now - 20).
        r.zadd(
            f"pacer:waitq:{{{_ALIAS}}}",
            {"orphan-1": now - 300, "orphan-2": now - 200, "real": now - 100},
        )
        r.hset(f"pacer:waitest:{{{_ALIAS}}}", mapping={"real": f"90000:{now}"})
        r.close()

        async def _go():
            p = Pacer()
            with pytest.raises(PacerTimeout):
                await p.acquire(_ALIAS, 20_000, hold_cap_s=0.6)

        _run(_go())
        r = _sync_redis()
        members = {m.decode() for m in r.zrange(f"pacer:waitq:{{{_ALIAS}}}", 0, -1)}
        r.close()
        assert "orphan-1" not in members and "orphan-2" not in members
        assert "real" in members  # the real head keeps its place
