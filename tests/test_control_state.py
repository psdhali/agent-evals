"""M1 (ADR-0034 / observability §0.1) — control-state read, fail-closed.

Covers the three required fail-closed tests (§M1.6) plus the staleness check
that the design names the easy-to-omit half: an unreachable Valkey, a missing
key, and a stale ``published_at`` must ALL read as paused — never as "not
paused".  These are DoD items, not optional coverage (Trap 2).
"""

from __future__ import annotations

import time

import pytest

from swebench_eval.control import state as control_state


class _FakeRedis:
    """A minimal stand-in for the control keys the module reads.

    HSET is FIELD-MERGE, matching real Redis — a heartbeat that HSETs only
    ``published_at`` must NOT clobber the other flags/audit fields (the
    heartbeat's whole point).  A replace-semantics fake would make the
    heartbeat test pass while the real code clobbers.
    """

    def __init__(self) -> None:
        self._flags: dict[bytes, bytes] = {}
        self._aborted: set[bytes] = set()
        self._active: set[bytes] = set()
        self.raise_on_get = False

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        if self.raise_on_get:
            raise RuntimeError("redis down")
        if key == "control:flags":
            return self._flags
        return {}

    def hget(self, key: str, field: str) -> bytes | None:
        if self.raise_on_get:
            raise RuntimeError("redis down")
        if key == "control:flags":
            return self._flags.get(field.encode())
        return None

    def smembers(self, key: str) -> set[bytes]:
        if self.raise_on_get:
            raise RuntimeError("redis down")
        if key == "control:aborted":
            return self._aborted
        return set()

    def hset(self, key: str, mapping: dict[str, bytes]) -> None:
        if key == "control:flags":
            # FIELD-MERGE: update the given fields, leave the rest intact.
            self._flags.update({k.encode(): v for k, v in mapping.items()})
        else:
            raise AssertionError(f"unexpected hset to {key}")

    def sadd(self, key: str, *members: str) -> None:
        if key == "control:aborted":
            self._aborted.update(m.encode() for m in members)
        else:
            raise AssertionError(f"unexpected sadd to {key}")

    def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        if key == "control:any-run-active":
            self._active.add(value)
        else:
            raise AssertionError(f"unexpected set to {key}")

    def exists(self, key: str) -> int:
        if key == "control:any-run-active":
            return 1 if self._active else 0
        return 0

    def delete(self, key: str) -> None:
        if key == "control:aborted":
            self._aborted = set()
        else:
            raise AssertionError(f"unexpected delete to {key}")


def _patch_redis(monkeypatch, fake: _FakeRedis) -> None:
    """Point the module's cached client at *fake* for the duration of a test."""
    monkeypatch.setattr(control_state, "_redis", lambda: fake)


def _flags(harness: int, eval_: int, gateway: int, published_at: float) -> dict[bytes, bytes]:
    return {
        b"harness": b"1" if harness else b"0",
        b"eval": b"1" if eval_ else b"0",
        b"gateway": b"1" if gateway else b"0",
        b"published_at": str(published_at).encode(),
    }


def test_read_fails_closed_when_redis_raises(monkeypatch) -> None:
    fake = _FakeRedis()
    fake.raise_on_get = True
    _patch_redis(monkeypatch, fake)

    view = control_state.read()
    assert view.harness_paused is True
    assert view.eval_paused is True
    assert view.stale is True


def test_read_fails_closed_when_key_missing(monkeypatch) -> None:
    fake = _FakeRedis()  # no flags key ever written
    _patch_redis(monkeypatch, fake)

    view = control_state.read()
    assert view.harness_paused is True
    assert view.eval_paused is True
    assert view.gateway_paused is True


def test_read_fails_closed_when_stale(monkeypatch) -> None:
    fake = _FakeRedis()
    fake._flags = _flags(harness=False, eval_=False, gateway=False, published_at=time.time() - 200)
    _patch_redis(monkeypatch, fake)

    view = control_state.read()
    assert view.harness_paused is True
    assert view.eval_paused is True
    assert view.gateway_paused is True
    assert view.stale is True


