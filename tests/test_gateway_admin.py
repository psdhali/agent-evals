"""gateway/admin.py URL construction.

2026-08-28: the first real ``POST /runs`` hit ``_provision_keys`` and failed
closed on ``/key/generate -> 404``. ``gateway_base_url()`` is deployed as
``http://gateway.eval.internal:4000/v1`` (the harness's OpenAI-compatible
chat-completions base) but every route in this module lives at the LiteLLM
proxy ROOT — ``.../v1/key/generate`` doesn't exist. These tests pin the fix
(``_admin_url`` strips a trailing ``/v1``) so a regression 404s a test, not a
live launch.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from swebench_eval.gateway import admin


@pytest.mark.parametrize(
    "base_url,expected",
    [
        ("http://gateway.eval.internal:4000/v1", "http://gateway.eval.internal:4000/key/generate"),
        ("http://gateway.eval.internal:4000/v1/", "http://gateway.eval.internal:4000/key/generate"),
        ("http://gateway.eval.internal:4000", "http://gateway.eval.internal:4000/key/generate"),
        ("http://gateway.eval.internal:4000/", "http://gateway.eval.internal:4000/key/generate"),
        ("http://localhost:4000/v1", "http://localhost:4000/key/generate"),
    ],
)
def test_admin_url_strips_v1(base_url: str, expected: str) -> None:
    assert admin._admin_url(base_url, "/key/generate") == expected


def test_admin_url_never_duplicates_v1_for_v1_named_path() -> None:
    # Guard the exact string-slice: a path that happens to start with
    # "v1" of its own (not this module's routes today, but the slice must
    # not eat into it) is untouched beyond the base.
    assert admin._admin_url("http://h:4000/v1", "/v1-lookalike") == "http://h:4000/v1-lookalike"


def test_generate_key_posts_to_admin_root_not_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        captured["url"] = url
        return httpx.Response(200, json={"key": "sk-fake", "token": "tok-1"})

    monkeypatch.setattr(httpx, "post", fake_post)

    raw_key, key_id = admin.generate_key(
        "http://gateway.eval.internal:4000/v1",
        "master-key",
        key_alias="run-1",
        models=["laguna-xs-2.1-claude_code"],
        max_budget=5.0,
    )

    assert captured["url"] == "http://gateway.eval.internal:4000/key/generate"
    assert raw_key == "sk-fake"
    assert key_id == "tok-1"


def test_generate_key_without_budget_omits_the_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner decision 2026-09-06: run keys carry no LiteLLM max_budget (its own
    price table overcounted cached MiniMax tokens 5x and cut a $10 run at $2.90
    real); the OpenRouter per-run key is the cap.  None must OMIT the field —
    sending ``"max_budget": null`` is not the same request."""
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        captured["json"] = kwargs.get("json")
        return httpx.Response(200, json={"key": "sk-fake", "token": "tok-2"})

    monkeypatch.setattr(httpx, "post", fake_post)

    admin.generate_key(
        "http://gateway.eval.internal:4000/v1",
        "master-key",
        key_alias="run-2",
        models=["minimax-m2.5-codex"],
        max_budget=None,
    )
    assert "max_budget" not in captured["json"]
    assert captured["json"]["key_alias"] == "run-2"


