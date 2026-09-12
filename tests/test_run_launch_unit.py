"""Unit tests — run-launch (BUILDER4-RUN-LAUNCH-ORCHESTRATOR-2026-08-26).

Fast, hermetic, no live Postgres/Redis/SQS — every boundary is mocked.  The
integration-only properties (genuine concurrency, the real state_rank guard,
schema migration) live in ``tests/test_run_launch_integration.py``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Self
from unittest import mock

import pytest

from swebench_eval.gateway import admin as gateway_admin
from swebench_eval.gateway import openrouter_admin, pricing
from swebench_eval.gateway.rotatable_models import ROTATABLE_MODELS
from swebench_eval.orchestrator.control_plane import run_key_cache, run_launch
from swebench_eval.orchestrator.run_config import RunConfig

# ---------------------------------------------------------------------------
# Rule 3 (repo): never any key material in a log, fixture, or return value.
# ---------------------------------------------------------------------------


def test_provision_keys_never_logs_or_returns_raw_key_material(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """rule 3: "Store the LiteLLM key id and a hash of the OpenRouter key,
    never either key."  Mints two real-shaped raw secrets, asserts NEITHER
    substring ever appears in any log record OR in the returned tuple.

    Mutation-check: temporarily add ``logger.info("minted %s", litellm_raw)``
    inside ``_provision_keys`` — this test then fails (the raw key appears in
    a captured log record). Verified by hand and reverted.
    """
    raw_litellm_key = "sk-litellm-SUPERSECRET-abc123"
    raw_openrouter_key = "sk-or-v1-SUPERSECRET-xyz789"

    monkeypatch.setattr(run_launch, "_fetch_openrouter_provisioning_key", lambda: "prov-key")
    monkeypatch.setattr(run_launch, "gateway_base_url", lambda: "http://gateway.local")
    monkeypatch.setattr(run_launch, "gateway_api_key", lambda: "master-key")
    monkeypatch.setattr(
        gateway_admin,
        "generate_key",
        lambda *a, **k: (raw_litellm_key, "litellm-key-id-non-secret"),
    )
    monkeypatch.setattr(
        openrouter_admin, "mint_key", lambda *a, **k: (raw_openrouter_key, "or-hash-non-secret")
    )
    monkeypatch.setattr(run_launch, "run_key_cache", mock.Mock())

    with caplog.at_level(logging.DEBUG):
        litellm_key_id, openrouter_key_hash = run_launch._provision_keys(
            "run-1", "custom_minimal", "cheap-oss-model", 10.0, None, None
        )

    assert litellm_key_id == "litellm-key-id-non-secret"
    assert openrouter_key_hash == "or-hash-non-secret"
    assert raw_litellm_key not in litellm_key_id
    assert raw_openrouter_key not in openrouter_key_hash

    for record in caplog.records:
        message = record.getMessage()
        assert raw_litellm_key not in message, f"raw LiteLLM key leaked into a log: {message}"
        assert raw_openrouter_key not in message, f"raw OpenRouter key leaked into a log: {message}"


def test_provision_keys_stores_raw_key_only_in_the_run_key_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ONE place the raw LiteLLM key is allowed to travel: the Redis
    transport to the harness dispatcher — never Aurora, never a return value
    beyond that one call."""
    raw_litellm_key = "sk-litellm-SUPERSECRET-abc123"
    stored: dict[str, str] = {}

    monkeypatch.setattr(run_launch, "_fetch_openrouter_provisioning_key", lambda: "prov-key")
    monkeypatch.setattr(run_launch, "gateway_base_url", lambda: "http://gateway.local")
    monkeypatch.setattr(run_launch, "gateway_api_key", lambda: "master-key")
    monkeypatch.setattr(
        gateway_admin, "generate_key", lambda *a, **k: (raw_litellm_key, "litellm-key-id")
    )
    monkeypatch.setattr(openrouter_admin, "mint_key", lambda *a, **k: ("sk-or-raw", "or-hash"))
    monkeypatch.setattr(run_key_cache, "store", lambda run_id, key: stored.__setitem__(run_id, key))

    litellm_key_id, _ = run_launch._provision_keys(
        "run-2", "custom_minimal", "cheap-oss-model", 10.0, None, None
    )

    assert stored == {"run-2": raw_litellm_key}
    assert litellm_key_id != raw_litellm_key  # the DB-bound value is never the secret


