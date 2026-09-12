"""Bring-up 2026-09-03: the discovery alias outlived its upstream OpenRouter key.

``ensure_discovery_alias_registered`` minted the alias's real OpenRouter key exactly once, at
first creation, and trusted it forever. The alias persisted in LiteLLM's Aurora db across the
teardown; the key was disabled outside the framework; the qwen probe's first batch was twelve
upstream ``401 "User not found"`` and the run raised without touching the provider.

Owner decision: a discovery key never outlives its probe — every probe mints a fresh upstream
key and ``discover_ceiling`` disables it in its ``finally``, the per-run / per-judge lifecycle.
"""

from __future__ import annotations

import pytest

from swebench_eval.gateway import admin as gateway_admin
from swebench_eval.gateway import openrouter_admin
from swebench_eval.orchestrator.control_plane import ceiling_discovery as cd
from swebench_eval.orchestrator.control_plane import run_launch


class _Calls:
    def __init__(self) -> None:
        self.minted: list[str] = []
        self.rotated: list[tuple[str, str]] = []
        self.waited: list[str] = []
        self.disabled: list[tuple[str, str]] = []


def _wire(monkeypatch: pytest.MonkeyPatch, *, exists: bool) -> _Calls:
    calls = _Calls()
    monkeypatch.setattr(cd, "gateway_base_url", lambda: "http://g")
    monkeypatch.setattr(cd, "gateway_api_key", lambda: "mk")
    monkeypatch.setattr(cd, "upstream_model_for", lambda alias: "qwen/qwen3-coder-next")
    monkeypatch.setattr(gateway_admin, "find_db_model_id", lambda *a: "mid" if exists else None)
    monkeypatch.setattr(gateway_admin, "ensure_model_registered", lambda *a, **k: "mid")
    monkeypatch.setattr(
        gateway_admin,
        "rotate_model_key",
        lambda b, m, alias, mid, raw, **k: calls.rotated.append((alias, raw)),
    )
    monkeypatch.setattr(run_launch, "_fetch_openrouter_provisioning_key", lambda: "prov")

    def _mint(prov: str, *, name: str, limit_usd: float) -> tuple[str, str]:
        calls.minted.append(name)
        return ("sk-or-new", "hash-1")

    monkeypatch.setattr(openrouter_admin, "mint_key", _mint)
    monkeypatch.setattr(
        openrouter_admin,
        "disable_key",
        lambda prov, key_hash: calls.disabled.append((prov, key_hash)),
    )
    monkeypatch.setattr(
        cd, "_wait_for_alias_ready", lambda b, m, alias, **k: calls.waited.append(alias)
    )
    return calls


@pytest.mark.parametrize("exists", [False, True])
def test_every_probe_mints_a_fresh_upstream_key_and_waits_for_it(
    monkeypatch: pytest.MonkeyPatch, exists: bool
) -> None:
    """New alias or pre-existing alias: same path — mint, rotate, wait. No 'minted once' branch
    is left for a stale key to hide behind."""
    calls = _wire(monkeypatch, exists=exists)
    lease = cd.ensure_discovery_alias_registered("qwen3-coder-next")
    assert lease.alias == "qwen3-coder-next-ceiling-discovery"
    assert lease.openrouter_key_hash == "hash-1"
    assert len(calls.minted) == 1
    assert calls.rotated == [(lease.alias, "sk-or-new")]
    assert calls.waited == [lease.alias]
    assert calls.disabled == []  # disabling is discover_ceiling's finally, not registration's


def test_key_name_is_suffixed_per_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Names are dashboard labels (OpenRouter's identity is the hash, no uniqueness enforced);
    a per-probe suffix keeps successive — including disabled — keys tellable apart."""
    calls = _wire(monkeypatch, exists=True)
    cd.ensure_discovery_alias_registered("qwen3-coder-next")
    cd.ensure_discovery_alias_registered("qwen3-coder-next")
    a, b = calls.minted
    assert a.startswith("ceiling-discovery-qwen3-coder-next-")
    assert b.startswith("ceiling-discovery-qwen3-coder-next-")
    assert a != b
    assert len(a) == len("ceiling-discovery-qwen3-coder-next-") + 8


def test_disable_uses_the_provisioning_key_and_the_leased_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _wire(monkeypatch, exists=True)
    lease = cd.DiscoveryUpstreamKey(alias="a-ceiling-discovery", openrouter_key_hash="h-9")
    cd.disable_discovery_upstream_key(lease)
    assert calls.disabled == [("prov", "h-9")]


def test_disable_never_raises_but_logs_the_hash_for_manual_cleanup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed disable must not mask the probe's own outcome — but the key is then live with
    budget on it, so it is an ERROR naming the hash."""
    _wire(monkeypatch, exists=True)
    lease = cd.DiscoveryUpstreamKey(alias="a-ceiling-discovery", openrouter_key_hash="h-9")

    def _boom(prov: str, key_hash: str) -> None:
        raise openrouter_admin.OpenRouterAdminError("PATCH /keys/h-9 -> 500")

    monkeypatch.setattr(openrouter_admin, "disable_key", _boom)
    with caplog.at_level("ERROR"):
        cd.disable_discovery_upstream_key(lease)  # no raise
    assert any(
        "MANUAL CLEANUP" in r.getMessage() and "h-9" in r.getMessage() for r in caplog.records
    )

    monkeypatch.setattr(
        run_launch,
        "_fetch_openrouter_provisioning_key",
        lambda: (_ for _ in ()).throw(run_launch.NoProvisioningKeyError("none")),
    )
    caplog.clear()
    with caplog.at_level("ERROR"):
        cd.disable_discovery_upstream_key(lease)  # no raise
    assert any(
        "no provisioning key" in r.getMessage() and "h-9" in r.getMessage() for r in caplog.records
    )
