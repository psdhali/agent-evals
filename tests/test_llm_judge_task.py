"""offline-analysis-design.md §9.5/§10.5 — the llm-judge task entrypoint."""

from __future__ import annotations

from typing import Any

import pytest

from swebench_eval.analysis import judge, leak_pass
from swebench_eval.orchestrator.control_plane import llm_judge_task


def test_run_calls_pass_a_before_pass_b(monkeypatch: pytest.MonkeyPatch) -> None:
    """§10.5, locked: Pass A must run first, every invocation."""
    order: list[str] = []

    def _fake_leak_pass(run_id: str, instance_ids: Any) -> int:
        order.append("pass_a")
        return 0

    monkeypatch.setattr(leak_pass, "run_leak_pass", _fake_leak_pass)

    def _fake_run_pass(*args, **kwargs):
        order.append("pass_b")
        return judge.JudgePassSummary(
            pass_id="p",
            total_eligible=0,
            total_judged=0,
            total_skipped_over_budget=0,
            total_parse_failed=0,
            strata={},
        )

    monkeypatch.setattr(judge, "run_pass", _fake_run_pass)
    monkeypatch.setattr(llm_judge_task, "leak_pass", leak_pass)
    monkeypatch.setattr(llm_judge_task, "judge", judge)

    llm_judge_task.run("run-1", max_spend_usd=5.0)

    assert order == ["pass_a", "pass_b"]


def test_run_scopes_pass_a_to_the_same_instance_ids_as_pass_b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        leak_pass,
        "run_leak_pass",
        lambda run_id, instance_ids: captured.setdefault("pass_a_instances", instance_ids) or 0,
    )

    def _fake_run_pass(conn_factory, run_id, **kwargs):
        captured["pass_b_instances"] = kwargs["instance_ids"]
        return judge.JudgePassSummary(
            pass_id="p",
            total_eligible=0,
            total_judged=0,
            total_skipped_over_budget=0,
            total_parse_failed=0,
            strata={},
        )

    monkeypatch.setattr(judge, "run_pass", _fake_run_pass)

    llm_judge_task.run("run-1", instance_ids=["a", "b"], max_spend_usd=5.0)

    assert captured["pass_a_instances"] == ["a", "b"]
    assert captured["pass_b_instances"] == ["a", "b"]


def test_run_generates_a_pass_id_when_none_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(leak_pass, "run_leak_pass", lambda run_id, instance_ids: 0)
    captured: dict[str, Any] = {}

    def _fake_run_pass(conn_factory, run_id, **kwargs):
        captured["pass_id"] = kwargs["pass_id"]
        return judge.JudgePassSummary(
            pass_id=kwargs["pass_id"],
            total_eligible=0,
            total_judged=0,
            total_skipped_over_budget=0,
            total_parse_failed=0,
            strata={},
        )

    monkeypatch.setattr(judge, "run_pass", _fake_run_pass)

    llm_judge_task.run("run-1", max_spend_usd=5.0)
    assert captured["pass_id"].startswith("judge-")


def test_main_requires_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JUDGE_RUN_ID", raising=False)
    monkeypatch.setenv("JUDGE_MAX_SPEND_USD", "5.0")
    with pytest.raises(SystemExit, match="JUDGE_RUN_ID"):
        llm_judge_task.main()


def test_main_requires_max_spend_no_silent_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUDGE_RUN_ID", "run-1")
    monkeypatch.delenv("JUDGE_MAX_SPEND_USD", raising=False)
    with pytest.raises(SystemExit, match="JUDGE_MAX_SPEND_USD"):
        llm_judge_task.main()


def test_main_parses_instance_ids_and_forwards_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUDGE_RUN_ID", "run-9")
    monkeypatch.setenv("JUDGE_MAX_SPEND_USD", "12.5")
    monkeypatch.setenv("JUDGE_INSTANCE_IDS", '["i1", "i2"]')
    monkeypatch.setenv("JUDGE_PRUNE_MODE", "full")
    monkeypatch.setenv("JUDGE_MODEL_ALIAS", "judge-model")
    monkeypatch.setenv("JUDGE_SAMPLE_RATE", "0.5")
    monkeypatch.setenv("JUDGE_MIN_PER_STRATUM", "3")
    monkeypatch.setenv("JUDGE_SEED", "42")

    captured: dict[str, Any] = {}

    def _fake_run(run_id: str, **kwargs: object) -> None:
        captured["run_id"] = run_id
        captured.update(kwargs)

    monkeypatch.setattr(llm_judge_task, "run", _fake_run)

    llm_judge_task.main()

    assert captured["run_id"] == "run-9"
    assert captured["max_spend_usd"] == 12.5
    assert captured["instance_ids"] == ["i1", "i2"]
    assert captured["prune_mode"] == "full"
    assert captured["sample_rate"] == 0.5
    assert captured["min_per_stratum"] == 3
    assert captured["seed"] == 42


