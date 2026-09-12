"""The kill-drill — acceptance §6 of
BUILDER6-SPLIT-SUPERVISOR-AND-RESULTS-WRITER-2026-08-31.md /
CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md:

  "Stop the supervisor, assert control_state.read() fail-closes and the
  dispatcher refuses to receive. This is the behaviour the split exists to
  protect — demonstrate it, do not assume it."

This is the whole reason run-supervisor is its own service: the 90-second
Aurora->Valkey heartbeat (control/state.py's STALE_AFTER_S) is the single
point of failure the split was built to isolate from the busiest consumer in
the system. "Stopping the supervisor" is simulated the same way the existing
90s-staleness test proves the POSITIVE case
(test_control_state.py::test_published_at_ages_past_90s_with_zero_results_still_not_paused,
a fake clock, no real 90s wait) — here in reverse: publish one healthy
heartbeat, then advance time past STALE_AFTER_S with NO further heartbeat
call at all (nothing standing in for run-supervisor after that point), and
assert every downstream reader — control_state.read() itself AND the real
harness-dispatcher admission gate that gaes receive_message — fails closed.

Requires a live Redis (the compose stack). Marked ``integration`` and
deselected by default, same convention as the sibling multi-replica file.

    uv run pytest tests/test_run_supervisor_kill_drill.py -m integration
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

from swebench_eval.control import state as control_state
from swebench_eval.orchestrator.control_plane import harness_dispatcher

pytestmark = pytest.mark.integration


class _StopLoop(Exception):
    """Raised by a mocked sleep to end run_harness_dispatcher after one gate pass."""


@pytest.fixture(autouse=True)
def _clean_control_flags():
    r = control_state._redis()
    r.delete("control:flags", "control:any-run-active", "control:aborted")
    yield
    r.delete("control:flags", "control:any-run-active", "control:aborted")


def test_control_state_read_fails_closed_after_the_supervisor_stops_heartbeating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heartbeat that run-supervisor alone owns: one healthy publish, then
    silence past STALE_AFTER_S, with no reconcile/heartbeat call standing in
    for it — exactly what a stopped ``run-supervisor`` ECS task looks like
    from every other reader's point of view.
    """
    # A healthy operator action, exactly as run-supervisor's own startup/tick
    # would leave things: unpaused, published_at freshly stamped.
    control_state.set_pause(["harness"], False, actor="test", reason="kill-drill setup")
    assert control_state.is_paused("harness") is False, "setup: gate should start open"
    assert control_state.read().stale is False, "setup: freshly-published state must not be stale"

    t0 = time.time()
    # Nothing calls heartbeat()/reconcile_from_db() again from here on — the
    # supervisor is "stopped." Jump the clock past STALE_AFTER_S with no
    # further publish, the same fake-clock technique
    # test_published_at_ages_past_90s_with_zero_results_still_not_paused uses
    # to prove the positive case without a real 90s wait.
    monkeypatch.setattr(time, "time", lambda: t0 + control_state.STALE_AFTER_S + 1)

    view = control_state.read()
    assert view.stale is True, "control_state.read() did not fail closed once published_at aged out"
    assert view.harness_paused is True
    assert view.eval_paused is True
    assert view.gateway_paused is True
    assert control_state.is_paused("harness") is True, "is_paused must reflect the fail-closed read"


def test_harness_dispatcher_refuses_to_receive_after_the_supervisor_stops_heartbeating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: the REAL admission gate (harness_dispatcher.py's
    ``_DispatcherAdmission.may_launch``), reading the REAL (fail-closed)
    ``control_state.is_paused("harness")`` — no mock on that call — must
    block, and ``run_harness_dispatcher``'s loop must never reach
    ``receive_message`` as a result. ECS itself is mocked (this test proves
    the CONTROL-PLANE split's contract, not ECS plumbing) with a fresh ground-
    truth timestamp so the gate's own stall path never masks the pause path
    it exists to prove.
    """
    control_state.set_pause(["harness"], False, actor="test", reason="kill-drill setup")
    assert control_state.is_paused("harness") is False, "setup: gate should start open"

    t0 = time.time()
    monkeypatch.setattr(
        "swebench_eval.orchestrator.control_plane.harness_dispatcher.time.time",
        lambda: t0 + control_state.STALE_AFTER_S + 1,
    )

    admission = harness_dispatcher._DispatcherAdmission(ceiling=10)
    # Force ground truth "fresh" at the faked now, so _maybe_refresh's own
    # 10s-refresh gate short-circuits and this test exercises the PAUSE
    # branch specifically — not a stall from an unmocked ECS call.
    admission._last_gt_at = t0 + control_state.STALE_AFTER_S + 1

    decision = admission.may_launch()
    assert decision.allowed is False, "admission must refuse once the heartbeat has gone stale"
    assert decision.reason == "paused", f"expected the pause branch, got {decision.reason!r}"

    # And the loop-level proof, same shape as
    # test_pause_does_not_receive.py::test_pause_does_not_receive_harness_dispatcher
    # — receive_message must never be called.
    receive = mock.Mock(side_effect=AssertionError("receive_message called after supervisor died"))
    monkeypatch.setattr(harness_dispatcher, "receive_message", receive)
    # **kwargs: run_harness_dispatcher now also passes autoscaler= (the L2
    # wiring) — a positional-only stub broke the moment that landed, and this
    # integration-marked test is deselected by default so it broke silently.
    monkeypatch.setattr(
        harness_dispatcher, "_DispatcherAdmission", lambda ceiling, **_kw: admission
    )
    monkeypatch.setenv("MAX_CONCURRENT_HARNESS_TASKS", "10")
    monkeypatch.setattr(
        "swebench_eval.orchestrator.control_plane.harness_dispatcher.time.sleep",
        mock.Mock(side_effect=_StopLoop()),
    )

    with pytest.raises(_StopLoop):
        harness_dispatcher.run_harness_dispatcher()

    receive.assert_not_called()
