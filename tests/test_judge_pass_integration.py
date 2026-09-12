"""offline-analysis-design.md §3/§9/§10 — Pass B against a real Postgres.

The gateway/OpenRouter calls are mocked (no live gateway in this suite —
that's an e2e concern for the real bring-up); everything DB-shaped is real:
candidate fetch via a real join, real judge_pass_lock, real writes to
judge_results/judge_dimension_scores/judge_sampling.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swebench_eval.analysis import judge, judge_keys
from swebench_eval.analysis.judge_llm import (
    PASS_SYNTHESIS_PROMPT_VERSION,
    JudgeCallResult,
    PassSynthesisResult,
)
from swebench_eval.analysis.rubric import load_rubric

pytestmark = pytest.mark.integration

_PREFIX = "jpi-test-"


def _db():
    from swebench_eval.database.connection import get_connection

    return get_connection()


@pytest.fixture(autouse=True)
def _clean_rows():
    def _clean():
        conn = _db()
        try:
            with conn.cursor() as cur:
                for table in (
                    "judge_dimension_scores",
                    "judge_results",
                    "judge_sampling",
                    "judge_pass_lock",
                    "instance_results",
                    "run_targets",
                    "runs",
                ):
                    if table == "judge_pass_lock":
                        cur.execute(
                            "DELETE FROM judge_pass_lock WHERE pass_id LIKE %s", (f"{_PREFIX}%",)
                        )
                    else:
                        cur.execute(f"DELETE FROM {table} WHERE run_id LIKE %s", (f"{_PREFIX}%",))
            conn.commit()
        finally:
            conn.close()

    _clean()
    yield
    _clean()


def _seed_run(run_id: str, harness: str = "mini_swe_agent") -> None:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (run_id, config_snapshot, status) VALUES (%s, '{}'::jsonb, 'completed')",
                (run_id,),
            )
            cur.execute(
                "INSERT INTO run_targets (run_id, harness, model_alias) VALUES (%s, %s, 'laguna-xs-2.1')",
                (run_id, harness),
            )
            # one resolved, one unresolved, one grade_invalid, one with a leak hit
            cur.execute(
                """INSERT INTO instance_results
                       (run_id, instance_id, attempt_number, phase, state, patch_path, trajectory_path)
                   VALUES (%s, 'inst-resolved', 1, 'harness', 'RESOLVED', 'p1', 't1'),
                          (%s, 'inst-unresolved', 1, 'harness', 'RESOLVED', 'p2', 't2'),
                          (%s, 'inst-invalid', 1, 'harness', 'RESOLVED', 'p3', 't3'),
                          (%s, 'inst-leaky', 1, 'harness', 'RESOLVED', 'p4', 't4')""",
                (run_id, run_id, run_id, run_id),
            )
            cur.execute(
                """UPDATE instance_results SET leaked_node_ids = ARRAY['tests/x.py::test_y']
                   WHERE run_id = %s AND instance_id = 'inst-leaky'""",
                (run_id,),
            )
            cur.execute(
                """INSERT INTO instance_results
                       (run_id, instance_id, attempt_number, phase, state, verdict)
                   VALUES (%s, 'inst-resolved', 1, 'eval', 'RESOLVED', 'resolved'),
                          (%s, 'inst-unresolved', 1, 'eval', 'UNRESOLVED', 'unresolved')""",
                (run_id, run_id),
            )
            cur.execute(
                """INSERT INTO instance_results
                       (run_id, instance_id, attempt_number, phase, state, grade_invalid)
                   VALUES (%s, 'inst-invalid', 1, 'eval', 'RESOLVED', TRUE)""",
                (run_id,),
            )
        conn.commit()
    finally:
        conn.close()


def test_fetch_candidates_derives_outcome_and_always_judge_from_real_joins() -> None:
    run_id = f"{_PREFIX}candidates"
    _seed_run(run_id)
    conn = _db()
    try:
        candidates = judge.fetch_candidates(conn, run_id)
    finally:
        conn.close()

    by_id = {c.instance_id: c for c in candidates}
    assert len(candidates) == 4
    assert all(c.harness == "mini_swe_agent" for c in candidates)
    assert by_id["inst-resolved"].outcome == "resolved"
    assert by_id["inst-unresolved"].outcome == "unresolved"
    assert by_id["inst-invalid"].outcome == "invalid"
    assert by_id["inst-invalid"].always_judge is True  # grade_invalid -> always_judge
    assert by_id["inst-leaky"].always_judge is True  # leaked_node_ids nonempty -> always_judge
    assert by_id["inst-resolved"].always_judge is False


def test_fetch_candidates_skips_attempts_without_a_trajectory() -> None:
    """2026-09-09: an aborted run's NEVER_DISPATCHED / ABORTED_IN_FLIGHT rows have no
    artifacts — nothing to judge, so they are not candidates (the Laguna pilot showed
    500 candidates for 84 judgeable attempts). A crash that uploaded its trajectory
    but no patch stays in."""
    run_id = f"{_PREFIX}no-trajectory"
    _seed_run(run_id)
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO instance_results
                       (run_id, instance_id, attempt_number, phase, state, error_category,
                        patch_path, trajectory_path)
                   VALUES (%s, 'inst-never', 1, 'harness', 'NEVER_DISPATCHED', NULL, NULL, NULL),
                          (%s, 'inst-aborted', 1, 'harness', 'ABORTED_IN_FLIGHT', NULL, NULL, NULL),
                          (%s, 'inst-crashed', 1, 'harness', 'ABORTED_IN_FLIGHT', 'HARNESS_CRASH',
                           NULL, 't-crash')""",
                (run_id, run_id, run_id),
            )
        conn.commit()
        candidates = judge.fetch_candidates(conn, run_id)
    finally:
        conn.close()

    ids = {c.instance_id for c in candidates}
    assert "inst-never" not in ids
    assert "inst-aborted" not in ids
    assert "inst-crashed" in ids
    crashed = next(c for c in candidates if c.instance_id == "inst-crashed")
    assert crashed.outcome == "HARNESS_CRASH"
    assert crashed.patch_path is None
    assert len(candidates) == 5  # the four seeded judgeable attempts + the crash


