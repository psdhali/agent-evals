"""gateway_admin.await_alias_served — the rotate -> served-by-every-replica wait.

Bring-up 2026-09-03: /model/update reloads only the replica that handled it; the others
re-read the DB on a timer (5 s in litellm_config.yaml). The judge 401'd on the stale replica
three seconds after "active after 2 probes"; the discovery probe measured that replica's
cooldown 429s as the provider's burst edge. The wait must demand a streak of 200s that SPANS
the reload interval, reset on anything that is not a 200, and fail loudly at the deadline.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from swebench_eval.gateway import admin as gateway_admin


def _drive(
    monkeypatch: pytest.MonkeyPatch, statuses: list[int | Exception], *, step_s: float = 1.0
) -> tuple[list[dict[str, Any]], Any]:
    """Fake httpx.post over *statuses* (the last one repeats) and a clock that advances
    *step_s* per monotonic() read — one read per loop iteration after the call."""
    seen: list[dict[str, Any]] = []
    it = iter(statuses)
    last: int | Exception = statuses[-1]

    def _post(url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float) -> Any:
        nonlocal last
        try:
            last = next(it)
        except StopIteration:
            pass
        seen.append({"url": url, "auth": headers["Authorization"], "json": json})
        if isinstance(last, Exception):
            raise last
        return SimpleNamespace(status_code=last)

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    clock = itertools.count(0.0, step_s)
    monkeypatch.setattr("time.monotonic", lambda: float(next(clock)))
    return seen, clock


def _baseline_calls(monkeypatch: pytest.MonkeyPatch) -> int:
    """How many calls an uninterrupted run of 200s takes to span min_span_s on this clock."""
    _drive(monkeypatch, [200], step_s=0.5)
    return gateway_admin.await_alias_served(
        "http://gw", "sk", "a", min_span_s=10.0, timeout_s=120.0
    )


def test_returns_only_after_a_streak_of_200s_spanning_min_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen, _ = _drive(monkeypatch, [200], step_s=1.0)  # 1 s of clock per loop iteration
    calls = gateway_admin.await_alias_served(
        "http://gw/", "sk-raw", "qwen3-coder-next", min_span_s=10.0, timeout_s=120.0
    )
    # a 10 s span at 1 s per iteration: ~11 calls — never the first one, which is what the
    # old checks returned on
    assert 9 <= calls <= 13
    assert calls == len(seen)
    assert seen[0]["url"] == "http://gw/chat/completions"
    assert seen[0]["auth"] == "Bearer sk-raw"
    assert seen[0]["json"] == {
        "model": "qwen3-coder-next",
        "messages": [{"role": "user", "content": "ready-check"}],
        "max_tokens": 1,
    }


@pytest.mark.parametrize("bad", [401, 429, 404, 500])
def test_any_non_200_resets_the_streak(monkeypatch: pytest.MonkeyPatch, bad: int) -> None:
    """401 = the stale replica; 429 = its cooldown or real pressure; anything else = not
    proven. Five 200s, then one bad, must NOT count toward the span: the run takes at least
    the uninterrupted baseline PLUS the six calls that came before the reset."""
    baseline = _baseline_calls(monkeypatch)
    statuses: list[int | Exception] = [200, 200, 200, 200, 200, bad, 200]  # five 200s, reset, 200
    _drive(monkeypatch, statuses, step_s=0.5)
    calls = gateway_admin.await_alias_served(
        "http://gw", "sk", "a", min_span_s=10.0, timeout_s=120.0
    )
    assert calls >= baseline + 6


def test_transport_errors_reset_the_streak_but_do_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _baseline_calls(monkeypatch)
    blip = httpx.ConnectError("blip")
    statuses: list[int | Exception] = [200, 200, 200, 200, 200, blip, 200]  # five 200s, reset, 200
    _drive(monkeypatch, statuses, step_s=0.5)
    calls = gateway_admin.await_alias_served(
        "http://gw", "sk", "a", min_span_s=10.0, timeout_s=120.0
    )
    assert calls >= baseline + 6


def test_deadline_raises_naming_the_alias_and_last_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _drive(monkeypatch, [401], step_s=10.0)
    with pytest.raises(RuntimeError, match="'judge-model'.*not served.*last status 401"):
        gateway_admin.await_alias_served(
            "http://gw", "sk", "judge-model", min_span_s=10.0, timeout_s=30.0, what="rotated key"
        )


def test_min_span_default_is_twice_the_replica_reload_interval() -> None:
    """The owner's number: 10 s — twice litellm_config.yaml's
    proxy_config_reload_interval_seconds (5). If the config changes, this changes with it."""
    assert gateway_admin._REPLICA_RELOAD_INTERVAL_S == 5.0
    assert gateway_admin._SERVED_MIN_SPAN_S == 10.0
