"""ADR-0037 / M0 §3 — the llm_calls writer (no live database required).

Covers the writer's contract without a Postgres: the column list matches the
design's llm_calls schema AND the keys the shim writes (so no column is silently
dropped), NULL preservation for unreported fields (Trap 3), and that the bulk
insert carries ON CONFLICT DO NOTHING so a whole-batch SQS redelivery is a no-op.
"""

from __future__ import annotations

from unittest import mock


def test_llm_call_columns_cover_the_shim_record() -> None:
    """M0 §3: every key the shim writes into a call record maps to an
    llm_calls column — a column dropped here would be silently lost, and a
    column the shim never writes would always be NULL (Trap 3)."""
    from swebench_eval.gateway import local_proxy
    from swebench_eval.orchestrator.control_plane.results_writer import _LLM_CALL_COLUMNS

    cols = set(_LLM_CALL_COLUMNS)
    # The design's full column set — keep in sync with infra/docker/init.sql.
    expected = {
        "run_id",
        "instance_id",
        "attempt_number",
        "call_index",
        "harness",
        "generation_id",
        "model_requested",
        "model_resolved",
        "provider_name",
        # 1.6 (review 2026-08-26): stored-free build identity.
        "system_fingerprint",
        "service_tier",
        "started_at",
        "latency_ms",
        "ttft_ms",
        # STEP 3 (review 2026-08-26): latency breakdown.
        "stream_ms",
        "shim_preflight_ms",
        "gateway_response_ms",
        "gateway_overhead_ms",
        "gateway_callback_ms",
        "path",
        "stream",
        "max_tokens_requested",
        # F1 (2026-09-04): the shim's injected output cap.
        "max_tokens_injected",
        # F3 (2026-09-04): the weighted pacer draw.
        "pacer_charge_tok",
        "upstream_error_retries",
        "temperature",
        "n_messages",
        "has_tools",
        "request_bytes",
        "http_status",
        "finish_reason",
        "stop_reason",
        # 1.6 + G-4 (review 2026-08-26): provider-native + Responses completion.
        "native_finish_reason",
        "responses_status",
        "responses_incomplete_reason",
        "error_type",
        "error_code",
        "response_bytes",
        "rate_limit_scope",
        "retry_after_s",
        "ratelimit_remaining_requests",
        "ratelimit_remaining_tokens",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "cost_usd",
        "upstream_inference_cost_usd",
        # 1.6 (review 2026-08-26): cost split.
        "upstream_inference_prompt_cost_usd",
        "upstream_inference_completions_cost_usd",
        "cost_source",
        "usage_parse_failed",
        # BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5/§2.6: the L1 pacer's per-call
        # footprint. paced_wait_ms/overload_retries were on the shim record since ADR-0041
        # and silently dropped here until now — exactly the loss this test exists to catch.
        "paced_wait_ms",
        "overload_retries",
        "overload_backoff_ms",
        "retry_upstream_ms",
        "pacer_was_queued",
        "pacer_queue_len",
        "pacer_deny_axis",
    }
    assert cols == expected, cols ^ expected

    # The shim's request-shape keys are llm_calls columns (drop none silently).
    shim = local_proxy.LocalProxy(
        upstream_url="http://127.0.0.1:1/v1",
        run_id="r",
        harness="h",
        instance_id="i",
        attempt_number=1,
        usage=None,
    )
    rec = shim._begin_call("/v1/chat/completions", "cheap-oss-model", b'{"stream":true}')
    assert set(rec) <= cols | {"run_id", "instance_id", "attempt_number", "harness"}


_RECORD_ONLY_KEYS = {"run_id", "instance_id", "attempt_number", "harness"}


