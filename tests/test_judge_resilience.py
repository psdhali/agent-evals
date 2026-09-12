"""Judge pass resilience (2026-09-08) — the first parallel pass (codex run c10df654) died at
75/503 on ONE upstream 429. Three parts, each pinned here:

1. ``judge_llm``: a 429 / 5xx / connection error is retried with bounded backoff (honouring
   ``Retry-After``); auth / not-found are not; exhaustion raises ``JudgeCallTransportError``.
2. ``judge.run_pass``: a ``JudgeCallTransportError`` is a per-candidate failure — counted,
   the pass goes on, the candidate stays unjudged; N in a row (the provider is down) aborts.
3. Resume: a relaunch skips candidates that already have a judge_results row unless
   ``rejudge=True`` — through ``run_pass``, the API routes, and the task's env.
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from swebench_eval.analysis import judge, judge_llm, leak_pass
from swebench_eval.analysis.rubric import load_rubric
from swebench_eval.orchestrator.api.main import app
from swebench_eval.orchestrator.control_plane import llm_judge_task
from tests.test_judge_parallel_unit import _candidates, _run, build_wired


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The parallel-pass wiring from test_judge_parallel_unit, as this module's own fixture."""
    return build_wired(monkeypatch)


# --- 1. transport retry --------------------------------------------------------------------


def _status_error(status: int, headers: dict[str, str] | None = None) -> openai.APIStatusError:
    request = httpx.Request("POST", "http://gateway.local/v1/chat/completions")
    response = httpx.Response(status, request=request, headers=headers or {})
    cls: type[openai.APIStatusError] = {
        429: openai.RateLimitError,
        401: openai.AuthenticationError,
        404: openai.NotFoundError,
        400: openai.BadRequestError,
        502: openai.InternalServerError,
        503: openai.InternalServerError,
    }[status]
    return cls(f"status {status}", response=response, body=None)


class _Client:
    """Fails ``fails`` times with the given exceptions, then returns ``result``."""

    def __init__(self, failures: list[BaseException], result: Any = "ok") -> None:
        self.failures = list(failures)
        self.result = result
        self.calls = 0

        client = self

        class _Completions:
            def create(self, **kwargs: Any) -> Any:
                client.calls += 1
                if client.failures:
                    raise client.failures.pop(0)
                return client.result

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(judge_llm, "_sleep", lambda s: slept.append(s))
    return slept


def test_a_429_is_retried_and_the_answer_comes_back(no_sleep: list[float]) -> None:
    client = _Client([_status_error(429), _status_error(503)], result="answer")
    assert judge_llm._create_with_transport_retry(client, model="m") == "answer"
    assert client.calls == 3
    assert len(no_sleep) == 2 and all(s > 0 for s in no_sleep)


def test_retry_after_header_is_honoured(no_sleep: list[float]) -> None:
    client = _Client([_status_error(429, {"retry-after": "7"})])
    judge_llm._create_with_transport_retry(client, model="m")
    assert no_sleep == [7.0]


def test_connection_errors_are_transient_too(no_sleep: list[float]) -> None:
    request = httpx.Request("POST", "http://gateway.local/v1/chat/completions")
    client = _Client([openai.APIConnectionError(request=request)])
    assert judge_llm._create_with_transport_retry(client, model="m") == "ok"
    assert client.calls == 2


@pytest.mark.parametrize("status", [401, 404, 400])
def test_auth_notfound_badrequest_are_not_retried(status: int, no_sleep: list[float]) -> None:
    client = _Client([_status_error(status)])
    with pytest.raises(openai.APIStatusError):
        judge_llm._create_with_transport_retry(client, model="m")
    assert client.calls == 1 and no_sleep == []


def test_exhaustion_raises_a_transport_error_naming_the_last_status(
    no_sleep: list[float],
) -> None:
    client = _Client([_status_error(429)] * 20)
    with pytest.raises(judge_llm.JudgeCallTransportError) as info:
        judge_llm._create_with_transport_retry(client, model="m")
    assert client.calls == judge_llm._TRANSPORT_MAX_ATTEMPTS
    assert info.value.attempts == judge_llm._TRANSPORT_MAX_ATTEMPTS
    assert info.value.last_status == 429
    assert "429" in str(info.value)


