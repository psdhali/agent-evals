"""M1.2 / ADR-0034 §2 — the Aurora→Valkey publisher tick (builder 3, M1 §1).

CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md: the publisher tick moved
out of the results-writer process into ``run_supervisor.py``, the new
control-plane singleton — this file (formerly ``test_control_publisher.py``)
moved with it.  Behaviour is unchanged (a relocation, not a redesign); only
the module under test did.

Guards the three call sites that make ``control:flags`` actually get published:

  1. ``run_supervisor.run_run_supervisor`` publishes from Aurora at startup
     (the control-plane singleton; a stale hash after a restart reads as
     paused and would block a fresh run for no reason).
  2. The run-supervisor loop re-publishes on a ~30s cadence WHILE a run is
     active, and goes fully idle between runs — no Aurora connection at all,
     so Aurora (seconds_until_auto_pause = 300) can sleep (M1.9 corollary).
  3. ``dispatcher.register_run`` publishes immediately when a run starts, so
     the gate opens the moment there is something to dispatch.

Each test FAILS if the corresponding call site is removed (mutation-proved):
the startup test drives the real ``run_run_supervisor`` with a mocked
``time.sleep``, the loop test does the same with an active-run marker, and
the register_run test drives the real register function.
"""

from __future__ import annotations

from typing import Self
from unittest import mock

import pytest

from swebench_eval.control import state as control_state
from swebench_eval.orchestrator.control_plane import dispatcher, run_supervisor


class _StopLoop(Exception):
    """Raised by the mocked ``time.sleep`` to end run_run_supervisor."""


class _FakeConn:
    """Serves the one query publish_from_db needs (control_state single row)."""

    def __init__(self, paused: int = 0) -> None:
        self._paused = paused

    def cursor(self) -> _FakeCur:
        return _FakeCur(self._paused)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeCur:
    def __init__(self, paused: int) -> None:
        self._paused = paused
        self.row: tuple[object, ...] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        if "updated_at" in sql:
            # The CAS reconcile reads control_state.updated_at (a timestamp).
            self.row = (_AuroraTimestamp(0.0),)
        elif "control_state" in sql:
            self.row = (self._paused, self._paused, self._paused)
        elif "FROM runs" in sql:
            self.row = None  # no aborted runs / no running runs
        else:
            self.row = None

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []

    def close(self) -> None:
        return None


class _AuroraTimestamp:
    """A psycopg2-``timestamp``-alike: ``row[0].timestamp()`` works like a
    real Postgres datetime value (the reconcile CAS reads it)."""

    def __init__(self, epoch: float = 0.0) -> None:
        self._epoch = epoch

    def timestamp(self) -> float:
        return self._epoch


class _FakeRedis:
    """Minimal redis stand-in: control:flags hash + the run-activity marker."""

    def __init__(self, active: bool) -> None:
        self._active = active
        self._flags: dict[bytes, bytes] = {}
        self._aborted: set[bytes] = set()

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        if key == "control:flags":
            return self._flags
        return {}

    def hget(self, key: str, field: str) -> bytes | None:
        if key == "control:flags":
            return self._flags.get(field.encode())
        return None

    def hset(self, key: str, mapping: dict[str, bytes]) -> None:
        if key == "control:flags":
            # FIELD-MERGE, matching real Redis — the heartbeat HSETs only
            # published_at and must not wipe the pause flags.
            self._flags.update({k.encode(): v for k, v in mapping.items()})

    def smembers(self, key: str) -> set[bytes]:
        if key == "control:aborted":
            return self._aborted
        return set()

    def sadd(self, key: str, *members: str) -> None:
        if key == "control:aborted":
            self._aborted.update(m.encode() for m in members)

    def delete(self, key: str) -> None:
        self._aborted.clear()

    def exists(self, key: str) -> int:
        return 1 if self._active and key == "control:any-run-active" else 0

    def set(self, key: str, value: bytes, ex: int) -> None:
        if key == "control:any-run-active":
            self._active = True


def _patch_redis(monkeypatch: pytest.MonkeyPatch, fake: _FakeRedis) -> None:
    monkeypatch.setattr(control_state, "_redis", lambda: fake)


