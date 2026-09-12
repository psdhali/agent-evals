"""L1 pacer head-of-line fairness against a real Redis —
BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03.md §2.4/§2.5.

Real Redis (the local compose valkey), same reasoning as test_pacer_integration.py: the
reservation lives inside the atomic Lua script, and a Python fake would test nothing about it.
The first test is the run-01788405363237319353 scenario itself — a 130K call under continuous
60K sibling pressure on a bucket that refills slower than the siblings consume.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

import swebench_eval.gateway.pacer as pacer_mod
from swebench_eval.gateway.pacer import (
    Admission,
    Pacer,
    PacerTimeout,
    pacer_waitest_key,
    pacer_waitq_key,
)

pytestmark = pytest.mark.integration

_ALIAS = "pacer-fairness-alias"


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


class TestHeadOfLineReservation:
    def test_the_matplotlib_scenario_big_call_is_not_starved_by_smaller_siblings(self) -> None:
        """c_burst 200K, r_tok 40K/s. Drain the bucket to ~20K, then a 130K call arrives while
        60K siblings keep arriving. Without the reservation the siblings admit every ~1.5s on
        partial refills and the level NEVER reaches 130K (the 10 x 100s death). With it, the
        130K call is head: siblings may only take the surplus above 130K (they need 190K),
        so the level accumulates to 130K in ~2.75s and the big call goes — then siblings flow."""
        _set_cfg(c_burst=200_000, r_tok=40_000, k_inflight=10_000_000, c_req=1000, r_qps=1000)

        async def _go():
            p = Pacer()
            # Drain: three 60K admissions leave ~20K in the bucket.
            for _ in range(3):
                adm = await p.acquire(_ALIAS, 60_000, hold_cap_s=2)
                await p.release(_ALIAS, adm)

            sibling_admits: list[float] = []
            stop = time.monotonic() + 8.0

            async def _siblings():
                # A continuous stream of 60K callers, each giving up after 0.7s and coming
                # straight back — the shape of a fleet of smaller-context instances turning.
                while time.monotonic() < stop:
                    try:
                        adm = await p.acquire(_ALIAS, 60_000, hold_cap_s=0.7)
                        sibling_admits.append(time.monotonic())
                        await p.release(_ALIAS, adm)
                    except PacerTimeout:
                        pass
                    await asyncio.sleep(0.05)

            t0 = time.monotonic()
            gens = [asyncio.create_task(_siblings()) for _ in range(3)]
            big = await p.acquire(_ALIAS, 130_000, hold_cap_s=8)
            big_at = time.monotonic() - t0
            await p.release(_ALIAS, big)
            await asyncio.gather(*gens)
            return big, big_at, sibling_admits, t0

        big, big_at, sibling_admits, t0 = _run(_go())
        assert isinstance(big, Admission)
        assert big.was_queued is True
        assert big.deny_axis_last == "tok"
        # ~110K of refill at 40K/s ≈ 2.75s; comfortably under the 8s cap and far from the
        # unbounded starvation the old script produced.
        assert 1.5 <= big_at <= 5.5, big_at
        # Surplus still flows: siblings admitted AFTER the head cleared (the queue did not
        # turn into largest-first starvation of the small calls).
        assert any(t - t0 > big_at for t in sibling_admits)

    def test_non_head_may_take_only_the_surplus_above_the_head_reservation(self) -> None:
        """Deterministic version of the rule: with the bucket EMPTY and a 100K head queued, a
        30K caller is denied even once 30K has refilled (it needs 130K); once the level
        passes 130K it admits while the head still waits for its own 100K... which it then
        gets, because the surplus taker only debited 30K above the reservation."""
        _set_cfg(c_burst=200_000, r_tok=100_000, k_inflight=10_000_000, c_req=1000, r_qps=1000)

        async def _go():
            p = Pacer()
            drain = await p.acquire(_ALIAS, 200_000, hold_cap_s=2)  # bucket -> 0
            await p.release(_ALIAS, drain)
            head_task = asyncio.create_task(p.acquire(_ALIAS, 100_000, hold_cap_s=5))
            await asyncio.sleep(0.4)  # ~40K refilled: head registered, still denied
            r = _sync_redis()
            queued = r.zcard(pacer_waitq_key(_ALIAS))
            r.close()
            t0 = time.monotonic()
            small = await p.acquire(_ALIAS, 30_000, hold_cap_s=5)
            small_at = time.monotonic() - t0
            head = await head_task
            return queued, small, small_at, head

        queued, small, small_at, head = _run(_go())
        assert queued == 1  # the head was in the queue while the small caller arrived
        # The small caller needed 30K + 100K = 130K of level, i.e. ~0.9s more from ~40K at
        # 100K/s — NOT the ~0s it would have taken without the reservation.
        assert small.was_queued is True
        assert small_at >= 0.5, small_at
        assert isinstance(head, Admission) and head.was_queued is True

    def test_request_axis_is_reserved_for_the_head_too(self) -> None:
        """Tokens abundant, requests scarce (c_req 1, slow refill): with a head waiting on the
        request axis, a second caller needs rlevel >= 2 — it cannot slip in on the single
        refilled request the head was waiting for."""
        _set_cfg(c_burst=10_000_000, r_tok=1_000_000, k_inflight=50_000_000, c_req=1, r_qps=2)

        async def _go():
            p = Pacer()
            first = await p.acquire(_ALIAS, 10, hold_cap_s=1)  # takes the one request
            await p.release(_ALIAS, first)
            head_task = asyncio.create_task(p.acquire(_ALIAS, 10, hold_cap_s=3))
            await asyncio.sleep(0.15)  # head registered (denied on 'req')
            t0 = time.monotonic()
            late = await p.acquire(_ALIAS, 10, hold_cap_s=3)
            late_at = time.monotonic() - t0
            head = await head_task
            return head, late, late_at

        head, late, late_at = _run(_go())
        assert head.was_queued is True and head.deny_axis_last == "req"
        assert late.was_queued is True and late.deny_axis_last == "req"
        assert late_at >= 0.6, late_at  # waited for a SECOND request to refill (~1s at 2/s)


class TestQueueMechanics:
    def test_priority_score_favors_the_bigger_need(self) -> None:
        """score = first_denied_at - est / r_tok. At r_tok 1000/s a 100K need gets a 100s head
        start over a 1K need registered a moment earlier — the big call becomes head. On
        hold-cap timeout both leave the queue."""
        # Scaling review F1: a need above c_burst now admits (clamped), so the bucket is
        # DRAINED by a full-size call first; both waiters then need refill they cannot get.
        _set_cfg(c_burst=200_000, r_tok=1000, k_inflight=10_000_000, c_req=1000, r_qps=1000)

        async def _go():
            p = Pacer()
            await p.acquire(_ALIAS, 200_000, hold_cap_s=1)  # level -> 0
            small_task = asyncio.create_task(p.acquire(_ALIAS, 1_000, hold_cap_s=0.8))
            await asyncio.sleep(0.1)
            big_task = asyncio.create_task(p.acquire(_ALIAS, 100_000, hold_cap_s=0.8))
            await asyncio.sleep(0.2)
            r = _sync_redis()
            head = r.zrange(pacer_waitq_key(_ALIAS), 0, 0)[0].decode()
            head_est = r.hget(pacer_waitest_key(_ALIAS), head).decode().split(":")[0]
            n_queued = r.zcard(pacer_waitq_key(_ALIAS))
            r.close()
            results = await asyncio.gather(small_task, big_task, return_exceptions=True)
            r = _sync_redis()
            left = r.zcard(pacer_waitq_key(_ALIAS)), r.hlen(pacer_waitest_key(_ALIAS))
            r.close()
            return head_est, n_queued, results, left

        head_est, n_queued, results, left = _run(_go())
        assert n_queued == 2
        assert head_est == "100000"  # the bigger need is head despite registering later
        assert all(isinstance(x, PacerTimeout) for x in results)
        assert left == (0, 0)  # timed-out waiters dequeued themselves

    def test_stale_waiter_is_pruned_by_the_script(self) -> None:
        """A waiter registered > TTL ago (a crashed shim) must not hold the head slot."""
        _set_cfg(c_burst=1_000_000, r_tok=100_000, k_inflight=5_000_000, c_req=100, r_qps=100)
        r = _sync_redis()
        r.zadd(pacer_waitq_key(_ALIAS), {"dead-waiter": time.time() - 600})
        r.hset(pacer_waitest_key(_ALIAS), "dead-waiter", f"900000:{time.time() - 600}")
        r.close()

        async def _go():
            p = Pacer()
            return await p.acquire(_ALIAS, 500_000, hold_cap_s=2)

        adm = _run(_go())
        # Admitted immediately: the dead 900K "head" was pruned, not reserved against us.
        assert adm.was_queued is False
        r = _sync_redis()
        assert r.zcard(pacer_waitq_key(_ALIAS)) == 0
        assert r.hlen(pacer_waitest_key(_ALIAS)) == 0
        r.close()

    def test_timeout_message_and_log_carry_the_bucket_state(self, monkeypatch, caplog) -> None:
        """§2.5: the during-load probe. A denied call logs its bucket state after
        _LONG_WAIT_LOG_S and the PacerTimeout names the last deny — level, inflight, r_tok,
        queue depth, axis — exactly what the original investigation never captured."""
        monkeypatch.setattr(pacer_mod, "_LONG_WAIT_LOG_S", 0.2)
        # Scaling review F1: est above c_burst admits now; drain a bucket the call fits.
        _set_cfg(c_burst=100_000, r_tok=5, k_inflight=10_000_000, c_req=1000, r_qps=1000)

        async def _go():
            p = Pacer()
            await p.acquire(_ALIAS, 100_000, hold_cap_s=1)  # level -> 0; refill 5 tok/s
            with pytest.raises(PacerTimeout) as ei:
                await p.acquire(_ALIAS, 50_000, hold_cap_s=0.6)
            return str(ei.value)

        with caplog.at_level(logging.WARNING):
            msg = _run(_go())
        assert "last deny level=" in msg and "axis=tok" in msg and "r_tok=5/s" in msg
        waiting = [r for r in caplog.records if "still waiting est=50000" in r.message]
        assert waiting and "queue_len=1" in waiting[0].message
        assert any("HOLD CAP" in r.message and "axis=tok" in r.message for r in caplog.records)

    def test_admission_diagnostics_default_clean_on_an_unqueued_admit(self) -> None:
        _set_cfg(c_burst=1_000_000, r_tok=100_000, k_inflight=5_000_000, c_req=100, r_qps=100)

        async def _go():
            return await Pacer().acquire(_ALIAS, 1_000, hold_cap_s=2)

        adm = _run(_go())
        assert adm.was_queued is False
        assert adm.queue_len_at_admit == 0
        assert adm.deny_axis_last is None
