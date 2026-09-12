"""The orchestrator image serves the operator UI at /ui and forwards /api in-process (option 2)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _reload_main(monkeypatch: pytest.MonkeyPatch, dist: Path | None) -> object:
    if dist is None:
        monkeypatch.delenv("UI_DIST_DIR", raising=False)
    else:
        monkeypatch.setenv("UI_DIST_DIR", str(dist))
    from swebench_eval.orchestrator.api import main

    return importlib.reload(main)


def test_api_prefix_is_stripped_and_ui_served(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<html><body>dashboard</body></html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    main = _reload_main(monkeypatch, tmp_path)
    client = TestClient(main.application)  # type: ignore[attr-defined]
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/health").json() == {"status": "ok"}  # what the SPA calls
    assert "dashboard" in client.get("/ui/").text
    assert client.get("/ui/assets/app.js").text == "console.log(1)"
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/ui/"


def test_without_a_build_the_api_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _reload_main(monkeypatch, None)
    client = TestClient(main.application)  # type: ignore[attr-defined]
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/ui/").status_code == 404
