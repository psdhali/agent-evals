"""Parallel judge pass (2026-09-07): N candidates in flight, ONE DB writer (the calling
thread), the §3.10 ceiling checked before every submission, a raising call still aborts the
pass after draining, and the live snapshot published as it goes. No DB, no gateway — the
pass's collaborators are faked at the module seams the integration suite already uses.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from swebench_eval.analysis import judge, judge_keys, judge_live
from swebench_eval.analysis.judge import JudgeCandidate
from swebench_eval.analysis.rubric import load_rubric


class _Conn:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.commits = 0
        self.closed = False

    def cursor(self) -> Any:
        conn = self

        class _Cur:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *a: object) -> None:
                pass

            def execute(self, sql: str, params: Any = None) -> None:
                conn.executed.append(sql)

        return _Cur()

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


class _FakeRedis:
    def __init__(self) -> None:
        self.snapshots: list[str] = []

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.snapshots.append(value)


def _candidates(n: int) -> list[JudgeCandidate]:
    return [
        JudgeCandidate(
            instance_id=f"repo__repo-{i}",
            attempt_number=1,
            harness="mini_swe_agent",
            outcome="resolved",
            patch_path=f"p/{i}",
            trajectory_path=f"t/{i}",
            leaked_node_ids=None,
            always_judge=False,
        )
        for i in range(n)
    ]


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return build_wired(monkeypatch)


def build_wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The wiring behind the ``wired`` fixture — a plain function so other test modules
    (test_judge_resilience.py) can declare their own fixture on it without the
    import-a-fixture / F811 dance."""
    state: dict[str, Any] = {
        "conn": _Conn(),
        "writes": [],  # (thread ident, instance_id)
        "in_flight": 0,
        "max_in_flight": 0,
        "lock": threading.Lock(),
        "redis": _FakeRedis(),
        "cost_per_call": 0.01,
        "call_delay_s": 0.05,
        "boom_for": set(),
    }
    monkeypatch.setattr(judge_keys, "claim_pass_lock", lambda conn, pass_id: None)
    monkeypatch.setattr(judge_keys, "release_pass_lock", lambda conn, pass_id: None)
    monkeypatch.setattr(
        judge_keys, "provision_pass_keys", lambda pass_id, budget: ("raw-key", "kid-1", "hash-1")
    )
    monkeypatch.setattr(judge_keys, "finalize_pass_keys", lambda *a, **k: None)
    monkeypatch.setattr(judge, "gateway_base_url", lambda: "http://gateway.local/v1")
    monkeypatch.setattr(
        judge, "fetch_candidates", lambda conn, run_id, ids=None: state["candidates"]
    )
    monkeypatch.setattr(judge, "stratify", lambda cands, **kw: (list(cands), {}))
    # resume rule (2026-09-08): nothing judged yet unless a test says otherwise
    monkeypatch.setattr(
        judge, "already_judged_keys", lambda conn, run_id: set(state.get("already_judged", ()))
    )
    monkeypatch.setattr(
        judge,
        "latest_judgment_by_key",
        lambda conn, run_id: {k: "judged" for k in state.get("already_judged", ())},
    )

    def _run_one(rubric: Any, candidate: JudgeCandidate, **kw: Any) -> Any:
        with state["lock"]:
            state["in_flight"] += 1
            state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        try:
            time.sleep(state["call_delay_s"])
            if candidate.instance_id in state["boom_for"]:
                raise RuntimeError(f"gateway exploded on {candidate.instance_id}")
            return ({"summary": candidate.instance_id}, [], state["cost_per_call"], False)
        finally:
            with state["lock"]:
                state["in_flight"] -= 1

    monkeypatch.setattr(judge, "_run_one", _run_one)

    def _write_result(
        conn: Any, run_id: str, candidate: JudgeCandidate, row: Any, dims: Any
    ) -> None:
        assert conn is state["conn"]
        state["writes"].append((threading.get_ident(), candidate.instance_id))

    monkeypatch.setattr(judge, "_write_result", _write_result)
    monkeypatch.setattr(
        judge_live,
        "write_judge_live",
        lambda st, client=None: state["redis"].set("k", st.to_payload()),
    )
    return state


def _run(
    state: dict[str, Any], *, workers: int, max_spend: float = 100.0
) -> judge.JudgePassSummary:
    return judge.run_pass(
        lambda: state["conn"],
        "run-1",
        pass_id="pass-1",
        max_spend_usd=max_spend,
        rubric=load_rubric(),  # the real rubric; _run_one is faked, so its content is unused
        fetch_artifact=lambda key: b"bytes",
        workers=workers,
    )


def test_workers_run_concurrently_but_every_write_is_on_the_calling_thread(
    wired: dict[str, Any],
) -> None:
    wired["candidates"] = _candidates(12)

    summary = _run(wired, workers=4)

    assert summary.total_judged == 12
    assert wired["max_in_flight"] >= 2, "no concurrency observed"
    assert wired["max_in_flight"] <= 4
    assert {t for t, _ in wired["writes"]} == {threading.get_ident()}
    assert sorted(i for _, i in wired["writes"]) == sorted(
        c.instance_id for c in wired["candidates"]
    )
    assert wired["conn"].commits == 1 and wired["conn"].closed