def test_recorded_call_keys_are_columns_on_every_shim_path(tmp_path, monkeypatch) -> None:
    """Review F8 (2026-09-03): the assertion above only ever saw ``_begin_call``'s request-shape
    keys — every pacing column is assigned on the RESPONSE path, which is exactly how
    paced_wait_ms/overload_retries sat dropped since ADR-0041. Drive a real request through the
    shim on each path and assert the RECORDED call's keys are all columns: success, retried
    success, hold-cap timeout, retries exhausted."""
    import swebench_eval.gateway.local_proxy as _lp
    from swebench_eval.orchestrator.control_plane.results_writer import _LLM_CALL_COLUMNS
    from tests.test_local_proxy_pacing import (
        FakePacer,
        _AlwaysOverloadUpstream,
        _OkUpstream,
        _Overload429ThenOkUpstream,
        _run_shim,
    )

    cols = set(_LLM_CALL_COLUMNS) | _RECORD_ONLY_KEYS
    monkeypatch.setattr(_lp, "_OVERLOAD_BACKOFF_BASE_S", 0.01)
    _Overload429ThenOkUpstream.fails_remaining = 2
    paths = {
        "success": (_OkUpstream, FakePacer()),
        "retried_success": (_Overload429ThenOkUpstream, FakePacer()),
        "hold_cap_timeout": (_OkUpstream, FakePacer(outcomes=["timeout"])),
        "retries_exhausted": (_AlwaysOverloadUpstream, FakePacer(outcomes=["admit", "timeout"])),
    }
    for name, (handler, pacer) in paths.items():
        (tmp_path / name).mkdir()
        _responses, records = _run_shim(tmp_path / name, handler, pacer)
        assert records, name
        extra = set(records[0]) - cols
        assert not extra, f"{name}: recorded keys with no llm_calls column: {sorted(extra)}"


def test_llm_value_preserves_null_and_parses_started_at() -> None:
    """M0 §0.6 Trap 3 + §2.2: missing fields stay NULL; started_at becomes a
    datetime; booleans are coerced."""
    from swebench_eval.orchestrator.control_plane.results_writer import _llm_value

    row = {"stream": True, "started_at": "2026-08-19T12:00:00+00:00", "http_status": 429}
    assert _llm_value(row, "stream") is True
    assert _llm_value(row, "started_at").isoformat().startswith("2026-08-19T12:00:00")
    assert _llm_value(row, "input_tokens") is None  # absent -> NULL, never 0
    assert _llm_value(row, "http_status") == 429


def test_insert_llm_calls_is_idempotent_sql() -> None:
    """M0 §3.4: the bulk insert carries ON CONFLICT DO NOTHING so a whole-batch
    redelivery is a no-op.  Assert the SQL the writer builds, without a DB."""
    from swebench_eval.orchestrator.control_plane import results_writer as rw

    captured: dict[str, object] = {}

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql: str, params=None) -> None:
            captured["sql"] = sql

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def commit(self) -> None:
            captured["committed"] = True

    with mock.patch(
        "swebench_eval.orchestrator.control_plane.results_writer.execute_values",
        lambda cur, sql, vals, *a, **k: cur.execute(sql),
    ):
        rw._insert_llm_calls(_FakeConn(), [{"run_id": "r", "call_index": 1}])

    sql = str(captured["sql"])
    assert "ON CONFLICT (run_id, instance_id, attempt_number, call_index) DO NOTHING" in sql
    assert captured.get("committed") is True


# ---------------------------------------------------------------------------
# M0 review fixes — persistence + carried timing (no live DB)
# ---------------------------------------------------------------------------


def test_result_extra_columns_carry_tokens_cost_and_adapter_crosscheck() -> None:
    """M0-2 (review): per-instance tokens/cost AND the adapter cross-check are in
    the single _RESULT_EXTRA_COLUMNS tuple so the INSERT persists them — the
    pre-M0 'computed then discarded' defect is closed (M0 §1.3 / DoD #2)."""
    from swebench_eval.orchestrator.control_plane.results_writer import _RESULT_EXTRA_COLUMNS
    from swebench_eval.queue.schemas import ResultMessage

    cols = set(_RESULT_EXTRA_COLUMNS)
    assert {"input_tokens", "output_tokens", "cost_usd"}.issubset(cols)
    assert {"adapter_input_tokens", "adapter_output_tokens", "adapter_cost_usd"}.issubset(cols)

    r = ResultMessage(
        run_id="r",
        instance_id="i",
        attempt_number=1,
        phase="harness",
        state="RESOLVED",
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.5,
        adapter_input_tokens=101,
        adapter_output_tokens=21,
        adapter_cost_usd=0.51,
    )
    from swebench_eval.orchestrator.control_plane.results_writer import _result_extra_values

    values = _result_extra_values(r)
    d = dict(zip(_RESULT_EXTRA_COLUMNS, values))
    assert d["input_tokens"] == 100
    assert d["adapter_input_tokens"] == 101
    assert d["cost_usd"] == 0.5