def test_main_defaults_when_optional_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUDGE_RUN_ID", "run-9")
    monkeypatch.setenv("JUDGE_MAX_SPEND_USD", "5.0")
    for key in (
        "JUDGE_PASS_ID",
        "JUDGE_INSTANCE_IDS",
        "JUDGE_PRUNE_MODE",
        "JUDGE_MODEL_ALIAS",
        "JUDGE_SAMPLE_RATE",
        "JUDGE_MIN_PER_STRATUM",
        "JUDGE_SEED",
    ):
        monkeypatch.delenv(key, raising=False)

    captured: dict[str, Any] = {}
    monkeypatch.setattr(llm_judge_task, "run", lambda run_id, **kwargs: captured.update(kwargs))

    llm_judge_task.main()

    assert captured["pass_id"] is None
    assert captured["instance_ids"] is None
    assert captured["prune_mode"] == "pruned"
    assert captured["model_alias"] == "judge-model"
    assert captured["sample_rate"] == 1.0
    assert captured["min_per_stratum"] == 5
    assert captured["seed"] is None


def test_main_forwards_synthesis_only_and_run_skips_pass_a(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-08 (owner): "regenerate report" — JUDGE_SYNTHESIS_ONLY=1 reaches run(), and
    run() then skips Pass A (no leak scan) and hands synthesis_only to run_pass."""
    monkeypatch.setenv("JUDGE_RUN_ID", "run-9")
    monkeypatch.setenv("JUDGE_MAX_SPEND_USD", "1.0")
    monkeypatch.setenv("JUDGE_SYNTHESIS_ONLY", "1")
    captured: dict[str, Any] = {}
    real_run = llm_judge_task.run

    def _fake_run(run_id: str, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(llm_judge_task, "run", _fake_run)
    llm_judge_task.main()
    assert captured["synthesis_only"] is True

    scanned: list[object] = []

    def _leak(*a: Any) -> int:
        scanned.append(a)
        return 0

    monkeypatch.setattr(leak_pass, "run_leak_pass", _leak)
    monkeypatch.setattr(llm_judge_task, "_seed_pacer_for", lambda alias: None)
    seen: dict[str, Any] = {}

    def _fake_pass(conn_factory: object, run_id: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return judge.JudgePassSummary(
            pass_id=kwargs["pass_id"],
            total_eligible=0,
            total_judged=0,
            total_skipped_over_budget=0,
            total_parse_failed=0,
            strata={},
        )

    monkeypatch.setattr(judge, "run_pass", _fake_pass)
    real_run("run-9", pass_id="p1", max_spend_usd=1.0, synthesis_only=True)
    assert seen["synthesis_only"] is True
    assert scanned == []  # no Pass A for a report-only pass


def test_main_installs_a_sigterm_handler_that_stops_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-08: an ECS stop-task must end the pass cleanly (lock released, keys
    finalised) instead of leaving the per-model lock behind."""
    import signal

    monkeypatch.setenv("JUDGE_RUN_ID", "run-9")
    monkeypatch.setenv("JUDGE_MAX_SPEND_USD", "1.0")
    captured: dict[str, Any] = {}

    def _fake_run(run_id: str, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(llm_judge_task, "run", _fake_run)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        llm_judge_task.main()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler) and handler is not previous
        stop_event = captured["stop_event"]
        assert not stop_event.is_set()
        handler(signal.SIGTERM, None)
        assert stop_event.is_set()
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_main_forwards_retry_no_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUDGE_RUN_ID", "run-9")
    monkeypatch.setenv("JUDGE_MAX_SPEND_USD", "1.0")
    monkeypatch.setenv("JUDGE_RETRY_NO_VERDICT", "1")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(llm_judge_task, "run", lambda run_id, **kw: captured.update(kw))
    llm_judge_task.main()
    assert captured["retry_no_verdict"] is True
