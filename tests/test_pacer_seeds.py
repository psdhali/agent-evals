"""Discovery seeds persisted in Aurora and rehydrated into pacer:cfg:{pool} (owner decision
2026-09-04): the row carries exactly what discovery HSETs, the rehydration restores the
ORIGINAL seeded_at, never overwrites a live hash, and degrades to defaults on any failure.
"""

from __future__ import annotations

import json
from typing import Any, Self
from unittest import mock

from swebench_eval.gateway.pacer import pacer_cfg_key
from swebench_eval.orchestrator.control_plane import pacer_seeds

_SEEDS = {
    "c_burst": 1_066_079,
    "r_tok": 105_000,
    "k_inflight": 2_025_550,
    "c_req": 160,
    "r_qps": 8.0,
    "r_tok_seed": 105_000,
    "k_inflight_seed": 2_025_550,
    "r_qps_seed": 8.0,
    "latency_s_max_context": 12.345,
    "cached_weight": 0.2,
    "cached_weight_seed": 0.2,
}
_STAMP = 1_756_830_000.0


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    def hset(self, key: str, mapping: dict[str, Any]) -> None:
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def expire(self, key: str, ttl: int) -> None:
        pass

    def delete(self, key: str) -> int:
        return 1 if self.hashes.pop(key, None) is not None else 0


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        pass

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.conn.executed.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.conn.row


class FakeConn:
    def __init__(self, row: tuple[Any, ...] | None = None) -> None:
        self.row = row
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.committed = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def test_persist_writes_one_row_with_the_seeds_verbatim_and_the_stamp_in_its_own_column(
    monkeypatch,
) -> None:
    conn = FakeConn()
    monkeypatch.setattr(pacer_seeds, "_db", lambda: conn)
    monkeypatch.setenv("ECS_TASK_ARN", "arn:task/abc")

    pacer_seeds.persist_seeds(
        "qwen3-coder-next",
        {**_SEEDS, "seeded_at": _STAMP},
        _STAMP,
        provider="Parasail",
        triggered_by="operator",
        report={"edge_found": True, "cached_steps": [{"step": 0}]},
    )

    assert conn.committed and conn.closed
    (sql, params), *_ = conn.executed
    assert "INSERT INTO pacer_cfg_seeds" in sql
    alias, seeds_json, seeded_at, provider, triggered_by, task_id, report_json = params
    assert alias == "qwen3-coder-next"
    assert json.loads(seeds_json) == _SEEDS  # seeded_at is NOT inside the JSON
    assert seeded_at == _STAMP
    assert (provider, triggered_by, task_id) == ("Parasail", "operator", "arn:task/abc")
    assert json.loads(report_json)["edge_found"] is True


def test_load_latest_returns_the_seeds_and_stamp_or_none(monkeypatch) -> None:
    monkeypatch.setattr(pacer_seeds, "_db", lambda: FakeConn(row=(_SEEDS, _STAMP)))
    assert pacer_seeds.load_latest_seeds("qwen3-coder-next") == (_SEEDS, _STAMP)

    # a driver handing JSONB back as text still round-trips
    monkeypatch.setattr(pacer_seeds, "_db", lambda: FakeConn(row=(json.dumps(_SEEDS), _STAMP)))
    assert pacer_seeds.load_latest_seeds("qwen3-coder-next") == (_SEEDS, _STAMP)

    monkeypatch.setattr(pacer_seeds, "_db", lambda: FakeConn(row=None))
    assert pacer_seeds.load_latest_seeds("never-probed") is None


def test_rehydrate_writes_the_hash_in_discovery_shape_with_the_original_stamp(
    monkeypatch, caplog
) -> None:
    """Same repr() encoding discovery uses, and seeded_at restored — not re-stamped — so a
    day-old probe still trips the planner's staleness policy."""
    monkeypatch.setattr(pacer_seeds, "load_latest_seeds", lambda alias: (_SEEDS, _STAMP))
    fake = FakeRedis()

    with caplog.at_level("INFO"):
        assert pacer_seeds.rehydrate_pool(fake, "qwen3-coder-next") is True

    cfg = fake.hashes[pacer_cfg_key("qwen3-coder-next")]
    assert cfg["r_tok"] == "105000"
    assert cfg["r_qps"] == "8.0"
    assert cfg["latency_s_max_context"] == "12.345"
    assert cfg["cached_weight"] == "0.2"
    assert cfg["seeded_at"] == repr(_STAMP)
    assert float(cfg["seeded_at"]) == _STAMP
    assert "rehydrated pacer:cfg:qwen3-coder-next" in caplog.text