def test_job_reference_roundtrips_queue_wait_s() -> None:
    """M0-6 (review): queue_wait_s computed by the dispatcher rides the
    JobReference wire env so the deployed task (which never receives) gets it."""
    from swebench_eval.queue.schemas import JobReference

    ref = JobReference(
        run_id="r",
        instance_id="i",
        attempt_number=1,
        receipt_handle="h",
        harness_name="opencode",
        model_alias="cheap-oss-model",
        timeout_seconds=600,
        max_tokens_per_instance=100,
        max_cost_usd_per_instance=1.0,
        dispatched_at=1.0,
        queue_wait_s=42.0,
    )
    env_dict = {item["name"]: item["value"] for item in ref.to_env()}
    assert env_dict["QUEUE_WAIT_S"] == "42.000"
    rebuilt = JobReference.from_env(env_dict)
    assert rebuilt.queue_wait_s == 42.0
    assert rebuilt.dispatched_at == 1.0


def test_leak_detectable_bool_survives_to_result() -> None:
    """N-1 (review): a leak-detectable instance (absent ids present) reads
    leak_detectable=True on the result; a 200-without-usage still records the
    row.  Assert the adapter cross-check and leak fields parse out of a dict."""
    from swebench_eval.orchestrator.control_plane.results_writer import _parse_result

    body = {
        "run_id": "r",
        "instance_id": "i",
        "attempt_number": 1,
        "phase": "harness",
        "state": "RESOLVED",
        "adapter_input_tokens": 7,
        "leak_detectable": True,
        "leaked_node_ids": ["tests/x.py::t1"],
    }
    r = _parse_result(body)
    assert r.adapter_input_tokens == 7
    assert r.leak_detectable is True
    assert r.leaked_node_ids == ["tests/x.py::t1"]


def test_result_extra_columns_carry_patch_extract_s() -> None:
    """R3-1 (round-3 review): the __measured__ patch_extract_s must actually reach
    the database — it is in _RESULT_EXTRA_COLUMNS and parsed from the message, so
    a worker that forwards it lands in the column (the value must not stop one hop
    short of Postgres, the R3-1 defect class)."""
    from swebench_eval.orchestrator.control_plane.results_writer import (
        _RESULT_EXTRA_COLUMNS,
        _parse_result,
    )
    from swebench_eval.queue.schemas import ResultMessage

    assert "patch_extract_s" in _RESULT_EXTRA_COLUMNS

    r = ResultMessage(
        run_id="r",
        instance_id="i",
        attempt_number=1,
        phase="harness",
        state="RESOLVED",
        patch_extract_s=1.25,
    )
    from swebench_eval.orchestrator.control_plane.results_writer import _result_extra_values

    values = _result_extra_values(r)
    d = dict(zip(_RESULT_EXTRA_COLUMNS, values))
    assert d["patch_extract_s"] == 1.25

    # round-trips through the writer's message parse too.
    body = {
        "run_id": "r",
        "instance_id": "i",
        "attempt_number": 1,
        "phase": "harness",
        "state": "RESOLVED",
        "patch_extract_s": 1.25,
    }
    parsed = _parse_result(body)
    assert parsed.patch_extract_s == 1.25


