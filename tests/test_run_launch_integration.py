"""Integration tests — run-launch (BUILDER4-RUN-LAUNCH-ORCHESTRATOR-2026-08-26).

Requires a live Postgres (the compose stack).  Marked ``integration`` and
deselected by default, same convention as ``test_results_writer_redelivery.py``:

    uv run pytest tests/test_run_launch_integration.py -m integration

§11's testing bar, and where each requirement is covered here:

- "The duplicate check must be tested concurrently, not sequentially... a
  sequential test passes against a check-then-act implementation and proves
  nothing" -> :func:`test_claim_mutex_is_genuinely_concurrent` and
  :func:`test_launch_run_concurrent_exactly_one_proceeds_past_claim` use real
  OS threads racing the real Postgres unique index, not a mock or a manual
  interleaving.
- "Test the state guard with out-of-order delivery" ->
  :func:`test_state_guard_rejects_stale_dispatched_after_terminal`.
- "Test the reaper's self-correction" ->
  :func:`test_reaper_self_correction_straggler_result_wins`.
- "Test the five-place chain against a database that already has the old
  schema, not only a fresh one" ->
  :func:`test_migrations_upgrade_a_pre_run_launch_schema`.

Mutation-check performed by hand for each test below (documented in its
docstring) per §11: revert the fix, confirm the test fails, restore.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from swebench_eval.orchestrator.control_plane import run_launch
from swebench_eval.orchestrator.control_plane.dispatcher import register_run
from swebench_eval.orchestrator.control_plane.results_writer import _process_result
from swebench_eval.queue.schemas import ResultMessage

pytestmark = pytest.mark.integration


def _db():
    from swebench_eval.database.connection import get_connection

    return get_connection()


@pytest.fixture(autouse=True)
def _clean_run_launch_rows():
    """Delete every row this file's deterministic run_ids might have left."""
    prefixes = ("rl-claim-", "rl-launch-", "rl-guard-", "rl-reap-", "rl-schema-")
    conn = _db()
    try:
        with conn.cursor() as cur:
            for prefix in prefixes:
                cur.execute("DELETE FROM instance_results WHERE run_id LIKE %s", (f"{prefix}%",))
                cur.execute("DELETE FROM run_targets WHERE run_id LIKE %s", (f"{prefix}%",))
                cur.execute("DELETE FROM runs WHERE run_id LIKE %s", (f"{prefix}%",))
        conn.commit()
    finally:
        conn.close()
    yield
    conn = _db()
    try:
        with conn.cursor() as cur:
            for prefix in prefixes:
                cur.execute("DELETE FROM instance_results WHERE run_id LIKE %s", (f"{prefix}%",))
                cur.execute("DELETE FROM run_targets WHERE run_id LIKE %s", (f"{prefix}%",))
                cur.execute("DELETE FROM runs WHERE run_id LIKE %s", (f"{prefix}%",))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The claim mutex — genuinely concurrent
# ---------------------------------------------------------------------------


