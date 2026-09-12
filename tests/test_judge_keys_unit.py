"""Unit tests — judge_keys.py (offline-analysis-design.md §10.2).

Fast, hermetic, no live Postgres/gateway — every boundary is mocked. The
mutex's genuine-constraint property lives in
test_judge_keys_lock_integration.py (needs a real UNIQUE violation).
"""

from __future__ import annotations

from typing import Any

import pytest

from swebench_eval.analysis import judge_keys
from swebench_eval.gateway import admin as gateway_admin
from swebench_eval.gateway import openrouter_admin


def _wire_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(judge_keys, "_fetch_openrouter_provisioning_key", lambda: "prov-key")
    monkeypatch.setattr(judge_keys, "gateway_base_url", lambda: "http://gateway.local")
    monkeypatch.setattr(judge_keys, "gateway_api_key", lambda: "master-key")
    # rotate->active probe hits the (mocked-away) gateway over real httpx —
    # stubbed here; its own behaviour is covered by TestAwaitRotatedKeyActive.
    monkeypatch.setattr(judge_keys, "_await_rotated_key_active", lambda base, raw: None)


def test_provision_pass_keys_never_logs_or_returns_a_leaked_raw_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """rule 3, applied to judge passes the same way test_run_launch_unit.py
    pins it for harness runs: never a raw key in a log or return-value
    field the caller would persist."""
    raw_litellm = "sk-litellm-SUPERSECRET-abc123"
    raw_or = "sk-or-v1-SUPERSECRET-xyz789"
    _wire_common(monkeypatch)
    monkeypatch.setattr(
        gateway_admin, "generate_key", lambda *a, **k: (raw_litellm, "litellm-key-id-1")
    )
    monkeypatch.setattr(openrouter_admin, "mint_key", lambda *a, **k: (raw_or, "or-hash-1"))
    monkeypatch.setattr(gateway_admin, "ensure_model_registered", lambda *a, **k: "model-id-1")
    monkeypatch.setattr(gateway_admin, "rotate_model_key", lambda *a, **k: None)

    with caplog.at_level("DEBUG"):
        raw_key, litellm_key_id, or_hash = judge_keys.provision_pass_keys("pass-1", 5.0)

    assert raw_key == raw_litellm  # returned ONCE, for this process's immediate use
    assert litellm_key_id == "litellm-key-id-1"
    assert or_hash == "or-hash-1"
    for record in caplog.records:
        assert raw_litellm not in record.getMessage()
        assert raw_or not in record.getMessage()


def test_provision_pass_keys_registers_and_rotates_judge_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_common(monkeypatch)
    calls: dict[str, Any] = {}
    monkeypatch.setattr(gateway_admin, "generate_key", lambda *a, **k: ("raw", "kid"))
    monkeypatch.setattr(openrouter_admin, "mint_key", lambda *a, **k: ("or-raw", "or-hash"))

    def _register(
        base: str,
        master: str,
        alias: str,
        litellm_params: dict[str, Any],
        model_info: dict[str, Any],
    ) -> str:
        calls["registered_alias"] = alias
        return "model-id-9"

    def _rotate(base: str, master: str, alias: str, model_id: str, new_key: str, **kw: Any) -> None:
        calls["rotated_alias"] = alias
        calls["rotated_model_id"] = model_id
        calls["rotated_key"] = new_key

    monkeypatch.setattr(gateway_admin, "ensure_model_registered", _register)
    monkeypatch.setattr(gateway_admin, "rotate_model_key", _rotate)

    judge_keys.provision_pass_keys("pass-2", 10.0)

    assert calls["registered_alias"] == "judge-model"
    assert calls["rotated_alias"] == "judge-model"
    assert calls["rotated_model_id"] == "model-id-9"
    assert calls["rotated_key"] == "or-raw"


def test_provision_pass_keys_fails_closed_with_no_provisioning_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from swebench_eval.orchestrator.control_plane.run_launch import NoProvisioningKeyError

    def _raise() -> str:
        raise NoProvisioningKeyError("no key")

    monkeypatch.setattr(judge_keys, "_fetch_openrouter_provisioning_key", _raise)
    with pytest.raises(NoProvisioningKeyError):
        judge_keys.provision_pass_keys("pass-3", 5.0)


def test_finalize_pass_keys_deletes_and_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_common(monkeypatch)
    calls: dict[str, Any] = {}
    monkeypatch.setattr(
        gateway_admin, "delete_key", lambda base, master, alias: calls.setdefault("deleted", alias)
    )
    monkeypatch.setattr(
        openrouter_admin,
        "disable_key",
        lambda prov, h: calls.setdefault("disabled_hash", h),
    )
    judge_keys.finalize_pass_keys("pass-4", "kid-1", "hash-1")
    assert calls["deleted"] == "pass-4"
    assert calls["disabled_hash"] == "hash-1"


def test_finalize_pass_keys_is_a_noop_with_no_ids() -> None:
    """Idempotent: finalizing a pass that never got keys (e.g. failed before
    provisioning) must not error."""
    judge_keys.finalize_pass_keys("pass-5", None, None)