def test_workers_1_is_the_sequential_pass(wired: dict[str, Any]) -> None:
    wired["candidates"] = _candidates(5)
    summary = _run(wired, workers=1)
    assert summary.total_judged == 5 and wired["max_in_flight"] == 1


def test_ceiling_is_checked_before_submission_and_overshoot_is_bounded_by_workers(
    wired: dict[str, Any],
) -> None:
    """§3.10 with N in flight: nothing starts once recorded spend reaches the cap; the
    overshoot is at most N calls (the ones already running), never the whole queue."""
    wired["candidates"] = _candidates(40)
    wired["cost_per_call"] = 1.0

    summary = _run(wired, workers=4, max_spend=10.0)

    assert 10 <= summary.total_judged <= 10 + 4
    assert summary.total_judged + summary.total_skipped_over_budget == 40
    assert summary.spend_usd == pytest.approx(float(summary.total_judged))


def test_a_raising_call_aborts_the_pass_after_draining_in_flight(wired: dict[str, Any]) -> None:
    """Same contract as the sequential loop: the exception propagates (keys still get
    finalised by the caller's finally) — but results that were already in flight are still
    recorded, and no NEW candidate starts after the failure."""
    wired["candidates"] = _candidates(20)
    wired["boom_for"] = {"repo__repo-3"}

    with pytest.raises(RuntimeError, match="gateway exploded on repo__repo-3"):
        _run(wired, workers=4)

    written = {i for _, i in wired["writes"]}
    assert "repo__repo-3" not in written
    # 3 was in the first batch of 4; at most that batch (+ whatever was submitted before the
    # failure surfaced) completed — never all 20.
    assert len(written) < 20
    last = wired["redis"].snapshots[-1]
    assert last["status"] == "failed" and "gateway exploded" in (last["last_error"] or "")
    assert wired["conn"].commits == 0  # the sampling row is NOT written for a failed pass


