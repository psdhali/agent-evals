"""The judge-model rotatable spec (offline-analysis-design.md §9.2/§10.2).

Covers what's specific to judge-model and not already exercised by the
generic ROTATABLE_MODELS loops in test_run_launch_unit.py /
test_gateway_admin.py: it's a single alias (not a per-harness family), it
points at deepseek-v4-flash-0731 (not one of the two models under
comparison — §3.8's no-self-preference rule), its window is the model's
REAL capacity rather than the harness families' 262144, and it prices from
the live-verified table rather than the conservative unknown default.
"""

from __future__ import annotations

from swebench_eval.gateway.pricing import MODEL_CACHE_READ_RATIOS, MODEL_PRICING
from swebench_eval.gateway.rotatable_models import (
    _LAGUNA,
    _PENDING_ROTATION_KEY,
    _QWEN,
    ROTATABLE_MODELS,
)


def test_judge_model_is_a_single_alias_not_a_family() -> None:
    """Unlike laguna/qwen (5 per-harness aliases each), judge-model is one
    alias — nothing else concurrently claims it (§10.2 point 4: a pass-level
    mutex serializes judge passes instead of per-harness partitioning)."""
    assert "judge-model" in ROTATABLE_MODELS
    assert "judge-model" not in _LAGUNA
    assert "judge-model" not in _QWEN
    judge_aliases = [
        a for a in ROTATABLE_MODELS if a == "judge-model" or a.startswith("judge-model-")
    ]
    assert judge_aliases == ["judge-model"]


def test_judge_model_is_not_one_of_the_two_models_under_comparison() -> None:
    """§3.8: the judge must not be the model under test — self-preference
    would land directly on the harness-comparison headline."""
    spec = ROTATABLE_MODELS["judge-model"]
    upstream = spec.litellm_params["model"]
    assert "laguna" not in str(upstream).lower()
    assert "qwen" not in str(upstream).lower()
    assert "deepseek" in str(upstream).lower()


def test_judge_model_window_is_the_real_backend_capacity() -> None:
    """NOT the 262144 the harness families use (that's qwen/laguna's window) —
    deepseek-v4-flash-0731's real window, matching cheap-oss-model's entry in
    litellm_config.yaml. Understating this would make prune_mode='auto'
    escalate when the model doesn't actually need it (§3.4/§9.4)."""
    spec = ROTATABLE_MODELS["judge-model"]
    assert spec.model_info["max_input_tokens"] == 1_048_576
    assert spec.model_info["max_output_tokens"] == 32_768


def test_judge_model_temperature_zero() -> None:
    """§3.8: reproducibility (accepted as not fully deterministic)."""
    spec = ROTATABLE_MODELS["judge-model"]
    assert spec.litellm_params["temperature"] == 0.0


def test_judge_model_key_is_the_pending_placeholder_until_rotated() -> None:
    """Never a real-looking shared credential before run_launch (here: the
    judge pass) rotates it via /model/update."""
    spec = ROTATABLE_MODELS["judge-model"]
    assert spec.litellm_params["api_key"] == _PENDING_ROTATION_KEY


def test_judge_model_priced_from_the_live_verified_table() -> None:
    """An unpriced alias falls to the $1/$5 unknown default and overstates
    every judge pass's cost ~15x against its own budget ceiling (§3.10/§9.3)."""
    # 2026-09-04: OpenInference's rate — the only allowlisted provider serving this backend.
    assert MODEL_PRICING["judge-model"] == (0.05, 0.16)
    assert MODEL_CACHE_READ_RATIOS["judge-model"] == 0.26
