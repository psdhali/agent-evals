"""Pass-level synthesis (owner, 2026-09-08): one judge-model call at the end of a
pass over a digest of every recorded judgment — "two instances had contamination
because …, a recurring tools issue in the environment …".

Unit-level: the digest builder (deterministic, bounded) and the never-fatal
wrapper around the call. The DB round trip is covered by
test_judge_pass_integration.py (compose stack)."""

from __future__ import annotations

from typing import Any, Self

import pytest

from swebench_eval.analysis import judge, judge_live
from swebench_eval.analysis.judge_llm import JudgeCallTransportError, PassSynthesisResult
from swebench_eval.analysis.rubric import load_rubric

RUBRIC = load_rubric()


def _dim(dimension_id: str, **overrides: Any) -> dict[str, Any]:
    scale = next(d.scale_type for d in RUBRIC.dimensions if d.id == dimension_id)
    base: dict[str, Any] = {
        "dimension_id": dimension_id,
        "scale_type": scale,
        "score_numeric": None if scale in ("boolean_with_span",) else 0,
        "score_secondary": 0 if scale in ("count_and_severity", "ratio") else None,
        "flag": False if scale == "boolean_with_span" else None,
        "span_start_turn": None,
        "span_end_turn": None,
        "reasoning": "nothing found",
        "evidence": [],
        "evidence_missing": False,
    }
    if scale == "ratio":
        base["score_secondary"] = 10
    base.update(overrides)
    return base


def _result(instance_id: str, dims: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    ids = {d["dimension_id"] for d in dims}
    full = dims + [_dim(d.id) for d in RUBRIC.dimensions if d.id not in ids]
    row: dict[str, Any] = {
        "instance_id": instance_id,
        "attempt_number": 1,
        "judged_at": "2026-09-08T00:00:00+00:00",
        "judge_model_resolved": "deepseek/deepseek-v4-flash-0731",
        "rubric_version": "1",
        "judge_prune_mode": "pruned",
        "input_truncated": False,
        "tool_output_pruned": False,
        "judge_parse_failed": False,
        "summary": None,
        "judge_cost_usd": 0.001,
        "dimensions": full,
    }
    row.update(overrides)
    return row


def test_digest_lists_every_dimension_in_rubric_order_with_counts_and_flagged_ids() -> None:
    results = [
        _result(
            "django__django-1",
            [
                _dim(
                    "contamination",
                    score_numeric=2,
                    reasoning="names the gold test before reading it",
                    evidence=[{"turn": 9, "quote": "test_structured_masked_column"}],
                ),
                _dim("environment_problem", flag=True, reasoning="rg: command not found"),
            ],
        ),
        _result(
            "django__django-2",
            [_dim("environment_problem", flag=True, reasoning="rg: command not found")],
        ),
        _result("django__django-3", []),
    ]
    digest = judge.build_pass_digest(
        results, RUBRIC, run_id="run-1", pass_id="judge-1", harness="opencode"
    )

    assert digest.startswith("JUDGE PASS DIGEST — run run-1, pass judge-1, harness opencode")
    assert "Attempts judged: 3." in digest
    headings = [line for line in digest.splitlines() if line.startswith("## ")]
    assert [h.split()[1] for h in headings] == list(RUBRIC.dimension_ids())

    contamination = digest.split("## contamination")[1].split("## ")[0]
    assert "distribution: score=0: 2, score=2: 1" in contamination
    assert "flagged attempts (1): django__django-1/1 [score=2]" in contamination
    assert "names the gold test before reading it" in contamination
    assert 'evidence turn 9: "test_structured_masked_column"' in contamination

    env = digest.split("## environment_problem")[1].split("## ")[0]
    assert "distribution: flag=False: 1, flag=True: 2" in env
    assert (
        "flagged attempts (2): django__django-1/1 [flag=True], django__django-2/1 [flag=True]"
        in env
    )

    # a dimension nobody tripped says so, in words
    assert "## test_gaming" in digest
    assert "flagged attempts: none" in digest.split("## test_gaming")[1].split("## ")[0]


def test_digest_bounds_quoted_examples_but_lists_every_flagged_id() -> None:
    results = [
        _result(
            f"repo__repo-{i}",
            [_dim("loop", flag=True, reasoning=f"loop reasoning {i} " + "x" * 600)],
        )
        for i in range(100)
    ]
    digest = judge.build_pass_digest(
        results, RUBRIC, run_id="r", pass_id="p", max_examples_per_dimension=10
    )
    loop = digest.split("## loop")[1].split("## ")[0]
    assert "flagged attempts (100):" in loop
    for i in range(100):
        assert f"repo__repo-{i}/1" in loop
    quoted = [line for line in loop.splitlines() if line.startswith("- repo__repo-")]
    assert len(quoted) == 10
    assert "judge reasoning for 10 of them (evenly sampled" in loop
    # reasoning is truncated to the digest bound (+ the ellipsis)
    assert all(len(line) < judge.DIGEST_REASONING_CHARS + 80 for line in quoted)
    assert all(line.endswith("…") for line in quoted)


def test_digest_thresholds_mirror_the_ui_findings_filter() -> None:
    """tool_efficiency flags at >= 25 % redundant, likert/count at > 0, boolean at
    flag=True; evidence_missing is reported as a demotion, never as a finding."""
    results = [
        _result("a__a-1", [_dim("tool_efficiency", score_numeric=3, score_secondary=12)]),
        _result("a__a-2", [_dim("tool_efficiency", score_numeric=2, score_secondary=12)]),
        _result("a__a-3", [_dim("hallucination", score_numeric=1, score_secondary=1)]),
        _result("a__a-4", [_dim("contamination", score_numeric=None, evidence_missing=True)]),
    ]
    digest = judge.build_pass_digest(results, RUBRIC, run_id="r", pass_id="p")
    tools = digest.split("## tool_efficiency")[1].split("## ")[0]
    assert "flagged attempts (1): a__a-1/1 [redundant=3/12]" in tools
    assert "redundant 25-50%: 1" in tools and "redundant 10-25%: 1" in tools
    halluc = digest.split("## hallucination")[1].split("## ")[0]
    assert "flagged attempts (1): a__a-3/1 [count=1 severity=1]" in halluc
    contamination = digest.split("## contamination")[1].split("## ")[0]
    assert "demoted for missing evidence (1): a__a-4/1" in contamination
    assert "flagged attempts: none" in contamination


def test_digest_is_deterministic() -> None:
    results = [
        _result(f"r__r-{i}", [_dim("gave_up_early", score_numeric=(i % 4))]) for i in range(30)
    ]
    a = judge.build_pass_digest(results, RUBRIC, run_id="r", pass_id="p")
    b = judge.build_pass_digest(list(reversed(results)), RUBRIC, run_id="r", pass_id="p")
    assert a == b


# --- the never-fatal wrapper ------------------------------------------------


class _Cursor:
    def __init__(self, harness: str | None) -> None:
        self._harness = harness

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, *args: Any) -> None:
        return None

    def fetchone(self) -> tuple[str] | None:
        return (self._harness,) if self._harness else None