def test_rehydrate_never_touches_a_live_hash(monkeypatch) -> None:
    """A fresh probe, an operator override or a still-warm cache is newer evidence than any
    row — and the DB is not even consulted."""

    def _boom(alias: str) -> None:
        raise AssertionError("must not read the DB when the hash is live")

    monkeypatch.setattr(pacer_seeds, "load_latest_seeds", _boom)
    fake = FakeRedis()
    fake.hset(pacer_cfg_key("qwen3-coder-next"), mapping={"r_tok": "555", "seeded_at": "1.0"})

    assert pacer_seeds.rehydrate_pool(fake, "qwen3-coder-next") is False
    assert fake.hashes[pacer_cfg_key("qwen3-coder-next")]["r_tok"] == "555"


# --- 2026-09-07: an operator edit persisted the pool hash's Redis STRINGS; the next bring-up
# rehydrated them as repr(str) = "'18.0'" and the pacer, the planner and /models all read the
# pool as unseeded. Numbers in the row, numbers in the hash, and a poisoned hash gets repaired.

_HASH_AS_READ_BACK = {  # what _hash()/hgetall hands set_pacer: every value a string
    **{k: repr(v) for k, v in _SEEDS.items()},
    "r_qps": "18.0",
    "seeded_at": "1788748558.1",
}


def test_persist_stores_numbers_even_when_handed_a_hash_read_back_as_strings(
    monkeypatch, caplog
) -> None:
    conn = FakeConn()
    monkeypatch.setattr(pacer_seeds, "_db", lambda: conn)

    with caplog.at_level("WARNING"):
        pacer_seeds.persist_seeds(
            "minimax-m2.5",
            {**_HASH_AS_READ_BACK, "note": "not a number"},
            _STAMP,
            triggered_by="operator:preet",
        )

    (_sql, params), *_ = conn.executed
    row = json.loads(params[1])
    assert row["r_qps"] == 18.0 and row["r_tok"] == 105_000.0
    assert all(isinstance(v, (int, float)) for v in row.values())
    assert "seeded_at" not in row and "note" not in row
    assert "dropped non-numeric field(s) ['note']" in caplog.text


def test_rehydrate_writes_parseable_numbers_from_a_row_that_holds_strings(monkeypatch) -> None:
    """The row minimax got on 2026-09-07 (string values). Every hash value must parse as a
    float — that is the only contract the Lua ``tonumber``, the planner and /models rely on."""
    row = {k: v for k, v in _HASH_AS_READ_BACK.items() if k != "seeded_at"}
    monkeypatch.setattr(pacer_seeds, "load_latest_seeds", lambda alias: (row, _STAMP))
    fake = FakeRedis()

    assert pacer_seeds.rehydrate_pool(fake, "minimax-m2.5") is True

    cfg = fake.hashes[pacer_cfg_key("minimax-m2.5")]
    assert len(cfg) == len(_SEEDS) + 1
    for k, v in cfg.items():
        assert float(v) == float(v), k  # parses, no quotes
    assert cfg["r_qps"] == "18.0" and float(cfg["r_tok"]) == 105_000.0
    assert cfg["seeded_at"] == repr(_STAMP)


def test_rehydrate_repairs_a_live_hash_whose_values_do_not_parse(monkeypatch, caplog) -> None:
    """A hash left by the bad rehydration ("'18.0'" with quotes) is not 'newer evidence' — no
    reader can use it. It is deleted and rebuilt from the row; a readable live hash is still
    never touched (previous test)."""
    monkeypatch.setattr(pacer_seeds, "load_latest_seeds", lambda alias: (_SEEDS, _STAMP))
    fake = FakeRedis()
    key = pacer_cfg_key("minimax-m2.5")
    fake.hset(key, mapping={k: repr(v) for k, v in _HASH_AS_READ_BACK.items()})
    assert fake.hashes[key]["r_qps"] == "'18.0'"

    with caplog.at_level("WARNING"):
        assert pacer_seeds.rehydrate_pool(fake, "minimax-m2.5") is True

    cfg = fake.hashes[key]
    assert cfg["r_qps"] == "8.0" and cfg["r_tok"] == "105000"
    assert all(float(v) == float(v) for v in cfg.values())
    assert "holds non-numeric values" in caplog.text


