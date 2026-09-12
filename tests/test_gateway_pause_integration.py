"""Gateway pause/resume against a real Postgres —
BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §2/§9.

§2's precedence table, exhaustively (§9.3): each of the 8 rows as its own
case, real Aurora-shaped rows (``gateway_key_blocked_by``), the row-lock
discipline mirroring ``test_restart_close_integration.py``.

``gateway_admin.block_key``/``unblock_key`` are monkeypatched to recorders —
the block_key/unblock_key <-> LiteLLM contract itself is pinned by
``tests/test_gateway_admin.py`` (mocked) and was live-verified end-to-end
against a real ``litellm:main-stable`` container 2026-08-31 (mint -> 200 ->
block -> 401 marker matched -> unblock -> 200 again, see the design doc §1);
these tests are exercising the DB-level precedence bookkeeping, same
rationale as the restart/close integration suite stubbing finalise_run.
"""

from __future__ import annotations

from typing import Any

import pytest

from swebench_eval.gateway import admin as gateway_admin
from swebench_eval.orchestrator.control_plane import gateway_pause

pytestmark = pytest.mark.integration

_PREFIX = "gwp-test-"


def _db():
    from swebench_eval.database.connection import get_connection

    return get_connection()


@pytest.fixture(autouse=True)
def _clean_rows():
    def _clean():
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM runs WHERE run_id LIKE %s", (f"{_PREFIX}%",))
            conn.commit()
        finally:
            conn.close()

    _clean()
    yield
    _clean()


@pytest.fixture(autouse=True)
def _stub_admin(monkeypatch: pytest.MonkeyPatch):
    """Recorders instead of real LiteLLM calls — see module docstring."""
    calls: dict[str, list[Any]] = {"block": [], "unblock": []}

    def _fake_block(base: str, master: str, key_id: str) -> None:
        calls["block"].append(key_id)

    def _fake_unblock(base: str, master: str, key_id: str) -> None:
        calls["unblock"].append(key_id)

    monkeypatch.setattr(gateway_admin, "block_key", _fake_block)
    monkeypatch.setattr(gateway_admin, "unblock_key", _fake_unblock)
    return calls


def _insert_run(
    conn,
    run_id: str,
    *,
    status: str = "running",
    blocked_by: str | None = None,
    has_key: bool = True,
) -> None:
    # F2 (implementation review, 2026-08-31): has_key=False models a row
    # caught between CLAIM and _record_key_ids — a real, reachable state
    # once the sweep's population was widened past status='running' (F1).
    key_id = f"tok-{run_id}" if has_key else None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (run_id, config_snapshot, status, litellm_key_id, "
            "gateway_key_blocked_by) "
            'VALUES (%s, \'{"harness": "custom_minimal", "model_alias": "cheap-oss-model"}\'::jsonb, '
            "%s, %s, %s)",
            (run_id, status, key_id, blocked_by),
        )
    conn.commit()