def test_generate_key_with_budget_sends_it(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        captured["json"] = kwargs.get("json")
        return httpx.Response(200, json={"key": "sk-fake", "token": "tok-3"})

    monkeypatch.setattr(httpx, "post", fake_post)
    admin.generate_key(
        "http://gateway.eval.internal:4000/v1",
        "master-key",
        key_alias="judge-1",
        models=["judge-model"],
        max_budget=5.0,
    )
    assert captured["json"]["max_budget"] == 5.0


def test_find_db_model_id_gets_from_admin_root_not_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        captured["url"] = url
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    result = admin.find_db_model_id(
        "http://gateway.eval.internal:4000/v1", "master-key", "laguna-xs-2.1-claude_code"
    )

    assert captured["url"] == "http://gateway.eval.internal:4000/model/info"
    assert result is None


# ---------------------------------------------------------------------------
# block_key / unblock_key — BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31
# ---------------------------------------------------------------------------
# Live-verified end-to-end against a real litellm:main-stable container
# 2026-08-31: mint -> pre-block call 200 -> block -> call 401 (marker matched)
# -> unblock -> call 200 again. These pin the URL/body contract with a mock so
# a regression fails a test, not a live pause.


def test_block_key_posts_to_admin_root_with_the_key_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return httpx.Response(200, json={"blocked": True})

    monkeypatch.setattr(httpx, "post", fake_post)

    admin.block_key("http://gateway.eval.internal:4000/v1", "master-key", "tok-abc")

    assert captured["url"] == "http://gateway.eval.internal:4000/key/block"
    assert captured["json"] == {"key": "tok-abc"}


def test_unblock_key_posts_to_admin_root_with_the_key_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return httpx.Response(200, json={"blocked": False})

    monkeypatch.setattr(httpx, "post", fake_post)

    admin.unblock_key("http://gateway.eval.internal:4000/v1", "master-key", "tok-abc")

    assert captured["url"] == "http://gateway.eval.internal:4000/key/unblock"
    assert captured["json"] == {"key": "tok-abc"}


def test_block_key_is_idempotent_a_failed_call_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause must be safe to retry — same discipline as delete_key."""

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(httpx, "post", fake_post)

    admin.block_key("http://gateway.eval.internal:4000/v1", "master-key", "tok-gone")  # no raise


def test_unblock_key_is_idempotent_a_failed_call_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume must be safe to retry — same discipline as delete_key."""

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(httpx, "post", fake_post)

    admin.unblock_key("http://gateway.eval.internal:4000/v1", "master-key", "tok-gone")  # no raise


def test_rotate_model_key_carries_the_full_litellm_params_not_just_the_key() -> None:
    """The wipe fix (measured live 2026-09-01): /model/update REPLACES litellm_params wholesale
    — a rotation sending only {model, api_key} stripped every other param (temperature/top_k/
    reasoning/extra_body pins) from the alias, proven functionally via a provider pin that
    stopped binding after rotation. The rotation must overlay the new key on the FULL spec."""
    from unittest import mock

    from swebench_eval.gateway import admin

    captured: dict[str, Any] = {}

    def _fake_post(base, master, path, body):
        captured.update(body)
        return {}

    with mock.patch.object(admin, "_post", _fake_post):
        admin.rotate_model_key(
            "http://g",
            "mk",
            "qwen3-coder-next-mini",
            "mid-1",
            "sk-or-new",
            upstream_model="openrouter/qwen/qwen3-coder-next",
            litellm_params={
                "model": "openrouter/qwen/qwen3-coder-next",
                "temperature": 1.0,
                "top_k": 40,
                "extra_body": {"provider": {"order": ["parasail"], "allow_fallbacks": False}},
            },
        )

    params = captured["litellm_params"]
    assert params["api_key"] == "sk-or-new"  # the new key is applied...
    assert params["temperature"] == 1.0  # ...and the generation params SURVIVE
    assert params["top_k"] == 40
    assert params["extra_body"]["provider"]["order"] == ["parasail"]  # the pin survives too


def test_qwen_rotatable_specs_carry_the_parasail_pin() -> None:
    """F4: all five qwen aliases pin parasail in their spec (effective on the four OpenAI-shaped
    paths, measured inert-but-harmless on the anthropic-shaped one; the account allowlist is the
    primary enforcement either way)."""
    from swebench_eval.gateway.rotatable_models import ROTATABLE_MODELS

    for alias, spec in ROTATABLE_MODELS.items():
        extra_body = cast("dict[str, Any]", spec.litellm_params.get("extra_body", {}))
        pin = extra_body.get("provider", {}).get("order")
        if alias.startswith("qwen3-coder-next"):
            assert pin == ["parasail"], alias
        elif alias.startswith("deepseek-v4-flash-0731"):
            # 2026-09-04 family; the slug is the OpenRouter tag prefix, not the display name
            assert pin == ["open-inference"], alias
        elif alias.startswith("gpt-5-mini"):
            assert pin == [
                "openai/flex"
            ], alias  # 2026-09-05: the FLEX tier (half price), by owner decision
        elif alias.startswith("minimax-m2.5"):
            assert pin == ["minimax"], alias  # 2026-09-04 family: the model's home provider
        else:
            assert pin is None, alias  # laguna is account-pinned to Poolside; no spec pin


# ── 2026-09-05: an existing row's window is reconciled with the spec ───────────


def _row(alias: str, max_in: int) -> dict[str, Any]:
    return {
        "data": [
            {
                "model_name": alias,
                "model_info": {
                    "id": "m-1",
                    "db_model": True,
                    "max_input_tokens": max_in,
                    "max_output_tokens": 16384,
                },
            }
        ]
    }


def test_ensure_model_registered_updates_a_stale_window_on_an_existing_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gpt-5-mini discovery alias was registered at 262,144 before the family moved to
    400,000: the stale row is deleted and re-registered with the spec (measured live
    2026-09-05: /model/update cannot change model_info), and the NEW id is returned."""
    from swebench_eval.gateway import admin

    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: httpx.Response(200, json=_row("gpt-5-mini-x", 262_144))
    )

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        posted.append((url, kwargs["json"]))
        return httpx.Response(200, json={"model_info": {"id": "m-2"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    params: dict[str, object] = {
        "model": "openrouter/openai/gpt-5-mini",
        "api_key": "sk-or-unset-pending-rotation",
    }
    model_id = admin.ensure_model_registered(
        "http://g/v1",
        "mk",
        "gpt-5-mini-x",
        params,
        {"max_input_tokens": 400_000, "max_output_tokens": 16384},
    )
    assert model_id == "m-2"  # a NEW row
    assert [u.rsplit("/", 1)[-1] for u, _ in posted] == ["delete", "new"]
    assert posted[0][1] == {"id": "m-1"}
    body = posted[1][1]
    assert body["model_name"] == "gpt-5-mini-x"
    assert body["litellm_params"] == params
    assert body["model_info"] == {"max_input_tokens": 400_000, "max_output_tokens": 16384}


def test_ensure_model_registered_leaves_a_matching_row_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from swebench_eval.gateway import admin

    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: httpx.Response(200, json=_row("gpt-5-mini-x", 400_000))
    )
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: (_ for _ in ()).throw(AssertionError("no POST expected"))
    )
    model_id = admin.ensure_model_registered(
        "http://g/v1",
        "mk",
        "gpt-5-mini-x",
        {"model": "openrouter/openai/gpt-5-mini"},
        {"max_input_tokens": 400_000, "max_output_tokens": 16384},
    )
    assert model_id == "m-1"
