"""PERSIST-TURNS-USED (2026-08-28): the shim's serviced-completion turn count is
persisted per-instance (ResultMessage -> instance_results), survives an abort,
and is semantically distinct from llm_calls row count.

The turn definition is load-bearing and harness-neutral: a forwarded model
completion (one serviced call == one turn). Probes / model lists / count_tokens /
shim refusals never increment it. It is one of only two enforced bounds now
(turn cap + cost), so which bound bit must be readable from the data.
"""

from __future__ import annotations


def test_turns_used_in_result_extra_columns() -> None:
    """Mutation check: turns_used must be a persisted instance_results column.

    Dropping it from _RESULT_EXTRA_COLUMNS (or from ResultMessage) silences the
    headline comparison axis and hides which of the two bounds bit.
    """
    from swebench_eval.orchestrator.control_plane.results_writer import _RESULT_EXTRA_COLUMNS

    assert "turns_used" in _RESULT_EXTRA_COLUMNS, "turns_used dropped from persist tuple"


def test_result_extra_values_carries_turns_used() -> None:
    """A ResultMessage's turns_used flows into the per-row values (positive value)."""
    from swebench_eval.orchestrator.control_plane.results_writer import (
        _RESULT_EXTRA_COLUMNS,
        _result_extra_values,
    )
    from swebench_eval.queue.schemas import ResultMessage

    r = ResultMessage(
        run_id="r",
        instance_id="i",
        attempt_number=1,
        phase="harness",
        state="PATCH_READY",
        input_tokens=100,
        output_tokens=20,
        turns_used=7,
        cost_usd=0.5,
    )
    values = _result_extra_values(r)
    d = dict(zip(_RESULT_EXTRA_COLUMNS, values))
    assert d["turns_used"] == 7


def test_reclassify_aborted_keeps_turns_and_metering_fields() -> None:
    """§4.1 regression: an aborted instance keeps turns_used AND the metering
    fields (cached/cache_write/reasoning/cost_source/usage_parse_failed_calls).

    _reclassify_aborted is now structural (dataclasses.replace), so new fields
    carry over by default — the old explicit allow-list silently dropped them.
    Mutation: revert to an explicit allow-list missing a field, this fails.
    """
    import types

    from swebench_eval.queue.schemas import ResultMessage
    from swebench_eval.workers.harness_worker import _reclassify_aborted

    r = ResultMessage(
        run_id="r",
        instance_id="i",
        attempt_number=1,
        phase="harness",
        state="PATCH_READY",
        input_tokens=10,
        output_tokens=5,
        cached_tokens=3,
        cache_write_tokens=1,
        reasoning_tokens=2,
        cost_source="provider",
        usage_parse_failed_calls=2,
        turns_used=42,
        cost_usd=0.5,
    )
    job = types.SimpleNamespace(run_id="r", instance_id="i", attempt_number=1)
    out = _reclassify_aborted(r, job)  # type: ignore[arg-type]  # SimpleNamespace stands in
    assert out.state == "ABORTED_IN_FLIGHT"
    assert out.turns_used == 42
    assert out.cached_tokens == 3
    assert out.cache_write_tokens == 1
    assert out.reasoning_tokens == 2
    assert out.cost_source == "provider"
    assert out.usage_parse_failed_calls == 2


def test_turns_never_inflated_by_non_completions() -> None:
    """§3/§5: probes / model lists / count_tokens are NOT completions, so they
    must not increment the turn count. `_is_completion_path` is the gate the
    shim increments under — this pins the turn definition to completions only.

    Mutation: revert `_is_completion_path` to not exclude count_tokens (or drop
    the probes), this fails. (Also proves turns != count-over-calls: a probe call
    still gets an llm_calls record but never a turn.)
    """
    import swebench_eval.gateway.local_proxy as lp

    assert lp._is_completion_path("/v1/chat/completions")
    assert lp._is_completion_path("/v1/messages?beta=true")
    assert lp._is_completion_path("/v1/responses")
    # Non-completions are not turns:
    assert not lp._is_completion_path("/v1/models")
    assert not lp._is_completion_path("/health")
    assert not lp._is_completion_path("/v1/messages/count_tokens?beta=true")
