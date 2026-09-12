"""ADR-0024: the S3-dropped run-config dispatch Lambda.

The load-bearing properties:
1. It calls the SAME launch_run() (D2, BUILDER4-RUN-LAUNCH-ORCHESTRATOR §4) as
   POST /runs — no reimplemented enqueue, and the claim mutex cannot be
   bypassed by dropping a file.
2. It moves pending -> processed/ on success, pending -> failed/ with error on
   failure.
3. Idempotency: at-least-once S3 delivery must NOT double-dispatch. The latch
   is "object no longer in pending/".
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from swebench_eval.orchestrator.control_plane.run_launch import LaunchResult
from swebench_eval.orchestrator.run_config import (
    DEFAULT_MAX_TOKENS_PER_INSTANCE,
    DEFAULT_MAX_TURNS_PER_INSTANCE,
)
from swebench_eval.orchestrator.s3_dispatch import _parse_run_config, handler

_BUCKET = "eval-dev-results-123456789012-us-west-2"
_KEY = "runs/pending/run_123.json"

_CONFIG = {
    "run_id": "abc",
    "instances": [
        {
            "instance_id": "astropy__astropy-12907",
            "repo": "astropy/astropy",
            "base_commit": "abc123",
            "problem_statement": "Fix the bug.",
        }
    ],
    "harness": "custom_minimal",
    "model_alias": "cheap-oss-model",
}


def _event(key: str = _KEY) -> dict[str, object]:
    return {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": _BUCKET},
                    "object": {"key": key},
                }
            }
        ]
    }


class _FakeS3:
    class exceptions:
        class ClientError(Exception):
            def __init__(self, code: str) -> None:
                self.response = {"Error": {"Code": code}}

    def __init__(self, keys: dict[str, bytes] | None = None) -> None:
        self._keys = dict(keys or {_KEY: json.dumps(_CONFIG).encode()})
        self.put_calls: list[tuple[str, str]] = []
        self.deletes: list[tuple[str, str]] = []
        self._s3 = self  # ._bucket not used in tests

    # --- ClientError stand-in -------------------------------------------------

    def head_object(self, Bucket: str, Key: str) -> None:
        if Key not in self._keys:
            raise self.exceptions.ClientError("404")

    def get_object(self, Bucket: str, Key: str) -> dict[str, object]:
        if Key not in self._keys:
            raise self.exceptions.ClientError("404")
        return {"Body": _FakeBody(self._keys[Key])}

    def copy_object(self, Bucket: str, Key: str, CopySource: dict[str, str]) -> None:
        self.put_object(Bucket=Bucket, Key=Key, Body=self._keys[CopySource["Key"]])

    def put_object(self, Bucket: str, Key: str, Body: bytes | str) -> None:
        self._keys[Key] = Body.encode() if isinstance(Body, str) else Body

    def delete_object(self, Bucket: str, Key: str) -> None:
        self.deletes.append((Bucket, Key))
        self._keys.pop(Key, None)


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def test_parse_run_config_absent_budget_keys_default_to_constants() -> None:
    """M4 (review 2026-08-25): absent keys resolve to the DEFAULT_* constants.

    METERING-COMPLETENESS (2026-08-28): the owner decision changed — we never
    abort on total token count, so DEFAULT_MAX_TOKENS_PER_INSTANCE is now None
    (unlimited).  Absent ``max_tokens`` therefore resolves to None (the DEFAULT),
    NOT to an implicit 500k; the M4 fix (`.get(k, DEFAULT)` honoring the constant)
    still holds — it just uses the new constant.  The TURN cap is unchanged (500)
    and remains the comparability axis, so absent ``max_turns`` still resolves to
    the non-None 500.  Should the old 500k ceiling ever come back, it is a
    one-line constant change, not a revert of this test's shape.
    """
    _, config, _, _ = _parse_run_config({"run_id": "r1", "harness": "opencode", "instances": [{}]})
    assert config.max_turns_per_instance == DEFAULT_MAX_TURNS_PER_INSTANCE
    assert config.max_tokens_per_instance == DEFAULT_MAX_TOKENS_PER_INSTANCE
    # Turn cap is the bound (never None); token ceiling is now deliberately off.
    assert config.max_turns_per_instance is not None
    assert config.max_tokens_per_instance is None


def test_parse_run_config_explicit_null_means_unlimited() -> None:
    """M4 (review): an EXPLICIT JSON null is the operator's choice to run with no
    cap — it must still resolve to None (unlimited), the one path that should."""
    _, config, _, _ = _parse_run_config(
        {
            "run_id": "r1",
            "harness": "opencode",
            "instances": [{}],
            "max_turns_per_instance": None,
            "max_tokens_per_instance": None,
        }
    )
    assert config.max_turns_per_instance is None
    assert config.max_tokens_per_instance is None


def test_parse_run_config_budget_cap_defaults_to_computed_ceiling() -> None:
    """run-launch D2: budget_cap_usd is new (ADR-0035) and absent from every
    pre-existing S3 run-config object — it must default to a computed ceiling
    (max_cost_usd_per_instance x instances x attempts), not a flat invented
    constant, and an explicit value in the object must still win."""
    _, _config, _instances, budget = _parse_run_config(
        {
            "run_id": "r1",
            "harness": "opencode",
            "instances": [{}, {}],
            "max_cost_usd_per_instance": 3.0,
            "attempts_per_instance": 2,
        }
    )
    assert budget == 3.0 * 2 * 2  # per-instance cap x instances x attempts

    _, _, _, explicit_budget = _parse_run_config(
        {"run_id": "r1", "harness": "opencode", "instances": [{}], "budget_cap_usd": 17.5}
    )
    assert explicit_budget == 17.5


def test_handler_dispatches_and_moves_to_processed() -> None:
    fake = _FakeS3()
    with (
        mock.patch("swebench_eval.orchestrator.s3_dispatch._s3", return_value=fake),
        mock.patch(
            "swebench_eval.orchestrator.s3_dispatch.launch_run",
            return_value=LaunchResult(run_id="abc", dispatched=1, seeded=1),
        ) as dispatch,
        # STEP 6: the window read is best-effort; a unit test has no Aurora.
        mock.patch(
            "swebench_eval.orchestrator.s3_dispatch._read_window_snapshot",
            return_value=(82_768, "gateway"),
        ),
    ):
        out = handler(_event(), None)

    assert out == {"dispatched": 1}
    assert dispatch.call_count == 1
    # pending copy deleted, processed copy exists
    assert any(k == _BUCKET and v == _KEY for k, v in fake.deletes)
    assert _KEY not in fake._keys
    assert "runs/processed/run_123.json" in fake._keys


def test_step6_window_written_into_processed_run_config() -> None:
    """STEP 6 (review 2026-08-26): the dispatcher-RESOLVED window must ride the
    S3 run-config copy — config_snapshot lives in Aurora (paused/destroyed on
    teardown), so without this the run's window is not auditable after AWS is
    down.  The processed object must carry context_window_tokens + source.

    Mutation: drop the overlay=_move_to(...) argument in the success path; this
    test fails (the processed object has no window fields)."""
    fake = _FakeS3()
    with (
        mock.patch("swebench_eval.orchestrator.s3_dispatch._s3", return_value=fake),
        mock.patch(
            "swebench_eval.orchestrator.s3_dispatch.launch_run",
            return_value=LaunchResult(run_id="abc", dispatched=1, seeded=1),
        ),
        mock.patch(
            "swebench_eval.orchestrator.s3_dispatch._read_window_snapshot",
            return_value=(82_768, "gateway"),
        ),
    ):
        handler(_event(), None)

    proc = json.loads(fake._keys["runs/processed/run_123.json"])
    assert proc["context_window_tokens"] == 82_768
    assert proc["context_window_source"] == "gateway"


def test_duplicate_delivery_is_a_noop_when_object_already_moved() -> None:
    # The object is already in processed/ (first delivery moved it); head_object
    # on the pending key then returns 404 -> the lambda must NOT dispatch.
    fake = _FakeS3(keys={"runs/processed/abc.json": b"{}"})
    with (
        mock.patch("swebench_eval.orchestrator.s3_dispatch._s3", return_value=fake),
        mock.patch("swebench_eval.orchestrator.s3_dispatch.launch_run") as dispatch,
    ):
        out = handler(_event(), None)

    assert out == {"dispatched": 0}
    assert dispatch.call_count == 0


def test_failure_moves_to_failed_with_error() -> None:
    fake = _FakeS3()
    with (
        mock.patch("swebench_eval.orchestrator.s3_dispatch._s3", return_value=fake),
        mock.patch(
            "swebench_eval.orchestrator.s3_dispatch.launch_run",
            side_effect=ValueError("no such model"),
        ),
        pytest.raises(ValueError),
    ):
        handler(_event(), None)

    assert any("runs/failed/" in k for k in fake._keys)
    content = next(v for k, v in fake._keys.items() if "runs/failed/" in k)
    parsed = json.loads(content)
    assert "error" in parsed
