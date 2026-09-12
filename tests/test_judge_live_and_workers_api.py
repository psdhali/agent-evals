"""judge_live (the TTL'd snapshot), the judge-model pacer seeding, and the API side of the
parallel judge: `workers` on the launch body (clamped 1..100, forwarded as JUDGE_WORKERS),
GET /runs/{id}/judge/live, and the task's JUDGE_WORKERS parsing."""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from swebench_eval.analysis import judge, judge_live, leak_pass
from swebench_eval.analysis.judge import JudgeCandidate
from swebench_eval.gateway.pacer import pacer_cfg_key
from swebench_eval.orchestrator.api.main import app
from swebench_eval.orchestrator.control_plane import llm_judge_task, pacer_seeds


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, tuple[str, int | None]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = (value, ex)

    def get(self, key: str) -> bytes | None:
        v = self.store.get(key)
        return v[0].encode() if v else None

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def hset(self, key: str, mapping: dict[str, Any]) -> None:
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})


# --- judge_live ---------------------------------------------------------------------------


def test_live_snapshot_round_trips_with_a_running_ttl_and_a_final_ttl() -> None:
    r = _FakeRedis()
    st = judge_live.JudgeLiveState(
        run_id="run-1", pass_id="p1", status="running", workers=24, selected=500, max_spend_usd=25
    )
    st.judged = 10
    st.in_flight.append(judge_live.InFlight("a", 1, 1.0))
    assert judge_live.write_judge_live(st, client=r) is True
    _, ttl = r.store[judge_live.judge_live_key("run-1")]
    assert ttl == judge_live.JUDGE_LIVE_TTL_S

    doc = judge_live.read_judge_live("run-1", client=r)
    assert doc is not None
    assert (doc["judged"], doc["selected"], doc["in_flight_count"]) == (10, 500, 1)
    assert doc["in_flight"][0]["instance_id"] == "a"
    assert doc["eta_s"] is not None and doc["elapsed_s"] >= 0

    st.status = "done"
    judge_live.write_judge_live(st, client=r)
    _, ttl = r.store[judge_live.judge_live_key("run-1")]
    assert ttl == judge_live.JUDGE_LIVE_FINAL_TTL_S


def test_live_write_and_read_never_raise_without_redis() -> None:
    class _Down:
        def set(self, *a: Any, **k: Any) -> None:
            raise ConnectionError("no valkey")

        def get(self, *a: Any) -> None:
            raise ConnectionError("no valkey")

    st = judge_live.JudgeLiveState(
        run_id="run-1", pass_id="p1", status="running", workers=1, selected=1
    )
    assert judge_live.write_judge_live(st, client=_Down()) is False
    assert judge_live.read_judge_live("run-1", client=_Down()) is None
    assert judge_live.read_judge_live("run-1", client=_FakeRedis()) is None


# --- pacer seeding for the judge alias ----------------------------------------------------