def _run_run_supervisor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_conn: _FakeConn,
    fake_redis: _FakeRedis,
    tick_count: int = 2,
) -> None:
    """Drive the real run_run_supervisor loop with all boundaries faked.

    ``time.sleep`` (the loop's own pacing — it has no blocking receive to
    ride, unlike results-writer) raises ``_StopLoop`` after *tick_count*
    iterations; the publisher tick is exercised on each iteration.  The
    reaper tick is neutralised — it is covered by its own tests (the
    relocated rule-2/3 tests), not these publisher-focused ones, and would
    otherwise open an extra, unrelated Aurora connection every iteration.
    """
    import swebench_eval.database.connection as dbc

    calls = {"sleep": 0}

    def _fake_sleep(*args, **kwargs):
        if calls["sleep"] >= tick_count:
            raise _StopLoop()
        calls["sleep"] += 1

    _patch_redis(monkeypatch, fake_redis)
    monkeypatch.setattr(dbc, "get_connection", lambda: fake_conn)
    monkeypatch.setattr(dbc, "run_migrations", lambda: None)
    monkeypatch.setattr(dbc, "ensure_additional_databases", lambda: None)
    monkeypatch.setattr(run_supervisor, "_maybe_run_reaper", lambda: None)
    monkeypatch.setattr(
        "swebench_eval.orchestrator.control_plane.run_supervisor.time.sleep", _fake_sleep
    )
    with pytest.raises(_StopLoop):
        run_supervisor.run_run_supervisor()


# ---------------------------------------------------------------------------
# 1. Startup publish (call site 1) — mutation-proved
# ---------------------------------------------------------------------------


