"""Tests for the ADR-0018 Redis progress move (R3-2) and its invariant.

Two kinds of test:
1. ``test_no_psycopg2_in_harness_worker_path`` — a PURE static source check
   (runs always, no Redis needed): after the progress move, ADR-0007's
   "workers never write directly to Postgres" has zero exceptions, so the
   harness worker must not import psycopg2 or reference the dropped table.
2. ``test_progress_round_trip`` — integration (requires the Redis container).
"""

from __future__ import annotations

import importlib
import inspect

import pytest


def test_no_psycopg2_in_harness_worker_path() -> None:
    """R3-2: the worker's Postgres write path is gone — no psycopg2 import."""
    mod = importlib.import_module("swebench_eval.workers.harness_worker")
    src = inspect.getsource(mod)
    assert (
        "psycopg2" not in src
    ), "harness_worker.py must not import psycopg2 (ADR-0007 zero exceptions)"
    assert "instance_progress" not in src, "harness_worker.py must not reference the dropped table"


def _redis_reachable() -> bool:
    try:
        from swebench_eval.database.redis_client import _redis_from_env

        return bool(_redis_from_env().ping())
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _redis_reachable(), reason="no Redis running")
def test_progress_round_trip() -> None:
    """write_progress then read_progress returns the same values."""
    from swebench_eval.database.redis_client import read_progress, write_progress
    from swebench_eval.harnesses.base import Usage

    usage = Usage(input_tokens=60, output_tokens=40, cost_usd=0.5)
    write_progress("run-p", "inst-p", 7, turn_number=3, usage=usage)
    got = read_progress("run-p", "inst-p", 7)
    assert got is not None
    assert got["turn_number"] == 3
    assert got["input_tokens"] == 60
    assert got["output_tokens"] == 40
    assert got["cost_usd"] == 0.5
    assert "updated_at" in got  # the staleness stamp is always present


def test_usage_payload_carries_all_seven_fields_and_updated_at() -> None:
    """METERING-COMPLETENESS: the ONE Redis payload serializer must carry the
    shim's whole cumulative Usage — all seven fields — plus the ``updated_at``
    staleness stamp.  The per-turn callback and the end-of-run write both route
    through this single function, so a finished instance can never be erased
    (they drifted once with turn_number=0).

    Mutation-proof: drop a field (e.g. ``reasoning_tokens`` or ``updated_at``)
    from `_usage_payload`; this test fails.
    """
    from swebench_eval.database.redis_client import _usage_payload
    from swebench_eval.harnesses.base import Usage

    usage = Usage(
        input_tokens=100,
        output_tokens=50,
        cached_tokens=30,
        cache_write_tokens=2,
        reasoning_tokens=8,
        cost_usd=0.42,
        source="gateway",
        retry_count=1,
    )
    payload = _usage_payload("run-u", "inst-u", 3, turn_number=12, usage=usage)

    for k in (
        "run_id",
        "instance_id",
        "attempt_number",
        "turn_number",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "cost_usd",
        "source",
        "retry_count",
        "updated_at",
    ):
        assert k in payload, f"payload missing {k}"

    # Every cumulative token/cost field survives verbatim.
    assert payload["input_tokens"] == 100
    assert payload["output_tokens"] == 50
    assert payload["cached_tokens"] == 30
    assert payload["cache_write_tokens"] == 2
    assert payload["reasoning_tokens"] == 8
    assert payload["cost_usd"] == 0.42
    assert payload["source"] == "gateway"
    assert payload["retry_count"] == 1
    assert isinstance(payload["updated_at"], float)  # wall-clock staleness stamp


def test_empty_redis_url_does_not_crash_at_parse(monkeypatch) -> None:
    """2026-09-02: a cross-tier terraform fallback rendered REDIS_URL='' (set-
    but-empty), and redis.from_url('') raises ValueError at PARSE time — which
    crash-looped the run-supervisor at boot.  Empty must fall back to the
    compose default (parseable); reachability failures then degrade through the
    normal fail-closed paths instead of killing the process.

    Mutation: revert _redis_from_env to os.environ.get('REDIS_URL', default)
    and this fails."""
    from swebench_eval.database.redis_client import _redis_from_env, clear_redis_cache

    monkeypatch.setenv("REDIS_URL", "")
    clear_redis_cache()
    try:
        client = _redis_from_env()  # must NOT raise
        kw = client.connection_pool.connection_kwargs
        assert kw.get("host") in ("localhost", "127.0.0.1")
    finally:
        clear_redis_cache()


def test_supervisor_startup_survives_publish_failure(monkeypatch) -> None:
    """2026-09-02: the unguarded startup _publish_control_from_aurora() took
    the whole supervisor down (heartbeat + reaper included) when Redis was
    unreachable.  The guarded startup must swallow the failure and reach the
    loop — readers stay fail-closed until the tick republishes."""
    from unittest import mock

    from swebench_eval.orchestrator.control_plane import run_supervisor as rs

    with (
        mock.patch.object(rs, "_publish_control_from_aurora", side_effect=RuntimeError("boom")),
        mock.patch.object(rs, "_maybe_publish_control") as pub,
        mock.patch.object(rs, "_maybe_run_reaper"),
        mock.patch("swebench_eval.database.connection.run_migrations"),
        mock.patch("swebench_eval.database.connection.ensure_additional_databases"),
        mock.patch("swebench_eval.logging_bootstrap.configure_logging"),
        # 2026-09-08: startup open-run recovery talks to Aurora/Redis/the gateway — stubbed,
        # like the migrations, so the scripted sleeps below belong to the loop alone
        mock.patch("swebench_eval.orchestrator.control_plane.open_run_recovery.recover_open_runs"),
        mock.patch("time.sleep", side_effect=[None, KeyboardInterrupt]),  # 2 ticks then stop
    ):
        try:
            rs.run_run_supervisor()
        except KeyboardInterrupt:
            pass
    assert pub.called, "the loop must be reached despite the startup publish failure"
