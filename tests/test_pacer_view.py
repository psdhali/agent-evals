"""GET /runs/{run_id}/pacer + the per-alias ledger reader —
BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03.md §2.5. FakeRedis over a dict (the reader only
needs hgetall / zrange / get); the route is exercised through the FastAPI TestClient with the
same scripted-Postgres boundary as tests/test_run_live.py."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Literal, Self
from unittest import mock

from fastapi.testclient import TestClient

from swebench_eval.gateway.pacer import (
    overload_key,
    paced_key,
    pacer_bucket_key,
    pacer_cfg_key,
    pacer_inflight_key,
    pacer_reqbucket_key,
    pacer_waitest_key,
    pacer_waitq_key,
)
from swebench_eval.orchestrator.api import pacer_view
from swebench_eval.orchestrator.api.main import app

_ALIAS = "laguna-xs-2.1-custom_minimal"
_NOW = 1_700_000_000.0


class FakeRedis:
    """hgetall / zrange(withscores) / get over a dict; hashes are dicts, zsets are
    {member: score}, strings are str. Values come back as bytes like the real client."""

    def __init__(self, data: dict[Any, Any] | None = None) -> None:
        self.data: dict[Any, Any] = data or {}

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        v = self.data.get(key)
        return (
            {str(k).encode(): str(x).encode() for k, x in v.items()} if isinstance(v, dict) else {}
        )

    def zrange(self, key: str, start: int, end: int, withscores: bool = False) -> list[Any]:
        v = self.data.get(("z", key))
        if not isinstance(v, dict):
            return []
        ordered = sorted(v.items(), key=lambda kv: kv[1])
        return (
            [(m.encode(), s) for m, s in ordered]
            if withscores
            else [m.encode() for m, _ in ordered]
        )

    def get(self, key: str) -> bytes | None:
        v = self.data.get(key)
        return str(v).encode() if v is not None and not isinstance(v, dict) else None

    def ping(self) -> bool:
        return True


def _ledger(*, waiters: bool = True, now: float = _NOW) -> FakeRedis:
    """A live alias as of *now*: cfg seeded, token bucket drained 10s ago, one in-flight call
    + one stale member, two waiters (the bigger one registered LATER but scored earlier),
    60s counters. The route reads the REAL clock for the counter buckets, so a route test
    must build this on ``time.time()``; the unit tests pin ``now`` for exact arithmetic."""
    b = int(now // 10)
    data: dict[Any, Any] = {
        pacer_cfg_key(_ALIAS): {
            "c_burst": "1000000",
            "r_tok": "50000",
            "k_inflight": "2000000",
            "c_req": "100",
            "r_qps": "1.5",
            "seeded_at": str(now - 3600),
        },
        # level 100K ten seconds ago at 50K/s -> 600K now (extrapolated), fill 0.6
        pacer_bucket_key(_ALIAS): {"level": "100000", "upd": str(now - 10)},
        # 98 requests, refills 1.5/s x 10s -> capped at 100
        pacer_reqbucket_key(_ALIAS): {"level": "98", "upd": str(now - 10)},
        pacer_inflight_key(_ALIAS): {
            "live-call": f"120000:{now - 5}",
            "dead-call": f"900000:{now - 900}",  # > TTL: the script would prune it
        },
        paced_key(_ALIAS, b): {"n": "4", "n_over_2s": "1", "sum_ms": "9000"},
        paced_key(_ALIAS, b - 3): {"n": "6", "n_over_2s": "0", "sum_ms": "600"},
        overload_key(_ALIAS, b - 1): "2",
    }
    if waiters:
        # §2.4 score = first_denied_at - est / r_tok. The 130K call was denied 0.5s AFTER
        # the 1K call, but its 2.6s refill-time head start (130K / 50K/s) makes it head.
        # (Not largest-first: a small call denied much earlier would stay ahead.)
        data[("z", pacer_waitq_key(_ALIAS))] = {
            "small": now - 1.0 - 1_000 / 50_000,  # score ≈ now - 1.02
            "big": now - 0.5 - 130_000 / 50_000,  # score ≈ now - 3.10 -> lower -> head
        }
        data[pacer_waitest_key(_ALIAS)] = {
            "small": f"1000:{now - 1.0}",
            "big": f"130000:{now - 0.5}",
        }
    return FakeRedis(data)


class TestReadAliasState:
    def test_unmeasured_alias_is_explicit_and_all_none_never_zero(self) -> None:
        st = pacer_view.read_alias_state(FakeRedis(), _ALIAS, "custom_minimal", now=_NOW)
        assert st["measured"] is False
        for k in (
            "c_burst",
            "r_tok",
            "bucket_level",
            "bucket_fill",
            "req_fill",
            "inflight_calls",
            "inflight_tokens",
            "queue_len",
            "head_est_tokens",
            "admits_60s",
            "over_2s_60s",
            "overloads_60s",
            "mean_wait_ms_60s",
        ):
            assert st[k] is None, k
        assert st["waiters"] == []

    def test_live_ledger_extrapolates_buckets_counts_inflight_and_orders_the_queue(self) -> None:
        st = pacer_view.read_alias_state(_ledger(), _ALIAS, "custom_minimal", now=_NOW)
        assert st["measured"] is True
        assert st["r_tok"] == 50_000 and st["seeded_at"] == _NOW - 3600
        # 100K + 10s x 50K/s = 600K of a 1M bucket
        assert st["bucket_level"] == 600_000 and st["bucket_fill"] == 0.6
        # 98 + 15 refilled, capped at c_req
        assert st["req_level"] == 100 and st["req_fill"] == 1.0
        # the dead member is not counted (the script would prune it)
        assert st["inflight_calls"] == 1 and st["inflight_tokens"] == 120_000
        assert st["inflight_fill"] == 0.06
        # head of line = lowest score = the BIG call despite registering later (§2.4 score)
        assert st["queue_len"] == 2
        assert st["head_est_tokens"] == 130_000 and st["head_waiting_s"] == 0.5
        assert [w["est_tokens"] for w in st["waiters"]] == [130_000, 1_000]
        assert st["waiters"][1]["waiting_s"] == 1.0
        # last-60s counters: 4+6 admits, 1 over 2s, mean 9600/10, 2 provider 429s
        assert st["admits_60s"] == 10 and st["over_2s_60s"] == 1
        assert st["mean_wait_ms_60s"] == 960.0 and st["overloads_60s"] == 2

    def test_empty_queue_is_zero_when_measured_not_none(self) -> None:
        st = pacer_view.read_alias_state(_ledger(waiters=False), _ALIAS, "x", now=_NOW)
        assert st["measured"] is True
        assert st["queue_len"] == 0 and st["waiters"] == [] and st["head_est_tokens"] is None


class TestResolvePacerAliases:
    def test_maps_family_prefix_to_the_per_harness_alias_and_keeps_known_aliases(self) -> None:
        out = pacer_view.resolve_pacer_aliases(
            [
                ("custom_minimal", "laguna-xs-2.1"),  # prefix -> per-harness alias
                ("claude_code", "laguna-xs-2.1-claude_code"),  # already the pacer alias
                ("mini_swe_agent", "totally-unknown"),  # passthrough, still renders
                ("custom_minimal", "laguna-xs-2.1"),  # duplicate target -> once
            ]
        )
        assert out == [
            ("custom_minimal", "laguna-xs-2.1-custom_minimal"),
            ("claude_code", "laguna-xs-2.1-claude_code"),
            ("mini_swe_agent", "totally-unknown"),
        ]


# ── the routes, through the app ─────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, conn: _FakeConn, results: list[dict[str, Any]]) -> None:
        self.conn = conn
        self._results = results
        self._i = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        self._i = len(self._results)
        return self._results

    def fetchone(self) -> dict[str, Any] | None:
        if self._i < len(self._results):
            row = self._results[self._i]
            self._i += 1
            return row
        return None

    def close(self) -> None:
        return None


class _FakeConn:
    def __init__(self, *result_sets: list[dict[str, Any]]) -> None:
        self._queue: list[list[dict[str, Any]]] = list(result_sets)
        self.executed: list[tuple[str, Any]] = []

    def cursor(self, cursor_factory: Any = None) -> _FakeCursor:
        results = self._queue.pop(0) if self._queue else []
        return _FakeCursor(self, results)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_run_pacer_route_renders_each_target_alias_from_the_ledger() -> None:
    conn = _FakeConn(
        [{"status": "running"}],
        [{"harness": "custom_minimal", "model_alias": "laguna-xs-2.1"}],
    )
    fake = _ledger(now=time.time())  # the route reads the real clock
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        mock.patch("swebench_eval.database.redis_client.is_redis_reachable", return_value=True),
        mock.patch("swebench_eval.database.redis_client._get_client", return_value=fake),
    ):
        resp = TestClient(app).get("/runs/run-1/pacer")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["alias"] == _ALIAS and item["harness"] == "custom_minimal"
    assert item["measured"] is True
    assert item["queue_len"] == 2 and item["head_est_tokens"] == 130_000
    assert [w["est_tokens"] for w in item["waiters"]] == [130_000, 1_000]
    assert item["overloads_60s"] == 2


def test_run_pacer_redis_unreachable_is_whole_response_unknown() -> None:
    conn = _FakeConn(
        [{"status": "running"}], [{"harness": "codex", "model_alias": "laguna-xs-2.1"}]
    )
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn),
        mock.patch("swebench_eval.database.redis_client.is_redis_reachable", return_value=False),
    ):
        resp = TestClient(app).get("/runs/run-1/pacer")
    assert resp.status_code == 200
    assert resp.json() == {
        "run_id": "run-1",
        "state": "unknown",
        "reason": "redis_unreachable",
        "items": [],
    }


def test_run_pacer_404_when_run_missing() -> None:
    conn = _FakeConn([])
    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn):
        assert TestClient(app).get("/runs/nope/pacer").status_code == 404


def test_instance_calls_route_returns_the_decomposition_in_call_order() -> None:
    row = {
        "call_index": 3,
        "started_at": datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC),
        "http_status": 429,
        "model_resolved": None,
        "error_type": "pacer_hold_cap_exceeded",
        "rate_limit_scope": None,
        "shim_preflight_ms": 12,
        "paced_wait_ms": 100_000,
        "overload_retries": None,
        "overload_backoff_ms": None,
        "retry_upstream_ms": None,
        "ttft_ms": None,
        "latency_ms": 0,
        "pacer_was_queued": True,
        "pacer_queue_len": 4,
        "pacer_deny_axis": "tok",
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "cost_usd": None,
    }
    conn = _FakeConn([{"status": "running"}], [row])
    with mock.patch("swebench_eval.orchestrator.api.main._db", return_value=conn):
        resp = TestClient(app).get("/runs/run-1/instances/matplotlib__matplotlib-1/1/calls")
    assert resp.status_code == 200
    body = resp.json()
    assert body["attempt_number"] == 1 and len(body["items"]) == 1
    c = body["items"][0]
    assert c["error_type"] == "pacer_hold_cap_exceeded" and c["latency_ms"] == 0
    assert c["paced_wait_ms"] == 100_000 and c["pacer_deny_axis"] == "tok"
    assert c["started_at"].startswith("2026-09-03T01:02:03")
    sql = conn.executed[-1][0]
    assert "FROM llm_calls" in sql and "ORDER BY call_index" in sql