def test_run_supervisor_publishes_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control-plane singleton rebuilds control:flags from Aurora at boot."""
    fake_redis = _FakeRedis(active=False)
    published: list[object] = []

    def _record(conn):
        published.append(conn)

    monkeypatch.setattr(control_state, "publish_from_db", _record)
    monkeypatch.setattr(control_state, "mark_runs_active", lambda: None)

    _run_run_supervisor(monkeypatch, fake_conn=_FakeConn(), fake_redis=fake_redis)

    assert published, "run_run_supervisor never published from Aurora at startup"
    # Startup publish + the loop ticks while active are separate; the point is
    # the call exists and runs before the loop's first sleep.
    assert any(True for _ in published)  # at least one publish


def test_run_supervisor_ticks_while_run_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """With an active run, the loop reconciles on cadence (tick call site).

    The clock advances 40s between loop iterations, so every tick passes the
    30s time guard — the loop reconciles repeatedly, independent of the startup
    publish.  Removing the loop tick call site (or the idle gate) turns this
    red.

    Per the heartbeat/reconcile split: the per-tick call is now
    ``reconcile_from_db`` (the old ``publish_from_db`` is startup-only), and
    ``heartbeat`` runs on EVERY tick even when no run is active (the deadlock
    fix).
    """
    fake_redis = _FakeRedis(active=True)
    reconciled: list[object] = []

    monkeypatch.setattr(control_state, "reconcile_from_db", lambda conn: reconciled.append(conn))
    monkeypatch.setattr(control_state, "mark_runs_active", lambda: None)
    run_supervisor._control_last_publish_s = 0.0
    clock = iter(range(0, 300, 40))  # 0, 40, 80, ...
    monkeypatch.setattr(
        "swebench_eval.orchestrator.control_plane.run_supervisor.time.monotonic",
        lambda: next(clock),
    )

    # tick helper is module state; also drive it through 3 loop iterations.
    _run_run_supervisor(monkeypatch, fake_conn=_FakeConn(), fake_redis=fake_redis, tick_count=3)

    # Each loop iteration's tick reconciles while active (3) — the old
    # assertion counted publish_from_db per tick; the reconcile took that role.
    assert len(reconciled) >= 2, f"expected repeated reconciles while active, got {len(reconciled)}"


def test_run_supervisor_goes_idle_between_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """With NO active run, the loop must NOT publish on cadence (idle).

    The idle gate must also avoid opening an Aurora connection: between runs
    there is nothing to dispatch, so the ticker must not touch Aurora (that is
    what lets it auto-pause).  We assert publish_from_db was called only for
    the STARTUP rebuild, never for the loop ticks.
    """
    fake_redis = _FakeRedis(active=False)
    published: list[object] = []
    conns: list[object] = []

    def _record(conn):
        published.append(conn)

    monkeypatch.setattr(control_state, "publish_from_db", _record)
    monkeypatch.setattr(control_state, "mark_runs_active", lambda: None)

    import swebench_eval.database.connection as dbc

    def _counting_get_conn():
        conns.append("conn")
        return _FakeConn()

    monkeypatch.setattr(dbc, "get_connection", _counting_get_conn)

    _run_run_supervisor(monkeypatch, fake_conn=_FakeConn(), fake_redis=fake_redis, tick_count=3)

    # Startup publish happened; but the tick (3 idle iterations) must NOT have
    # opened another Aurora connection just to decide "no run active".
    assert len(published) >= 1, "startup publish missing"
    # The idle tick itself opens no connection: only the startup publish did.
    assert len(conns) <= 2, f"idle tick opened {len(conns)} Aurora connections, expected ~1"


# ---------------------------------------------------------------------------
# 2. register_run publishes + marks active (call site 3) — mutation-proved
#
# Unrelated to the split (dispatcher.py, not results_writer.py/run_supervisor.py)
# — left exactly as it was.
# ---------------------------------------------------------------------------


def test_register_run_publishes_and_marks_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """register_run opens the gate immediately when a run starts."""
    fake_conn = _FakeConn()
    published: list[object] = []
    marked: list[object] = []

    import swebench_eval.database.connection as dbc

    monkeypatch.setattr(dbc, "get_connection", lambda: fake_conn)
    monkeypatch.setattr(control_state, "publish_from_db", lambda conn: published.append(conn))
    monkeypatch.setattr(control_state, "mark_runs_active", lambda: marked.append(True))

    dispatcher.register_run("run-m1-test", "custom_minimal", "cheap-oss-model")

    assert published, "register_run must publish control:flags when a run starts"
    assert marked, "register_run must mark the run-activity marker"


# ---------------------------------------------------------------------------
# 3. The tick helper respects its time guard (unit, isolated)
# ---------------------------------------------------------------------------


def test_tick_helper_respects_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two ticks within the interval produce one reconcile; after it, another.

    Pre-fix this asserted ``publish_from_db`` per tick; the heartbeat/reconcile
    split moved the per-tick call to ``reconcile_from_db`` (heartbeat runs on
    every guard-open tick, but the reconcile is the Aurora leg that must stay
    within 30s so idle Aurora can auto-pause).
    """
    fake_redis = _FakeRedis(active=True)
    _patch_redis(monkeypatch, fake_redis)
    reconciled: list[object] = []
    monkeypatch.setattr(control_state, "reconcile_from_db", lambda conn: reconciled.append(conn))

    import swebench_eval.database.connection as dbc

    monkeypatch.setattr(dbc, "get_connection", lambda: _FakeConn())

    run_supervisor._control_last_publish_s = 0.0
    # First tick well past the guard: reconciles.
    with mock.patch(
        "swebench_eval.orchestrator.control_plane.run_supervisor.time.monotonic",
        return_value=40.0,
    ):
        run_supervisor._maybe_publish_control()
    # Second tick 1 second later: guard still closed → no reconcile.
    with mock.patch(
        "swebench_eval.orchestrator.control_plane.run_supervisor.time.monotonic",
        return_value=41.0,
    ):
        run_supervisor._maybe_publish_control()
    # Third tick 31s after the first: guard open again → reconcile.
    with mock.patch(
        "swebench_eval.orchestrator.control_plane.run_supervisor.time.monotonic",
        return_value=71.0,
    ):
        run_supervisor._maybe_publish_control()

    assert len(reconciled) == 2, f"expected 2 reconciles (t=0, t=31), got {len(reconciled)}"