def test_fetch_candidates_honors_the_instance_ids_selector() -> None:
    run_id = f"{_PREFIX}selector"
    _seed_run(run_id)
    conn = _db()
    try:
        candidates = judge.fetch_candidates(conn, run_id, instance_ids=["inst-resolved"])
    finally:
        conn.close()
    assert [c.instance_id for c in candidates] == ["inst-resolved"]


def _valid_dimensions(rubric) -> dict[str, object]:
    """A genuine (if trivial) judgment: every rubric dimension addressed at
    baseline with reasoning. This is what a REAL judge response looks like —
    the old fakes returned ``{"dimensions": {}}``, which ADR-0042 now treats as
    the empty-judgment failure it always was, not a success."""
    return {d.id: {"score": 0, "reasoning": "baseline, no issue"} for d in rubric.dimensions}


def _fake_judge_call(**kwargs) -> JudgeCallResult:
    rubric = kwargs["rubric"]
    parsed = {"dimensions": _valid_dimensions(rubric), "summary": "ok"}
    return JudgeCallResult(
        raw_response_text=json.dumps(parsed),
        reasoning_text="",
        parsed=parsed,
        parse_error=None,
        model_requested="judge-model",
        model_resolved="deepseek/deepseek-v4-flash-0731",
        provider="deepseek",
        generation_id="gen-1",
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.001,
    )


