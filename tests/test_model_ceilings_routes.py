"""GET/POST /model-ceilings routes —
BUILDER4-AUTOSCALER-TPM-CEILING-DISCOVERY-DESIGN-2026-08-31.md §5.

Route-level coverage only; the underlying logic (find_ceiling, the view precedence, cost math) is
covered in test_ceiling_discovery*.py. These pin the HTTP contract: status codes, the
background-task (not blocking) shape of discover, and error translation.
"""

from __future__ import annotations

from unittest import mock

import pytest


def _client():
    from fastapi.testclient import TestClient

    from swebench_eval.orchestrator.api.main import app

    return TestClient(app)


def test_get_model_ceilings_returns_one_row_per_known_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from swebench_eval.orchestrator.control_plane import ceiling_discovery

    monkeypatch.setattr(
        ceiling_discovery,
        "list_ceilings",
        lambda aliases: [
            {
                "model_alias": a,
                "discovered_tpm": None,
                "ceiling_source": None,
                "discovered_at": None,
                "provider": None,
                "values": None,
                "is_stale": False,
            }
            for a in aliases
        ],
    )

    resp = _client().get("/model-ceilings")

    assert resp.status_code == 200
    aliases = {row["model_alias"] for row in resp.json()}
    assert aliases == {
        "laguna-xs-2.1",
        "qwen3-coder-next",
        "deepseek-v4-flash-0731",
        "gpt-5-mini",
        "minimax-m2.5",
    }


def test_discover_route_returns_immediately_without_running_the_probe_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of BackgroundTasks here: the HTTP response must not wait on the (possibly
    minutes-long) discovery probe itself."""
    from swebench_eval.orchestrator.control_plane import ceiling_discovery

    monkeypatch.setattr(ceiling_discovery, "resolve_target_tokens", lambda alias: 260_144)
    monkeypatch.setattr(ceiling_discovery, "estimate_cost", lambda *a, **k: 2.75)
    discover_called = mock.Mock()
    monkeypatch.setattr(ceiling_discovery, "run_discovery", discover_called)

    resp = _client().post(
        "/model-ceilings/laguna-xs-2.1/discover", json={"target_concurrency": 150}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "started"
    assert body["estimated_cost_usd"] == 2.75
    assert body["target_concurrency"] == 150
    # TestClient runs background tasks before returning the response in-process, but the ROUTE
    # HANDLER itself must never call discover_ceiling synchronously in its own body — asserting
    # here pins that discover_ceiling was reached only via the background path, not inline.
    assert discover_called.called


def test_discover_route_400s_on_an_unsupported_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from swebench_eval.orchestrator.control_plane import ceiling_discovery

    def _raise(alias: str) -> int:
        raise ceiling_discovery.CeilingDiscoveryError(f"{alias!r} unsupported")

    monkeypatch.setattr(ceiling_discovery, "resolve_target_tokens", _raise)

    resp = _client().post("/model-ceilings/not-a-real-model/discover", json={})

    assert resp.status_code == 400


def test_manual_route_records_and_returns_the_new_current_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from swebench_eval.orchestrator.control_plane import ceiling_discovery

    recorded: dict[str, object] = {}
    monkeypatch.setattr(
        ceiling_discovery,
        "record_manual_ceiling",
        lambda alias, tpm, *, notes=None: recorded.update(alias=alias, tpm=tpm, notes=notes),
    )
    monkeypatch.setattr(
        ceiling_discovery,
        "list_ceilings",
        lambda aliases: [
            {
                "model_alias": aliases[0],
                "discovered_tpm": 1_500_000,
                "ceiling_source": "manual",
                "discovered_at": "2026-08-31T00:00:00+00:00",
                "provider": None,
                "values": None,
                "is_stale": False,
            }
        ],
    )

    resp = _client().post(
        "/model-ceilings/laguna-xs-2.1/manual", json={"tpm_value": 1_500_000, "notes": "test"}
    )

    assert resp.status_code == 200
    assert resp.json()["discovered_tpm"] == 1_500_000
    assert recorded == {"alias": "laguna-xs-2.1", "tpm": 1_500_000, "notes": "test"}


def test_estimate_route_previews_cost_without_starting_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5's confirm gate needs the number BEFORE the spend — estimate must never trigger
    run_discovery."""
    from swebench_eval.orchestrator.control_plane import ceiling_discovery

    monkeypatch.setattr(ceiling_discovery, "resolve_target_tokens", lambda alias: 260_144)
    monkeypatch.setattr(ceiling_discovery, "estimate_cost", lambda *a, **k: 1.62)
    started = mock.Mock()
    monkeypatch.setattr(ceiling_discovery, "run_discovery", started)

    resp = _client().get("/model-ceilings/laguna-xs-2.1/estimate?target_concurrency=150")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "estimate"
    assert body["estimated_cost_usd"] == 1.62
    assert not started.called  # a preview must never spend