def test_seed_alias_from_pool_copies_the_pool_hash_once(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _FakeRedis()
    r.hset(pacer_cfg_key("deepseek-v4-flash-0731"), mapping={"r_tok": "244608", "seeded_at": "1.0"})
    monkeypatch.setattr(
        "swebench_eval.gateway.rotatable_models.pool_alias_for",
        lambda alias: "deepseek-v4-flash-0731" if alias == "judge-model" else None,
    )
    assert pacer_seeds.seed_alias_from_pool(r, "judge-model") == 2
    assert r.hashes[pacer_cfg_key("judge-model")]["r_tok"] == "244608"
    # never overwrites an existing alias hash
    r.hset(pacer_cfg_key("judge-model"), mapping={"r_tok": "1"})
    assert pacer_seeds.seed_alias_from_pool(r, "judge-model") == 0
    assert r.hashes[pacer_cfg_key("judge-model")]["r_tok"] == "1"
    # unknown alias: nothing
    assert pacer_seeds.seed_alias_from_pool(r, "nope") == 0


def test_seed_alias_from_pool_rehydrates_an_empty_pool_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = _FakeRedis()
    monkeypatch.setattr(
        "swebench_eval.gateway.rotatable_models.pool_alias_for",
        lambda alias: "deepseek-v4-flash-0731",
    )
    monkeypatch.setattr(
        pacer_seeds, "load_latest_seeds", lambda alias: ({"r_tok": 244608, "k_inflight": 5}, 1.0)
    )
    assert pacer_seeds.seed_alias_from_pool(r, "judge-model") == 3  # r_tok, k_inflight, seeded_at
    assert r.hashes[pacer_cfg_key("judge-model")]["k_inflight"] == "5"


# --- API: workers on the launch body + the live route -------------------------------------


def _candidate() -> JudgeCandidate:
    return JudgeCandidate(
        instance_id="i1",
        attempt_number=1,
        harness="mini_swe_agent",
        outcome="resolved",
        patch_path="p",
        trajectory_path="t",
        leaked_node_ids=None,
        always_judge=False,
    )


def _launch(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> tuple[Any, Any]:
    monkeypatch.setenv("JUDGE_TASK_FAMILY", "eval-dev-llm-judge")
    monkeypatch.setenv("CLUSTER", "eval-dev-cluster")
    monkeypatch.setenv("JUDGE_SUBNET_IDS", '["subnet-1"]')
    monkeypatch.setenv("JUDGE_SECURITY_GROUP_IDS", '["sg-1"]')
    fake_ecs = mock.MagicMock()
    fake_boto3 = mock.MagicMock()
    fake_boto3.client.return_value = fake_ecs
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=mock.MagicMock()),
        mock.patch.object(judge, "fetch_candidates", return_value=[_candidate()]),
        mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
    ):
        resp = TestClient(app).post("/runs/run-1/judge", json=body)
    return resp, fake_ecs


def test_launch_forwards_workers_as_env_with_24_as_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp, ecs = _launch(monkeypatch, {"max_spend_usd": 3.5})
    assert resp.status_code == 200
    env = {
        e["name"]: e["value"]
        for e in ecs.run_task.call_args.kwargs["overrides"]["containerOverrides"][0]["environment"]
    }
    assert env["JUDGE_WORKERS"] == "24"

    resp, ecs = _launch(monkeypatch, {"max_spend_usd": 3.5, "workers": 64})
    assert resp.status_code == 200
    env = {
        e["name"]: e["value"]
        for e in ecs.run_task.call_args.kwargs["overrides"]["containerOverrides"][0]["environment"]
    }
    assert env["JUDGE_WORKERS"] == "64"


@pytest.mark.parametrize("bad", [0, 101, -3])
def test_launch_rejects_workers_outside_1_to_100(monkeypatch: pytest.MonkeyPatch, bad: int) -> None:
    resp, ecs = _launch(monkeypatch, {"max_spend_usd": 3.5, "workers": bad})
    assert resp.status_code == 422
    ecs.run_task.assert_not_called()