def test_finalize_pass_keys_survives_missing_provisioning_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Same discipline as revoke_run_keys: a missing provisioning key at
    finalisation time logs loudly for manual cleanup rather than raising and
    leaving the LiteLLM-side delete undone."""
    from swebench_eval.orchestrator.control_plane.run_launch import NoProvisioningKeyError

    _wire_common(monkeypatch)
    monkeypatch.setattr(gateway_admin, "delete_key", lambda base, master, alias: None)

    def _raise() -> str:
        raise NoProvisioningKeyError("no key")

    monkeypatch.setattr(judge_keys, "_fetch_openrouter_provisioning_key", _raise)
    with caplog.at_level("ERROR"):
        judge_keys.finalize_pass_keys("pass-6", "kid", "hash")
    assert any("MANUAL CLEANUP" in r.getMessage() for r in caplog.records)


class TestAwaitRotatedKeyActive:
    """The rotate->active handoff (2026-09-02, judge task 55ce189a; tightened 2026-09-03,
    judge task judge-01788481792836629782): the first version returned on the first non-401,
    which proves ONE gateway replica — the judge then 401'd on the other. It now delegates to
    ``gateway_admin.await_alias_served`` (a streak of 200s spanning both replicas; its own
    behaviour is pinned in test_await_alias_served.py) and must pass the judge alias, the
    judge's raw key and its deadline through unchanged."""

    def test_delegates_to_the_shared_every_replica_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def _await(base: str, bearer: str, alias: str, **kw: Any) -> int:
            seen.update(base=base, bearer=bearer, alias=alias, **kw)
            return 11

        monkeypatch.setattr(gateway_admin, "await_alias_served", _await)
        judge_keys._await_rotated_key_active("http://gw/v1", "sk-raw", timeout_s=45.0)
        assert seen["base"] == "http://gw/v1"
        assert seen["bearer"] == "sk-raw"  # the pass's own scoped key, not the master key
        assert seen["alias"] == judge_keys.JUDGE_MODEL_ALIAS
        assert seen["timeout_s"] == 45.0

    def test_deadline_raises_instead_of_probing_forever(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _never(*a: Any, **kw: Any) -> int:
            raise RuntimeError("rotated judge-model key for 'judge-model' still not served")

        monkeypatch.setattr(gateway_admin, "await_alias_served", _never)
        with pytest.raises(RuntimeError, match="not served"):
            judge_keys._await_rotated_key_active("http://gw/v1", "sk-raw", timeout_s=30.0)


def test_cleanup_pass_releases_the_lock_and_revokes_both_keys_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-08: a killed judge task never reaches finalisation — cleanup_pass does the
    same three things by pass id alone, and reports what it found."""
    from swebench_eval.analysis import judge_keys as jk
    from swebench_eval.gateway import admin as gateway_admin

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(jk, "release_pass_lock", lambda conn, pid: calls.append(("lock", pid)))
    monkeypatch.setattr(jk, "gateway_base_url", lambda: "http://g/v1")
    monkeypatch.setattr(jk, "gateway_api_key", lambda: "master")
    monkeypatch.setattr(
        gateway_admin, "delete_key", lambda base, master, alias: calls.append(("litellm", alias))
    )
    monkeypatch.setattr(jk, "_fetch_openrouter_provisioning_key", lambda: "prov")
    monkeypatch.setattr(openrouter_admin, "find_key_hash", lambda prov, name: "hash-abc123")
    monkeypatch.setattr(
        openrouter_admin, "disable_key", lambda prov, h: calls.append(("openrouter", h))
    )

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return (1,)

    class _Conn:
        def cursor(self):
            return _Cur()

    report = jk.cleanup_pass(_Conn(), "judge-1")
    assert report["lock_held"] is True
    assert report["litellm_key"] == "deleted-or-absent"
    assert report["openrouter_key"].startswith("disabled (hash-abc")
    assert calls == [("lock", "judge-1"), ("litellm", "judge-1"), ("openrouter", "hash-abc123")]


def test_cleanup_pass_reports_failures_and_keeps_going(monkeypatch: pytest.MonkeyPatch) -> None:
    from swebench_eval.analysis import judge_keys as jk
    from swebench_eval.gateway import admin as gateway_admin

    monkeypatch.setattr(jk, "release_pass_lock", lambda conn, pid: None)
    monkeypatch.setattr(jk, "gateway_base_url", lambda: "http://g/v1")
    monkeypatch.setattr(jk, "gateway_api_key", lambda: "master")

    def _boom(*a):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(gateway_admin, "delete_key", _boom)
    monkeypatch.setattr(jk, "_fetch_openrouter_provisioning_key", lambda: "prov")
    monkeypatch.setattr(openrouter_admin, "find_key_hash", lambda prov, name: None)

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

    report = jk.cleanup_pass(_Conn(), "judge-2")
    assert report["lock_held"] is False
    assert report["litellm_key"].startswith("delete failed: gateway down")
    assert report["openrouter_key"].startswith("not found")


def test_find_key_hash_pages_through_the_provisioning_list(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    pages = {
        0: [{"name": f"k{i}", "hash": f"h{i}"} for i in range(100)],
        100: [{"name": "judge-9", "hash": "h-judge-9"}],
    }
    seen: list[int] = []

    def _get(url, headers=None, params=None, timeout=None):
        seen.append(params["offset"])
        return httpx.Response(200, json={"data": pages[params["offset"]]})

    monkeypatch.setattr(httpx, "get", _get)
    assert openrouter_admin.find_key_hash("prov", "judge-9") == "h-judge-9"
    assert seen == [0, 100]
    assert openrouter_admin.find_key_hash("prov", "nope") is None