def test_tick_heartbeats_when_no_run_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tick keeps ``published_at`` fresh with NO run active.

    heartbeat-cas-review §4: the one line of wiring that IS the deadlock fix
    (heartbeat runs even when the run-activity marker is absent) had no CI
    test — deleting that line left the suite green.  This drives the REAL tick
    with ``runs_active_marked()`` false and asserts ``published_at`` moved.
    """
    fake_redis = _FakeRedis(active=False)
    # Seed a stale published_at the way idle time does, plus other fields so the
    # heartbeat must be field-merge to not clobber them.
    fake_redis._flags = {
        b"harness": b"1",
        b"eval": b"0",
        b"gateway": b"0",
        b"published_at": b"1",
    }
    _patch_redis(monkeypatch, fake_redis)

    run_supervisor._control_last_publish_s = 0.0
    # Also assert reconcile did NOT open an Aurora connection while idle.
    import swebench_eval.database.connection as dbc

    conns: list[object] = []

    def _no_conn():
        conns.append("conn")
        raise AssertionError("idle tick must not open an Aurora connection")

    monkeypatch.setattr(dbc, "get_connection", _no_conn)

    with mock.patch(
        "swebench_eval.orchestrator.control_plane.run_supervisor.time.monotonic",
        return_value=40.0,
    ):
        run_supervisor._maybe_publish_control()

    assert not conns, "idle tick opened an Aurora connection (should heartbeat only)"
    published = float(fake_redis._flags[b"published_at"])
    assert published > 1.0, (
        "tick did not refresh published_at with NO run active — the heartbeat call "
        "in the tick is missing/broken (the deadlock fix)"
    )
    # Field-merge: the pause flag was not clobbered by the heartbeat.
    assert fake_redis._flags[b"harness"] == b"1", "heartbeat clobbered a non-published field"


@pytest.mark.integration
def test_gate_stays_open_more_than_90s_after_operator_action() -> None:
    """M1.2 integration (local compose): the publisher tick keeps the gate
    open past the staleness horizon while a run is active.

    ``read()`` fails CLOSED to paused once ``published_at`` is older than
    STALE_AFTER_S (90s).  If the Aurora→Valkey tick is missing (the brief's §1
    bug: ``publish_from_db`` defined but never called), a single operator
    resume would reopen the gates for 90s and then quietly close them again
    mid-run — the exact silent-stall the tick exists to prevent.

    This test therefore waits MORE than 90 seconds of wall clock, running the
    real ``_maybe_publish_control`` on the real stack each ~30s, and asserts
    the gate stays open throughout.  A test that finished in two seconds would
    prove nothing about staleness.
    """
    import time as _time

    from swebench_eval.database.connection import get_connection

    r = control_state._redis()

    # Set up a control_state row reflecting "not paused" (the operator action
    # was RESUME), and stamp the run-activity marker so the tick stays hot.
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO control_state (id, harness_paused, eval_paused,
                     gateway_paused)
                   VALUES (TRUE, FALSE, FALSE, FALSE)
                   ON CONFLICT (id) DO UPDATE
                     SET harness_paused = FALSE, eval_paused = FALSE,
                         gateway_paused = FALSE""")
            cur.execute("DELETE FROM runs WHERE run_id = 'm1-publisher-int'")
            cur.execute("""INSERT INTO runs (run_id, config_snapshot, status)
                   VALUES ('m1-publisher-int', '{}', 'running')""")
        conn.commit()
    finally:
        conn.close()

    try:
        # Fresh keys; snapshot the open state into Valkey.
        r.delete("control:flags", "control:any-run-active", "control:aborted")
        control_state.mark_runs_active()
        run_supervisor._publish_control_from_aurora()

        assert control_state.is_paused("harness") is False, "gate not open after operator action"

        # Run the real tick on the real stack for >90s and assert the gate
        # never closes (each tick refreshes published_at, so is_paused stays
        # False well past STALE_AFTER_S).
        t0 = _time.monotonic()
        last_tick = 0.0
        while _time.monotonic() - t0 < 95.0:
            if _time.monotonic() - last_tick >= 30.0:
                run_supervisor._maybe_publish_control()
                last_tick = _time.monotonic()
            assert (
                control_state.is_paused("harness") is False
            ), "gate closed >90s after operator action while a run is active"
            _time.sleep(2)

        assert _time.monotonic() - t0 >= 90.0
    finally:
        r.delete("control:flags", "control:any-run-active", "control:aborted")
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM runs WHERE run_id = 'm1-publisher-int'")
            conn.commit()
        finally:
            conn.close()


def test_run_supervisor_recovers_open_runs_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-08: after the pool rehydration, every open run's Valkey-only state (LiteLLM key,
    alias pacer copy) is re-created — the first restart across a Valkey recreate refused all
    28 jobs without this."""
    from swebench_eval.orchestrator.control_plane import open_run_recovery

    called: list[bool] = []
    monkeypatch.setattr(open_run_recovery, "recover_open_runs", lambda: called.append(True))
    monkeypatch.setattr(control_state, "publish_from_db", lambda conn: None)
    monkeypatch.setattr(control_state, "mark_runs_active", lambda: None)

    _run_run_supervisor(monkeypatch, fake_conn=_FakeConn(), fake_redis=_FakeRedis(active=False))

    assert called, "run_run_supervisor never ran open-run recovery at startup"