def test_provision_keys_waits_for_every_replica_after_rotating_a_db_model_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner decision 2026-09-03: every key-minting path (launch / discovery / judge) waits
    for the rotated key to be served by EVERY gateway replica before handing it out. The
    wait must come AFTER the rotation, use the run's own scoped key as the bearer, and
    target the run's alias."""
    order: list[str] = []
    seen: dict[str, Any] = {}
    monkeypatch.setattr(run_launch, "_fetch_openrouter_provisioning_key", lambda: "prov-key")
    monkeypatch.setattr(run_launch, "gateway_base_url", lambda: "http://gateway.local")
    monkeypatch.setattr(run_launch, "gateway_api_key", lambda: "master-key")
    monkeypatch.setattr(gateway_admin, "generate_key", lambda *a, **k: ("sk-run-raw", "kid"))
    monkeypatch.setattr(openrouter_admin, "mint_key", lambda *a, **k: ("sk-or-raw", "or-hash"))
    monkeypatch.setattr(run_launch, "run_key_cache", mock.Mock())

    def _register(*a: Any, **k: Any) -> str:
        order.append("register")
        return "mid"

    monkeypatch.setattr(gateway_admin, "ensure_model_registered", _register)
    monkeypatch.setattr(gateway_admin, "rotate_model_key", lambda *a, **k: order.append("rotate"))

    def _await(base: str, bearer: str, alias: str, **kw: Any) -> int:
        order.append("await")
        seen.update(base=base, bearer=bearer, alias=alias)
        return 11

    monkeypatch.setattr(gateway_admin, "await_alias_served", _await)

    run_launch._provision_keys(
        "run-3", "custom_minimal", "qwen3-coder-next-custom_minimal", 10.0, None, None
    )

    assert order == ["register", "rotate", "await"]
    assert seen == {
        "base": "http://gateway.local",
        "bearer": "sk-run-raw",
        "alias": "qwen3-coder-next-custom_minimal",
    }


def test_provision_keys_does_not_wait_for_a_non_rotatable_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A yaml-declared alias is never rotated, so there is no rotation to wait for."""
    monkeypatch.setattr(run_launch, "_fetch_openrouter_provisioning_key", lambda: "prov-key")
    monkeypatch.setattr(run_launch, "gateway_base_url", lambda: "http://gateway.local")
    monkeypatch.setattr(run_launch, "gateway_api_key", lambda: "master-key")
    monkeypatch.setattr(gateway_admin, "generate_key", lambda *a, **k: ("sk-run-raw", "kid"))
    monkeypatch.setattr(openrouter_admin, "mint_key", lambda *a, **k: ("sk-or-raw", "or-hash"))
    monkeypatch.setattr(run_launch, "run_key_cache", mock.Mock())
    waited: list[str] = []

    def _await(*a: Any, **k: Any) -> int:
        waited.append(a[2])
        return 1

    monkeypatch.setattr(gateway_admin, "await_alias_served", _await)
    run_launch._provision_keys("run-4", "custom_minimal", "cheap-oss-model", 10.0, None, None)
    assert waited == []


# ---------------------------------------------------------------------------
# POST /runs response shapes (§3.1)
# ---------------------------------------------------------------------------


def test_post_runs_duplicate_returns_flat_409_body() -> None:
    """§3.1: "The 409 must carry the existing run_id... the id is the payload,
    not decoration" — the body must be FLAT, not FastAPI's default
    HTTPException {"detail": ...} wrapper."""
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app
    from swebench_eval.orchestrator.control_plane.run_launch import DuplicateRunError

    with mock.patch(
        "swebench_eval.orchestrator.api.run_launch_routes.launch",
        side_effect=DuplicateRunError("existing-run-42", "custom_minimal", "cheap-oss-model"),
    ):
        client = TestClient(app)
        resp = client.post(
            "/runs",
            json={
                "instance_ids": "all",
                "harness": "custom_minimal",
                "model_alias": "cheap-oss-model",
                "budget_cap_usd": 10.0,
            },
        )

    assert resp.status_code == 409
    body = resp.json()
    assert body == {
        "status": "duplicate",
        "run_id": "existing-run-42",
        "harness": "custom_minimal",
        "model_alias": "cheap-oss-model",
        "message": "a run is already in progress: existing-run-42",
    }
    assert "detail" not in body, "409 body must be flat, not HTTPException's {'detail': ...}"


def test_launch_run_refuses_before_claim_when_gateway_globally_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §4/§9.4: refuse
    the launch — never mint anything, never write a CLAIM row — while the
    ``gateway`` pool is globally paused. Asserts ``_claim`` is never even
    called, not just that the exception propagates."""
    from swebench_eval.control import state as control_state

    monkeypatch.setattr(control_state, "is_paused", lambda pool: pool == "gateway")
    claim_calls: list[Any] = []
    monkeypatch.setattr(run_launch, "_claim", lambda *a, **k: claim_calls.append(a))

    with pytest.raises(run_launch.GatewayPausedError, match="globally paused"):
        run_launch.launch_run(
            "run-refused-1",
            instances=[],
            config=RunConfig(harness="custom_minimal", model_alias="cheap-oss-model"),
            budget_cap_usd=10.0,
        )
    assert claim_calls == [], "must refuse BEFORE claim — nothing minted, nothing written"


