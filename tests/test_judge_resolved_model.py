"""judge_llm.resolved_model (owner, 2026-09-08): LiteLLM echoes the alias as
``response.model`` for a db-model, so every judge_results row (and the pass
report) said "judge-model". Resolve through the alias's registry entry."""

from __future__ import annotations

from types import SimpleNamespace

from swebench_eval.analysis.judge_llm import resolved_model


def test_alias_echo_resolves_to_the_registry_upstream_without_the_routing_prefix() -> None:
    resp = SimpleNamespace(model="judge-model")
    assert resolved_model(resp, "judge-model") == "deepseek/deepseek-v4-flash-0731"


def test_a_real_wire_model_name_is_kept_verbatim() -> None:
    resp = SimpleNamespace(model="deepseek/deepseek-v4-flash-0731:deepinfra")
    assert resolved_model(resp, "judge-model") == "deepseek/deepseek-v4-flash-0731:deepinfra"


def test_missing_wire_model_still_resolves_through_the_registry() -> None:
    assert resolved_model(SimpleNamespace(), "judge-model") == "deepseek/deepseek-v4-flash-0731"


def test_unknown_alias_falls_back_to_whatever_the_wire_said() -> None:
    assert resolved_model(SimpleNamespace(model="mystery"), "mystery") == "mystery"
    assert resolved_model(SimpleNamespace(), "mystery") is None