class _Conn:
    def __init__(self, harness: str | None = "opencode") -> None:
        self._harness = harness

    def cursor(self) -> _Cursor:
        return _Cursor(self._harness)


def _summary(spend: float = 1.0) -> judge.JudgePassSummary:
    return judge.JudgePassSummary(
        pass_id="judge-1",
        total_eligible=2,
        total_judged=2,
        total_skipped_over_budget=0,
        total_parse_failed=0,
        strata={},
        spend_usd=spend,
    )


def _live() -> judge_live.JudgeLiveState:
    return judge_live.JudgeLiveState(
        run_id="run-1", pass_id="judge-1", status="running", workers=1, selected=2
    )


def _call(summary: judge.JudgePassSummary, live: judge_live.JudgeLiveState) -> None:
    judge._synthesize_pass_findings(
        _Conn(),
        summary,
        live,
        run_id="run-1",
        rubric=RUBRIC,
        api_base_url="http://gateway.local/v1",
        raw_api_key="raw-key",
        model_alias="judge-model",
        max_spend_usd=6.0,
    )


def test_synthesis_is_stored_with_its_cost_and_the_live_status_passes_through_synthesizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(judge_live, "write_judge_live", lambda state, **k: True)
    monkeypatch.setattr(
        judge,
        "fetch_latest_results",
        lambda conn, run_id: [_result("x__x-1", [_dim("loop", flag=True)])],
    )
    seen: dict[str, Any] = {}
    statuses: list[str] = []

    def _fake_synth(**kwargs: Any) -> PassSynthesisResult:
        seen.update(kwargs)
        return PassSynthesisResult(
            text="## Overview\n1 attempt judged.",
            model_requested="judge-model",
            model_resolved="deepseek/deepseek-v4-flash-0731",
            input_tokens=1000,
            output_tokens=200,
            cost_usd=0.02,
        )

    monkeypatch.setattr(judge, "synthesize_pass", _fake_synth)
    live = _live()
    orig = judge_live.write_judge_live

    def _write_live(state: Any, **k: Any) -> bool:
        statuses.append(state.status)
        return True

    monkeypatch.setattr(judge_live, "write_judge_live", _write_live)
    del orig
    summary = _summary(spend=1.0)
    _call(summary, live)

    assert summary.synthesis == "## Overview\n1 attempt judged."
    assert summary.synthesis_error is None
    assert summary.synthesis_model_resolved == "deepseek/deepseek-v4-flash-0731"
    assert summary.synthesis_cost_usd == pytest.approx(0.02)
    assert summary.spend_usd == pytest.approx(1.02)
    assert live.spend_usd == pytest.approx(1.02)
    # republished (heartbeat) while the call is in flight — always as synthesizing
    assert statuses and set(statuses) == {"synthesizing"}
    assert seen["api_key"] == "raw-key" and seen["model_alias"] == "judge-model"
    assert "harness opencode" in seen["digest_text"] and "x__x-1/1" in seen["digest_text"]


