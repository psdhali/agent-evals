"""capacity_snapshot's pacer-pressure columns — BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03
§2.5: pacer_queue_len (max wait-queue depth over aliases with a FRESH burst bucket) and
paced_over_2s_share (share of the last 60 s of admissions that waited > 2 s, max over
aliases). None = not measured, never 0; a client without ZCARD yields None for the depth
while the share is still read; a stale alias contributes nothing."""

from __future__ import annotations

import time
from typing import Any

from swebench_eval.gateway.pacer import paced_key, pacer_bucket_key, pacer_waitq_key
from swebench_eval.orchestrator.control_plane.capacity_observer import CapacityObserver

_ALIAS = "laguna-xs-2.1-custom_minimal"


class _FakeRedis:
    """get/scan_iter/hgetall over a dict; hashes are dicts. ZCARD only when asked for."""

    def __init__(self, data: dict[Any, Any], *, with_zcard: bool = True) -> None:
        self.data = data
        if with_zcard:
            self.zcard = self._zcard

    def get(self, key: str) -> Any:
        v = self.data.get(key)
        return v if not isinstance(v, (dict, int)) else None

    def hgetall(self, key: str) -> dict[str, str]:
        v = self.data.get(key)
        return dict(v) if isinstance(v, dict) else {}

    def scan_iter(self, pattern: str, count: int = 100) -> Any:
        prefix = pattern.rstrip("*")
        return iter([k for k in self.data if isinstance(k, str) and k.startswith(prefix)])

    def _zcard(self, key: str) -> int:
        v = self.data.get(("z", key))
        return int(v) if isinstance(v, int) else 0


def _data(*, fresh: bool = True, depth: int = 3) -> dict[Any, Any]:
    now = time.time()
    b = int(now // 10)
    return {
        pacer_bucket_key(_ALIAS): {"level": "100", "upd": str(now - (5 if fresh else 900))},
        ("z", pacer_waitq_key(_ALIAS)): depth,
        # last 60s: 10 admissions, 2 of them waited > 2s -> share 0.2
        paced_key(_ALIAS, b): {"n": "4", "n_over_2s": "1", "sum_ms": "9000"},
        paced_key(_ALIAS, b - 2): {"n": "6", "n_over_2s": "1", "sum_ms": "600"},
    }


def _observer(client: Any) -> CapacityObserver:
    return CapacityObserver(
        redis_client=client,
        ecs_client=None,
        sqs_depth_fn=lambda q: (0, 0),
        conn_factory=lambda: None,
        tick_interval_s=0.0,
    )


def test_pressure_reads_queue_depth_and_over_2s_share_for_a_fresh_alias() -> None:
    depth, share = _observer(_FakeRedis(_data()))._pacer_pressure()
    assert depth == 3
    assert share == 0.2


def test_stale_alias_contributes_nothing_not_zero() -> None:
    depth, share = _observer(_FakeRedis(_data(fresh=False)))._pacer_pressure()
    assert depth is None and share is None


def test_client_without_zcard_yields_none_depth_but_still_reads_the_share() -> None:
    depth, share = _observer(_FakeRedis(_data(), with_zcard=False))._pacer_pressure()
    assert depth is None
    assert share == 0.2


def test_empty_queue_on_a_fresh_alias_is_zero_not_none() -> None:
    depth, _share = _observer(_FakeRedis(_data(depth=0)))._pacer_pressure()
    assert depth == 0


def test_observation_row_carries_both_fields_on_the_harness_pool_only() -> None:
    obs = _observer(_FakeRedis(_data())).observe()
    harness = next(o for o in obs if o.pool == "harness")
    evalrow = next(o for o in obs if o.pool == "eval")
    assert harness.pacer_queue_len == 3 and harness.paced_over_2s_share == 0.2
    assert evalrow.pacer_queue_len is None and evalrow.paced_over_2s_share is None