def test_read_healthy_paused_flags(monkeypatch) -> None:
    fake = _FakeRedis()
    fake._flags = _flags(harness=True, eval_=False, gateway=False, published_at=time.time())
    _patch_redis(monkeypatch, fake)

    view = control_state.read()
    assert view.harness_paused is True
    assert view.eval_paused is False
    assert view.gateway_paused is False
    assert view.stale is False


def test_is_paused_reads_the_computed_flag(monkeypatch) -> None:
    fake = _FakeRedis()
    fake._flags = _flags(harness=True, eval_=False, gateway=False, published_at=time.time())
    _patch_redis(monkeypatch, fake)

    assert control_state.is_paused("harness") is True
    assert control_state.is_paused("eval") is False


def test_is_run_aborted_from_set(monkeypatch) -> None:
    fake = _FakeRedis()
    fake._aborted = {b"run-abc"}
    _patch_redis(monkeypatch, fake)

    assert control_state.is_run_aborted("run-abc") is True
    assert control_state.is_run_aborted("run-other") is False


def test_is_run_aborted_fails_closed(monkeypatch) -> None:
    fake = _FakeRedis()
    fake.raise_on_get = True
    _patch_redis(monkeypatch, fake)

    # Cannot confirm "not aborted" -> read as aborted (door only over-stops).
    assert control_state.is_run_aborted("run-anything") is True


def test_set_pause_writes_flags(monkeypatch) -> None:
    """set_pause publishes only the named pools and stamps published_at."""
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)

    control_state.set_pause(["harness"], True)
    raw = fake._flags
    assert raw[b"harness"] == b"1"
    assert raw[b"eval"] == b"0"
    assert raw[b"gateway"] == b"0"
    assert float(raw[b"published_at"]) > 0

    control_state.set_pause(["harness", "eval"], False)
    raw = fake._flags
    assert raw[b"harness"] == b"0"
    assert raw[b"eval"] == b"0"
    assert raw[b"gateway"] == b"0"


def test_set_pause_rejects_unknown_pool(monkeypatch) -> None:
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    with pytest.raises(ValueError, match="unknown pool"):
        control_state.set_pause(["datacenter"], True)


def test_set_pause_merges_named_pools_over_current(monkeypatch) -> None:
    """M1 review-find §2: pausing eval must NOT resume harness.

    The old implementation rebuilt all three flags from zero on every call, so
    ``pause(["harness"])`` then ``pause(["eval"])`` silently RESUMED harness —
    the exact escalation ("pause harness, now also pause eval") restarting the
    money spend it just stopped.  ``set_pause`` must merge the named pools over
    the current hash.
    """
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    control_state.set_pause(["harness"], True)
    control_state.set_pause(["eval"], True)
    view = control_state.read()
    assert view.harness_paused is True, "pausing eval resumed harness (merge broken)"
    assert view.eval_paused is True
    # And the reverse order, plus a resume on one leaving the other paused.
    control_state.set_pause(["harness"], False)
    view = control_state.read()
    assert view.harness_paused is False
    assert view.eval_paused is True
    assert view.gateway_paused is False


def test_set_pause_publishes_audit_fields(monkeypatch) -> None:
    """M1 review-find §2 / M1.10: the operator's who+why reaches Valkey.

    The API already wrote updated_by/reason to Aurora; this asserts they are
    published in the control:flags hash and surfaced by read() so
    GET /control can return them.
    """
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    control_state.set_pause(["harness"], True, actor="alice", reason="investigating")
    view = control_state.read()
    assert view.harness_paused is True
    assert view.updated_by == "alice"
    assert view.reason == "investigating"


def test_request_abort_publishes_audit_fields(monkeypatch) -> None:
    """M1 review-find §2: abort's who+why is published, not just Aurora."""
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    control_state.request_abort("run-9", scope="harness", reason="runaway", actor="bob")
    assert control_state.is_run_aborted("run-9") is True
    view = control_state.read()
    assert view.updated_by == "bob"
    assert view.reason == "runaway"


# ---------------------------------------------------------------------------
# The pause-deadlock fix (builder1-hw-rebuild-pause-fix-and-e2e.md §3):
#   heartbeat (always) + reconcile (CAS, only while a run is active).
# ---------------------------------------------------------------------------