def test_synthesis_failure_is_recorded_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(judge_live, "write_judge_live", lambda state, **k: True)
    monkeypatch.setattr(judge, "fetch_latest_results", lambda conn, run_id: [_result("x__x-1", [])])

    def _boom(**kwargs: Any) -> PassSynthesisResult:
        raise JudgeCallTransportError("judge call gave up", attempts=6, last_status=503)

    monkeypatch.setattr(judge, "synthesize_pass", _boom)
    summary = _summary()
    _call(summary, _live())  # must not raise
    assert summary.synthesis is None
    assert summary.synthesis_error is not None
    assert summary.synthesis_error.startswith("JudgeCallTransportError: judge call gave up")
    assert summary.spend_usd == pytest.approx(1.0)


def test_synthesis_is_skipped_at_the_ceiling_and_when_nothing_was_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(judge_live, "write_judge_live", lambda state, **k: True)
    monkeypatch.setattr(
        judge, "synthesize_pass", lambda **k: calls.append("called") or None  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr(judge, "fetch_latest_results", lambda conn, run_id: [])

    at_ceiling = _summary(spend=6.0)
    _call(at_ceiling, _live())
    assert at_ceiling.synthesis is None
    assert at_ceiling.synthesis_error == "skipped: budget ceiling reached before the synthesis call"

    nothing = _summary(spend=0.0)
    _call(nothing, _live())
    assert nothing.synthesis_error == "skipped: no judged attempts to summarise"
    assert calls == []


def test_digest_excludes_timed_out_judgments_and_names_them() -> None:
    results = [
        _result("a__a-1", [_dim("loop", flag=True)]),
        _result("a__a-2", [_dim("loop", flag=None, score_numeric=None)], judge_method="timeout"),
    ]
    digest = judge.build_pass_digest(results, RUBRIC, run_id="r", pass_id="p")
    assert "Attempts judged: 1." in digest
    assert "could NOT score because its call timed out" in digest
    assert "(1; excluded from every distribution below" in digest and "a__a-2/1" in digest
    loop = digest.split("## loop")[1].split("## ")[0]
    assert "flagged attempts (1): a__a-1/1" in loop
    assert "a__a-2" not in loop


def test_synthesis_is_abandoned_at_the_wall_clock_cap_and_on_a_stop_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-08: the streamed report call is otherwise unbounded (OpenRouter keep-alives
    hold a stalled upstream open). Past the cap — or when the operator stops the pass —
    the report is recorded as not written and the pass goes on, without waiting for the
    stuck thread."""
    import threading
    import time as _time

    monkeypatch.setattr(judge_live, "write_judge_live", lambda state, **k: True)
    monkeypatch.setattr(judge, "fetch_latest_results", lambda conn, run_id: [_result("x__x-1", [])])
    never = threading.Event()
    monkeypatch.setattr(judge, "synthesize_pass", lambda **k: never.wait(30))
    monkeypatch.setattr(judge, "SYNTHESIS_HEARTBEAT_S", 0.05)
    monkeypatch.setattr(judge, "SYNTHESIS_MAX_WALL_S", 0.3)

    summary = _summary()
    t0 = _time.monotonic()
    _call(summary, _live())
    assert _time.monotonic() - t0 < 3.0
    assert summary.synthesis is None
    assert summary.synthesis_error is not None and "wall-clock cap" in summary.synthesis_error

    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(judge, "SYNTHESIS_MAX_WALL_S", 60.0)
    summary2 = _summary()
    judge._synthesize_pass_findings(
        _Conn(),
        summary2,
        _live(),
        run_id="run-1",
        rubric=RUBRIC,
        api_base_url="http://gateway.local/v1",
        raw_api_key="raw-key",
        model_alias="judge-model",
        max_spend_usd=6.0,
        stop_event=stop,
    )
    assert summary2.synthesis_error == "skipped: pass stopped by the operator during the report"
    never.set()