def test_patch_extract_timeout_maps_to_category_and_state() -> None:
    """R3-2: the new terminated_reason is classified as a distinct non-retryable
    infra category and a FAILED_HARNESS state (never PATCH_READY, so the partial
    patch is never auto-graded)."""
    from swebench_eval.database.state_machine import (
        map_terminated_reason_to_error_category,
        map_terminated_reason_to_state,
    )

    assert (
        map_terminated_reason_to_error_category("patch_extract_timeout", None)
        == "HARNESS_PATCH_EXTRACT_TIMEOUT"
    )
    assert map_terminated_reason_to_state("patch_extract_timeout", None) == "FAILED_HARNESS"


def test_stop_reason_survives_into_insert_tuple() -> None:
    """R5.2 (review M2): the shim captures stop_reason (Anthropic messages /
    OpenAI responses) but the DB insert was a fixed column list without it — so
    every claude_code/codex call inserted finish_reason = NULL, the exact
    symptom R5.2 existed to fix.  Assert the value survives into the actual
    execute_values tuple, crossing the DB boundary the two shim-level tests
    stopped short of."""
    from swebench_eval.orchestrator.control_plane.results_writer import (
        _LLM_CALL_COLUMNS,
        _insert_llm_calls,
        _llm_value,
    )

    assert "stop_reason" in _LLM_CALL_COLUMNS, "stop_reason must be an llm_calls column"

    row = {
        "run_id": "run-x",
        "instance_id": "i",
        "attempt_number": 1,
        "call_index": 1,
        "harness": "claude_code",
        "started_at": "2026-08-19T12:00:00+00:00",
        "http_status": 200,
        "finish_reason": None,
        "stop_reason": "max_tokens",
    }
    value = _llm_value(row, "stop_reason")
    assert value == "max_tokens", value

    captured: dict[str, object] = {}

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql: str, params=None) -> None:
            captured["sql"] = sql

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def commit(self) -> None:
            captured["committed"] = True

    values_seen: list[tuple[object, ...]] = []

    def _capture_values(cur, sql, vals, *a, **k):
        values_seen.extend(vals)
        cur.execute(sql)

    with mock.patch(
        "swebench_eval.orchestrator.control_plane.results_writer.execute_values",
        _capture_values,
    ):
        _insert_llm_calls(_FakeConn(), [row])

    assert values_seen, "execute_values must be called with the row tuples"
    tuple_row = values_seen[0]
    col_index = _LLM_CALL_COLUMNS.index("stop_reason")
    assert tuple_row[col_index] == "max_tokens", tuple_row


def test_result_extra_columns_carry_complete_metering_fields() -> None:
    """METERING-COMPLETENESS (2026-08-28): the per-instance rollup must carry the
    full metering picture, not a three-field projection.  cached/cache_write/
    reasoning + the reconciled cost_source + the usage_parse_failed_calls
    completeness marker must be in _RESULT_EXTRA_COLUMNS AND flow through
    `_result_extra_values` from the ResultMessage.

    Mutation-proof: drop `usage_parse_failed_calls` from _RESULT_EXTRA_COLUMNS (or
    from the ResultMessage) and this test fails.
    """
    from swebench_eval.orchestrator.control_plane.results_writer import (
        _RESULT_EXTRA_COLUMNS,
        _result_extra_values,
    )
    from swebench_eval.queue.schemas import ResultMessage

    new = {
        "cached_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "cost_source",
        "usage_parse_failed_calls",
    }
    assert new.issubset(_RESULT_EXTRA_COLUMNS), f"missing {new - set(_RESULT_EXTRA_COLUMNS)}"

    r = ResultMessage(
        run_id="r",
        instance_id="i",
        attempt_number=1,
        phase="harness",
        state="PATCH_READY",
        input_tokens=1000,
        output_tokens=200,
        cached_tokens=300,
        cache_write_tokens=5,
        reasoning_tokens=40,
        cost_source="provider",
        usage_parse_failed_calls=2,
        cost_usd=0.9,
    )
    values = _result_extra_values(r)
    d = dict(zip(_RESULT_EXTRA_COLUMNS, values))
    assert d["cached_tokens"] == 300
    assert d["cache_write_tokens"] == 5
    assert d["reasoning_tokens"] == 40
    assert d["cost_source"] == "provider"
    assert d["usage_parse_failed_calls"] == 2
