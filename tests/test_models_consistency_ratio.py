"""Item 14 (BUILDER4-RESUME-2026-09-04-FIXBATCH): GET /models carries the pool's pacer
consistency ratio so the launch screen can warn BEFORE a run, not after its first
starvation. r_tok / (k_inflight / L_A) — the same figure discovery logs (§2.3)."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from swebench_eval.orchestrator.api import run_launch_routes as routes


class _Redis:
    def __init__(self, cfg: dict[str, dict[str, str]]) -> None:
        self.cfg = cfg

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        return {k.encode(): v.encode() for k, v in self.cfg.get(key, {}).items()}


def _with_redis(cfg: dict[str, dict[str, str]], reachable: bool = True):
    return mock.patch.multiple(
        "swebench_eval.database.redis_client",
        is_redis_reachable=lambda: reachable,
        _get_client=lambda: _Redis(cfg),
    )


def test_ratio_comes_from_the_pool_cfg_not_the_per_harness_alias() -> None:
    """qwen3-coder-next-mini rotates its own key but shares the qwen pool's pacer:cfg."""
    cfg = {
        "pacer:cfg:{qwen3-coder-next}": {
            "r_tok": "26782",
            "k_inflight": "1822219",
            "latency_s_max_context": "53.4",
            "seeded_at": "1757000000.0",
        }
    }
    with _with_redis(cfg):
        ratio, seeded = routes._pacer_consistency("qwen3-coder-next-mini")
    assert ratio == pytest.approx(26_782 / (1_822_219 / 53.4), abs=1e-3)
    assert seeded == pytest.approx(1_757_000_000.0)


def test_no_cfg_or_missing_fields_read_as_unknown_never_a_number() -> None:
    with _with_redis({}):
        assert routes._pacer_consistency("laguna-xs-2.1-codex") == (None, None)
    partial = {"pacer:cfg:{laguna-xs-2.1}": {"r_tok": "75482", "seeded_at": "1.0"}}
    with _with_redis(partial):
        assert routes._pacer_consistency("laguna-xs-2.1-codex") == (None, 1.0)


def test_redis_unreachable_or_broken_is_unknown_and_never_raises() -> None:
    with _with_redis({}, reachable=False):
        assert routes._pacer_consistency("qwen3-coder-next-mini") == (None, None)
    with mock.patch(
        "swebench_eval.database.redis_client.is_redis_reachable", side_effect=RuntimeError("x")
    ):
        assert routes._pacer_consistency("qwen3-coder-next-mini") == (None, None)


def test_models_endpoint_carries_the_ratio(monkeypatch) -> None:
    import httpx

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "data": [
                    {"model_name": "qwen3-coder-next-mini", "model_info": {"max_input_tokens": 1}},
                    {"model_name": "cheap-oss-model", "model_info": {}},
                ]
            }

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    monkeypatch.setenv("LITELLM_BASE_URL", "http://gw")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test")
    cfg = {
        "pacer:cfg:{qwen3-coder-next}": {
            "r_tok": "100",
            "k_inflight": "1000",
            "latency_s_max_context": "5",
        }
    }
    with _with_redis(cfg):
        items = {m.alias: m for m in routes.models().items}
    assert items["qwen3-coder-next-mini"].consistency_ratio == pytest.approx(0.5)
    assert items["cheap-oss-model"].consistency_ratio is None