def test_heartbeat_refreshes_published_at_and_does_not_clobber_other_fields(
    monkeypatch,
) -> None:
    """Mutation-proved: heartbeat touches ONLY published_at.

    Seeds a hash with paused flags + audit fields, then heartbeats.  The
    published_at advances, and the pause/audit fields are LEFT INTACT.  If the
    heartbeat were implemented with ``_write_flags`` (rebuild from an in-memory
    dict), it would clobber the other fields and this test fails.
    """
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    control_state.set_pause(["harness"], True, actor="alice", reason="investigating")
    before = dict(fake._flags)
    first_published = float(before[b"published_at"])

    control_state.heartbeat()

    after = fake._flags
    # published_at advanced beyond the original stamp.
    assert float(after[b"published_at"]) >= first_published
    # Every non-published field survives byte-for-byte.
    for field in (b"harness", b"eval", b"gateway", b"updated_by", b"reason"):
        assert after.get(field) == before.get(field), f"heartbeat clobbered {field!r}"


def test_heartbeat_fires_with_no_active_run(monkeypatch) -> None:
    """The deadlock-breaker: heartbeat must run even with zero run activity.

    The old keep-alive was gated on ``runs_active_marked()`` — which is only
    re-stamped by register_run and by *results*, both downstream of the pause
    gate.  So a paused dispatcher produced no results, the marker lapsed, the
    tick returned early, and nothing ever republished → a permanent pause with
    no self-recovery.  The heartbeat has no such dependency.

    This test stages a STALE ``published_at`` (Aurora/decision ages it exactly
    this way) and proves ONLY the heartbeat rescues the read — ``set_pause``
    stamps the timestamp itself, so it must not be the thing that keeps us
    fresh here.  With the heartbeat a no-op the read stays stale → fails.
    """
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    # Seed the flags via the normal writer, then age the timestamp past the
    # staleness horizon the way real idle time does.
    control_state.set_pause(["harness"], False)
    fake._flags[b"published_at"] = str(time.time() - 200).encode()

    control_state.heartbeat()

    view = control_state.read()
    assert view.stale is False
    assert view.harness_paused is False


def test_reconcile_skips_write_when_hash_updated_at_is_newer(monkeypatch) -> None:
    """Mutation-proved CAS: the operator pause survives a stale reconcile.

    We simulate the race: the hash carries a NEWER updated_at than the Aurora
    row a reconcile is about to write.  ``reconcile_from_db`` must skip the
    flag write (heartbeat only), so the operator's pause survives.
    Inverting the comparison makes this fail ON THE ASSERTION.
    """

    class _UpdatedAtRow:
        def timestamp(self) -> float:
            return 100.0  # Aurora row updated_at is OLDER than the hash

    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    # Operator pause already in Valkey with a fresh updated_at (~ epoch now).
    control_state.set_pause(["harness"], True)
    assert float(fake._flags[b"updated_at"]) > 100.0, "test setup: hash must be newer"

    # The fake connection returns rows in the order each reader calls cursor():
    #   _read_updated_at         -> fetchone  = a scalar row w/ .timestamp()
    #   _read_control_flags      -> fetchone  = (harness, eval, gateway) ints
    #   _read_aborted_run_ids    -> fetchall  = [] (empty)
    class FakeCursor:
        def __init__(self, rows) -> None:
            self._rows = rows
            self._i = 0

        def execute(self, sql, params=None) -> None:
            pass

        def fetchone(self):
            if self._i < len(self._rows):
                row = self._rows[self._i]
                self._i += 1
                return row
            return None

        def fetchall(self):
            return []

        def close(self) -> None:
            pass

    rows_updated = [(_UpdatedAtRow(),)]
    rows_flags = [(1, 1, 1)]  # Aurora flags: harness paused (1) — stale vs the hash
    call_count = {"n": 0}

    def _cursor() -> FakeCursor:
        n = call_count["n"]
        call_count["n"] += 1
        if n == 0:
            return FakeCursor(rows_updated)
        if n == 1:
            return FakeCursor(rows_flags)
        return FakeCursor([])  # aborted: fetchall -> []

    conn = type("Conn", (), {"cursor": staticmethod(_cursor)})()
    control_state.reconcile_from_db(conn)

    # Reconcile must NOT have re-written the flags from Aurora (harness=1 from
    # the stale row is still in the fake rows above) — the operator pause in the
    # hash (also harness=1 here, but the hash's updated_at == its own fresh
    # stamp) must win, and MUST NOT have advanced to a stale 100.0 baseline.
    view = control_state.read()
    assert float(fake._flags[b"updated_at"]) > 100.0, (
        "reconcile wrote a STALE Aurora baseline over the operator's pause (CAS broken) — "
        "compare-and-set must skip when hash.updated_at >= row.updated_at"
    )
    assert view.harness_paused is True, "reconcile clobbered a newer operator pause (CAS broken)"