def test_rehydrate_degrades_to_defaults_on_db_failure_or_no_row(monkeypatch, caplog) -> None:
    fake = FakeRedis()

    def _down(alias: str) -> None:
        raise RuntimeError("aurora paused")

    monkeypatch.setattr(pacer_seeds, "load_latest_seeds", _down)
    with caplog.at_level("WARNING"):
        assert pacer_seeds.rehydrate_pool(fake, "qwen3-coder-next") is False
    assert "pacer stays on defaults" in caplog.text
    assert pacer_cfg_key("qwen3-coder-next") not in fake.hashes

    monkeypatch.setattr(pacer_seeds, "load_latest_seeds", lambda alias: None)
    with caplog.at_level("INFO"):
        assert pacer_seeds.rehydrate_pool(fake, "laguna-xs-2.1") is False
    assert "needs a probe" in caplog.text
    assert pacer_cfg_key("laguna-xs-2.1") not in fake.hashes


def test_rehydrate_known_pools_covers_every_discovery_pool_and_reports_what_it_wrote(
    monkeypatch,
) -> None:
    rows = {"qwen3-coder-next": (_SEEDS, _STAMP), "deepseek-v4-flash-0731": (_SEEDS, _STAMP)}
    monkeypatch.setattr(pacer_seeds, "load_latest_seeds", lambda alias: rows.get(alias))
    fake = FakeRedis()

    written = pacer_seeds.rehydrate_known_pools(fake)

    assert set(pacer_seeds.known_pools()) == {
        "laguna-xs-2.1",
        "qwen3-coder-next",
        "deepseek-v4-flash-0731",
        "gpt-5-mini",  # 2026-09-04 published-pool families
        "minimax-m2.5",
    }
    assert set(written) == {"qwen3-coder-next", "deepseek-v4-flash-0731"}
    assert pacer_cfg_key("laguna-xs-2.1") not in fake.hashes  # no row -> untouched


def test_launch_rehydrates_an_empty_pool_before_the_alias_copy(monkeypatch) -> None:
    """The safety net: after an eval-tier destroy the pool hash is empty at the first launch;
    the launch restores it from Aurora, THEN copies pool -> run alias as before (E2E-RUN1 #2),
    seeded_at verbatim — the staleness clock belongs to the discovery, not the launch."""
    from swebench_eval.orchestrator.control_plane import run_launch
    from swebench_eval.orchestrator.run_config import RunConfig

    monkeypatch.setattr(pacer_seeds, "load_latest_seeds", lambda alias: (_SEEDS, _STAMP))
    fake = FakeRedis()
    config = RunConfig(model_alias="deepseek-v4-flash-0731-mini")
    with mock.patch("swebench_eval.database.redis_client._get_client", return_value=fake):
        run_launch._publish_autoscaler_overrides("run-1", config)

    pool = fake.hashes[pacer_cfg_key("deepseek-v4-flash-0731")]
    alias = fake.hashes[pacer_cfg_key("deepseek-v4-flash-0731-mini")]
    assert pool["r_tok"] == "105000" and alias["r_tok"] == "105000"
    assert alias["seeded_at"] == repr(_STAMP)


def test_discovery_seed_honours_a_shared_stamp(monkeypatch) -> None:
    """run_discovery stamps ONCE and hands the same seeded_at to the row and the hash, so a
    rehydration restores exactly the clock the live hash had."""
    from swebench_eval.orchestrator.control_plane import ceiling_discovery as cd

    fake = FakeRedis()
    monkeypatch.setattr("swebench_eval.database.redis_client._get_client", lambda: fake)

    cd._seed_pacer_cfg("qwen3-coder-next", {**_SEEDS, "seeded_at": _STAMP})

    cfg = fake.hashes[pacer_cfg_key("qwen3-coder-next")]
    assert cfg["seeded_at"] == repr(_STAMP)
    assert cfg["r_tok"] == "105000"
    assert set(cfg) == set(_SEEDS) | {"seeded_at"}


def test_discovery_persist_failure_is_an_error_not_a_probe_failure(monkeypatch, caplog) -> None:
    from swebench_eval.orchestrator.control_plane import ceiling_discovery as cd

    def _down(*a: Any, **k: Any) -> None:
        raise RuntimeError("aurora paused")

    monkeypatch.setattr(pacer_seeds, "persist_seeds", _down)
    with caplog.at_level("ERROR"):
        cd._persist_seeds("qwen3-coder-next", _SEEDS, _STAMP, "Parasail", "operator", {})
    assert "will not survive the next eval-tier destroy" in caplog.text