def test_artifact_fetch_failure_skips_the_candidate_and_is_counted_live(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    wired["candidates"] = _candidates(6)

    def _fetch(key: str) -> bytes:
        if key == "t/2":
            raise OSError("s3 down")
        return b"bytes"

    summary = judge.run_pass(
        lambda: wired["conn"],
        "run-1",
        pass_id="pass-1",
        max_spend_usd=100.0,
        rubric=load_rubric(),
        fetch_artifact=_fetch,
        workers=3,
    )
    assert summary.total_judged == 5
    assert wired["redis"].snapshots[-1]["skipped_artifacts"] == 1


def test_live_snapshots_track_the_pass_and_end_with_done(wired: dict[str, Any]) -> None:
    wired["candidates"] = _candidates(8)

    _run(wired, workers=3)

    snaps = wired["redis"].snapshots
    assert snaps[0]["status"] == "running" and snaps[0]["selected"] == 8
    assert snaps[0]["workers"] == 3
    assert any(s["in_flight_count"] > 0 for s in snaps)
    assert max(s["in_flight_count"] for s in snaps) <= 3
    judged = [s["judged"] for s in snaps]
    assert judged == sorted(judged) and judged[-1] == 8
    assert snaps[-1]["status"] == "done" and snaps[-1]["finished_at"] is not None
    assert snaps[-1]["in_flight"] == []
    assert snaps[-1]["spend_usd"] == pytest.approx(0.08)


def test_workers_is_clamped_to_the_hard_cap(wired: dict[str, Any]) -> None:
    wired["candidates"] = _candidates(3)
    _run(wired, workers=10_000)
    assert wired["redis"].snapshots[0]["workers"] == judge.JUDGE_MAX_WORKERS
    _run(wired, workers=0)
    assert wired["redis"].snapshots[-1]["workers"] == 1


def test_stop_event_keeps_judged_rows_abandons_in_flight_and_still_finalises(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-08 (owner: "kill the judge and relaunch with more workers"): a stop
    request — the task's SIGTERM handler — ends the pass within ~1 s without waiting
    for the in-flight calls, keeps everything already written, writes the ledger with
    the synthesis skipped, publishes status "stopped", and reaches key finalisation +
    lock release (the finally block) — a relaunch then resumes instead of being refused."""
    wired["candidates"] = _candidates(40)
    wired["call_delay_s"] = 5.0  # every call is "slow"; the stop must not wait for them
    stop = threading.Event()
    finalised: list[str] = []
    monkeypatch.setattr(judge_keys, "finalize_pass_keys", lambda *a, **k: finalised.append("keys"))
    monkeypatch.setattr(judge_keys, "release_pass_lock", lambda c, p: finalised.append("lock"))
    synth: list[str] = []
    monkeypatch.setattr(judge, "synthesize_pass", lambda **k: synth.append("called"))

    # let the first submissions start, then ask for the stop
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    summary = judge.run_pass(
        lambda: wired["conn"],
        "run-1",
        pass_id="pass-stop",
        max_spend_usd=100.0,
        rubric=load_rubric(),
        fetch_artifact=lambda key: b"bytes",
        workers=4,
        stop_event=stop,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, f"stop waited for in-flight calls ({elapsed:.1f}s)"
    assert summary.total_judged == 0  # nothing came back in 0.3 s; the 4 in flight were abandoned
    assert summary.synthesis is None
    assert summary.synthesis_error == "skipped: pass stopped by the operator"
    assert synth == []
    assert finalised == ["keys", "lock"]
    assert wired["redis"].snapshots[-1]["status"] == "stopped"


def test_stop_after_some_results_keeps_them_and_submits_nothing_new(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic: the stop is requested from the main thread's own write of the
    3rd result, so exactly what was recorded before the stop is kept, nothing new is
    submitted, and the status reads "stopped"."""
    wired["candidates"] = _candidates(60)
    wired["call_delay_s"] = 0.02
    stop = threading.Event()
    monkeypatch.setattr(judge, "synthesize_pass", lambda **k: None)
    real_write = judge._write_result

    def _write_then_stop(conn: Any, run_id: str, candidate: Any, row: Any, dims: Any) -> None:
        real_write(conn, run_id, candidate, row, dims)
        if len(wired["writes"]) == 3:
            stop.set()

    monkeypatch.setattr(judge, "_write_result", _write_then_stop)
    summary = judge.run_pass(
        lambda: wired["conn"],
        "run-1",
        pass_id="pass-stop-2",
        max_spend_usd=100.0,
        rubric=load_rubric(),
        fetch_artifact=lambda key: b"bytes",
        workers=2,
        stop_event=stop,
    )
    # the batch that completed alongside the 3rd result may also be recorded (same
    # `done` set), never anything submitted after the stop
    assert 3 <= summary.total_judged <= 4
    assert len(wired["writes"]) == summary.total_judged
    assert wired["redis"].snapshots[-1]["status"] == "stopped"


def test_a_timed_out_call_is_recorded_as_a_no_verdict_judgment_and_never_trips_the_breaker(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-08 (owner: "if it takes more than 10 mins, just leave those as failed").
    A candidate whose call hit the ceiling gets a judge_results row (judge_method
    "timeout", every dimension null), is counted as timed_out, does not count toward the
    8-consecutive-failures breaker even when many in a row do it, and the pass ends "done"
    with its report step reached."""
    from swebench_eval.analysis.judge_llm import JudgeCallTransportError
    from swebench_eval.analysis.rubric import load_rubric

    wired["candidates"] = _candidates(20)
    wired["call_delay_s"] = 0.01
    rubric = load_rubric()
    real_run_one = judge._run_one

    def _run_one_timeout(rubric_: Any, candidate: JudgeCandidate, **kw: Any) -> Any:
        if int(candidate.instance_id.split("-")[-1]) < 12:  # 12 in a row time out
            raise JudgeCallTransportError(
                "judge call timed out after 600s", attempts=1, last_status=504, timed_out=True
            )
        return real_run_one(rubric_, candidate, **kw)

    monkeypatch.setattr(judge, "_run_one", _run_one_timeout)
    monkeypatch.setattr(judge, "synthesize_pass", lambda **k: None)
    rows: list[tuple[str, Any, Any]] = []

    def _write(conn: Any, run_id: str, candidate: JudgeCandidate, row: Any, dims: Any) -> None:
        rows.append((candidate.instance_id, row, dims))

    monkeypatch.setattr(judge, "_write_result", _write)
    summary = judge.run_pass(
        lambda: wired["conn"],
        "run-1",
        pass_id="pass-timeouts",
        max_spend_usd=100.0,
        rubric=rubric,
        fetch_artifact=lambda key: b"bytes",
        workers=4,
    )
    assert summary.total_timed_out == 12
    assert summary.total_judged == 8
    assert summary.total_call_failed == 0
    assert len(rows) == 20
    timeout_rows = [(r, d) for _, r, d in rows if r.get("judge_method") == "timeout"]
    assert len(timeout_rows) == 12
    row, dims = timeout_rows[0]
    assert row["judge_parse_failed"] is False and row["summary"] is None
    assert row["judge_cost_usd"] == 0.0
    assert row["judge_model_resolved"] == "deepseek/deepseek-v4-flash-0731"
    assert len(dims) == len(rubric.dimensions)
    assert all(d.score_numeric is None and d.flag is None for d in dims)
    assert all(
        d.reasoning and d.reasoning.startswith("not judged: the judge call timed out") for d in dims
    )
    assert wired["redis"].snapshots[-1]["status"] == "done"
    assert wired["redis"].snapshots[-1]["timed_out"] == 12
