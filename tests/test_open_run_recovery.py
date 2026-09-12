"""open_run_recovery (2026-09-08): after an eval-tier cycle every OPEN run has lost its
Valkey-only state — the raw LiteLLM key (dispatch refuses, ADR-0035) and the run alias's
pacer:cfg copy (gateway pacer on defaults). Supervisor startup re-mints / re-fills both, per
run, best-effort. No DB, no gateway, no Redis here — every seam is faked."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from swebench_eval.gateway import admin as gateway_admin
from swebench_eval.orchestrator.control_plane import (
    open_run_recovery,
    pacer_seeds,
    run_key_cache,
)


class _Cur:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *a: object) -> None:
        pass

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.conn.rows)


class _Conn:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self.closed = False

    def cursor(self) -> Any:
        return _Cur(self)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "cached": {},  # run_id -> raw key
        "deleted": [],
        "minted": [],  # (alias, models, metadata)
        "filled": [],  # (alias, fill_missing)
        "mint_fail_for": set(),
    }
    monkeypatch.setattr(run_key_cache, "fetch", lambda run_id: state["cached"].get(run_id))
    monkeypatch.setattr(
        run_key_cache, "store", lambda run_id, raw: state["cached"].__setitem__(run_id, raw)
    )
    monkeypatch.setattr(
        gateway_admin, "delete_key", lambda b, m, alias: state["deleted"].append(alias)
    )

    def _generate(b: Any, m: Any, *, key_alias: str, models: Any, **kw: Any) -> tuple[str, str]:
        if key_alias in state["mint_fail_for"]:
            raise RuntimeError(f"gateway down for {key_alias}")
        state["minted"].append((key_alias, models, kw.get("metadata")))
        return f"sk-raw-{key_alias}", f"id-{key_alias}"

    monkeypatch.setattr(gateway_admin, "generate_key", _generate)
    monkeypatch.setattr("swebench_eval.harnesses.routing.gateway_base_url", lambda: "http://g/v1")
    monkeypatch.setattr("swebench_eval.harnesses.routing.gateway_api_key", lambda: "master")

    def _seed(client: Any, alias: str, *, fill_missing: bool = False) -> int:
        state["filled"].append((alias, fill_missing))
        return 3 if alias.endswith("-opencode") else 0

    monkeypatch.setattr(pacer_seeds, "seed_alias_from_pool", _seed)
    return state


def _rows() -> list[tuple[Any, ...]]:
    return [
        ("run-oc", {"harness": "opencode", "model_alias": "minimax-m2.5-opencode"}, "old-1"),
        ("run-cc", {"harness": "claude_code", "model_alias": "minimax-m2.5-claude_code"}, "old-2"),
    ]


def test_reminting_only_runs_without_a_cached_key_and_refilling_every_alias(
    wired: dict[str, Any],
) -> None:
    wired["cached"]["run-cc"] = "sk-still-here"
    conn = _Conn(_rows())

    report = open_run_recovery.recover_open_runs(conn=conn, client=object())

    assert report.runs_seen == 2
    assert report.keys_reminted == ["run-oc"] and report.keys_present == ["run-cc"]
    assert wired["deleted"] == ["run-oc"]
    alias, models, metadata = wired["minted"][0]
    assert alias == "run-oc" and models == ["minimax-m2.5-opencode"]
    assert metadata["harness"] == "opencode" and metadata["reminted"]
    assert wired["cached"]["run-oc"] == "sk-raw-run-oc"
    assert wired["cached"]["run-cc"] == "sk-still-here"  # untouched
    # the new non-secret id is recorded, the raw key never reaches the DB
    updates = [(s, p) for s, p in conn.executed if s.startswith("UPDATE runs")]
    assert updates == [
        ("UPDATE runs SET litellm_key_id = %s WHERE run_id = %s", ("id-run-oc", "run-oc"))
    ]
    assert not any("sk-raw" in str(p) for _, p in conn.executed)
    assert conn.commits == 1
    # the pacer alias is re-filled for EVERY open run, cached key or not
    assert wired["filled"] == [
        ("minimax-m2.5-opencode", True),
        ("minimax-m2.5-claude_code", True),
    ]
    assert report.pacer_filled == {"run-oc": 3}
    assert not conn.closed  # a caller-owned connection stays open


def test_the_query_selects_only_running_runs_that_went_through_launch(
    wired: dict[str, Any],
) -> None:
    conn = _Conn([])
    open_run_recovery.recover_open_runs(conn=conn, client=object())
    sql = conn.executed[0][0]
    assert "status = 'running'" in sql and "openrouter_key_hash IS NOT NULL" in sql


def test_one_run_failing_does_not_stop_the_others(
    wired: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    wired["mint_fail_for"] = {"run-oc"}
    conn = _Conn(_rows())
    with caplog.at_level(logging.ERROR):
        report = open_run_recovery.recover_open_runs(conn=conn, client=object())
    assert "run-oc" in report.failed and "gateway down" in report.failed["run-oc"]
    assert report.keys_reminted == ["run-cc"]
    assert "run-oc failed" in caplog.text


def test_a_row_without_a_model_alias_is_a_per_run_failure(wired: dict[str, Any]) -> None:
    conn = _Conn([("run-x", {"harness": "codex"}, None)])
    report = open_run_recovery.recover_open_runs(conn=conn, client=object())
    assert "run-x" in report.failed and "model_alias" in report.failed["run-x"]
    assert wired["minted"] == [] and wired["filled"] == []


def test_recovery_never_raises_even_when_it_cannot_connect(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom() -> Any:
        raise ConnectionError("no aurora")

    monkeypatch.setattr("swebench_eval.orchestrator.control_plane.run_launch._db", _boom)
    with caplog.at_level(logging.ERROR):
        report = open_run_recovery.recover_open_runs(client=object())
    assert report.runs_seen == 0
    assert "before it could inspect any run" in caplog.text


def test_summary_line_reads_naturally() -> None:
    r = open_run_recovery.OpenRunRecovery(
        runs_seen=5, keys_reminted=["a"], keys_present=["b", "c"], pacer_filled={"a": 3}
    )
    assert r.summary() == (
        "open runs: 5; litellm key re-minted: 1, already cached: 2; pacer alias fields "
        "filled: 3 across 1 run(s); failed: 0"
    )


def test_restart_refills_the_alias_pacer_before_enqueueing(monkeypatch: pytest.MonkeyPatch) -> None:
    """restart._dispatch_restarted (2026-09-08): the pool -> alias pacer copy is re-filled
    (fill_missing) before the first restarted job goes out."""
    from swebench_eval.orchestrator.control_plane import restart

    seeded: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        restart,
        "_run_config_snapshot",
        lambda run_id: {"harness": "opencode", "model_alias": "a-b"},
    )
    monkeypatch.setattr("swebench_eval.database.redis_client._get_client", lambda: object())

    def _seed(client: Any, alias: str, *, fill_missing: bool = False) -> int:
        seeded.append((alias, fill_missing))
        return 0

    monkeypatch.setattr(pacer_seeds, "seed_alias_from_pool", _seed)
    restart._dispatch_restarted("run-1", [])
    assert seeded == [("a-b", True)]

    # a Redis failure never blocks the restart itself
    def _down() -> Any:
        raise ConnectionError("valkey down")

    monkeypatch.setattr("swebench_eval.database.redis_client._get_client", _down)
    restart._dispatch_restarted("run-1", [])  # no raise