def test_reconcile_does_not_skip_when_aurora_read_fails(monkeypatch) -> None:
    """MUST-FIX 2 (heartbeat-cas-review §3): a failed Aurora read must NOT take
    the CAS skip branch.

    ``_read_updated_at`` returns ``None`` on error (a dropped connection or a
    missing ``control_state.updated_at`` column).  If reconcile treated that as
    ``0.0``, ``_hash_updated_at() >= 0.0`` is true and it would skip the flag
    write — the heartbeat holding the gate open over flags that never arrive,
    i.e. FAIL OPEN: an error reads as running.  The reconcile must write the
    flags (Aurora's fail-closed all-paused default) instead of skipping.
    """
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    # Seed the hash WITHOUT updated_at so _hash_updated_at() == 0.0.  With the
    # buggy None-as-0.0 the skip branch fires (0.0 >= 0.0).  Correctly, the
    # Aurora failure makes row_updated_at None -> no skip -> flags written.
    fake._flags = {b"published_at": b"100"}

    class _FailConn:
        def cursor(self):
            raise RuntimeError("aurora down")

    # MUST NOT raise and MUST NOT skip: the Aurora failure returns None from
    # _read_updated_at, and reconcile must fall through to write the
    # fail-closed all-paused flags (never hold a fresh heartbeat over stale
    # flags).  A mutation that compares None as 0.0 turns _hash_updated_at()
    # >= row_updated_at into 0.0 >= None here — raising TypeError and failing.
    control_state.reconcile_from_db(_FailConn())

    # It must have written the flags (fail-closed all-paused).
    view = control_state.read()
    assert view.harness_paused is True, (
        "reconcile skipped the flag write on an Aurora read failure — the hash now "
        "holds a fresh published_at with no pause flags (FAIL OPEN).  Must write the "
        "fail-closed all-paused flags instead."
    )


def test_published_at_ages_past_90s_with_zero_results_still_not_paused(monkeypatch) -> None:
    """The one that matters — a *time* bug (DoD-style).

    Without a heartbeat the published_at goes stale 90s after the last write
    and every read fails closed to paused.  This test drives the heartbeat
    repeatedly across a >90s window with ZERO results (no run-activity marker,
    no reconcile) and asserts ``is_paused("harness")`` stays False at every
    step.  A test that finishes in two seconds proves none of this — it is
    exactly how the deadlock shipped.
    """
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)

    class _Clock:
        def __init__(self, start: float) -> None:
            self.now = start

        def __call__(self) -> float:
            return self.now

    # Patch the stdlib time.time, which is what control_state reads.
    clock = _Clock(1_000_000.0)
    monkeypatch.setattr(time, "time", clock)

    # Seed the flags unpaused at t0.
    control_state.set_pause(["harness"], False)
    start = clock.now

    # Simulate the control-plane loop waking every ~30s: heartbeat each
    # tick, NO result ever arrives (the marker is never stamped), advance
    # past 90s.
    while clock.now - start < 140:  # span > STALE_AFTER_S (90)
        clock.now += 30
        control_state.heartbeat()
        # The heartbeat refreshed published_at, so even just after a tick
        # the read must not be stale.
        assert (
            control_state.is_paused("harness") is False
        ), "heartbeat did not keep the gate open across the idle window"
        assert control_state.read().stale is False