def test_backoff_grows_and_is_capped(no_sleep: list[float]) -> None:
    client = _Client([_status_error(429)] * 20)
    with pytest.raises(judge_llm.JudgeCallTransportError):
        judge_llm._create_with_transport_retry(client, model="m")
    # attempt k sleeps ~ base * 2^(k-1) * jitter(0.5..1.5), capped
    assert all(s <= judge_llm._TRANSPORT_CAP_S * 1.5 for s in no_sleep)
    assert no_sleep[0] <= judge_llm._TRANSPORT_BASE_S * 1.5


def test_call_judge_and_transcribe_go_through_the_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def _fake_retry(client: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        raise judge_llm.JudgeCallTransportError("gave up", attempts=6, last_status=429)

    monkeypatch.setattr(judge_llm, "_create_with_transport_retry", _fake_retry)
    rubric = mock.MagicMock(dimensions=[], version=1)
    with mock.patch("openai.OpenAI", return_value=object()):
        with pytest.raises(judge_llm.JudgeCallTransportError):
            judge_llm.call_judge(
                api_base_url="http://g/v1",
                api_key="k",
                model_alias="judge-model",
                rubric=rubric,
                absent_node_ids=None,
                trajectory_text="t",
            )
        with pytest.raises(judge_llm.JudgeCallTransportError):
            judge_llm.transcribe_reasoning(
                api_base_url="http://g/v1",
                api_key="k",
                model_alias="judge-model",
                rubric=rubric,
                reasoning_text="r",
            )
    assert len(seen) == 2 and all(k["model"] == "judge-model" for k in seen)


# --- 2. per-candidate failure, circuit breaker ---------------------------------------------


def _transport_failures_for(
    wired: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    ids: set[str],
) -> None:
    real = judge._run_one

    def _run_one(rubric: Any, candidate: judge.JudgeCandidate, **kw: Any) -> Any:
        if candidate.instance_id in ids:
            raise judge_llm.JudgeCallTransportError(
                f"gave up on {candidate.instance_id}", attempts=6, last_status=429
            )
        return real(rubric, candidate, **kw)

    monkeypatch.setattr(judge, "_run_one", _run_one)


def test_a_transport_failure_is_counted_and_the_pass_continues(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    wired["candidates"] = _candidates(12)
    _transport_failures_for(wired, monkeypatch, {"repo__repo-3", "repo__repo-7"})

    summary = _run(wired, workers=4)

    assert summary.total_judged == 10
    assert summary.total_call_failed == 2
    written = {i for _, i in wired["writes"]}
    assert "repo__repo-3" not in written and "repo__repo-7" not in written
    assert len(written) == 10
    last = wired["redis"].snapshots[-1]
    assert last["status"] == "done" and last["call_failed"] == 2
    assert "gave up on" in (last["last_error"] or "")
    assert wired["conn"].commits == 1  # the sampling row IS written: the pass finished


def test_consecutive_transport_failures_trip_the_circuit_breaker(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    wired["candidates"] = _candidates(40)
    bad = {f"repo__repo-{i}" for i in range(10, 40)}  # 30 in a row after 10 good ones
    _transport_failures_for(wired, monkeypatch, bad)

    with pytest.raises(RuntimeError, match="consecutive judge calls failed"):
        _run(wired, workers=4)

    last = wired["redis"].snapshots[-1]
    assert last["status"] == "failed"
    assert last["call_failed"] >= judge.JUDGE_MAX_CONSECUTIVE_CALL_FAILURES
    # never all 30 bad ones: the breaker stopped submitting
    assert last["call_failed"] < 30
    assert wired["conn"].commits == 0


def test_a_success_resets_the_consecutive_counter(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alternating failures never reach the breaker — only a RUN of failures does."""
    wired["candidates"] = _candidates(30)
    bad = {f"repo__repo-{i}" for i in range(0, 30, 2)}  # every other one
    _transport_failures_for(wired, monkeypatch, bad)

    summary = _run(wired, workers=1)

    assert summary.total_call_failed == 15 and summary.total_judged == 15
    assert wired["redis"].snapshots[-1]["status"] == "done"


def test_any_other_exception_still_aborts_the_pass(
    wired: dict[str, Any],
) -> None:
    """The old contract for non-transport failures (auth, config, a bug) is unchanged."""
    wired["candidates"] = _candidates(8)
    wired["boom_for"] = {"repo__repo-2"}
    with pytest.raises(RuntimeError, match="gateway exploded"):
        _run(wired, workers=2)


# --- 3. resume -----------------------------------------------------------------------------


def test_run_pass_skips_already_judged_candidates_unless_rejudge(
    wired: dict[str, Any],
) -> None:
    wired["candidates"] = _candidates(6)
    wired["already_judged"] = {("repo__repo-0", 1), ("repo__repo-1", 1)}

    summary = _run(wired, workers=2)
    assert summary.total_judged == 4 and summary.total_already_judged == 2
    assert summary.total_eligible == 4
    assert {i for _, i in wired["writes"]} == {f"repo__repo-{i}" for i in range(2, 6)}
    first = wired["redis"].snapshots[0]
    assert first["selected"] == 4 and first["already_judged"] == 2

    wired["writes"].clear()
    summary = judge.run_pass(
        lambda: wired["conn"],
        "run-1",
        pass_id="pass-2",
        max_spend_usd=100.0,
        rubric=load_rubric(),  # the real rubric; _run_one is faked, so its content is unused
        fetch_artifact=lambda key: b"bytes",
        workers=2,
        rejudge=True,
    )
    assert summary.total_judged == 6 and summary.total_already_judged == 0
    assert len(wired["writes"]) == 6


def test_drop_already_judged_uses_the_judge_results_rows() -> None:
    class _Cur:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def execute(self, sql: str, params: Any) -> None:
            assert "judge_results" in sql and params == ("run-1",)

        def fetchall(self) -> list[tuple[str, int, str, bool]]:
            return [("repo__repo-1", 1, "primary", False)]

    class _Conn:
        def cursor(self) -> Any:
            return _Cur()

    kept = judge.drop_already_judged(_Conn(), "run-1", _candidates(3))
    assert [c.instance_id for c in kept] == ["repo__repo-0", "repo__repo-2"]


class _RouteConn:
    """A judge_results table with one already-judged pair."""

    class _Cur:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def execute(self, *a: Any, **k: Any) -> None:
            pass

        def fetchall(self) -> list[tuple[str, int, str, bool]]:
            return [("i1", 1, "primary", False)]

    def cursor(self) -> Any:
        return self._Cur()

    def close(self) -> None:
        pass


def _cands() -> list[judge.JudgeCandidate]:
    return [
        judge.JudgeCandidate(
            instance_id=i,
            attempt_number=1,
            harness="mini",
            outcome="resolved",
            patch_path="p",
            trajectory_path="t",
            leaked_node_ids=None,
            always_judge=False,
        )
        for i in ("i1", "i2")
    ]


def test_estimate_route_excludes_already_judged_unless_rejudge() -> None:
    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=_RouteConn()),
        mock.patch.object(judge, "fetch_candidates", return_value=_cands()),
    ):
        client = TestClient(app)
        assert client.get("/runs/run-1/judge/estimate").json()["candidate_count"] == 1
        assert client.get("/runs/run-1/judge/estimate?rejudge=true").json()["candidate_count"] == 2


def test_launch_route_forwards_rejudge_and_refuses_when_everything_is_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_TASK_FAMILY", "eval-dev-llm-judge")
    monkeypatch.setenv("JUDGE_SUBNET_IDS", json.dumps(["subnet-1"]))
    monkeypatch.setenv("JUDGE_SECURITY_GROUP_IDS", json.dumps(["sg-1"]))
    fake_ecs = mock.MagicMock()
    fake_ecs.run_task.return_value = {"tasks": [{"taskArn": "arn:task/1"}], "failures": []}
    only_judged = [c for c in _cands() if c.instance_id == "i1"]

    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=_RouteConn()),
        mock.patch.object(judge, "fetch_candidates", return_value=only_judged),
        mock.patch("boto3.client", return_value=fake_ecs),
    ):
        client = TestClient(app)
        resp = client.post("/runs/run-1/judge", json={"max_spend_usd": 1.0})
        assert resp.status_code == 400 and "rejudge=true" in resp.json()["detail"]

        resp = client.post("/runs/run-1/judge", json={"max_spend_usd": 1.0, "rejudge": True})
        assert resp.status_code == 200
        env = {
            e["name"]: e["value"]
            for e in fake_ecs.run_task.call_args.kwargs["overrides"]["containerOverrides"][0][
                "environment"
            ]
        }
        assert env["JUDGE_REJUDGE"] == "1"

    with (
        mock.patch("swebench_eval.orchestrator.api.main._db", return_value=_RouteConn()),
        mock.patch.object(judge, "fetch_candidates", return_value=_cands()),
        mock.patch("boto3.client", return_value=fake_ecs),
    ):
        resp = TestClient(app).post("/runs/run-1/judge", json={"max_spend_usd": 1.0})
        assert resp.status_code == 200
        env = {
            e["name"]: e["value"]
            for e in fake_ecs.run_task.call_args.kwargs["overrides"]["containerOverrides"][0][
                "environment"
            ]
        }
        assert env["JUDGE_REJUDGE"] == "0"


def test_task_main_forwards_judge_rejudge(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(llm_judge_task, "run", lambda run_id, **kw: seen.update(kw))
    monkeypatch.setattr("swebench_eval.logging_bootstrap.configure_logging", lambda: None)
    monkeypatch.setenv("JUDGE_RUN_ID", "run-1")
    monkeypatch.setenv("JUDGE_MAX_SPEND_USD", "5.0")

    monkeypatch.delenv("JUDGE_REJUDGE", raising=False)
    llm_judge_task.main()
    assert seen["rejudge"] is False

    monkeypatch.setenv("JUDGE_REJUDGE", "1")
    llm_judge_task.main()
    assert seen["rejudge"] is True


def test_task_run_forwards_rejudge_to_run_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(leak_pass, "run_leak_pass", lambda run_id, ids: 0)
    monkeypatch.setattr(llm_judge_task, "_seed_pacer_for", lambda alias: None)
    monkeypatch.setattr(llm_judge_task, "load_rubric", lambda: object())
    monkeypatch.setattr(llm_judge_task, "_make_artifact_fetcher", lambda: None)
    monkeypatch.setattr(llm_judge_task, "_make_artifact_store", lambda: None)
    seen: dict[str, Any] = {}

    def _pass(*a: Any, **kw: Any) -> Any:
        seen.update(kw)
        return judge.JudgePassSummary("p", 0, 0, 0, 0, {})

    monkeypatch.setattr(judge, "run_pass", _pass)
    llm_judge_task.run("run-1", max_spend_usd=1.0, rejudge=True)
    assert seen["rejudge"] is True


def test_drop_already_judged_can_retry_the_no_verdict_rows() -> None:
    """2026-09-08 (owner): timed-out / parse-failed judgments are rows (a resume skips
    them) — retry_no_verdict keeps exactly those, without a full re-judge."""

    class _Cur:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def execute(self, sql: str, params: Any) -> None:
            assert "DISTINCT ON" in sql and params == ("run-1",)

        def fetchall(self) -> list[tuple[str, int, str, bool]]:
            return [
                ("repo__repo-0", 1, "primary", False),
                ("repo__repo-1", 1, "timeout", False),
                ("repo__repo-2", 1, "primary", True),
            ]

    class _Conn:
        def cursor(self) -> Any:
            return _Cur()

    cands = _candidates(4)
    assert [c.instance_id for c in judge.drop_already_judged(_Conn(), "run-1", cands)] == [
        "repo__repo-3"
    ]
    kept = judge.drop_already_judged(_Conn(), "run-1", cands, retry_no_verdict=True)
    assert [c.instance_id for c in kept] == ["repo__repo-1", "repo__repo-2", "repo__repo-3"]
    assert judge.latest_judgment_by_key(_Conn(), "run-1") == {
        ("repo__repo-0", 1): "judged",
        ("repo__repo-1", 1): "timeout",
        ("repo__repo-2", 1): "parse_failed",
    }