def test_launch_run_proceeds_when_gateway_not_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal is specific to the gateway pool being paused — an
    unrelated pool's pause state (or none) must not block a launch here."""
    from swebench_eval.control import state as control_state

    monkeypatch.setattr(control_state, "is_paused", lambda pool: False)

    class _ReachedClaim(Exception):
        pass

    def _fake_claim(*a: Any, **k: Any) -> None:
        raise _ReachedClaim

    monkeypatch.setattr(run_launch, "_claim", _fake_claim)

    with pytest.raises(_ReachedClaim):
        run_launch.launch_run(
            "run-refused-2",
            instances=[],
            config=RunConfig(harness="custom_minimal", model_alias="cheap-oss-model"),
            budget_cap_usd=10.0,
        )


def test_post_runs_gateway_paused_returns_503() -> None:
    """D4-style fail closed: refused for an operational reason, not a bad
    request — same status/shape as the existing provisioning-key refusal."""
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app
    from swebench_eval.orchestrator.control_plane.run_launch import GatewayPausedError

    with mock.patch(
        "swebench_eval.orchestrator.api.run_launch_routes.launch",
        side_effect=GatewayPausedError("gateway is globally paused — refusing to launch"),
    ):
        client = TestClient(app)
        resp = client.post(
            "/runs",
            json={
                "instance_ids": "all",
                "harness": "custom_minimal",
                "model_alias": "cheap-oss-model",
                "budget_cap_usd": 10.0,
            },
        )
    assert resp.status_code == 503
    assert "paused" in resp.json()["message"]


def test_post_runs_no_provisioning_key_returns_503() -> None:
    """D4: fail closed — refused for an infra/config reason, not a bad request."""
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app
    from swebench_eval.orchestrator.control_plane.run_launch import NoProvisioningKeyError

    with mock.patch(
        "swebench_eval.orchestrator.api.run_launch_routes.launch",
        side_effect=NoProvisioningKeyError("no provisioning key"),
    ):
        client = TestClient(app)
        resp = client.post(
            "/runs",
            json={
                "instance_ids": "all",
                "harness": "custom_minimal",
                "model_alias": "cheap-oss-model",
                "budget_cap_usd": 10.0,
            },
        )
    assert resp.status_code == 503


def test_post_runs_success_returns_201_with_run_id() -> None:
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app
    from swebench_eval.orchestrator.api.schemas import RunLaunchResponse

    with mock.patch(
        "swebench_eval.orchestrator.api.run_launch_routes.launch",
        return_value=RunLaunchResponse(run_id="new-run-1", dispatched=3, seeded=3),
    ):
        client = TestClient(app)
        resp = client.post(
            "/runs",
            json={
                "instance_ids": ["a", "b", "c"],
                "harness": "custom_minimal",
                "model_alias": "cheap-oss-model",
                "budget_cap_usd": 10.0,
            },
        )
    assert resp.status_code == 201
    assert resp.json() == {
        "status": "launched",
        "run_id": "new-run-1",
        "dispatched": 3,
        "seeded": 3,
    }


def test_context_window_tokens_defaults_to_none_not_sent() -> None:
    """§3.1 warning: absent from the request -> None ("resolve"), never a
    value the client didn't send — the exact defect that made
    context_window_source='run_config' on every Stage 6 run."""
    from swebench_eval.orchestrator.api.schemas import RunLaunchRequest

    req = RunLaunchRequest(
        instance_ids="all",
        harness="custom_minimal",
        model_alias="cheap-oss-model",
        budget_cap_usd=10.0,
        # the field's default (Field(None, ...)); spelled out because mypy's dataclass_transform
        # view of pydantic does not see a positional Field default as one
        harness_instructions=None,
    )
    assert req.context_window_tokens is None


# ---------------------------------------------------------------------------
# Ledger emits (D6)
# ---------------------------------------------------------------------------


