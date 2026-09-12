"""§6.6 — dispatcher -> SQS -> model_tpm_observations, no consumer-maintained pointer.

Producer side: the Autoscaler emits observation events on TRANSITIONS (one overload event per
freeze, one recovery per clean exit, one reconciliation_peak per applied growth), fire-and-
forget. Consumer side: the results-writer daemon's insert appends one fact per message and
refuses (raises -> visibility retry -> DLQ) on a malformed body rather than dropping it.
"""

from __future__ import annotations

from typing import Any, Self
from unittest import mock

import pytest

import swebench_eval.orchestrator.control_plane.harness_dispatcher as hd
from swebench_eval.orchestrator.control_plane.results_writer import _insert_model_observation


class FakeRedis:
    """Just enough for the Autoscaler's tick: empty fleet, no cfg, no counters."""

    def scan_iter(self, pattern: str, count: int = 100) -> Any:
        return iter([])

    def get(self, key: str) -> None:
        return None

    def hgetall(self, key: str) -> dict[str, str]:
        return {}

    def set(self, *a: Any, **k: Any) -> None:
        return None

    def hset(self, *a: Any, **k: Any) -> None:
        return None


def _scaler(**kwargs: Any) -> hd.Autoscaler:
    return hd.Autoscaler(model_alias="laguna-xs-2.1", mode="observe", redis_client=FakeRedis())


class TestProducer:
    def test_overload_transition_emits_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(hd, "send_message", lambda q, b: sent.append((q, b)))
        scaler = _scaler()
        scaler._tick_interval_s = 0.0
        # Two consecutive overloaded ticks -> ONE overload event (transition, not per-tick).
        with mock.patch.object(scaler, "_overloads_window", return_value=3):
            scaler._tick(in_flight_tasks=5)
            scaler._tick(in_flight_tasks=5)
        events = [b["event_type"] for _q, b in sent]
        assert events == ["overload"]
        queue, body = sent[0]
        assert queue == "model-observations"
        assert body["model_alias"] == "laguna-xs-2.1"
        assert body["value_kind"] == "projected_arrival_tok_per_min"
        assert isinstance(body["tpm_value"], int)

    def test_clean_exit_emits_recovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[dict[str, Any]] = []
        monkeypatch.setattr(hd, "send_message", lambda q, b: sent.append(b))
        scaler = _scaler()
        scaler._tick_interval_s = 0.0
        with mock.patch.object(scaler, "_overloads_window", return_value=1):
            scaler._tick(in_flight_tasks=5)  # enters cooldown -> overload event
        scaler._cooldown_until = 0.0  # the freeze has expired
        with mock.patch.object(scaler, "_overloads_window", return_value=0):
            scaler._tick(in_flight_tasks=5)  # cooldown over, clean window -> recovery
        assert [b["event_type"] for b in sent] == ["overload", "recovery_stabilized"]

    def test_no_alias_no_emission(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[Any] = []
        monkeypatch.setattr(hd, "send_message", lambda q, b: sent.append(b))
        scaler = hd.Autoscaler(model_alias="", mode="observe", redis_client=FakeRedis())
        scaler._tick_interval_s = 0.0
        with mock.patch.object(scaler, "_overloads_window", return_value=3):
            scaler._tick(in_flight_tasks=5)
        assert sent == []

    def test_send_failure_never_breaks_the_tick(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(q: str, b: dict[str, Any]) -> None:
            raise RuntimeError("sqs down")

        monkeypatch.setattr(hd, "send_message", _boom)
        scaler = _scaler()
        scaler._tick_interval_s = 0.0
        with mock.patch.object(scaler, "_overloads_window", return_value=3):
            decision = scaler._tick(in_flight_tasks=5)  # must not raise
        assert decision.binding_constraint == "cooldown"


class _FakeCursor:
    def __init__(self, executed: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._executed = executed

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._executed.append((sql, params))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.committed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.executed)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        pass


class TestConsumer:
    def test_inserts_one_fact(self) -> None:
        conn = _FakeConn()
        body = {
            "model_alias": "laguna-xs-2.1",
            "run_id": "run-1",
            "event_type": "recovery_stabilized",
            "value_kind": "projected_arrival_tok_per_min",
            "tpm_value": 4_200_000,
            "at_concurrency": 37,
            "notes": "mode=live",
        }
        with mock.patch("swebench_eval.database.connection.get_connection", return_value=conn):
            _insert_model_observation(body)
        assert conn.committed
        sql, params = conn.executed[0]
        assert "INSERT INTO model_tpm_observations" in sql
        assert params[0] == "laguna-xs-2.1"
        assert params[2] == "recovery_stabilized"
        assert params[4] == 4_200_000

    def test_malformed_body_raises_never_drops(self) -> None:
        """A body without model_alias must RAISE — the message then rides visibility retry
        into the DLQ instead of being silently deleted as processed."""
        with pytest.raises(KeyError):
            _insert_model_observation({"event_type": "overload", "tpm_value": 1})
