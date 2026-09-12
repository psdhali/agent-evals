"""L1 pacer against a real Redis — the scaling-review fixes (BUILDER4-SCALING-REVIEW-2026-09-03
F1, F9, F2's ledger). Same compose valkey as test_pacer_integration.py."""

from __future__ import annotations

import asyncio
import time

import pytest

from swebench_eval.gateway.pacer import Pacer, PacerTimeout
from tests.test_pacer_integration import (
    _ALIAS,
    _clean_keys,
    _run,
    _set_cfg,
    _sync_redis,
)

pytestmark = pytest.mark.integration
_FIXTURES = (_clean_keys,)  # the imported autouse fixture applies to this module too


class TestOversizedCallAdmitsAlone:
    """F1: est > c_burst used to be denied for the whole hold cap AND, as head, froze every
    sibling behind its reservation. The reviewer's probe: c_burst 150K, r_tok 30K/s, one 200K
    call plus five 10K siblings, hold cap 8 s — every call timed out at 8.0 s."""

    def test_est_above_c_burst_is_admitted_against_a_full_bucket(self) -> None:
        _set_cfg(c_burst=150_000, r_tok=30_000, k_inflight=285_000, c_req=100, r_qps=100)

        async def _go():
            p = Pacer()
            t0 = time.monotonic()
            adm = await p.acquire(_ALIAS, 200_000, hold_cap_s=8)
            return adm, time.monotonic() - t0

        adm, took = _run(_go())
        assert adm.paced_wait_ms < 1_000 and took < 1.5  # immediate, not a 240 s stall
        r = _sync_redis()
        level = float(r.hget(f"pacer:bucket:{{{_ALIAS}}}", "level"))
        assert level == pytest.approx(-50_000, abs=1)  # the debt is owed, not waived
        r.close()

    def test_siblings_behind_an_oversized_head_are_not_frozen(self) -> None:
        _set_cfg(c_burst=150_000, r_tok=30_000, k_inflight=285_000, c_req=100, r_qps=100)

        async def _go():
            p = Pacer()
            big = asyncio.create_task(p.acquire(_ALIAS, 200_000, hold_cap_s=8))
            await asyncio.sleep(0.05)
            smalls = [
                asyncio.create_task(p.acquire(_ALIAS, 10_000, hold_cap_s=8)) for _ in range(5)
            ]
            t0 = time.monotonic()
            results = await asyncio.gather(big, *smalls, return_exceptions=True)
            return results, time.monotonic() - t0

        results, took = _run(_go())
        assert not any(isinstance(x, PacerTimeout) for x in results)
        # The big call drained the bucket to -50K; five 10K siblings need 100K of refill at
        # 30K/s -> ~3.3 s for the last one. Bounded by refill, never by the hold cap.
        assert took < 5.0

    def test_est_above_k_inflight_is_admitted_when_nothing_is_in_flight(self) -> None:
        _set_cfg(c_burst=1_000_000, r_tok=30_000, k_inflight=100_000, c_req=100, r_qps=100)

        async def _go():
            p = Pacer()
            return await p.acquire(_ALIAS, 200_000, hold_cap_s=3)

        adm = _run(_go())
        assert adm.paced_wait_ms < 1_000


class TestWaiterPruneHorizon:
    """F9: a dead waiter is pruned on ITS hold cap plus a margin, not the 300 s in-flight TTL."""

    def test_dead_waiter_is_pruned_on_the_caller_supplied_horizon(self) -> None:
        # Bucket of 10 tokens, refill 1 tok/s: the first call admits and drains it, the
        # second is denied (10 s of refill) and registers as a waiter, then "dies".
        _set_cfg(c_burst=10, r_tok=1, k_inflight=1_000_000, c_req=100, r_qps=100)

        async def _go():
            p = Pacer()
            await p._admit_once(_ALIAS, 10, "first")
            denied = await p._admit_once(_ALIAS, 10, "dead-waiter")
            assert denied.admitted is False and denied.queue_len == 1
            await asyncio.sleep(0.3)
            # A third caller: with the default horizon the dead waiter is still the head...
            v_default = await p._admit_once(_ALIAS, 10, "third")
            # ...with a 0.2 s horizon it has been pruned and the third caller IS the head.
            v_short = await p._admit_once(_ALIAS, 10, "third", waiter_ttl_s=0.2)
            return v_default, v_short

        v_default, v_short = _run(_go())
        assert v_default.is_head is False and v_default.head_est == 10
        assert v_short.is_head is True and v_short.head_est == 0
        assert v_short.queue_len == 1  # only "third" remains registered


class TestAdmittedTokensLedger:
    """F2's measured arrival: admissions publish their token estimate into the 10 s buckets."""

    def test_admissions_publish_tok_into_the_paced_bucket(self) -> None:
        _set_cfg(c_burst=1_000_000, r_tok=100_000, k_inflight=1_000_000, c_req=100, r_qps=100)

        def _tok_now(r) -> int:
            bucket = int(time.time() // 10)
            return sum(
                int(r.hget(f"paced:{{{_ALIAS}}}:{b}", "tok") or 0) for b in (bucket, bucket - 1)
            )

        async def _go():
            p = Pacer()
            await p.acquire(_ALIAS, 40_000, hold_cap_s=2)
            await p.acquire(_ALIAS, 25_000, hold_cap_s=2)

        r = _sync_redis()
        before = _tok_now(r)  # the paced buckets are not in the fixture's cleanup pattern
        _run(_go())
        after = _tok_now(r)
        r.close()
        assert after - before == 65_000