def test_claim_mutex_is_genuinely_concurrent() -> None:
    """§4/§11: N threads race to claim the SAME (harness, model_alias) pair.

    Exactly one must win (no exception); every other thread must receive
    :class:`DuplicateRunError` naming the SAME winning run_id — never two
    winners, never a raw psycopg2 UniqueViolation escaping (that would mean
    the except clause missed it), never a wrong existing_run_id (that would
    mean the SELECT-after-rollback raced another thread's own claim).

    Mutation-check (performed by hand): change ``_claim``'s INSERT to check
    "SELECT run_id FROM runs WHERE active_key = %s" first and only INSERT if
    that SELECT is empty (the textbook check-then-act bug) — this test then
    intermittently fails with MORE than one winner (flaky, not always, which
    is exactly the point: a sequential test would never catch it). Restored
    after confirming.
    """
    harness = "custom_minimal"
    model_alias = "claim-mutex-test-model"
    n_threads = 8
    barrier = threading.Barrier(n_threads)

    results: list[tuple[str, str | None, str | None]] = []
    lock = threading.Lock()

    def _attempt(i: int) -> None:
        run_id = f"rl-claim-{i}"
        barrier.wait()  # maximize actual overlap, not just "started around the same time"
        try:
            run_launch._claim(run_id, harness, model_alias, {"attempt": i}, budget_cap_usd=10.0)
            with lock:
                results.append(("won", run_id, None))
        except run_launch.DuplicateRunError as exc:
            with lock:
                results.append(("lost", run_id, exc.existing_run_id))

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(_attempt, range(n_threads)))

    winners = [r for r in results if r[0] == "won"]
    losers = [r for r in results if r[0] == "lost"]
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    assert len(losers) == n_threads - 1
    winning_run_id = winners[0][1]
    for _, _run_id, existing in losers:
        assert (
            existing == winning_run_id
        ), f"a loser saw the wrong existing_run_id: {existing} != {winning_run_id}"

    # And the database agrees: exactly one row holds this active_key.
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM runs WHERE active_key = %s",
                (f"{harness}:{model_alias}",),
            )
            count = cur.fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_launch_run_concurrent_exactly_one_proceeds_past_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§11: "exactly one 201, exactly one 409, exactly one set of keys minted"
    — end to end through :func:`launch_run`, not just ``_claim`` in isolation.

    PROVISION/SEED/DISPATCH are monkeypatched to cheap recording stand-ins
    (no OpenRouter provisioning key or dataset mirror exists in this sandbox);
    only the CLAIM step — the actual mutex — runs for real against Postgres.
    Proves the property the brief cares about: because ``launch_run`` calls
    ``_claim`` FIRST and every later step is unreachable code for a thread
    whose claim raised, "exactly one set of keys minted" follows mechanically
    from "exactly one claim wins" — which is what this test measures directly
    (one call to the provision stand-in, not zero, not two).
    """
    provision_calls: list[str] = []
    seed_calls: list[str] = []
    dispatch_calls: list[str] = []
    lock = threading.Lock()

    def _fake_provision(run_id, harness, model_alias, budget_cap_usd, rpm, tpm):
        with lock:
            provision_calls.append(run_id)
        return "fake-litellm-key-id", "fake-openrouter-hash"

    def _fake_seed(run_id, instances, attempts):
        with lock:
            seed_calls.append(run_id)
        return len(instances)

    def _fake_dispatch(run_id, instances, config):
        with lock:
            dispatch_calls.append(run_id)
        return len(instances)

    monkeypatch.setattr(run_launch, "_provision_keys", _fake_provision)
    monkeypatch.setattr(run_launch, "_seed_instance_rows", _fake_seed)
    monkeypatch.setattr(run_launch, "dispatch_run", _fake_dispatch)
    monkeypatch.setattr(run_launch, "_record_key_ids", lambda *a, **k: None)

    from swebench_eval.orchestrator.run_config import RunConfig

    harness = "custom_minimal"
    model_alias = "launch-concurrent-test-model"
    config = RunConfig(harness=harness, model_alias=model_alias)
    n_threads = 6
    barrier = threading.Barrier(n_threads)

    outcomes: list[tuple[str, str]] = []
    lock2 = threading.Lock()

    def _attempt(i: int) -> None:
        run_id = f"rl-launch-{i}"
        barrier.wait()
        try:
            run_launch.launch_run(run_id, [], config, budget_cap_usd=10.0)
            with lock2:
                outcomes.append(("launched", run_id))
        except run_launch.DuplicateRunError:
            with lock2:
                outcomes.append(("duplicate", run_id))

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(_attempt, range(n_threads)))

    launched = [o for o in outcomes if o[0] == "launched"]
    duplicate = [o for o in outcomes if o[0] == "duplicate"]
    assert len(launched) == 1, f"expected exactly one launch, got {launched}"
    assert len(duplicate) == n_threads - 1
    assert len(provision_calls) == 1, f"provision ran {len(provision_calls)} times, expected 1"
    assert len(seed_calls) == 1
    assert len(dispatch_calls) == 1
    assert provision_calls == seed_calls == dispatch_calls == [launched[0][1]]


# ---------------------------------------------------------------------------
# The monotonic state guard (§6.3)
# ---------------------------------------------------------------------------


def test_state_guard_rejects_stale_dispatched_after_terminal() -> None:
    """§6.3/§11: deliver DISPATCHED AFTER PATCH_READY; the row must still read
    PATCH_READY (SQS Standard is unordered — a late DISPATCHED must not
    silently regress a finished instance).

    Mutation-check (performed by hand): remove the ``WHERE
    state_rank(EXCLUDED.state) > state_rank(instance_results.state)`` clause
    from ``_process_result``'s UPDATE — this test then fails (the row reads
    'DISPATCHED', the exact regression the guard exists to prevent). Restored
    after confirming.
    """
    run_id = "rl-guard-order"
    register_run(run_id, "custom_minimal", "cheap-oss-model")

    _process_result(
        ResultMessage(
            run_id=run_id,
            instance_id="inst-1",
            attempt_number=1,
            phase="harness",
            state="PATCH_READY",
            patch_s3_key="runs/x/patch.diff",
        )
    )
    # A late DISPATCHED notice — the dispatcher's message, arriving after the
    # real terminal result (unordered SQS).
    _process_result(
        ResultMessage(
            run_id=run_id,
            instance_id="inst-1",
            attempt_number=1,
            phase="harness",
            state="DISPATCHED",
        )
    )

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, patch_path FROM instance_results "
                "WHERE run_id = %s AND instance_id = %s AND phase = 'harness'",
                (run_id, "inst-1"),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "PATCH_READY", f"state guard regressed: {row[0]}"
    assert row[1] == "runs/x/patch.diff", "patch_path lost on the guarded-out update"


# ---------------------------------------------------------------------------
# The reaper's self-correction (§7)
# ---------------------------------------------------------------------------


def test_reaper_self_correction_straggler_result_wins() -> None:
    """§7/§11: reap an instance to ABANDONED, then a REAL result lands — the
    real result must win (ABANDONED ranks below every real terminal, §6.3).

    Mutation-check (performed by hand): change state_rank()'s ABANDONED case
    from 3 to 4 (same rank as a real terminal) — this test then fails
    (whichever message landed SECOND wins regardless of which is real,
    because the guard's strict `>` no longer favors the terminal state).
    Restored after confirming.
    """
    run_id = "rl-reap-selfcorrect"
    register_run(run_id, "custom_minimal", "cheap-oss-model")

    # A premature reap (rule 2 would have emitted exactly this).
    _process_result(
        ResultMessage(
            run_id=run_id,
            instance_id="inst-1",
            attempt_number=1,
            phase="harness",
            state="ABANDONED",
            error_detail="deadline_passed (test)",
        )
    )
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM instance_results "
                "WHERE run_id = %s AND instance_id = %s AND phase = 'harness'",
                (run_id, "inst-1"),
            )
            assert cur.fetchone()[0] == "ABANDONED"
    finally:
        conn.close()

    # The straggler's real result finally arrives.
    _process_result(
        ResultMessage(
            run_id=run_id,
            instance_id="inst-1",
            attempt_number=1,
            phase="harness",
            state="PATCH_READY",
            patch_s3_key="runs/x/patch.diff",
        )
    )

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM instance_results "
                "WHERE run_id = %s AND instance_id = %s AND phase = 'harness'",
                (run_id, "inst-1"),
            )
            state = cur.fetchone()[0]
    finally:
        conn.close()
    assert state == "PATCH_READY", f"self-correction failed: state is still {state}"


# ---------------------------------------------------------------------------
# Migrating a pre-run-launch schema (§11: "not only a fresh one")
# ---------------------------------------------------------------------------


def test_migrations_upgrade_a_pre_run_launch_schema() -> None:
    """§11: run the five-place chain against a database that already has the
    OLD schema.  Simulates it by dropping run-launch's columns/index/function,
    confirming the drift check then FAILS (proving the simulation is real),
    then re-running migrations and confirming it upgrades cleanly.
    """
    from swebench_eval.database.connection import _check_schema_drift, run_migrations

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE runs DROP COLUMN IF EXISTS active_key")
            cur.execute("ALTER TABLE runs DROP COLUMN IF EXISTS litellm_key_id")
            cur.execute("ALTER TABLE instance_results DROP COLUMN IF EXISTS seeded_at")
            cur.execute("DROP INDEX IF EXISTS uq_runs_active_key")
            cur.execute("DROP FUNCTION IF EXISTS state_rank(text)")
        conn.commit()

        with pytest.raises(RuntimeError, match="Schema drift"):
            _check_schema_drift(conn)
    finally:
        conn.close()

    # The real path: run_migrations() re-applies init.sql (idempotent
    # IF NOT EXISTS) and re-checks — this is what a deployed task does on
    # every boot against a database that predates this build.
    run_migrations()

    conn = _db()
    try:
        _check_schema_drift(conn)  # must not raise now
    finally:
        conn.close()