@pytest.fixture
def _wire_judge_pass(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(judge_keys, "claim_pass_lock", lambda conn, pass_id: None)
    monkeypatch.setattr(judge_keys, "release_pass_lock", lambda conn, pass_id: None)
    monkeypatch.setattr(
        judge_keys, "provision_pass_keys", lambda pass_id, budget: ("raw-key", "kid-1", "hash-1")
    )
    monkeypatch.setattr(judge_keys, "finalize_pass_keys", lambda *a, **k: None)
    monkeypatch.setattr(judge, "call_judge", lambda **kwargs: _fake_judge_call(**kwargs))
    monkeypatch.setattr(judge, "gateway_base_url", lambda: "http://gateway.local/v1")
    # 2026-09-08: the pass-level synthesis call — faked like call_judge; the digest it
    # is handed is the real one built from the rows the pass just wrote.
    monkeypatch.setattr(judge, "synthesize_pass", lambda **kwargs: _fake_synthesis(**kwargs))


_SYNTHESIS_DIGESTS: list[str] = []


def _fake_synthesis(**kwargs) -> PassSynthesisResult:
    _SYNTHESIS_DIGESTS.append(kwargs["digest_text"])
    return PassSynthesisResult(
        text="## Overview\nAll attempts judged clean.",
        model_requested=kwargs["model_alias"],
        model_resolved="deepseek/deepseek-v4-flash-0731",
        input_tokens=2000,
        output_tokens=300,
        cost_usd=0.003,
    )


def test_run_pass_writes_judge_results_and_dimension_scores(_wire_judge_pass) -> None:
    run_id = f"{_PREFIX}e2e"
    _seed_run(run_id)
    rubric = load_rubric()

    summary = judge.run_pass(
        _db,
        run_id,
        pass_id=f"{_PREFIX}pass-1",
        sample_rate=1.0,
        min_per_stratum=1,
        seed=1,
        prune_mode="full",
        max_spend_usd=100.0,
        rubric=rubric,
        fetch_artifact=lambda key: b"fake artifact bytes",
    )

    assert summary.total_judged == 4
    assert summary.total_eligible == 4

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM judge_results WHERE run_id = %s", (run_id,))
            assert cur.fetchone()[0] == 4
            cur.execute("SELECT COUNT(*) FROM judge_dimension_scores WHERE run_id = %s", (run_id,))
            assert cur.fetchone()[0] == 4 * len(rubric.dimensions)
            cur.execute(
                "SELECT total_eligible, total_judged, total_parse_failed, "
                "litellm_key_id, openrouter_key_hash "
                "FROM judge_sampling WHERE run_id = %s AND pass_id = %s",
                (run_id, f"{_PREFIX}pass-1"),
            )
            row = cur.fetchone()
            assert row == (4, 4, 0, "kid-1", "hash-1")
            # 2026-09-08: the synthesis rides on the same row, with its own cost
            cur.execute(
                "SELECT synthesis, synthesis_cost_usd, synthesis_model_resolved, "
                "synthesis_error, synthesis_prompt_version "
                "FROM judge_sampling WHERE run_id = %s AND pass_id = %s",
                (run_id, f"{_PREFIX}pass-1"),
            )
            synth = cur.fetchone()
            assert synth[0] == "## Overview\nAll attempts judged clean."
            assert float(synth[1]) == pytest.approx(0.003)
            assert synth[2] == "deepseek/deepseek-v4-flash-0731"
            assert synth[3] is None
            assert synth[4] == PASS_SYNTHESIS_PROMPT_VERSION
    finally:
        conn.close()
    # the digest handed to the synthesis call was built from the rows just written
    assert summary.synthesis is not None
    assert summary.spend_usd == pytest.approx(4 * 0.001 + 0.003)
    assert _SYNTHESIS_DIGESTS and "Attempts judged: 4." in _SYNTHESIS_DIGESTS[-1]


def test_run_pass_finishes_when_the_synthesis_call_fails(
    _wire_judge_pass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gloss must never cost the pass: judgments written, judge_sampling written,
    synthesis NULL with the error recorded, no exception."""
    run_id = f"{_PREFIX}synth-fail"
    _seed_run(run_id)

    def _boom(**kwargs):
        raise RuntimeError("gateway said no")

    monkeypatch.setattr(judge, "synthesize_pass", _boom)
    summary = judge.run_pass(
        _db,
        run_id,
        pass_id=f"{_PREFIX}pass-synth-fail",
        sample_rate=1.0,
        min_per_stratum=1,
        seed=1,
        prune_mode="full",
        max_spend_usd=100.0,
        rubric=load_rubric(),
        fetch_artifact=lambda key: b"fake artifact bytes",
    )
    assert summary.total_judged == 4
    assert summary.synthesis is None
    assert summary.synthesis_error == "RuntimeError: gateway said no"
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT total_judged, synthesis, synthesis_error FROM judge_sampling "
                "WHERE run_id = %s AND pass_id = %s",
                (run_id, f"{_PREFIX}pass-synth-fail"),
            )
            assert cur.fetchone() == (4, None, "RuntimeError: gateway said no")
    finally:
        conn.close()


def test_run_pass_stops_at_budget_ceiling(_wire_judge_pass) -> None:
    """§3.10: a ceiling checked BEFORE each call, not after — proven by
    setting it below the cost of covering every candidate."""
    run_id = f"{_PREFIX}budget"
    _seed_run(run_id)
    rubric = load_rubric()

    summary = judge.run_pass(
        _db,
        run_id,
        pass_id=f"{_PREFIX}pass-budget",
        sample_rate=1.0,
        min_per_stratum=1,
        seed=1,
        prune_mode="full",
        max_spend_usd=0.001,  # one call's worth (cost_usd=0.001 per fake call)
        rubric=rubric,
        fetch_artifact=lambda key: b"fake artifact bytes",
    )

    assert summary.total_judged == 1
    assert summary.total_skipped_over_budget == 3


def test_fetch_pass_summaries_makes_a_budget_truncated_pass_visible(_wire_judge_pass) -> None:
    """§3.2 of BUILDER3-JUDGE-VERIFIED-AND-UI-BRIEF-2026-09-02.md: before this,
    judge_sampling was written and never read — a pass truncated by the
    budget ceiling rendered identically to a complete one. total_eligible
    (4) vs total_judged (1) vs total_skipped_over_budget (3) is exactly the
    "judged N of M — K skipped at the $X ceiling" the UI banner needs."""
    run_id = f"{_PREFIX}pass-visibility"
    _seed_run(run_id)
    rubric = load_rubric()

    judge.run_pass(
        _db,
        run_id,
        pass_id=f"{_PREFIX}pass-truncated",
        sample_rate=1.0,
        min_per_stratum=1,
        seed=1,
        prune_mode="full",
        max_spend_usd=0.001,  # one call's worth -> the other 3 are skipped
        rubric=rubric,
        fetch_artifact=lambda key: b"fake artifact bytes",
    )

    conn = _db()
    try:
        passes = judge.fetch_pass_summaries(conn, run_id)
    finally:
        conn.close()

    assert len(passes) == 1
    p = passes[0]
    assert p["pass_id"] == f"{_PREFIX}pass-truncated"
    assert p["total_eligible"] == 4
    assert p["total_judged"] == 1
    assert p["total_skipped_over_budget"] == 3
    assert p["total_parse_failed"] == 0


def test_fetch_pass_summaries_orders_newest_pass_first(_wire_judge_pass) -> None:
    run_id = f"{_PREFIX}pass-order"
    _seed_run(run_id)
    rubric = load_rubric()

    for suffix in ("first", "second"):
        judge.run_pass(
            _db,
            run_id,
            pass_id=f"{_PREFIX}pass-order-{suffix}",
            instance_ids=["inst-resolved"],
            sample_rate=1.0,
            min_per_stratum=1,
            seed=1,
            prune_mode="full",
            max_spend_usd=100.0,
            rubric=rubric,
            fetch_artifact=lambda key: b"fake artifact bytes",
        )

    conn = _db()
    try:
        passes = judge.fetch_pass_summaries(conn, run_id)
    finally:
        conn.close()

    assert [p["pass_id"] for p in passes] == [
        f"{_PREFIX}pass-order-second",
        f"{_PREFIX}pass-order-first",
    ]


def test_run_pass_always_finalizes_keys_even_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """§10.2 point 5: finalisation on success, failure, OR budget-exhaustion."""
    run_id = f"{_PREFIX}error-path"
    _seed_run(run_id)
    rubric = load_rubric()

    calls: dict[str, object] = {}
    monkeypatch.setattr(judge_keys, "claim_pass_lock", lambda conn, pass_id: None)
    monkeypatch.setattr(
        judge_keys, "release_pass_lock", lambda conn, pass_id: calls.setdefault("released", True)
    )
    monkeypatch.setattr(
        judge_keys, "provision_pass_keys", lambda pass_id, budget: ("raw-key", "kid-1", "hash-1")
    )
    monkeypatch.setattr(
        judge_keys,
        "finalize_pass_keys",
        lambda pass_id, kid, h: calls.setdefault("finalized", (kid, h)),
    )

    def _boom(**kwargs):
        raise RuntimeError("gateway exploded")

    monkeypatch.setattr(judge, "call_judge", _boom)
    monkeypatch.setattr(judge, "gateway_base_url", lambda: "http://gateway.local/v1")

    with pytest.raises(RuntimeError, match="gateway exploded"):
        judge.run_pass(
            _db,
            run_id,
            pass_id=f"{_PREFIX}pass-err",
            sample_rate=1.0,
            min_per_stratum=1,
            seed=1,
            prune_mode="full",
            max_spend_usd=100.0,
            rubric=rubric,
            fetch_artifact=lambda key: b"fake artifact bytes",
        )

    assert calls["finalized"] == ("kid-1", "hash-1")
    assert calls["released"] is True


def test_run_pass_respects_the_instance_selector(_wire_judge_pass) -> None:
    run_id = f"{_PREFIX}selector-e2e"
    _seed_run(run_id)
    rubric = load_rubric()

    summary = judge.run_pass(
        _db,
        run_id,
        pass_id=f"{_PREFIX}pass-sel",
        instance_ids=["inst-resolved"],
        sample_rate=1.0,
        min_per_stratum=1,
        seed=1,
        prune_mode="full",
        max_spend_usd=100.0,
        rubric=rubric,
        fetch_artifact=lambda key: b"fake artifact bytes",
    )
    assert summary.total_eligible == 1
    assert summary.total_judged == 1


def test_second_concurrent_pass_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = f"{_PREFIX}concurrent"
    _seed_run(run_id)
    rubric = load_rubric()

    conn = _db()
    try:
        judge_keys.claim_pass_lock(conn, f"{_PREFIX}holder")
        with pytest.raises(judge_keys.JudgePassLockedError):
            judge.run_pass(
                _db,
                run_id,
                pass_id=f"{_PREFIX}pass-blocked",
                sample_rate=1.0,
                min_per_stratum=1,
                seed=1,
                prune_mode="full",
                max_spend_usd=100.0,
                rubric=rubric,
                fetch_artifact=lambda key: b"x",
            )
    finally:
        judge_keys.release_pass_lock(conn, f"{_PREFIX}holder")
        conn.close()


def test_fetch_latest_results_returns_all_dimensions_per_instance(_wire_judge_pass) -> None:
    run_id = f"{_PREFIX}results-read"
    _seed_run(run_id)
    rubric = load_rubric()

    judge.run_pass(
        _db,
        run_id,
        pass_id=f"{_PREFIX}pass-results",
        sample_rate=1.0,
        min_per_stratum=1,
        seed=1,
        prune_mode="full",
        max_spend_usd=100.0,
        rubric=rubric,
        fetch_artifact=lambda key: b"fake artifact bytes",
    )

    conn = _db()
    try:
        results = judge.fetch_latest_results(conn, run_id)
    finally:
        conn.close()

    assert len(results) == 4
    by_id = {r["instance_id"]: r for r in results}
    assert len(by_id["inst-resolved"]["dimensions"]) == len(rubric.dimensions)
    assert by_id["inst-resolved"]["judge_model_resolved"] == "deepseek/deepseek-v4-flash-0731"
    assert by_id["inst-resolved"]["summary"] == "ok"


def test_fetch_latest_results_shows_only_the_newest_row_after_a_rejudge(
    _wire_judge_pass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.1 of BUILDER3-JUDGE-VERIFIED-AND-UI-BRIEF-2026-09-02.md: the
    original version of this test asserted only row COUNTS (len==1, count==2),
    which stays green even under `judged_at DESC -> ASC` — i.e. it never
    checked that the "latest" row shown is actually the newest one. Make the
    two passes distinguishable and assert on the distinguishing field."""
    run_id = f"{_PREFIX}rejudge"
    _seed_run(run_id)
    rubric = load_rubric()

    judge.run_pass(
        _db,
        run_id,
        pass_id=f"{_PREFIX}pass-first",
        instance_ids=["inst-resolved"],
        sample_rate=1.0,
        min_per_stratum=1,
        seed=1,
        prune_mode="full",
        max_spend_usd=100.0,
        rubric=rubric,
        fetch_artifact=lambda key: b"fake artifact bytes",
    )

    def _second_pass_call(**kwargs) -> JudgeCallResult:
        parsed = {"dimensions": _valid_dimensions(kwargs["rubric"]), "summary": "NEW-JUDGMENT"}
        return JudgeCallResult(
            raw_response_text=json.dumps(parsed),
            reasoning_text="",
            parsed=parsed,
            parse_error=None,
            model_requested="judge-model",
            model_resolved="deepseek/deepseek-v4-flash-0731",
            provider="deepseek",
            generation_id="gen-2",
            input_tokens=100,
            output_tokens=20,
            cost_usd=0.001,
        )

    monkeypatch.setattr(judge, "call_judge", _second_pass_call)
    judge.run_pass(
        _db,
        run_id,
        pass_id=f"{_PREFIX}pass-second",
        instance_ids=["inst-resolved"],
        sample_rate=1.0,
        min_per_stratum=1,
        seed=1,
        prune_mode="full",
        max_spend_usd=100.0,
        rubric=rubric,
        fetch_artifact=lambda key: b"fake artifact bytes",
        # 1321b43 made a relaunch RESUME (already-judged candidates skipped) — this
        # test is about a deliberate re-judge, which is now opt-in.
        rejudge=True,
    )

    conn = _db()
    try:
        results = judge.fetch_latest_results(conn, run_id)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM judge_results WHERE run_id = %s AND instance_id = %s",
                (run_id, "inst-resolved"),
            )
            row_count = cur.fetchone()[0]
    finally:
        conn.close()

    assert len(results) == 1  # ONE latest row shown, not two
    assert row_count == 2  # but BOTH are kept — re-judging is a new row, never an overwrite
    assert results[0]["summary"] == "NEW-JUDGMENT"  # and it's the newest one, not the first


def _hollow_call(reasoning: str):
    """The ADR-0042 failure shape: valid JSON, no ``dimensions`` block — the
    full judgment (if any) rode in ``reasoning_content`` instead of content."""

    def _call(**kwargs) -> JudgeCallResult:
        return JudgeCallResult(
            raw_response_text='{": ": ", "}',
            reasoning_text=reasoning,
            parsed={": ": ", "},
            parse_error=None,
            model_requested="judge-model",
            model_resolved="deepseek/deepseek-v4-flash-0731",
            provider="deepseek",
            generation_id="gen-hollow",
            input_tokens=100,
            output_tokens=9000,
            cost_usd=0.002,
        )

    return _call


def test_cascade_recovers_a_hollow_response_via_transcribe(
    _wire_judge_pass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0042: primary + retry come back hollow (judgment stuck in
    reasoning_content); step C transcribes that reasoning into the schema and
    the candidate is judged — NOT silently recorded as an all-null score."""
    run_id = f"{_PREFIX}cascade-transcribe"
    _seed_run(run_id)
    rubric = load_rubric()

    monkeypatch.setattr(judge, "call_judge", _hollow_call("the agent fixed the bug correctly"))

    def _transcribe_ok(**kwargs) -> JudgeCallResult:
        parsed = {"dimensions": _valid_dimensions(kwargs["rubric"]), "summary": "recovered"}
        return JudgeCallResult(
            raw_response_text=json.dumps(parsed),
            reasoning_text="",
            parsed=parsed,
            parse_error=None,
            model_requested="judge-model",
            model_resolved="deepseek/deepseek-v4-flash-0731",
            provider="deepseek",
            generation_id="gen-transcribe",
            input_tokens=200,
            output_tokens=400,
            cost_usd=0.001,
        )

    monkeypatch.setattr(judge, "transcribe_reasoning", _transcribe_ok)

    stored: dict[str, str] = {}

    def _store(key: str, data: str) -> str:
        stored[key] = data
        return key

    summary = judge.run_pass(
        _db,
        run_id,
        pass_id=f"{_PREFIX}pass-transcribe",
        instance_ids=["inst-resolved"],
        sample_rate=1.0,
        min_per_stratum=1,
        seed=1,
        prune_mode="full",
        max_spend_usd=100.0,
        rubric=rubric,
        fetch_artifact=lambda key: b"fake artifact bytes",
        store_artifact=_store,
    )

    assert summary.total_judged == 1
    assert summary.total_parse_failed == 0

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT judge_method, judge_attempts, judge_parse_failed, summary, "
                "raw_response_s3_key FROM judge_results WHERE run_id = %s",
                (run_id,),
            )
            method, attempts, parse_failed, row_summary, s3_key = cur.fetchone()
    finally:
        conn.close()

    assert method == "transcribe"
    assert attempts == 3  # primary + retry + transcribe
    assert parse_failed is False
    assert row_summary == "recovered"
    assert s3_key and s3_key in stored  # §3.6: raw responses stored, cascade auditable


def test_cascade_all_empty_is_recorded_as_a_parse_failure(
    _wire_judge_pass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0042 step D: when every attempt is empty and no reasoning exists to
    transcribe, the candidate is a real parse failure — never a silent
    zero-score judgment counted as judged-successfully."""
    run_id = f"{_PREFIX}cascade-fail"
    _seed_run(run_id)
    rubric = load_rubric()

    # No reasoning anywhere -> transcribe is skipped; plain (force_json=False)
    # also comes back hollow -> D.
    monkeypatch.setattr(judge, "call_judge", _hollow_call(""))

    summary = judge.run_pass(
        _db,
        run_id,
        pass_id=f"{_PREFIX}pass-fail",
        instance_ids=["inst-resolved"],
        sample_rate=1.0,
        min_per_stratum=1,
        seed=1,
        prune_mode="full",
        max_spend_usd=100.0,
        rubric=rubric,
        fetch_artifact=lambda key: b"fake artifact bytes",
    )

    assert summary.total_judged == 1  # a row is still written
    assert summary.total_parse_failed == 1  # but it is a failure, not a silent zero

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT judge_method, judge_attempts, judge_parse_failed, summary "
                "FROM judge_results WHERE run_id = %s",
                (run_id,),
            )
            method, attempts, parse_failed, row_summary = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM judge_dimension_scores WHERE run_id = %s", (run_id,))
            dim_rows = cur.fetchone()[0]
    finally:
        conn.close()

    assert method == "failed"
    assert attempts == 3  # primary + retry + plain (transcribe skipped: no reasoning)
    assert parse_failed is True
    assert row_summary is None
    assert dim_rows == 0  # a failure writes NO dimension scores


def test_run_pass_synthesis_only_regenerates_the_report_and_judges_nothing(
    _wire_judge_pass,
) -> None:
    """2026-09-08 (owner): "regenerate report" — a second pass that judges nothing, only
    re-runs the synthesis over the rows the first pass wrote; its judge_sampling row is
    marked synthesis_only, the judge_results count is unchanged."""
    run_id = f"{_PREFIX}synth-only"
    _seed_run(run_id)
    rubric = load_rubric()
    common: dict[str, Any] = {  # **-unpacked into run_pass's typed keywords
        "sample_rate": 1.0,
        "min_per_stratum": 1,
        "seed": 1,
        "prune_mode": "full",
        "max_spend_usd": 100.0,
        "rubric": rubric,
        "fetch_artifact": lambda key: b"fake artifact bytes",
    }
    judge.run_pass(_db, run_id, pass_id=f"{_PREFIX}pass-judge", **common)
    summary = judge.run_pass(
        _db, run_id, pass_id=f"{_PREFIX}pass-report", synthesis_only=True, **common
    )
    assert summary.total_eligible == 0 and summary.total_judged == 0
    assert summary.synthesis_only is True
    assert summary.synthesis == "## Overview\nAll attempts judged clean."
    assert summary.spend_usd == pytest.approx(0.003)
    assert "Attempts judged: 4." in _SYNTHESIS_DIGESTS[-1]
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM judge_results WHERE run_id = %s", (run_id,))
            assert cur.fetchone()[0] == 4
            cur.execute(
                "SELECT pass_id, synthesis_only, total_judged, synthesis IS NOT NULL "
                "FROM judge_sampling WHERE run_id = %s ORDER BY created_at",
                (run_id,),
            )
            assert cur.fetchall() == [
                (f"{_PREFIX}pass-judge", False, 4, True),
                (f"{_PREFIX}pass-report", True, 0, True),
            ]
        passes = judge.fetch_pass_summaries(conn, run_id)
    finally:
        conn.close()
    assert [p["synthesis_only"] for p in passes] == [True, False]  # newest first
