"""Compaction build (Stage 1.1/1.4): context-window resolution + the /model/info client.

BUILD-SPEC rev 2 §2 defines the resolution ORDER — run-config override → live
gateway → baked yaml → DEFAULT floor with a WARNING — and Stage 1.4 binds the
window resolution and O-1's ``model_resolved`` to ONE /model/info client so the
two callers cannot drift on the response shape.

The two-sided contract on the threading chain (Stage 1.2): absent key -> the
DEFAULT constant, explicit JSON null -> None (compaction disabled).  That is the
M4 defect class, and it must not be reintroduced on the new key.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from swebench_eval.gateway.model_info import fetch_model_info
from swebench_eval.orchestrator.control_plane.dispatcher import resolve_context_window
from swebench_eval.orchestrator.run_config import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    RunConfig,
)

# ---------------------------------------------------------------------------
# resolve_context_window — the 4-step order
# ---------------------------------------------------------------------------


def test_run_config_override_wins() -> None:
    """An explicit operator override beats the gateway and the baked config."""
    cfg = RunConfig(model_alias="qwen3-coder-next", context_window_tokens=999_999)
    window, source = resolve_context_window(
        cfg,
        {"qwen3-coder-next": {"max_input_tokens": 262_144}},
        live_fetch=lambda alias: {"max_input_tokens": 262_144},  # would otherwise win
    )
    assert window == 999_999
    assert source == "run_config"


def test_live_gateway_beats_baked_config() -> None:
    """Step 2 (live /model/info) is preferred over step 3 (baked yaml): the image's
    baked copy and the deployed gateway are built from the same repo but redeployed
    independently, and a recorded window that did not match what served the tokens
    is worse than none (BUILD-SPEC §2)."""
    cfg = RunConfig(model_alias="qwen3-coder-next")
    window, source = resolve_context_window(
        cfg,
        {"qwen3-coder-next": {"max_input_tokens": 999_999}},  # baked, stale
        live_fetch=lambda alias: {"max_input_tokens": 262_144},  # live is truth
    )
    assert window == 262_144
    assert source == "gateway"


def test_baked_config_fallback_when_gateway_unreachable() -> None:
    """Step 3: gateway down / returns nothing -> the baked yaml supplies the window."""
    cfg = RunConfig(model_alias="laguna-xs-2.1")
    window, source = resolve_context_window(
        cfg,
        {"laguna-xs-2.1": {"max_input_tokens": 262_144}},
        live_fetch=lambda alias: None,  # unreachable / no metadata
    )
    assert window == 262_144
    assert source == "baked_config"


def test_default_floor_with_warning_when_nothing_known() -> None:
    """Step 4: no gateway AND no baked metadata -> DEFAULT_CONTEXT_WINDOW_TOKENS,
    with a WARNING (the gateway has no metadata for the model)."""
    cfg = RunConfig(model_alias="unknown-model")
    assert DEFAULT_CONTEXT_WINDOW_TOKENS is not None
    with mock.patch("swebench_eval.orchestrator.control_plane.dispatcher.logger.warning") as warn:
        window, source = resolve_context_window(
            cfg,
            {},  # baked config has nothing for this alias
            live_fetch=lambda alias: None,
        )
    assert window == DEFAULT_CONTEXT_WINDOW_TOKENS
    assert source == "default"
    warn.assert_called_once()


def test_live_fetch_exception_falls_back_not_raises() -> None:
    """Provenance must never fail a run: a throwing live probe falls through to
    the baked config / default instead of propagating."""
    cfg = RunConfig(model_alias="qwen3-coder-next")

    def _boom(alias: str):
        raise RuntimeError("connection refused")

    window, source = resolve_context_window(
        cfg,
        {"qwen3-coder-next": {"max_input_tokens": 262_144}},
        live_fetch=_boom,
    )
    assert window == 262_144
    assert source == "baked_config"


def test_none_window_when_everything_none() -> None:
    """Only when DEFAULT_CONTEXT_WINDOW_TOKENS is deliberately None does resolution
    yield a None window (a no-window opt-out), not an implicit default."""
    cfg = RunConfig(model_alias="x")
    with mock.patch(
        "swebench_eval.orchestrator.control_plane.dispatcher.DEFAULT_CONTEXT_WINDOW_TOKENS",
        None,
    ):
        window, source = resolve_context_window(cfg, {}, live_fetch=lambda a: None)
    assert window is None
    assert source == "none"


# ---------------------------------------------------------------------------
# The /model/info client (Stage 1.4) — one client, two callers
# ---------------------------------------------------------------------------


def _model_info_response(alias: str, max_input: int, upstream: str) -> dict[str, Any]:
    """A LiteLLM /model/info response for a single alias."""
    return {
        "data": [
            {
                "model_name": alias,
                "model_info": {"max_input_tokens": max_input},
                "litellm_params": {"model": upstream},
            }
        ]
    }


def test_client_parses_window_and_upstream_model() -> None:
    """The single client returns BOTH the window (needed by Stage 1.1) and the
    literal upstream model (needed by O-1's model_resolved)."""
    with mock.patch("swebench_eval.gateway.model_info.httpx.get") as get:
        get.return_value.status_code = 200
        get.return_value.raise_for_status = mock.Mock()
        get.return_value.json.return_value = _model_info_response(
            "qwen3-coder-next", 262_144, "openrouter/qwen/qwen3-coder-next"
        )
        info = fetch_model_info("http://gw/v1", "key", "qwen3-coder-next")
    assert info == {
        "max_input_tokens": 262_144,
        "model": "openrouter/qwen/qwen3-coder-next",
    }
    # and the URL/auth are what the gateway expects
    _url = get.call_args.args[0]
    assert _url.endswith("/model/info")
    headers = get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer key"


def test_client_returns_none_on_transport_failure() -> None:
    """A lookup failure is a fallback, never a run failure — returns None."""
    with mock.patch(
        "swebench_eval.gateway.model_info.httpx.get",
        side_effect=Exception("network down"),
    ):
        assert fetch_model_info("http://gw/v1", "key", "qwen3-coder-next") is None


def test_client_returns_none_for_unknown_alias() -> None:
    """An alias absent from the gateway response is None (fallback), not an error."""
    with mock.patch("swebench_eval.gateway.model_info.httpx.get") as get:
        get.return_value.status_code = 200
        get.return_value.raise_for_status = mock.Mock()
        get.return_value.json.return_value = _model_info_response(
            "qwen3-coder-next", 262_144, "openrouter/qwen/qwen3-coder-next"
        )
        assert fetch_model_info("http://gw/v1", "key", "other-alias") is None


# ---------------------------------------------------------------------------
# state machine: context_exhausted (Stage 1.3)
# ---------------------------------------------------------------------------


def test_context_exhausted_maps_to_failed_harness_not_retryable_not_terminal() -> None:
    """context_exhausted is a framework-imposed cut, same family as
    HARNESS_MAX_TURNS_EXCEEDED — never graded, never cleanly terminal, never
    retried."""
    from swebench_eval.database.state_machine import (
        is_retryable,
        is_terminal,
        map_terminated_reason_to_error_category,
        map_terminated_reason_to_state,
    )

    assert map_terminated_reason_to_state("context_exhausted", "a real diff") == "FAILED_HARNESS"
    assert (
        map_terminated_reason_to_error_category("context_exhausted", "a real diff")
        == "HARNESS_CONTEXT_EXHAUSTED"
    )
    # Neither terminal nor retryable — the category exists but sits in neither set,
    # exactly like HARNESS_MAX_TURNS_EXCEEDED.
    assert is_terminal("HARNESS_CONTEXT_EXHAUSTED") is False
    assert is_retryable("HARNESS_CONTEXT_EXHAUSTED") is False