def test_live_route_returns_the_snapshot_or_null(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(judge_live, "read_judge_live", lambda run_id, client=None: None)
    resp = TestClient(app).get("/runs/run-1/judge/live")
    assert resp.status_code == 200 and resp.json() == {"run_id": "run-1", "live": None}

    st = judge_live.JudgeLiveState(
        run_id="run-1", pass_id="p1", status="running", workers=24, selected=500, max_spend_usd=25
    )
    st.judged = 7
    st.in_flight.append(judge_live.InFlight("django__django-1", 1, 5.0))
    payload = json.loads(json.dumps(st.to_payload()))
    monkeypatch.setattr(judge_live, "read_judge_live", lambda run_id, client=None: payload)
    resp = TestClient(app).get("/runs/run-1/judge/live")
    assert resp.status_code == 200
    live = resp.json()["live"]
    assert (live["judged"], live["selected"], live["status"], live["workers"]) == (
        7,
        500,
        "running",
        24,
    )
    assert live["in_flight"] == [
        {"instance_id": "django__django-1", "attempt_number": 1, "started_at": 5.0}
    ]
    assert live["in_flight_count"] == 1

    # a snapshot from another version (missing fields) reads as absent, never 500
    monkeypatch.setattr(judge_live, "read_judge_live", lambda run_id, client=None: {"pass_id": "x"})
    resp = TestClient(app).get("/runs/run-1/judge/live")
    assert resp.status_code == 200 and resp.json()["live"] is None


# --- the task: JUDGE_WORKERS + pacer seeding ---------------------------------------------


def test_task_main_forwards_and_clamps_judge_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _run(run_id: str, **kw: Any) -> Any:
        seen.update(kw)

    monkeypatch.setattr(llm_judge_task, "run", _run)
    monkeypatch.setattr("swebench_eval.logging_bootstrap.configure_logging", lambda: None)
    monkeypatch.setenv("JUDGE_RUN_ID", "run-1")
    monkeypatch.setenv("JUDGE_MAX_SPEND_USD", "5.0")

    monkeypatch.delenv("JUDGE_WORKERS", raising=False)
    llm_judge_task.main()
    assert seen["workers"] == judge.JUDGE_DEFAULT_WORKERS

    monkeypatch.setenv("JUDGE_WORKERS", "500")
    llm_judge_task.main()
    assert seen["workers"] == judge.JUDGE_MAX_WORKERS

    monkeypatch.setenv("JUDGE_WORKERS", "eight")
    with pytest.raises(SystemExit, match="JUDGE_WORKERS"):
        llm_judge_task.main()


def test_task_run_seeds_the_judge_alias_pacer_before_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    def _leak(run_id: str, ids: Any) -> int:
        order.append("A")
        return 0

    monkeypatch.setattr(leak_pass, "run_leak_pass", _leak)
    monkeypatch.setattr(
        llm_judge_task, "_seed_pacer_for", lambda alias: order.append(f"seed:{alias}")
    )
    monkeypatch.setattr(llm_judge_task, "load_rubric", lambda: object())
    monkeypatch.setattr(llm_judge_task, "_make_artifact_fetcher", lambda: None)
    monkeypatch.setattr(llm_judge_task, "_make_artifact_store", lambda: None)

    def _pass(*a: Any, **kw: Any) -> Any:
        order.append(f"B:workers={kw['workers']}")
        return judge.JudgePassSummary("p", 0, 0, 0, 0, {})

    monkeypatch.setattr(judge, "run_pass", _pass)
    llm_judge_task.run("run-1", max_spend_usd=1.0, workers=12)
    assert order == ["A", "seed:judge-model", "B:workers=12"]


def test_seed_alias_from_pool_fill_missing_adds_only_what_the_alias_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-08: after an eval-tier cycle an operator r_qps edit recreates the alias hash
    with ONE field; fill_missing adds the pool's other fields and keeps the operator's value."""
    r = _FakeRedis()
    r.hset(
        pacer_cfg_key("minimax-m2.5"),
        mapping={"r_tok": "272374", "k_inflight": "3791231", "r_qps": "24", "seeded_at": "1.0"},
    )
    r.hset(pacer_cfg_key("minimax-m2.5-opencode"), mapping={"r_qps": "18", "seeded_at": "2.0"})
    monkeypatch.setattr(
        "swebench_eval.gateway.rotatable_models.pool_alias_for",
        lambda alias: "minimax-m2.5" if alias.startswith("minimax-m2.5-") else None,
    )
    # default (no fill): an existing hash is never touched
    assert pacer_seeds.seed_alias_from_pool(r, "minimax-m2.5-opencode") == 0
    assert set(r.hashes[pacer_cfg_key("minimax-m2.5-opencode")]) == {"r_qps", "seeded_at"}

    assert pacer_seeds.seed_alias_from_pool(r, "minimax-m2.5-opencode", fill_missing=True) == 2
    alias = r.hashes[pacer_cfg_key("minimax-m2.5-opencode")]
    assert alias["r_tok"] == "272374" and alias["k_inflight"] == "3791231"
    assert alias["r_qps"] == "18" and alias["seeded_at"] == "2.0"  # kept, not the pool's
    # idempotent: a complete alias gets nothing
    assert pacer_seeds.seed_alias_from_pool(r, "minimax-m2.5-opencode", fill_missing=True) == 0
    # an EMPTY alias with fill_missing behaves like the plain copy
    assert pacer_seeds.seed_alias_from_pool(r, "minimax-m2.5-codex", fill_missing=True) == 4