def _blocked_by(conn, run_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT gateway_key_blocked_by FROM runs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# §2's 8-row precedence table, exhaustively
# ---------------------------------------------------------------------------


def test_row1_null_plus_global_pause_becomes_global(_stub_admin) -> None:
    run_id = f"{_PREFIX}row1"
    conn = _db()
    _insert_run(conn, run_id, blocked_by=None)

    gateway_pause.sweep_global_pause()

    assert _blocked_by(conn, run_id) == "global"
    assert _stub_admin["block"] == [f"tok-{run_id}"]
    conn.close()


@pytest.mark.parametrize("status", ["provisioning", "seeding", "dispatching"])
def test_f1_sweep_catches_a_mid_launch_run_not_just_running(status: str, _stub_admin) -> None:
    """F1 (implementation review, 2026-08-31): status='running' alone missed
    the LONGEST phase of a launch (SEED+DISPATCH is hundreds of sequential
    SQS sends for a large run) — proved with a row-scoped test showing rows
    stuck at 'seeding'/'dispatching' never got blocked. The widened
    population must catch all three pre-running in-flight statuses."""
    run_id = f"{_PREFIX}f1-{status}"
    conn = _db()
    _insert_run(conn, run_id, status=status, blocked_by=None, has_key=True)

    gateway_pause.sweep_global_pause()

    assert _blocked_by(conn, run_id) == "global"
    assert _stub_admin["block"] == [f"tok-{run_id}"]
    conn.close()


def test_f1_sweep_does_not_touch_a_claimed_run(_stub_admin) -> None:
    """The widened population is explicitly provisioning/seeding/dispatching/
    running — NOT 'claimed'. A freshly-claimed row never has a key yet
    (litellm_key_id is only ever set once, well after CLAIM), so there would
    be nothing to block regardless; block_if_globally_paused's post-mint
    re-check is what covers it once a key exists."""
    run_id = f"{_PREFIX}f1-claimed"
    conn = _db()
    _insert_run(conn, run_id, status="claimed", blocked_by=None, has_key=False)

    gateway_pause.sweep_global_pause()

    assert _blocked_by(conn, run_id) is None
    assert _stub_admin["block"] == []
    conn.close()


def test_f2_sweep_leaves_a_no_key_row_null_not_falsely_marked(_stub_admin) -> None:
    """F2 (implementation review, 2026-08-31) — the important one: F1's
    widened population can catch a row between CLAIM and _record_key_ids,
    where litellm_key_id is still NULL. The naive fix (block conditionally,
    mark unconditionally) stamped 'global' on it anyway — an unblocked key
    that READS as blocked, which then made block_if_globally_paused's own
    `IS NULL` guard skip it later. Strictly worse than the original bug
    (unknown must never render as healthy). The marker must stay NULL until
    a key is actually blocked."""
    run_id = f"{_PREFIX}f2-no-key"
    conn = _db()
    _insert_run(conn, run_id, status="provisioning", blocked_by=None, has_key=False)

    gateway_pause.sweep_global_pause()

    assert _blocked_by(conn, run_id) is None, (
        "a no-key row must stay NULL, not be falsely marked 'global' — "
        "block_if_globally_paused still needs to catch it once the key exists"
    )
    assert _stub_admin["block"] == []
    conn.close()


def test_f2_sweep_return_count_reflects_actually_blocked_not_rows_touched(
    _stub_admin,
) -> None:
    """sweep_global_pause's return value is documented as the count BLOCKED —
    a no-key row must not inflate it. Row-scoped only (the reviewer's own
    first-pass mistake was asserting on the table-wide count and picking up
    unrelated leftover rows from other suites sharing this Postgres —
    ``sweep_global_pause`` sees the WHOLE ``runs`` table, not just this
    file's ``_PREFIX`` rows, so the return value itself is never a safe
    thing to assert an exact value on here)."""
    has_key_id = f"{_PREFIX}f2-count-haskey"
    no_key_id = f"{_PREFIX}f2-count-nokey"
    conn = _db()
    _insert_run(conn, has_key_id, status="running", blocked_by=None, has_key=True)
    _insert_run(conn, no_key_id, status="provisioning", blocked_by=None, has_key=False)
    conn.close()

    gateway_pause.sweep_global_pause()

    conn = _db()
    assert _blocked_by(conn, has_key_id) == "global"
    assert _blocked_by(conn, no_key_id) is None
    conn.close()
    assert f"tok-{has_key_id}" in _stub_admin["block"]
    assert f"tok-{no_key_id}" not in _stub_admin["block"]


def test_row2_null_plus_per_run_pause_becomes_operator(_stub_admin) -> None:
    run_id = f"{_PREFIX}row2"
    conn = _db()
    _insert_run(conn, run_id, blocked_by=None)

    gateway_pause.pause_gateway(run_id)

    assert _blocked_by(conn, run_id) == "operator"
    assert _stub_admin["block"] == [f"tok-{run_id}"]
    conn.close()


def test_row3_global_plus_global_resume_becomes_null(_stub_admin) -> None:
    run_id = f"{_PREFIX}row3"
    conn = _db()
    _insert_run(conn, run_id, blocked_by="global")

    gateway_pause.sweep_global_resume()

    assert _blocked_by(conn, run_id) is None
    assert _stub_admin["unblock"] == [f"tok-{run_id}"]
    conn.close()


def test_row4_global_plus_per_run_pause_upgrades_to_operator(_stub_admin) -> None:
    """Survives the NEXT global resume — the upgrade is the point."""
    run_id = f"{_PREFIX}row4"
    conn = _db()
    _insert_run(conn, run_id, blocked_by="global")

    gateway_pause.pause_gateway(run_id)
    assert _blocked_by(conn, run_id) == "operator"

    # Prove the survival half of "upgraded": a global resume must not touch it.
    gateway_pause.sweep_global_resume()
    assert _blocked_by(conn, run_id) == "operator"
    conn.close()


def test_row5_operator_plus_global_pause_is_untouched(_stub_admin) -> None:
    run_id = f"{_PREFIX}row5"
    conn = _db()
    _insert_run(conn, run_id, blocked_by="operator")

    gateway_pause.sweep_global_pause()

    assert _blocked_by(conn, run_id) == "operator"
    # The sweep only targets gateway_key_blocked_by IS NULL rows — an
    # already-operator-blocked run's key is never re-blocked (it's already
    # blocked; block_key must not even be called for it).
    assert _stub_admin["block"] == []
    conn.close()


def test_row6_operator_plus_global_resume_is_not_touched(_stub_admin) -> None:
    """This IS the actual fix (§8's original motivating scenario, folded into
    §2): a global resume must never silently un-pause a run an operator
    deliberately, individually paused."""
    run_id = f"{_PREFIX}row6"
    conn = _db()
    _insert_run(conn, run_id, blocked_by="operator")

    gateway_pause.sweep_global_resume()

    assert _blocked_by(conn, run_id) == "operator"
    assert _stub_admin["unblock"] == []
    conn.close()


def test_row7_operator_plus_per_run_resume_carves_out_to_null(_stub_admin) -> None:
    run_id = f"{_PREFIX}row7"
    conn = _db()
    _insert_run(conn, run_id, blocked_by="operator")

    gateway_pause.resume_gateway(run_id)

    assert _blocked_by(conn, run_id) is None
    assert _stub_admin["unblock"] == [f"tok-{run_id}"]
    conn.close()


def test_row8_global_plus_per_run_resume_carves_out_to_null(_stub_admin) -> None:
    """This run resumes even while every other globally-paused run stays
    blocked — the carve-out §2 promises."""
    run_id = f"{_PREFIX}row8"
    other_id = f"{_PREFIX}row8-other"
    conn = _db()
    _insert_run(conn, run_id, blocked_by="global")
    _insert_run(conn, other_id, blocked_by="global")

    gateway_pause.resume_gateway(run_id)

    assert _blocked_by(conn, run_id) is None
    assert _blocked_by(conn, other_id) == "global", "the OTHER run must stay blocked"
    conn.close()


# ---------------------------------------------------------------------------
# Per-run pause/resume: closed-run refusal (same gate as restart)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("closed_status", ["completed", "aborted", "aborting", "finalising"])
def test_pause_gateway_refuses_on_a_closed_run(closed_status: str, _stub_admin) -> None:
    run_id = f"{_PREFIX}closed-{closed_status}"
    conn = _db()
    _insert_run(conn, run_id, status=closed_status, blocked_by=None)
    conn.close()

    with pytest.raises(gateway_pause.GatewayPauseError, match="closed"):
        gateway_pause.pause_gateway(run_id)
    assert _stub_admin["block"] == []


@pytest.mark.parametrize("closed_status", ["completed", "aborted", "aborting", "finalising"])
def test_resume_gateway_refuses_on_a_closed_run(closed_status: str, _stub_admin) -> None:
    run_id = f"{_PREFIX}closed-r-{closed_status}"
    conn = _db()
    _insert_run(conn, run_id, status=closed_status, blocked_by="operator")
    conn.close()

    with pytest.raises(gateway_pause.GatewayPauseError, match="closed"):
        gateway_pause.resume_gateway(run_id)
    assert _stub_admin["unblock"] == []


def test_pause_gateway_no_such_run_raises() -> None:
    with pytest.raises(gateway_pause.GatewayPauseError, match="no such run"):
        gateway_pause.pause_gateway(f"{_PREFIX}does-not-exist")


# ---------------------------------------------------------------------------
# block_if_globally_paused — the launch-vs-global-pause race, found writing
# up the review request (not in the original design doc). A run past the
# launch-refusal check but not yet 'running' falls through both existing
# mechanisms; this is the fix, called right after the key is minted.
# ---------------------------------------------------------------------------


def test_block_if_globally_paused_blocks_a_mid_provision_run(
    _stub_admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swebench_eval.control import state as control_state

    run_id = f"{_PREFIX}race-blocks"
    conn = _db()
    # Mid-PROVISION: status is NOT 'running' yet — the sweep's WHERE clause
    # would never see this row, which is exactly the gap.
    _insert_run(conn, run_id, status="provisioning", blocked_by=None)
    conn.close()

    monkeypatch.setattr(control_state, "is_paused", lambda pool: pool == "gateway")

    blocked = gateway_pause.block_if_globally_paused(run_id, f"tok-{run_id}")

    assert blocked is True
    assert _stub_admin["block"] == [f"tok-{run_id}"]
    conn = _db()
    assert _blocked_by(conn, run_id) == "global"
    conn.close()


def test_block_if_globally_paused_is_a_noop_when_not_paused(
    _stub_admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swebench_eval.control import state as control_state

    run_id = f"{_PREFIX}race-not-paused"
    conn = _db()
    _insert_run(conn, run_id, status="provisioning", blocked_by=None)
    conn.close()

    monkeypatch.setattr(control_state, "is_paused", lambda pool: False)

    blocked = gateway_pause.block_if_globally_paused(run_id, f"tok-{run_id}")

    assert blocked is False
    assert _stub_admin["block"] == []
    conn = _db()
    assert _blocked_by(conn, run_id) is None
    conn.close()


def test_block_if_globally_paused_is_a_noop_with_no_key_yet(
    _stub_admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to block before the key is minted — must not raise or touch
    the DB, so a caller can call it unconditionally with whatever it has."""
    from swebench_eval.control import state as control_state

    monkeypatch.setattr(control_state, "is_paused", lambda pool: True)

    blocked = gateway_pause.block_if_globally_paused("whatever", None)

    assert blocked is False
    assert _stub_admin["block"] == []


def test_block_if_globally_paused_does_not_clobber_an_operator_block(
    _stub_admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-suspenders: the WHERE ... IS NULL guard must hold even here,
    matching the sweep's own "never touch an operator row" rule, though in
    practice a brand-new run's column is always NULL at this point."""
    from swebench_eval.control import state as control_state

    run_id = f"{_PREFIX}race-operator-held"
    conn = _db()
    _insert_run(conn, run_id, status="provisioning", blocked_by="operator")
    conn.close()

    monkeypatch.setattr(control_state, "is_paused", lambda pool: True)

    gateway_pause.block_if_globally_paused(run_id, f"tok-{run_id}")

    conn = _db()
    assert _blocked_by(conn, run_id) == "operator", "must not be downgraded to 'global'"
    conn.close()


# ---------------------------------------------------------------------------
# §9.6 end-to-end: global pause -> instance lands PAUSED_BY_OPERATOR -> does
# NOT auto-recover on resume -> restart brings it back. The shim-level half
# (a blocked-key 401 sets shim.paused, harness_worker.py:658 relabels it
# PAUSED_BY_OPERATOR) is unit-tested in test_local_proxy.py; this ties the
# DB-level half together: the global sweep's bookkeeping, and restart.py's
# already-shipped PAUSED_BY_OPERATOR fix, composed in one scenario.
# ---------------------------------------------------------------------------


def test_e2e_global_pause_then_paused_instance_survives_resume_and_is_restartable(
    _stub_admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swebench_eval.orchestrator.control_plane import restart

    run_id = f"{_PREFIX}e2e"
    conn = _db()
    try:
        _insert_run(conn, run_id, blocked_by=None)
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO instance_results
                       (run_id, instance_id, attempt_number, phase, state, error_category)
                   VALUES (%s, 'inst-1', 1, 'harness', 'FAILED_HARNESS', 'PAUSED_BY_OPERATOR')""",
                (run_id,),
            )
        conn.commit()
    finally:
        conn.close()

    # 1. Global pause blocks this run's key (already-live population).
    gateway_pause.sweep_global_pause()
    conn = _db()
    assert _blocked_by(conn, run_id) == "global"
    conn.close()

    # 2. A GLOBAL resume must NOT be what un-sticks the instance — restart is
    #    the only path back for an attempt whose SQS message is already
    #    consumed-and-deleted (§8/§3's "resume only reopens the gate").
    #    Resuming the pool does clear the block bookkeeping...
    gateway_pause.sweep_global_resume()
    conn = _db()
    assert _blocked_by(conn, run_id) is None
    conn.close()
    # ...but the instance row itself is untouched by resume — still exactly
    # the terminal PAUSED_BY_OPERATOR state it landed in. Only restart moves
    # it forward.
    conn = _db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, error_category FROM instance_results "
            "WHERE run_id = %s AND instance_id = 'inst-1'",
            (run_id,),
        )
        state, category = cur.fetchone()
    conn.close()
    assert (state, category) == ("FAILED_HARNESS", "PAUSED_BY_OPERATOR")

    # 3. Restart is what actually brings it back, tagged as an operational
    #    retry (not a legitimate finish, not a deliberate pass@k rerun).
    monkeypatch.setattr(restart, "_dispatch_restarted", lambda *a, **k: None)
    report = restart.restart_instances(run_id, ["inst-1"])
    assert report["skipped"] == []
    assert report["restarted"] == [
        {"instance_id": "inst-1", "attempt_number": 2, "retry_reason": "operator_infra_retry"}
    ]