def test_harness_dispatcher_emits_dispatched_after_run_task_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D6: DISPATCHED is emitted onto `results`, phase=harness, no artifacts,
    AFTER RunTask succeeds — never before (a failed launch must not claim a
    dispatch that didn't happen)."""
    from swebench_eval.orchestrator.control_plane import harness_dispatcher as disp
    from swebench_eval.queue.schemas import HarnessJob

    sent: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(disp, "send_message", lambda q, body: sent.append((q, body)))

    job = HarnessJob(
        run_id="run-x",
        instance_id="inst-x",
        repo_url="https://github.com/x/y",
        base_commit="c",
        problem_statement="p",
        attempt_number=1,
        harness_name="custom_minimal",
        model_alias="cheap-oss-model",
    )
    disp._emit_dispatched(job)

    assert len(sent) == 1
    queue, body = sent[0]
    assert queue == "results"
    assert body["run_id"] == "run-x"
    assert body["instance_id"] == "inst-x"
    assert body["attempt_number"] == 1
    assert body["phase"] == "harness"
    assert body["state"] == "DISPATCHED"
    assert not body.get("patch_s3_key") and not body.get("trajectory_s3_key")


def test_emit_dispatched_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort: a launch that already succeeded must not be reported as
    failed because the ledger notice couldn't be sent."""
    from swebench_eval.orchestrator.control_plane import harness_dispatcher as disp
    from swebench_eval.queue.schemas import HarnessJob

    monkeypatch.setattr(
        disp, "send_message", mock.Mock(side_effect=RuntimeError("SQS unreachable"))
    )
    job = HarnessJob(
        run_id="run-x",
        instance_id="inst-x",
        repo_url="u",
        base_commit="c",
        problem_statement="p",
        attempt_number=1,
        harness_name="custom_minimal",
        model_alias="cheap-oss-model",
    )
    disp._emit_dispatched(job)  # must not raise


# ---------------------------------------------------------------------------
# The reaper — rule 1 (DLQ) and rule 3 (never-dispatched)
# ---------------------------------------------------------------------------


def test_dlq_message_is_persisted_before_being_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    """§7 rule 1: "Persist the body before deleting" — order matters; asserted
    directly via a call-order list, not just that both happened."""
    from swebench_eval.orchestrator.control_plane import results_writer as rw

    calls: list[str] = []

    def _persist(dlq: str, run_id: str, inst: str, body: dict[str, Any]) -> str:
        calls.append("persist")
        return "runs/x/dlq/key.json"

    monkeypatch.setattr(rw, "_persist_dlq_body", _persist)
    monkeypatch.setattr(rw, "_emit_reap_result", lambda *a, **k: calls.append("emit"))
    monkeypatch.setattr(rw, "delete_message", lambda q, h: calls.append("delete"))

    msg = {
        "body": {"run_id": "run-1", "instance_id": "inst-1", "attempt_number": 1},
        "receipt_handle": "h1",
        "attributes": {"ApproximateReceiveCount": "3"},
    }
    rw._process_dlq_message("harness-jobs-dlq", "harness", msg)

    assert calls == ["persist", "emit", "delete"]


def test_dlq_message_without_run_id_is_persisted_but_not_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An un-attributable dead-lettered message must still be evidence (S3),
    but there is no run_id to emit a ResultMessage against."""
    from swebench_eval.orchestrator.control_plane import results_writer as rw

    calls: list[str] = []

    def _persist(*a: Any, **k: Any) -> str:
        calls.append("persist")
        return "k.json"

    monkeypatch.setattr(rw, "_persist_dlq_body", _persist)
    monkeypatch.setattr(rw, "_emit_reap_result", lambda *a, **k: calls.append("emit"))
    monkeypatch.setattr(rw, "delete_message", lambda q, h: calls.append("delete"))

    msg = {"body": {}, "receipt_handle": "h1", "attributes": {}}
    rw._process_dlq_message("harness-jobs-dlq", "harness", msg)

    assert calls == ["persist", "delete"]  # no "emit" — nothing to attribute it to


class _FakeCursor:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, *a: Any, **k: Any) -> None:
        pass

    def fetchall(self) -> list[Any]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)


def _rule3_setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    visible: int = 0,
    not_visible: int = 0,
    running: set[str] | None = None,
    progress: object | None = None,
) -> list[tuple[Any, ...]]:
    """Common rule-3 test wiring: no pause, a fresh tracking dict (module-level
    state must not leak between tests), queue depth, and both liveness checks
    (default: nothing alive). Returns the list _emit_reap_result appends to."""
    from swebench_eval.control import state as control_state
    from swebench_eval.orchestrator.control_plane import run_supervisor as rw
    from swebench_eval.queue import client as queue_client

    monkeypatch.setattr(control_state, "is_paused", lambda pool: False)
    monkeypatch.setattr(rw, "_never_dispatched_empty_since", {})
    monkeypatch.setattr(
        queue_client,
        "get_queue_depth",
        lambda name: queue_client.QueueDepth(
            visible=visible, not_visible=not_visible, oldest_age_s=None
        ),
    )
    monkeypatch.setattr(rw, "_running_instance_ids_for_run", lambda run_id: running or set())
    monkeypatch.setattr(
        "swebench_eval.database.redis_client.read_progress", lambda *a, **k: progress
    )
    emitted: list[tuple[Any, ...]] = []
    monkeypatch.setattr(rw, "_emit_reap_result", lambda *a, **k: emitted.append(a))
    return emitted


def test_never_dispatched_rule_requires_a_drained_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """§7 rule 3: "with two runs live this simply will not fire. That is the
    correct direction to be wrong in." — a non-empty queue must refuse to
    conclude never-dispatched."""
    from swebench_eval.orchestrator.control_plane import run_supervisor as rw

    emitted = _rule3_setup(monkeypatch, visible=2)
    rw._reap_never_dispatched(_FakeConn([("inst-1", 1)]), "run-1")

    assert emitted == [], "must not conclude never-dispatched while the queue still has messages"


def test_never_dispatched_rule_does_not_fire_on_first_empty_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-08-29 hardening: one empty-queue read is no longer sufficient on
    its own — the live incident this pins was reaped 21s after a real
    dispatch off exactly one such read. The first sighting must only start
    the tracking clock, never reap immediately."""
    from swebench_eval.orchestrator.control_plane import run_supervisor as rw

    emitted = _rule3_setup(monkeypatch)
    rw._reap_never_dispatched(_FakeConn([("inst-1", 1)]), "run-1")

    assert emitted == []
    assert ("run-1", "inst-1", 1) in rw._never_dispatched_empty_since


def test_never_dispatched_rule_fires_after_consecutive_empty_window_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ticks, both empty, with the second past
    _NEVER_DISPATCHED_MIN_CONSECUTIVE_EMPTY_S: only then does it reap — and
    only because both liveness checks (ECS, progress key) also confirm
    nothing is alive."""
    from swebench_eval.orchestrator.control_plane import run_supervisor as rw

    emitted = _rule3_setup(monkeypatch)
    conn = _FakeConn([("inst-1", 1)])

    t0 = 1_000.0
    monkeypatch.setattr(time, "monotonic", lambda: t0)
    rw._reap_never_dispatched(conn, "run-1")
    assert emitted == []

    monkeypatch.setattr(
        time, "monotonic", lambda: t0 + rw._NEVER_DISPATCHED_MIN_CONSECUTIVE_EMPTY_S + 1
    )
    rw._reap_never_dispatched(conn, "run-1")

    assert len(emitted) == 1
    assert emitted[0][:5] == ("run-1", "inst-1", 1, "harness", "NEVER_DISPATCHED")
    assert ("run-1", "inst-1", 1) not in rw._never_dispatched_empty_since


def test_never_dispatched_rule_still_waiting_before_window_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second empty tick that hasn't yet reached the consecutive-window
    threshold must not reap either — not just the very first sighting."""
    from swebench_eval.orchestrator.control_plane import run_supervisor as rw

    emitted = _rule3_setup(monkeypatch)
    conn = _FakeConn([("inst-1", 1)])

    t0 = 2_000.0
    monkeypatch.setattr(time, "monotonic", lambda: t0)
    rw._reap_never_dispatched(conn, "run-1")
    monkeypatch.setattr(
        time, "monotonic", lambda: t0 + rw._NEVER_DISPATCHED_MIN_CONSECUTIVE_EMPTY_S - 1
    )
    rw._reap_never_dispatched(conn, "run-1")

    assert emitted == []


def test_never_dispatched_rule_resets_on_a_non_empty_read_between_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty read between two empty ones must restart the consecutive
    count, not let the earlier (now-stale) sighting carry it across the gap."""
    from swebench_eval.orchestrator.control_plane import run_supervisor as rw
    from swebench_eval.queue import client as queue_client

    emitted = _rule3_setup(monkeypatch)
    conn = _FakeConn([("inst-1", 1)])

    t0 = 3_000.0
    monkeypatch.setattr(time, "monotonic", lambda: t0)
    rw._reap_never_dispatched(conn, "run-1")  # first empty sighting, recorded

    monkeypatch.setattr(time, "monotonic", lambda: t0 + 10)
    monkeypatch.setattr(
        queue_client,
        "get_queue_depth",
        lambda name: queue_client.QueueDepth(visible=1, not_visible=0, oldest_age_s=None),
    )
    rw._reap_never_dispatched(conn, "run-1")  # real work appears — must reset
    assert ("run-1", "inst-1", 1) not in rw._never_dispatched_empty_since

    # Empty again, well past the ORIGINAL t0 window, but only moments after
    # the reset — must NOT fire off the stale t0 sighting.
    monkeypatch.setattr(
        queue_client,
        "get_queue_depth",
        lambda name: queue_client.QueueDepth(visible=0, not_visible=0, oldest_age_s=None),
    )
    monkeypatch.setattr(time, "monotonic", lambda: t0 + 11)
    rw._reap_never_dispatched(conn, "run-1")

    assert emitted == [], "must count from the reset, not from the pre-reset sighting"


def test_never_dispatched_rule_blocked_by_a_live_ecs_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even past the age + consecutive-empty window, a real ECS task
    (startedBy=run_id, matching rule 2's own check) must block the reap —
    this is exactly the check that would have caught the live incident."""
    from swebench_eval.orchestrator.control_plane import run_supervisor as rw

    emitted = _rule3_setup(monkeypatch, running={"inst-1"})
    conn = _FakeConn([("inst-1", 1)])

    t0 = 4_000.0
    monkeypatch.setattr(time, "monotonic", lambda: t0)
    rw._reap_never_dispatched(conn, "run-1")
    monkeypatch.setattr(
        time, "monotonic", lambda: t0 + rw._NEVER_DISPATCHED_MIN_CONSECUTIVE_EMPTY_S + 1
    )
    rw._reap_never_dispatched(conn, "run-1")

    assert emitted == []


def test_never_dispatched_rule_blocked_by_a_live_progress_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as the ECS check, for the other live signal rule 2 already
    trusts: a live Redis progress key blocks the reap."""
    from swebench_eval.orchestrator.control_plane import run_supervisor as rw

    emitted = _rule3_setup(monkeypatch, progress={"cumulative_tokens": 1})
    conn = _FakeConn([("inst-1", 1)])

    t0 = 5_000.0
    monkeypatch.setattr(time, "monotonic", lambda: t0)
    rw._reap_never_dispatched(conn, "run-1")
    monkeypatch.setattr(
        time, "monotonic", lambda: t0 + rw._NEVER_DISPATCHED_MIN_CONSECUTIVE_EMPTY_S + 1
    )
    rw._reap_never_dispatched(conn, "run-1")

    assert emitted == []


def test_never_dispatched_rule_skipped_while_harness_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7 preconditions: "the run is not paused... A paused pool legitimately
    produces nothing, and rule 2 [and rule 3] would read that as mass death."""
    from swebench_eval.control import state as control_state
    from swebench_eval.orchestrator.control_plane import run_supervisor as rw

    monkeypatch.setattr(control_state, "is_paused", lambda pool: pool == "harness")
    called = {"cursor": False}

    class _FakeConn:
        def cursor(self):
            called["cursor"] = True
            raise AssertionError("must not query while harness is paused")

    rw._reap_never_dispatched(_FakeConn(), "run-1")
    assert not called["cursor"]


# ---------------------------------------------------------------------------
# Gateway alias/pricing consistency (§5.1's own stated trap)
# ---------------------------------------------------------------------------


def test_every_rotatable_alias_has_a_pricing_entry() -> None:
    """§5.1: "Every new alias needs a pricing.py entry in the same change... a
    missing one falls to the $1/$5 unknown default, overstates cost ~15x, and
    has already tripped the $5/instance ceiling on a false reading."""
    for alias in ROTATABLE_MODELS:
        assert alias in pricing.MODEL_PRICING, f"{alias} missing from MODEL_PRICING"
        assert alias in pricing.MODEL_CACHE_READ_RATIOS, (
            f"{alias} missing from MODEL_CACHE_READ_RATIOS (falls back to the "
            "model-agnostic default, understating cost ~2x for laguna-shaped models)"
        )


def test_rotatable_model_spec_never_carries_a_real_looking_shared_key() -> None:
    """The placeholder api_key in rotatable_models.py must be obviously fake
    and get replaced before any request can use it — a copy-paste of a real
    key into this module would be exactly the leak rule 3 forbids."""
    for spec in ROTATABLE_MODELS.values():
        assert "unset-pending-rotation" in str(spec.litellm_params.get("api_key", ""))


# ---------------------------------------------------------------------------
# Per-run key wiring to the harness task (ADR-0035 decision 1)
# ---------------------------------------------------------------------------


def test_gateway_api_key_prefers_litellm_api_key_over_master(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A harness task's env carries BOTH (LITELLM_API_KEY from containerOverrides,
    LITELLM_MASTER_KEY never actually reaches it post-cutover, but the
    function must prefer the per-run key even if both happen to be present).

    Mutation-check: swap the `or` for `os.environ.get("LITELLM_MASTER_KEY", ...)
    or os.environ.get("LITELLM_API_KEY")` (master-first) — this test then fails
    (returns the master key instead of the per-run one). Verified by hand,
    reverted.
    """
    from swebench_eval.harnesses import routing

    monkeypatch.setenv("LITELLM_API_KEY", "sk-per-run-abc")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-admin-master")
    assert routing.gateway_api_key() == "sk-per-run-abc"


def test_gateway_api_key_falls_back_to_master_when_no_per_run_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator's own admin calls (dispatcher.py, run_launch.py) rely
    on exactly this fallback — their task environment never carries
    LITELLM_API_KEY, only LITELLM_MASTER_KEY."""
    from swebench_eval.harnesses import routing

    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-admin-master")
    assert routing.gateway_api_key() == "sk-admin-master"


def test_agent_env_deny_strips_raw_litellm_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same discipline as LITELLM_MASTER_KEY: the raw per-run key must not
    leak through the generic subprocess-env passthrough — each adapter still
    gets it via its own explicit re-injection, never by this var name
    surviving untouched.

    Mutation-check: remove "LITELLM_API_KEY" from _AGENT_ENV_DENY — this test
    then fails (the raw key appears in agent_environment()'s output).
    Verified by hand, reverted.
    """
    from swebench_eval.harnesses import routing

    monkeypatch.setenv("LITELLM_API_KEY", "sk-per-run-abc")
    env = routing.agent_environment()
    assert "LITELLM_API_KEY" not in env


def test_job_reference_round_trips_litellm_api_key() -> None:
    """to_env() -> from_env() must carry the per-run key through unchanged —
    this IS the transport the harness task depends on to ever see it."""
    from swebench_eval.queue.schemas import JobReference

    ref = JobReference(
        run_id="run-1",
        instance_id="inst-1",
        attempt_number=1,
        receipt_handle="h1",
        harness_name="custom_minimal",
        model_alias="cheap-oss-model",
        timeout_seconds=600,
        max_tokens_per_instance=500_000,
        max_cost_usd_per_instance=5.0,
        litellm_api_key="sk-per-run-abc",
    )
    environ = {e["name"]: e["value"] for e in ref.to_env()}
    assert environ["LITELLM_API_KEY"] == "sk-per-run-abc"
    restored = JobReference.from_env(environ)
    assert restored.litellm_api_key == "sk-per-run-abc"


def test_job_reference_litellm_api_key_absent_is_none() -> None:
    """A reference built with no cached key (litellm_api_key=None) must
    round-trip to None, never an empty string or a KeyError — the worker's
    gateway_api_key() fallback depends on actually seeing None/absent."""
    from swebench_eval.queue.schemas import JobReference

    ref = JobReference(
        run_id="run-1",
        instance_id="inst-1",
        attempt_number=1,
        receipt_handle="h1",
        harness_name="custom_minimal",
        model_alias="cheap-oss-model",
        timeout_seconds=600,
        max_tokens_per_instance=500_000,
        max_cost_usd_per_instance=5.0,
    )
    environ = {e["name"]: e["value"] for e in ref.to_env()}
    assert environ["LITELLM_API_KEY"] == ""
    restored = JobReference.from_env(environ)
    assert restored.litellm_api_key is None


def test_reference_for_carries_the_cached_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from swebench_eval.orchestrator.control_plane import harness_dispatcher as disp
    from swebench_eval.orchestrator.control_plane import run_key_cache
    from swebench_eval.queue.schemas import HarnessJob

    monkeypatch.setattr(run_key_cache, "fetch", lambda run_id: "sk-per-run-abc")
    job = HarnessJob(
        run_id="run-1",
        instance_id="inst-1",
        repo_url="u",
        base_commit="c",
        problem_statement="p",
        attempt_number=1,
        harness_name="custom_minimal",
        model_alias="cheap-oss-model",
    )
    ref = disp._reference_for(job, "receipt-1")
    assert ref.litellm_api_key == "sk-per-run-abc"


def test_reference_for_refuses_when_enforced_and_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """D4's spirit extended to dispatch: with ENFORCE_PER_RUN_KEY=1, a job with
    no cached key must never launch with the master key silently.

    Mutation-check: drop the `if _enforce_per_run_key(): raise ...` branch —
    this test then fails (no exception is raised, the reference is built with
    litellm_api_key=None instead). Verified by hand, reverted.
    """
    from swebench_eval.orchestrator.control_plane import harness_dispatcher as disp
    from swebench_eval.orchestrator.control_plane import run_key_cache
    from swebench_eval.queue.schemas import HarnessJob

    monkeypatch.setattr(run_key_cache, "fetch", lambda run_id: None)
    monkeypatch.setenv("ENFORCE_PER_RUN_KEY", "1")
    job = HarnessJob(
        run_id="run-1",
        instance_id="inst-1",
        repo_url="u",
        base_commit="c",
        problem_statement="p",
        attempt_number=1,
        harness_name="custom_minimal",
        model_alias="cheap-oss-model",
    )
    with pytest.raises(disp.DispatchRefusedError, match="no per-run LiteLLM key cached"):
        disp._reference_for(job, "receipt-1")


def test_reference_for_warns_and_continues_when_not_enforced(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The default (ENFORCE_PER_RUN_KEY unset): a missing cached key degrades
    to the master-key fallback with a loud warning, never a hard refusal —
    this is what keeps local/dev/legacy dispatch paths working during
    rollout."""
    from swebench_eval.orchestrator.control_plane import harness_dispatcher as disp
    from swebench_eval.orchestrator.control_plane import run_key_cache
    from swebench_eval.queue.schemas import HarnessJob

    monkeypatch.setattr(run_key_cache, "fetch", lambda run_id: None)
    monkeypatch.delenv("ENFORCE_PER_RUN_KEY", raising=False)
    job = HarnessJob(
        run_id="run-1",
        instance_id="inst-1",
        repo_url="u",
        base_commit="c",
        problem_statement="p",
        attempt_number=1,
        harness_name="custom_minimal",
        model_alias="cheap-oss-model",
    )
    with caplog.at_level(logging.WARNING):
        ref = disp._reference_for(job, "receipt-1")
    assert ref.litellm_api_key is None
    assert any("no per-run LiteLLM key cached" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# scripts/reconcile_gateway_models.py
# ---------------------------------------------------------------------------


def test_reconcile_gateway_models_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second run must register nothing new — find_db_model_id (not
    ensure_model_registered) is the existence check, so a call that already
    exists never re-creates it.

    Mutation-check: call ensure_model_registered unconditionally (skip the
    find_db_model_id short-circuit) — this test then fails (register_calls
    grows on the second pass instead of staying empty). Verified by hand,
    reverted.
    """
    from scripts.reconcile_gateway_models import reconcile

    registered: dict[str, str] = {}
    register_calls: list[str] = []

    def _find(base_url: str, master_key: str, alias: str) -> str | None:
        return registered.get(alias)

    def _register(
        base_url: str,
        master_key: str,
        alias: str,
        params: dict[str, object],
        info: dict[str, object],
    ) -> str:
        register_calls.append(alias)
        model_id = f"id-{alias}"
        registered[alias] = model_id
        return model_id

    monkeypatch.setattr(gateway_admin, "find_db_model_id", _find)
    monkeypatch.setattr(gateway_admin, "ensure_model_registered", _register)

    first = reconcile("http://gateway.local", "master-key", dry_run=False)
    assert set(first) == set(ROTATABLE_MODELS)
    assert len(register_calls) == len(ROTATABLE_MODELS)

    register_calls.clear()
    second = reconcile("http://gateway.local", "master-key", dry_run=False)
    assert second == first
    assert register_calls == [], "second reconcile re-registered an already-existing alias"


def test_reconcile_gateway_models_dry_run_makes_no_change(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.reconcile_gateway_models import reconcile

    register_calls: list[Any] = []

    def _register(*a: Any, **k: Any) -> str:
        register_calls.append(a)
        return "unexpected"

    monkeypatch.setattr(gateway_admin, "find_db_model_id", lambda *a, **k: None)
    monkeypatch.setattr(gateway_admin, "ensure_model_registered", _register)

    results = reconcile("http://gateway.local", "master-key", dry_run=True)
    assert results == {}
    assert register_calls == [], "dry-run must never call ensure_model_registered"
