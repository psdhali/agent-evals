"""results_writer._parse_result() field completeness.

dev/BUILDER4-RESULT-PARSE-DROPS-SIX-METERING-FIELDS-2026-08-28.md: six fields
(cached_tokens, cache_write_tokens, reasoning_tokens, cost_source,
usage_parse_failed_calls, turns_used) were added to ResultMessage and
_RESULT_EXTRA_COLUMNS by the metering-completeness commits (207c729, cc642b1)
but never added to _parse_result's hand-typed extraction list — the
dataclass's own None defaults silently took over, so every row wrote NULL
regardless of what the harness worker genuinely put on the wire. Same bug
shape as the _is_completion_path/count_tokens bug: a field lives in one
hand-maintained list and a parallel hand-maintained list elsewhere is missed.

This test is deliberately GENERIC — every ResultMessage field, not just the
six the report named — so the next field added the same way fails a test
instead of shipping silently NULL again. It caught a second, deeper gap this
way: native_trajectory_s3_key had no instance_results column at all (not a
_parse_result oversight, a genuinely missing destination) — fixed in
dev/BUILDER4-NATIVE-TRAJECTORY-S3-KEY-NEVER-PERSISTED-2026-08-29.md, now
covered by this same test like every other field.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from swebench_eval.orchestrator.control_plane.results_writer import _parse_result
from swebench_eval.queue.schemas import ResultMessage

# Nothing excluded — every ResultMessage field now has a real
# instance_results column and is read by _parse_result. Keep this frozenset
# only as the documented escape hatch for a field that's genuinely not
# wired anywhere yet (see the module docstring for the one that used to live
# here); do not add an entry just to make a failing test pass.
_NOT_YET_WIRED_ANYWHERE: frozenset[str] = frozenset()


def _sentinel_and_expected(f: dataclasses.Field[Any]) -> tuple[object, object]:
    """(value to put in the wire body, value _parse_result should produce)."""
    t = f.type
    if t == "tuple[str, ...]":
        # The real wire format is JSON — a list, not a tuple — but the
        # dataclass field (and _parse_result's tuple(...) call) is a tuple.
        return (["sentinel-a", "sentinel-b"], ("sentinel-a", "sentinel-b"))
    if t == "list[str] | None":
        list_value: object = ["sentinel-x", "sentinel-y"]
        return list_value, list_value
    if t in ("str", "str | None"):
        str_value: object = f"sentinel-{f.name}"
        return str_value, str_value
    if t in ("int", "int | None"):
        # A distinct value per field so a transposition (right value, wrong
        # field) would also be caught, not just a missing one.
        int_value: object = 100_000 + abs(hash(f.name)) % 900_000
        return int_value, int_value
    if t in ("float", "float | None"):
        float_value: object = 1.0 + (abs(hash(f.name)) % 1000) / 1000
        return float_value, float_value
    if t in ("bool", "bool | None"):
        return True, True
    raise AssertionError(f"unhandled ResultMessage field type for {f.name!r}: {t!r}")


_FIELDS = [f for f in dataclasses.fields(ResultMessage) if f.name not in _NOT_YET_WIRED_ANYWHERE]


@pytest.mark.parametrize("field", _FIELDS, ids=[f.name for f in _FIELDS])
def test_parse_result_field_round_trips(field: dataclasses.Field[Any]) -> None:
    """Every wired ResultMessage field survives _parse_result unchanged.

    Mutation-check: comment out any one of the six cached_tokens/
    cache_write_tokens/reasoning_tokens/cost_source/usage_parse_failed_calls/
    turns_used lines in _parse_result — exactly that field's test fails,
    every other field's test stays green (confirming the check is precise,
    not incidentally passing as a group). Verified by hand and reverted.
    """
    body: dict[str, object] = {}
    expected: dict[str, object] = {}
    for f in _FIELDS:
        wire_value, expected_value = _sentinel_and_expected(f)
        body[f.name] = wire_value
        expected[f.name] = expected_value

    parsed = _parse_result(body)

    actual = getattr(parsed, field.name)
    assert actual == expected[field.name], (
        f"field {field.name!r}: sent {body[field.name]!r} on the wire, "
        f"_parse_result produced {actual!r} (expected {expected[field.name]!r})"
    )


def test_parse_result_all_fields_absent_from_wire_stays_at_dataclass_defaults() -> None:
    """The inverse check: a body with only the required fields must leave
    every optional field at ITS OWN dataclass default, never crash and never
    silently fabricate a value — Trap 3, same discipline as everywhere else
    in this pipeline."""
    body = {
        "run_id": "run-1",
        "instance_id": "inst-1",
        "attempt_number": 1,
        "phase": "harness",
        "state": "PATCH_READY",
    }
    parsed = _parse_result(body)
    default = ResultMessage(
        run_id="run-1", instance_id="inst-1", attempt_number=1, phase="harness", state="PATCH_READY"
    )
    assert parsed == default
