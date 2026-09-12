"""Results Writer — consumes the results queue, writes to Postgres.

The single point where state-machine validation and idempotent upserts live
(ADR-0007).  Also the only component that enqueues eval jobs — reactively,
after processing a harness result with a non-empty patch (architecture.md §5.1).

ADR-0016: a forcefully-killed instance's partial patch is persisted for
inspection but never enqueued for grading.  BUDGET_EXCEEDED, FAILED_HARNESS,
and STUCK all skip the eval enqueue step.

ADR-0034 M1 gate #6: the Results Writer must NEVER be gated, under pause or
abort.  It is the only consumer of the results queue; pause and abort both
deliberately let in-flight work run to completion, so a result emitted by that
work must always have somewhere to land.  "Completing" the pause by gating
this consumer looks consistent and is a defect — in-flight results would have
nowhere to go.  Empty the results queue even when every pool is paused.

CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md: this module used to also
host the control publisher/heartbeat and reaper rules 2 (deadline) and 3
(never-dispatched) — those moved to ``run_supervisor.py``, the new singleton
service, so the 90-second heartbeat that everything else's liveness depends
on no longer shares a process with the busiest consumer in the system.  This
module keeps the results consume loop, the ``llm_calls`` daemon, and the DLQ
consumers (reaper rule 1) — a judgement call recorded in that design doc §2:
they are queue consumers made safe by SQS exclusivity, not singleton-required.
``_emit_reap_result`` stays here (rule 1 needs it) and ``run_supervisor.py``
imports it for rules 2/3 — one producer, not two copies.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from typing import Any

import psycopg2
from psycopg2.extras import execute_values

from swebench_eval import aws_names
from swebench_eval.control import state as control_state
from swebench_eval.database.state_machine import is_run_closed
from swebench_eval.queue.client import (
    delete_message,
    get_artifact,
    receive_message,
    send_message,
)
from swebench_eval.queue.schemas import EvalJob, ResultMessage

logger = logging.getLogger(__name__)

# Harness states that are terminal for the harness phase but should NOT
# trigger an eval job (ADR-0016: partial patch is inspectable, never auto-graded).
_NO_EVAL_STATES = {
    "FAILED_HARNESS",
    "STUCK",
    "BUDGET_EXCEEDED",
    "EMPTY_PATCH",
    # ADR-0034 M1: abort outcomes are never graded — NEVER_DISPATCHED has no
    # patch (the job never ran), and ABORTED_IN_FLIGHT's partial patch is
    # preserved for inspection per ADR-0016, never auto-graded.
    "ABORTED_IN_FLIGHT",
    "NEVER_DISPATCHED",
}


# ---------------------------------------------------------------------------
# The publisher tick (ADR-0034 §2 / M1.2) — MOVED to run_supervisor.py
# (CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md).  Aurora→Valkey heartbeat
# and reconcile are now owned by the run-supervisor singleton, not this
# process; results-writer only marks run activity (below) so the supervisor's
# idle/active gate stays fed.
# ---------------------------------------------------------------------------


def run_results_writer() -> None:
    """Consume the ``results`` queue indefinitely.

    For each message:
    1. Validate the state transition.
    2. Idempotent upsert to ``instance_results``.
    3. If the harness phase produced a non-empty patch and is not in a
       no-eval state, enqueue an eval job.
    4. Delete the message.
    """
    # 5a-ii: the deployed container has a fresh Aurora — the schema and ADR-0022's
    # `litellm_spend` database are NOT pre-provisioned (M-2/N-2: the app owns
    # creation). smoke_test calls these locally; the control-plane singleton is
    # the deployed path that must too, or its first write fails with
    # `relation "runs" does not exist` and every result lands in the DLQ.
    from swebench_eval.database.connection import ensure_additional_databases, run_migrations

    # Review finding (2026-08-16): configure INFO logging FIRST, so the
    # results_writer's consume/enqueue decisions reach CloudWatch — the
    # auto-enqueue leg can then be evidenced by record, not by timing.
    from swebench_eval.logging_bootstrap import configure_logging

    configure_logging()
    logger.info("results-writer starting (polling results)")

    run_migrations()
    ensure_additional_databases()
    # ADR-0034 §2 / M1.2: rebuilding control:flags from Aurora on start is now
    # run-supervisor's job (its own singleton startup) — see
    # CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md.  results-writer no
    # longer touches control:flags at all, only the run-activity marker below.

    # ADR-0037 / M0 §3.5: the llm_calls consumer lives in THIS process (900
    # messages per Lite run does not justify a standing ECS service against a
    # $100/quarter budget), but on its OWN queue, its OWN thread, and its OWN
    # transaction — a failed call-row bulk insert must never roll back an
    # instance verdict (M0 §3.5 hard rule).
    threading.Thread(target=run_llm_calls_writer, daemon=True, name="llm-calls-writer").start()

    # §6.6 (ceiling-discovery design): the autoscaler's observation events — own queue, own
    # thread, same isolation pattern as run_llm_calls_writer. The consumer's ONLY job is
    # appending a fact to model_tpm_observations; the model_ceilings view reconciles on read,
    # so there is no pointer here that can lag or drift.
    threading.Thread(
        target=run_model_observations_writer, daemon=True, name="model-observations-writer"
    ).start()

    # run-launch §7 rule 1 / D8: one daemon thread per DLQ, same pattern as
    # run_llm_calls_writer above — own queue, own loop, own failure isolation.
    threading.Thread(
        target=_run_dlq_reaper,
        args=("harness-jobs", "harness"),
        daemon=True,
        name="harness-dlq-reaper",
    ).start()
    threading.Thread(
        target=_run_dlq_reaper, args=("eval-jobs", "eval"), daemon=True, name="eval-dlq-reaper"
    ).start()

    while True:
        msg = receive_message("results", wait_seconds=20)
        if msg is not None:
            # A result arriving is proof a run is doing work — keep the
            # activity marker fresh (state.py mark_runs_active) so
            # run-supervisor's publisher tick (a separate process now) keeps
            # reconciling while the run is live.
            control_state.mark_runs_active()

        if msg is None:
            continue

        try:
            result = _parse_result(msg["body"])
            _process_result(result)
            delete_message("results", msg["receipt_handle"])
        except Exception:
            logger.exception("Failed to process result message %s", msg.get("message_id"))
            # Do NOT delete — let visibility timeout expire for retry.


# ADR-0037 / M0 §2.2-column order for the bulk insert.  The JSONL line the shim
# writes uses these exact keys, so a line maps to a row by column name; the
# list order (not dict order) is authoritative for execute_values, and every
# column carry is NULL when the key is absent (Trap 3).
_LLM_CALL_COLUMNS: tuple[str, ...] = (
    "run_id",
    "instance_id",
    "attempt_number",
    "call_index",
    "harness",
    "generation_id",
    "model_requested",
    "model_resolved",
    "provider_name",
    # 1.6 (review 2026-08-26): stored-free build identity fields.
    "system_fingerprint",
    "service_tier",
    "started_at",
    "latency_ms",
    "ttft_ms",
    # STEP 3 (review 2026-08-26): the per-call latency breakdown.
    "stream_ms",
    "shim_preflight_ms",
    "gateway_response_ms",
    "gateway_overhead_ms",
    "gateway_callback_ms",
    "path",
    "stream",
    "max_tokens_requested",
    "max_tokens_injected",  # F1 (2026-09-04): the shim's injected output cap, NULL when not
    "pacer_charge_tok",  # F3 (2026-09-04): the weighted arrival-bucket draw for the call
    "upstream_error_retries",  # 2026-09-04: embedded-upstream-error retries (200-with-error)
    "temperature",
    "n_messages",
    "has_tools",
    "request_bytes",
    "http_status",
    "finish_reason",
    "stop_reason",
    # 1.6 + G-4 (review 2026-08-26): provider-native stop signal + Responses-API
    # completion fields (codex speaks the OpenAI Responses API — `status` /
    # `incomplete_details.reason`, not finish_reason/stop_reason).
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
    # 1.6 (review 2026-08-26): the provider's prompt/completion cost split.
    "upstream_inference_prompt_cost_usd",
    "upstream_inference_completions_cost_usd",
    "cost_source",
    "usage_parse_failed",
    # BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5/§2.6: the L1 pacer's per-call
    # footprint. paced_wait_ms/overload_retries were on the record since ADR-0041 but NOT
    # in this allowlist — silently dropped on every insert.
    "paced_wait_ms",
    "overload_retries",
    "overload_backoff_ms",
    "retry_upstream_ms",
    "pacer_was_queued",
    "pacer_queue_len",
    "pacer_deny_axis",
)

# Same env default as the harness worker — the llm_calls.jsonl artifacts land
# in the same bucket it writes, on the FREE S3 gateway endpoint (M0 §3.1).
_ARTIFACTS_BUCKET = os.environ.get("ARTIFACTS_BUCKET", "eval-artifacts")

# M0 §4/§5 phase-timing + ADR-0038 contamination columns on instance_results,
# in ResultMessage field order.  A single tuple keeps the INSERT, the DO UPDATE
# SET, and the message→row mapping from drifting apart.  None = not measured
# (Trap 3).  psycopg2 adapts a list/tuple to a TEXT[] column automatically.
_RESULT_EXTRA_COLUMNS: tuple[str, ...] = (
    "queue_wait_s",
    "provision_s",
    "image_pull_s",
    "worker_boot_s",
    "repo_prep_s",
    "agent_s",
    "patch_extract_s",
    "artifact_upload_s",
    "task_observed_s",
    "task_billed_s",
    "repo_prep_cache_hit",
    "image_pull_cold",
    "eval_queue_wait_s",
    "eval_patch_fetch_s",
    "eval_image_pull_s",
    "eval_test_s",
    "eval_log_upload_s",
    "eval_image_pull_cold",
    # ADR-0038.
    "stripped_test_paths",
    "grade_invalid",
    "leaked_node_ids",
    "leak_detectable",
    "gold_patch_similarity",
    # ADR-0037 / M0 §1.3 — per-instance token/cost + the adapter cross-check.
    "input_tokens",
    "output_tokens",
    # METERING-COMPLETENESS (2026-08-28): the rollup was a three-field projection;
    # add the dropped Usage fields + the completeness marker + the reconciled
    # cost_source so the report reads the full picture.
    "cached_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "cost_source",
    "usage_parse_failed_calls",
    # BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5: the pacer's per-instance rollup
    # (same shape as usage_parse_failed_calls — per-call facts rolled up). harness only.
    "paced_wait_ms_total",
    "paced_calls",
    "pacer_timeouts",
    "overload_retries_total",
    # PERSIST-TURNS-USED: the shim's harness-neutral turn count (a headline
    # comparison axis + one of the two enforced bounds). harness-phase only.
    "turns_used",
    "cost_usd",
    "adapter_input_tokens",
    "adapter_output_tokens",
    "adapter_cost_usd",
    # Compaction build (BUILD-SPEC §6) — per-instance compaction measurement.
    "compactions_fired",
    "compaction_tokens_before",
    "compaction_tokens_after",
    "context_window_tokens",
    # 2026-08-29 (dev/BUILDER4-NATIVE-TRAJECTORY-S3-KEY-NEVER-PERSISTED-2026-08-29.md):
    # B6/E11a's native trajectory pointer — genuinely uploaded by
    # harness_worker.py since that feature landed, never had a column or a
    # read here at all. Named exactly like the ResultMessage field (not
    # renamed to a "_path" suffix like its siblings) so _result_extra_values'
    # getattr(result, c) needs no special-case mapping.
    "native_trajectory_s3_key",
    # dev/BUILDER4-EXPOSE-MISSING-ARTIFACTS-2026-08-28.md: SWE-bench's own
    # grade logs — uploaded by eval_worker._upload_run_logs since the 5b
    # review, the keys just never made it past a log line. Eval-phase only;
    # empty string (never None-coerced here, same as the field's own default)
    # when the runner never produced a log dir.
    "test_output_s3_key",
    "run_log_s3_key",
)


def _result_extra_values(result: ResultMessage) -> tuple[object, ...]:
    """The per-row values for :data:`_RESULT_EXTRA_COLUMNS`, co-erced for arrays."""
    out: list[object] = []
    for c in _RESULT_EXTRA_COLUMNS:
        v = getattr(result, c)
        if isinstance(v, tuple):
            v = list(v)  # psycopg2 adapts a list to TEXT[]
        out.append(v)
    return tuple(out)


# 2026-08-28 (BUILDER4-ROUND2-LEDGER-FIXES-2026-08-28.md item 4): the columns
# the main guarded upsert's hardcoded DO UPDATE SET already COALESCEs, minus
# `state` (owned by the guard) and `dispatched_at`/`dispatch_count` (their
# ownership is message-type-conditional — only a DISPATCHED message's
# non-NULL param advances them, via the CASE in the main statement — not a
# plain "fill if null", and writing a stale dispatched_at long after the fact
# would confuse the reaper's deadline math, §7 rule 2, which measures FROM
# it). Combined with `_RESULT_EXTRA_COLUMNS` below, this is every remaining
# evidence column a blocked write's follow-up fill covers.
_CORE_EVIDENCE_COLUMNS: tuple[str, ...] = (
    "error_category",
    "error_detail",
    "verdict",
    "wall_clock_harness_s",
    "wall_clock_eval_s",
    "touches_test_files",
    "patch_path",
    "trajectory_path",
    "raw_log_path",
    "report_path",
    "report_json",
)

# Name-driven, deliberately: see _fill_blocked_write_evidence.  A field added
# to _RESULT_EXTRA_COLUMNS later (this round's turns_used, or whatever comes
# next) is covered by the blocked-write fill automatically — nothing to
# remember in a second, hand-typed list.
_EVIDENCE_COLUMNS: tuple[str, ...] = _CORE_EVIDENCE_COLUMNS + _RESULT_EXTRA_COLUMNS


def _core_evidence_values(result: ResultMessage) -> tuple[object, ...]:
    """The per-row values for :data:`_CORE_EVIDENCE_COLUMNS`, in that order —
    mirrors exactly what the main INSERT's VALUES tuple already computes for
    these same fields."""
    return (
        result.error_category or None,
        result.error_detail or None,
        result.verdict or None,
        result.wall_clock_s if result.phase == "harness" else None,
        result.wall_clock_s if result.phase == "eval" else None,
        result.touches_test_files,
        result.patch_s3_key or None,
        result.trajectory_s3_key or None,
        result.raw_log_s3_key or None,
        result.report_json_s3_key or None,
        # report_json: no ResultMessage field feeds it today (the report
        # lives in MinIO, referenced by report_path) — matches the main
        # INSERT's own hardcoded None for this column. Included for column-
        # set parity, not because it does anything yet.
        None,
    )


def _fill_blocked_write_evidence(cur: Any, result: ResultMessage) -> None:
    """§6.3b's guard blocking a write must not also discard evidence a
    genuine message carried (item 4 of the round-2 ledger-fixes brief).

    The guarded upsert skips the ENTIRE row when state_rank doesn't advance
    — including patch/trajectory/log pointers to artifacts already sitting
    in S3 (ADR-0034 explicitly wants an aborted attempt's patch and
    trajectory retained), plus every other COALESCE'd field: diagnosis,
    verdict, timing, token/cost, the ADR-0038 contamination columns,
    turns_used.

    Fills ONLY currently-NULL columns — ``COALESCE(instance_results.c, %s)``,
    existing value first — never overwrites a value the row already has,
    even if this blocked message disagrees with it. The row that won the
    guard is still authoritative for anything it actually set; this only
    covers gaps it left.

    A SEPARATE statement, not folded into the guarded upsert above as a
    CASE, deliberately: making that statement always touch a row (so its
    COALESCEs could run unconditionally) would make ``RETURNING`` always
    return a row, and ``advanced`` would become permanently True —
    re-introducing duplicate eval jobs on every redelivery, the exact bug
    item 1 just fixed. Keeping the guard's result meaningful requires
    keeping it a separate, simple statement.
    """
    set_clause = ",\n            ".join(
        f"{c} = COALESCE(instance_results.{c}, %s)" for c in _EVIDENCE_COLUMNS
    )
    # _core_evidence_values is all scalars (no array-typed columns among the
    # core evidence set); _result_extra_values already coerces tuple->list
    # for the TEXT[] columns it covers (stripped_test_paths etc).
    values: tuple[object, ...] = _core_evidence_values(result) + _result_extra_values(result)
    cur.execute(
        f"""UPDATE instance_results SET
            {set_clause}
            WHERE run_id = %s AND instance_id = %s AND attempt_number = %s AND phase = %s""",
        (
            *values,
            result.run_id,
            result.instance_id,
            result.attempt_number,
            result.phase,
        ),
    )


def run_llm_calls_writer() -> None:
    """Consume the ``llm-calls`` queue (M0 §3): one pointer per run/instance/
    attempt points at a bulk llm_calls.jsonl object; read it and bulk-insert.

    Runs as a daemon thread inside the results-writer process.  The composite PK
    + ``ON CONFLICT DO NOTHING`` makes whole-batch redelivery a no-op, so any
    number of concurrent readers is safe and ordering does not matter.

    M0-4 (review): the receive sits INSIDE the try with a bounded sleep so a
    transient SQS error (throttle, credential-refresh blip, network) retries
    instead of killing the daemon thread permanently — the main results loop
    would otherwise keep serving silently while every pointer sits unread.  The
    main-thread results loop dies-with-the-process and ECS restarts it; a daemon
    thread has no such supervision, so it must survive on its own.
    """
    while True:
        try:
            msg = receive_message("llm-calls", wait_seconds=20)
        except Exception:
            logger.exception("llm-calls receive failed (transient); retrying in 5s")
            time.sleep(5)
            continue
        if msg is None:
            continue
        try:
            _process_llm_calls_pointer(msg["body"])
            delete_message("llm-calls", msg["receipt_handle"])
        except Exception:
            logger.exception("Failed to process llm-calls message %s", msg.get("message_id"))
            # Do NOT delete — visibility timeout will retry.


def run_model_observations_writer() -> None:
    """Consume the ``model-observations`` queue (§6.6): the live dispatcher's autoscaler
    events (reconciliation_peak / overload / recovery_stabilized), one INSERT each into
    ``model_tpm_observations`` — append-only, no pointer maintenance (the ``model_ceilings``
    view computes the current value on read, every time). Same daemon-thread survival rules
    as run_llm_calls_writer: the receive sits inside the try, a failed insert is NOT deleted
    (visibility retry, then the DLQ)."""
    while True:
        try:
            msg = receive_message("model-observations", wait_seconds=20)
        except Exception:
            logger.exception("model-observations receive failed (transient); retrying in 5s")
            time.sleep(5)
            continue
        if msg is None:
            continue
        try:
            _insert_model_observation(msg["body"])
            delete_message("model-observations", msg["receipt_handle"])
        except Exception:
            logger.exception(
                "Failed to process model-observations message %s", msg.get("message_id")
            )
            # Do NOT delete — visibility timeout retries, maxReceiveCount sends it to the DLQ.


def _insert_model_observation(body: dict[str, Any]) -> None:
    """One row, one fact. Its own connection — never shares a transaction with a result
    write (the M0 §3.5 rule, same reason as llm-calls). Raises on a malformed body so the
    message rides visibility-retry into the DLQ instead of being silently dropped."""
    model_alias = str(body["model_alias"])  # KeyError on absence is the refusal
    event_type = str(body["event_type"])
    tpm_value = int(body["tpm_value"])
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO model_tpm_observations
                       (model_alias, run_id, event_type, value_kind, tpm_value,
                        at_concurrency, provider, notes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    model_alias,
                    body.get("run_id"),
                    event_type,
                    str(body.get("value_kind") or "tpm"),
                    tpm_value,
                    body.get("at_concurrency"),
                    body.get("provider"),
                    body.get("notes"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _process_llm_calls_pointer(body: dict[str, Any]) -> None:
    """Read one llm_calls.jsonl object from S3 and bulk-insert its rows.

    Its OWN connection and transaction — never shares one with a result write
    (M0 §3.5 hard rule), so a failed call-row insert cannot roll back a verdict.
    """
    from swebench_eval.database.connection import get_connection

    raw = get_artifact(_ARTIFACTS_BUCKET, str(body["s3_key"])).decode("utf-8")
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("llm-calls: skipping malformed JSONL line: %.80s", line)

    conn = get_connection()
    try:
        _insert_llm_calls(conn, rows)
    finally:
        conn.close()


def _llm_value(row: dict[str, Any], column: str) -> Any:
    """Map one JSONL field to its DB value, preserving NULL (Trap 3)."""
    v = row.get(column)
    if v is None:
        return None
    if column in ("stream", "has_tools", "usage_parse_failed", "pacer_was_queued"):
        return bool(v)
    if column == "started_at":
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v)
            except ValueError:
                return None
        return v
    return v


def _insert_llm_calls(conn: Any, rows: list[dict[str, Any]]) -> None:
    """Bulk-insert *rows* into llm_calls, idempotently (ON CONFLICT DO NOTHING)."""
    cols = ", ".join(_LLM_CALL_COLUMNS)
    values = [tuple(_llm_value(r, c) for c in _LLM_CALL_COLUMNS) for r in rows]
    with conn.cursor() as cur:
        execute_values(
            cur,
            f"INSERT INTO llm_calls ({cols}) VALUES %s "
            "ON CONFLICT (run_id, instance_id, attempt_number, call_index) DO NOTHING",
            values,
        )
    conn.commit()


def _parse_result(body: dict[str, Any]) -> ResultMessage:
    """Parse a result message body into a ``ResultMessage``."""
    return ResultMessage(
        run_id=str(body["run_id"]),
        instance_id=str(body["instance_id"]),
        attempt_number=int(body["attempt_number"]),
        phase=str(body["phase"]),
        state=str(body["state"]),
        error_category=str(body.get("error_category", "")),
        error_detail=str(body.get("error_detail", "")),
        verdict=str(body.get("verdict", "")),
        wall_clock_s=float(body.get("wall_clock_s", 0)),
        patch_s3_key=str(body.get("patch_s3_key", "")),
        trajectory_s3_key=str(body.get("trajectory_s3_key", "")),
        raw_log_s3_key=str(body.get("raw_log_s3_key", "")),
        # dev/BUILDER4-NATIVE-TRAJECTORY-S3-KEY-NEVER-PERSISTED-2026-08-29.md:
        # genuinely on the wire (B6/E11a), never read here before now.
        native_trajectory_s3_key=str(body.get("native_trajectory_s3_key", "")),
        report_json_s3_key=str(body.get("report_json_s3_key", "")),
        # dev/BUILDER4-EXPOSE-MISSING-ARTIFACTS-2026-08-28.md: SWE-bench's own
        # grade logs, genuinely on the wire since the 5b review, never read
        # here before now.
        test_output_s3_key=str(body.get("test_output_s3_key", "")),
        run_log_s3_key=str(body.get("run_log_s3_key", "")),
        input_tokens=_opt_int(body, "input_tokens"),
        output_tokens=_opt_int(body, "output_tokens"),
        # METERING-COMPLETENESS (2026-08-28) / dev/BUILDER4-RESULT-PARSE-DROPS-
        # SIX-METERING-FIELDS-2026-08-28.md: these six were added to
        # ResultMessage and _RESULT_EXTRA_COLUMNS by the metering-completeness
        # commits but never added HERE — dataclass defaults (None) silently
        # took over, so every row wrote NULL regardless of what the harness
        # worker actually sent on the wire. Confirmed live: llm_calls carried
        # real, non-trivial values for the same run/instance the whole time.
        cached_tokens=_opt_int(body, "cached_tokens"),
        cache_write_tokens=_opt_int(body, "cache_write_tokens"),
        reasoning_tokens=_opt_int(body, "reasoning_tokens"),
        cost_source=body.get("cost_source") or None,
        usage_parse_failed_calls=_opt_int(body, "usage_parse_failed_calls"),
        # Pacer rollup (design doc §2.5) — read HERE, not left to dataclass defaults (the
        # METERING-COMPLETENESS lesson above: a field on the wire but not parsed writes NULL).
        paced_wait_ms_total=_opt_int(body, "paced_wait_ms_total"),
        paced_calls=_opt_int(body, "paced_calls"),
        pacer_timeouts=_opt_int(body, "pacer_timeouts"),
        overload_retries_total=_opt_int(body, "overload_retries_total"),
        turns_used=_opt_int(body, "turns_used"),
        cost_usd=_opt_float(body, "cost_usd"),
        touches_test_files=bool(body.get("touches_test_files", False)),
        # M0 §1.3 cross-check: the adapter's own reported figure (None when it
        # reported nothing — Trap 3).
        adapter_input_tokens=_opt_int(body, "adapter_input_tokens"),
        adapter_output_tokens=_opt_int(body, "adapter_output_tokens"),
        adapter_cost_usd=_opt_float(body, "adapter_cost_usd"),
        # M0 §4/§5 timing — None (absent) when not measured.
        queue_wait_s=_opt_float(body, "queue_wait_s"),
        provision_s=_opt_float(body, "provision_s"),
        image_pull_s=_opt_float(body, "image_pull_s"),
        worker_boot_s=_opt_float(body, "worker_boot_s"),
        repo_prep_s=_opt_float(body, "repo_prep_s"),
        agent_s=_opt_float(body, "agent_s"),
        patch_extract_s=_opt_float(body, "patch_extract_s"),
        artifact_upload_s=_opt_float(body, "artifact_upload_s"),
        task_observed_s=_opt_float(body, "task_observed_s"),
        task_billed_s=_opt_float(body, "task_billed_s"),
        repo_prep_cache_hit=_opt_bool(body, "repo_prep_cache_hit"),
        image_pull_cold=_opt_bool(body, "image_pull_cold"),
        eval_queue_wait_s=_opt_float(body, "eval_queue_wait_s"),
        eval_patch_fetch_s=_opt_float(body, "eval_patch_fetch_s"),
        eval_image_pull_s=_opt_float(body, "eval_image_pull_s"),
        eval_test_s=_opt_float(body, "eval_test_s"),
        eval_log_upload_s=_opt_float(body, "eval_log_upload_s"),
        eval_image_pull_cold=_opt_bool(body, "eval_image_pull_cold"),
        # ADR-0038 — contamination + honesty.
        stripped_test_paths=tuple(body.get("stripped_test_paths", ()) or ()),
        grade_invalid=_opt_bool(body, "grade_invalid"),
        leaked_node_ids=(
            list(body["leaked_node_ids"]) if body.get("leaked_node_ids") is not None else None
        ),
        leak_detectable=_opt_bool(body, "leak_detectable"),
        gold_patch_similarity=_opt_float(body, "gold_patch_similarity"),
        # Compaction build (BUILD-SPEC §6) — not-measured stays NULL.
        compactions_fired=_opt_int(body, "compactions_fired"),
        compaction_tokens_before=_opt_int(body, "compaction_tokens_before"),
        compaction_tokens_after=_opt_int(body, "compaction_tokens_after"),
        context_window_tokens=_opt_int(body, "context_window_tokens"),
    )


def _opt_float(body: dict[str, Any], key: str) -> float | None:
    v = body.get(key)
    return None if v is None else float(v)


def _opt_int(body: dict[str, Any], key: str) -> int | None:
    v = body.get(key)
    return None if v is None else int(v)


def _opt_bool(body: dict[str, Any], key: str) -> bool | None:
    v = body.get(key)
    return None if v is None else bool(v)


def _log_missing_run_id(result: ResultMessage, exc: BaseException) -> None:
    """R4.3: an FK violation on ``run_id`` means the run was never registered.

    Log it at ERROR naming the missing run_id with the actionable fix, and emit
    a CloudWatch metric so the silence that hid this for a week cannot recur.
    The FK constraint itself is correct and stays (R4.3) — the fix is a `runs`
    row (scripts/recovery/create_run_row.py), not a tolerant insert.
    """
    logger.error(
        "results_writer FK violation on run_id=%s: run was never registered "
        "(no runs row — dispatch bypassed the normal S3-dispatch path). Recovery: "
        "scripts/recovery/create_run_row.py --run-id %s then redrive the DLQ. %r",
        result.run_id,
        result.run_id,
        exc,
    )
    try:
        import boto3

        boto3.client("cloudwatch", region_name=aws_names.region()).put_metric_data(
            Namespace="EvalFramework",
            MetricData=[
                {
                    "MetricName": "ResultsWriterMissingRunId",
                    "Value": 1.0,
                    "Unit": "Count",
                }
            ],
        )
    except Exception:  # a metric emit must never break the message loop
        logger.debug("could not emit ResultsWriterMissingRunId metric", exc_info=True)


# run-launch §6.2/§6.3: the harness dispatcher's DISPATCHED notice owns
# dispatched_at/dispatch_count on instance_results — no other message type
# ever sets them.  Computed here (not carried on ResultMessage) because the
# dispatcher already sends a plain state-only notice and "when results_writer
# processed it" is close enough for the reaper's margin (§7 rule 2's margin
# is measured in tens of minutes).
_DISPATCHED_STATE = "DISPATCHED"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _process_result(result: ResultMessage) -> None:
    """Validate and persist a single result message.

    run-launch §6.3: the ON CONFLICT UPDATE is guarded by ``state_rank()`` —
    SQS Standard is unordered and at-least-once, so a late DISPATCHED can
    arrive after PATCH_READY; the guard makes that a no-op instead of a silent
    regression.  Column ownership (§6.3a) falls out of the same guarded
    UPDATE: a DISPATCHED message carries no verdict/paths/etc (all NULL on
    that message), so COALESCE leaves them exactly as a real result already
    set them.
    """
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    try:
        advanced = False
        # M0 §4/§5: timing columns ride the same row (1:1, no second table).
        _col_list = ", ".join(_RESULT_EXTRA_COLUMNS)
        _upd_list = ",\n".join(
            f"                     {c} = COALESCE(EXCLUDED.{c}, instance_results.{c})"
            for c in _RESULT_EXTRA_COLUMNS
        )
        with conn.cursor() as cur:
            try:
                cur.execute(
                    f"""INSERT INTO instance_results
                       (run_id, instance_id, attempt_number, phase, state,
                        error_category, error_detail, verdict,
                        wall_clock_harness_s, wall_clock_eval_s,
                        touches_test_files, patch_path, trajectory_path,
                        raw_log_path, report_path, report_json,
                        dispatched_at, dispatch_count,
                        {_col_list})
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s,
                        {'%s, ' * (len(_RESULT_EXTRA_COLUMNS) - 1)}%s)
                       ON CONFLICT (run_id, instance_id, attempt_number, phase)
                       DO UPDATE SET
                         state = EXCLUDED.state,
                         -- F5: never null error/diagnostic fields on redelivery.  SQS is
                         -- at-least-once (ADR-0015 heartbeats make redelivery the normal
                         -- crash path), so a redelivered message that omits these must not
                         -- destroy the diagnosis while artifact paths survive.
                         error_category = COALESCE(EXCLUDED.error_category, instance_results.error_category),
                         error_detail = COALESCE(EXCLUDED.error_detail, instance_results.error_detail),
                         verdict = COALESCE(EXCLUDED.verdict, instance_results.verdict),
                         wall_clock_harness_s = COALESCE(EXCLUDED.wall_clock_harness_s, instance_results.wall_clock_harness_s),
                         wall_clock_eval_s = COALESCE(EXCLUDED.wall_clock_eval_s, instance_results.wall_clock_eval_s),
                         touches_test_files = COALESCE(EXCLUDED.touches_test_files, instance_results.touches_test_files),
                         patch_path = COALESCE(EXCLUDED.patch_path, instance_results.patch_path),
                         trajectory_path = COALESCE(EXCLUDED.trajectory_path, instance_results.trajectory_path),
                         raw_log_path = COALESCE(EXCLUDED.raw_log_path, instance_results.raw_log_path),
                         report_path = COALESCE(EXCLUDED.report_path, instance_results.report_path),
                         report_json = COALESCE(EXCLUDED.report_json, instance_results.report_json),
                         -- run-launch §6.3a: column ownership — only a
                         -- DISPATCHED message ever carries a non-NULL
                         -- dispatched_at param, so only it advances these.
                         dispatched_at = COALESCE(EXCLUDED.dispatched_at, instance_results.dispatched_at),
                         dispatch_count = CASE WHEN EXCLUDED.dispatched_at IS NOT NULL
                                                THEN COALESCE(instance_results.dispatch_count, 0) + 1
                                                ELSE instance_results.dispatch_count END,
                         {_upd_list}
                       -- run-launch §6.3b: the monotonic state guard.  A late/
                       -- lower-ranked message (e.g. a redelivered or straggler
                       -- DISPATCHED arriving after PATCH_READY) must not
                       -- regress a row that already reached a higher rank —
                       -- state_rank() and its ladder live in init.sql.
                       WHERE state_rank(EXCLUDED.state) > state_rank(instance_results.state)
                       RETURNING (xmax = 0) AS inserted""",
                    (
                        result.run_id,
                        result.instance_id,
                        result.attempt_number,
                        result.phase,
                        result.state,
                        result.error_category or None,
                        result.error_detail or None,
                        result.verdict or None,
                        result.wall_clock_s if result.phase == "harness" else None,
                        result.wall_clock_s if result.phase == "eval" else None,
                        result.touches_test_files,
                        result.patch_s3_key or None,
                        result.trajectory_s3_key or None,
                        result.raw_log_s3_key or None,
                        result.report_json_s3_key or None,
                        # report_json stays default {} for now — the report lives in
                        # MinIO and is referenced by report_path (S3 key).
                        None,
                        _now_utc() if result.state == _DISPATCHED_STATE else None,
                        1 if result.state == _DISPATCHED_STATE else 0,
                        *_result_extra_values(result),
                    ),
                )
                row = cur.fetchone()
                # 2026-08-28 fix (BUILDER4-EVAL-HANDOFF-BLOCKER-2026-08-28.md):
                # this used to compute `inserted = bool(row and row[0])` —
                # true ONLY for a genuine INSERT (`xmax = 0`). run_launch.
                # _seed_instance_rows pre-inserts a PENDING row for every
                # harness attempt at launch, so the real PATCH_READY write
                # always takes the ON CONFLICT DO UPDATE branch — a genuine,
                # successful state advance, but `inserted` was False. Gating
                # the eval-enqueue on `inserted` (below) meant it NEVER fired
                # for any run-launch-originated run: zero eval jobs, ever.
                #
                # `advanced` is what the guard actually proves — "this
                # write's state_rank was strictly greater, so the WHERE
                # clause let it through" — true for a fresh INSERT (nothing to
                # compare against) AND for a genuine PATCH_READY landing over
                # a seeded PENDING row. A guard MISS (the WHERE clause skipped
                # this row — a stale/lower-ranked message) returns no row at
                # all — RETURNING only fires for rows the statement actually
                # touched (INSERT always touches one; a guarded-out UPDATE
                # touches none) — so idempotency survives by the same
                # mechanism: a redelivered PATCH_READY over an existing
                # PATCH_READY is equal-rank, fails the strict `>`, returns no
                # row, `advanced` is False. Identical behaviour for seeded and
                # unseeded rows, so the S3 path and the API path cannot
                # diverge.
                advanced = row is not None
                # item 4: the guard blocking this write must not also
                # discard evidence it carried — fill whatever's still NULL
                # on the row that won, never overwrite what it already set.
                # Same cursor/transaction as the guarded upsert above, so
                # both commit together.
                if not advanced:
                    _fill_blocked_write_evidence(cur, result)
            except psycopg2.errors.ForeignKeyViolation as exc:
                # R4.3: the run was never registered (hand-launched dispatch
                # bypassed the dispatcher's runs-row insert).  Name the missing
                # run_id loudly instead of silently DLQ-ing after 7 redeliveries.
                _log_missing_run_id(result, exc)
                raise
        conn.commit()

        # If the state guard let this write through (§6.3b's `advanced`,
        # above — not `inserted`, which a pre-seeded PENDING row makes false
        # for every genuine result) and it's a harness PATCH_READY, enqueue
        # an eval job.  Guarding on `advanced` means a redelivered harness
        # result does NOT spawn a duplicate eval job (idempotency key
        # (run_id, instance_id, attempt_number, phase)) — a redelivery is
        # equal-rank, the guard blocks it, no row returns, advanced is False.
        if advanced and result.phase == "harness" and result.state == "PATCH_READY":
            _enqueue_eval_job(conn, result)

        # ADR-0034 M1.8: maintain the run's summary on every result — counts
        # by outcome including the abort states and the denominator rule.
        _maintain_run_summary(conn, result.run_id)

        # run-launch §8 / BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md §1,§3:
        # a run with zero non-terminal rows in EITHER phase is READY to close,
        # not finished — closing (stop stragglers, revoke keys, release the
        # pair) is now a deliberate operator action (POST /runs/{id}/close),
        # never automatic here. Owner decision 2026-08-29: keys must stay live
        # so a failed instance can be reviewed and manually restarted before
        # anyone decides the run is actually done. This just logs the signal.
        try:
            if is_ready_to_close(conn, result.run_id):
                logger.info(
                    "run %s: zero non-terminal rows — ready for operator review/close",
                    result.run_id,
                )
        except Exception:
            # results loop; a missed log line here costs nothing real.
            logger.exception("ready-to-close check failed for %s", result.run_id)

        logger.info(
            "Processed %s result for %s/%s attempt %d → %s",
            result.phase,
            result.run_id,
            result.instance_id,
            result.attempt_number,
            result.state,
        )
    finally:
        conn.close()


def _enqueue_eval_job(conn: Any, result: ResultMessage) -> None:
    """Enqueue an eval job for a harness result with a non-empty patch.

    run-launch §6.2: this is also where the eval PHASE ROW IS SEEDED — a
    PENDING row inserted directly, in its own small transaction on the same
    connection, BEFORE the job reaches the queue ("results_writer, where it
    enqueues the eval job — direct, it is already the writer").  This closes
    the same gap §6.1 closed for the harness phase: a missing eval result is
    now a visible non-terminal row rather than a silent absence, and the
    reaper has a ``seeded_at`` to measure eval's deadline from (eval rows
    carry no ``dispatched_at`` — that column is harness-dispatcher-owned,
    §6.3a — eval has no equivalent RunTask-style dispatch step).
    """
    with conn.cursor() as cur:
        # BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md M3/M4: carry the
        # harness-phase row's retry_reason onto the eval-phase row for the
        # SAME attempt, so an operator-restarted attempt that reaches eval is
        # still identifiable as one logical attempt in the derived resolve-
        # rate denominator (queries.py:resolve_rate_denominator) — without
        # this, the eval row would read NULL and look like a second, distinct
        # attempt even though it is the eval half of the same restarted one.
        cur.execute(
            """INSERT INTO instance_results
               (run_id, instance_id, attempt_number, phase, state, seeded_at, retry_reason)
               VALUES (%s, %s, %s, 'eval', 'PENDING', now(),
                       (SELECT retry_reason FROM instance_results
                        WHERE run_id = %s AND instance_id = %s
                          AND attempt_number = %s AND phase = 'harness'))
               ON CONFLICT (run_id, instance_id, attempt_number, phase) DO NOTHING""",
            (
                result.run_id,
                result.instance_id,
                result.attempt_number,
                result.run_id,
                result.instance_id,
                result.attempt_number,
            ),
        )
    conn.commit()

    # The eval job needs fail_to_pass / pass_to_pass.  In Phase 3, these are
    # loaded from the dataset by the eval worker.  The job message carries
    # the instance_id and the worker reads the full record from the dataset.
    job = EvalJob(
        run_id=result.run_id,
        instance_id=result.instance_id,
        attempt_number=result.attempt_number,
        patch_s3_key=result.patch_s3_key,
        fail_to_pass="",  # loaded by worker from dataset
        pass_to_pass="",  # loaded by worker from dataset
    )
    send_message("eval-jobs", _dataclass_to_dict(job))
    logger.info(
        "Enqueued eval job for %s/%s attempt %d",
        result.run_id,
        result.instance_id,
        result.attempt_number,
    )


def _dataclass_to_dict(obj: Any) -> dict[str, object]:
    import dataclasses

    return dataclasses.asdict(obj)


# ADR-0034 M1.8: states excluded from the resolve-rate denominator (an abort
# must not manufacture a rate from work that never ran).
_ABORT_EXCLUDED = {"ABORTED_IN_FLIGHT", "NEVER_DISPATCHED"}

# run-launch §6.1/§6.2: the harness-phase states that mean "not yet concluded"
# — pre-seeded PENDING rows, and the DISPATCHED/HARNESS_RUNNING ledger states.
# Excluded from `completed` alongside `_ABORT_EXCLUDED` so a run mid-flight
# does not count its own not-yet-run instances as completed.
_NON_TERMINAL_HARNESS_STATES = {"PENDING", "DISPATCHED", "HARNESS_RUNNING"}


def _maintain_run_summary(conn: Any, run_id: str) -> None:
    """Recompute ``run_summary.summary_json`` for *run_id* from instance_results.

    Summary is truth-derived (a GROUP BY over the rows it owns), not a counter.
    Carries ``expected``, ``completed``, ``aborted_in_flight``,
    ``never_dispatched``, ``resolved`` and BOTH denominators — ``attempted``
    (every instance that ran the harness) and ``gradeable`` (instances with a
    real verdict that was not refused as ``grade_invalid``) — plus the two
    non-conflatable rates they yield.  The two denominators deliberately
    disagree on any run with an infrastructure or invalid-grade failure
    (ADR-0038 §4): reporting only one invites re-running until the flattering
    number cooperates.

    run-launch §6.1 fixed a real defect here: ``expected`` used to be derived
    as ``completed + aborted + never`` — computed FROM the very rows it was
    meant to be checked against, so it could never show a shortfall (a
    genuinely missing result was invisible).  Seeding a PENDING row per
    instance at launch (§4 SEED) makes the harness-phase row count the TRUE
    denominator by construction: ``expected`` is now ``count(*) FILTER (WHERE
    phase = 'harness')`` — a stuck/missing instance stays visible as a
    non-terminal row instead of quietly matching whatever showed up.
    """
    import json

    with conn.cursor() as cur:
        cur.execute(
            """SELECT
                 count(*) FILTER (WHERE phase = 'harness'),
                 count(*) FILTER (WHERE phase = 'harness' AND state NOT IN %s),
                 count(*) FILTER (WHERE state = 'ABORTED_IN_FLIGHT'),
                 count(*) FILTER (WHERE state = 'NEVER_DISPATCHED'),
                 count(*) FILTER (WHERE phase = 'eval' AND verdict = 'resolved'),
                 count(*) FILTER (WHERE phase = 'eval'
                                  AND verdict IN ('resolved', 'unresolved')
                                  AND grade_invalid IS NOT TRUE),
                 count(*) FILTER (WHERE phase = 'harness' AND state = 'EMPTY_PATCH')
               FROM instance_results WHERE run_id = %s""",
            (tuple(_ABORT_EXCLUDED | _NON_TERMINAL_HARNESS_STATES), run_id),
        )
        expected, completed, aborted, never, resolved, verdicts, empty_patches = (
            int(x or 0) for x in cur.fetchone()
        )
        # 2026-09-06 (first 500-run, owner): an EMPTY_PATCH attempt is a MODEL
        # failure with no eval row — the agent submitted nothing.  ADR-0038 §4
        # excludes only infrastructure categories and invalid grades from
        # `gradeable`, so empty patches stay in the denominator; leaving them
        # out was the flattering direction the rule exists to prevent
        # (379/496 = 76.4% read vs 379/499 = 76.0% true on that run).
        gradeable = verdicts + empty_patches

        attempted = completed + aborted
        summary = {
            "expected": expected,
            "completed": completed,
            "aborted_in_flight": aborted,
            "never_dispatched": never,
            "resolved": resolved,
            "empty_patches": empty_patches,
            # the two names the report must never conflate (ADR-0038 §4 / §7.5):
            "attempted": attempted,
            "gradeable": gradeable,
            "denominator": gradeable,  # the honesty denominator
            "resolved_per_attempted": (resolved / attempted) if attempted else None,
            "resolved_per_gradeable": (resolved / gradeable) if gradeable else None,
        }
        cur.execute(
            """INSERT INTO run_summary (run_id, summary_json)
               VALUES (%s, %s)
               ON CONFLICT (run_id) DO UPDATE SET summary_json = EXCLUDED.summary_json""",
            (run_id, json.dumps(summary)),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# run-launch §7 — the reaper: finding runs that will never finish on their own
# ---------------------------------------------------------------------------
#
# Lives on a TIMER, not in the message handler: the condition it detects is
# the ABSENCE of messages, and an event-driven handler does nothing when
# nothing arrives.  Rule 1 (DLQ consumption) is the one exception — it is
# positive evidence (a message that arrived), so it runs as its own always-on
# daemon thread (D8) IN THIS PROCESS, never gated by the tick or by
# pause/abort (a dead-lettered job is definite evidence regardless of pause).
#
# Rules 2 (deadline) and 3 (never-dispatched) are DB-scan-driven, in-process
# timers whose "consecutive empty"/"last run" state must be exactly ONE view
# — CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md moved both, plus the
# tick that drives them (``_maybe_run_reaper``), to ``run_supervisor.py``.

# A row in one of these states is still outstanding work — the reaper and
# the finalisation check both use this.  ABANDONED is deliberately absent:
# it is terminal for "is this run finished" purposes even though state_rank()
# still lets a later straggler result overwrite it (self-correcting, §6.3).
# Also imported by abort.py and restart.py — stays here, not moved with rules
# 2/3, since it is not part of either rule's own state.
_ALL_NON_TERMINAL_STATES = frozenset({"PENDING", "DISPATCHED", "HARNESS_RUNNING", "EVAL_RUNNING"})


def _dataclass_to_result_dict(msg: ResultMessage) -> dict[str, object]:
    import dataclasses

    return dataclasses.asdict(msg)


def _emit_reap_result(
    run_id: str, instance_id: str, attempt_number: int, phase: str, state: str, error_detail: str
) -> None:
    """§7: "Every reap is recorded, never silent" — onto ``results``, the same
    queue and code path every other result travels, so it is processed
    through the SAME monotonic state guard (§6.3) rather than a bespoke write.
    """
    msg = ResultMessage(
        run_id=run_id,
        instance_id=instance_id,
        attempt_number=attempt_number,
        phase=phase,
        state=state,
        error_detail=error_detail,
    )
    send_message("results", _dataclass_to_result_dict(msg))
    logger.warning(
        "REAP %s/%s attempt %d (%s) -> %s: %s",
        run_id,
        instance_id,
        attempt_number,
        phase,
        state,
        error_detail,
    )


# ---------------------------------------------------------------------------
# Rule 1 — dead-lettered (positive evidence).  D8: a daemon thread per DLQ,
# following run_llm_calls_writer's pattern (results_writer.py, "the same
# pattern run_llm_calls_writer already uses").
# ---------------------------------------------------------------------------


def _persist_dlq_body(dlq_name: str, run_id: str, instance_id: str, body: dict[str, Any]) -> str:
    """§7 rule 1: "Persist the body before deleting. The DLQ is where the last
    real root-cause came from." Mirrors ``abort.py``'s drain-manifest pattern
    (persist to S3, never invent a second one).
    """
    from swebench_eval.queue.client import upload_artifact

    bucket = os.environ.get("ARTIFACTS_BUCKET", "eval-artifacts")
    safe_run = run_id or "unknown-run"
    safe_instance = instance_id or "unknown-instance"
    key = f"runs/{safe_run}/dlq/{dlq_name}/{safe_instance}-{int(time.time() * 1000)}.json"
    upload_artifact(bucket, key, json.dumps(body))
    return key


def _process_dlq_message(dlq_name: str, phase: str, msg: dict[str, Any]) -> None:
    body = msg["body"]
    if not isinstance(body, dict):
        try:
            body = json.loads(body)
        except (TypeError, ValueError):
            body = {}
    run_id = str(body.get("run_id", ""))
    instance_id = str(body.get("instance_id", ""))
    attempt_number = int(body.get("attempt_number", 0) or 0)
    receive_count = (msg.get("attributes") or {}).get("ApproximateReceiveCount", "unknown")

    s3_key = _persist_dlq_body(dlq_name, run_id, instance_id, body)

    if run_id and instance_id:
        _emit_reap_result(
            run_id,
            instance_id,
            attempt_number,
            phase,
            "ABANDONED",
            f"dead-lettered from {dlq_name} (ApproximateReceiveCount={receive_count}, "
            f"body persisted at s3://{os.environ.get('ARTIFACTS_BUCKET', 'eval-artifacts')}/{s3_key})",
        )
    else:
        logger.warning(
            "%s: dead-lettered message has no run_id/instance_id; body persisted at %s only",
            dlq_name,
            s3_key,
        )
    delete_message(dlq_name, msg["receipt_handle"])


def _run_dlq_reaper(queue_name: str, phase: str) -> None:
    """Daemon thread body: consume ``<queue_name>-dlq`` forever.

    §7: "Do not scan the DLQ — consume it... O(1) per dead job, once.
    results_writer never scans anything — it processes one more ordinary
    result. The DLQ finally drains."
    """
    dlq_name = f"{queue_name}-dlq"
    while True:
        try:
            msg = receive_message(dlq_name, wait_seconds=20)
        except Exception:
            logger.exception("%s receive failed (transient); retrying in 5s", dlq_name)
            time.sleep(5)
            continue
        if msg is None:
            continue
        try:
            _process_dlq_message(dlq_name, phase, msg)
        except Exception:
            logger.exception("Failed to process %s message %s", dlq_name, msg.get("message_id"))
            # Do NOT delete — visibility timeout will retry.


# ---------------------------------------------------------------------------
# Rules 2 (deadline) and 3 (never-dispatched) — MOVED to run_supervisor.py.
# See that module for ``_phase_timeout_seconds``, ``_reap_deadline_rule``, and
# ``_reap_never_dispatched``.
#
# ``_running_instance_ids_for_run`` stays HERE, not moved with them: it has
# no module-level state (a pure ECS read), and abort.py's ``_sweep_remaining_
# rows`` needs it too (the same live-ECS-task check rule 2 uses, reused
# rather than re-implemented) — one definition for both consumers, same
# treatment as ``_emit_reap_result`` and ``is_ready_to_close`` above/below.
# run_supervisor.py imports it rather than duplicating it.
# ---------------------------------------------------------------------------


def _running_instance_ids_for_run(run_id: str) -> set[str]:
    """Live ECS state: instance_ids with a RUNNING task ``startedBy=run_id``.

    ADR-0032: the instance identity travels in ``containerOverrides``
    (``INSTANCE_ID``), not in the task's own name — DescribeTasks is how the
    reaper reads it back.  No ``CLUSTER`` configured (local/test/dev) means
    no live ECS to protect anything — treated as "nothing running", which is
    the correct fail direction here (the deadline+no-progress-key checks
    still have to agree before anything is reaped).
    """
    cluster = os.environ.get("CLUSTER", "")
    if not cluster:
        return set()
    import boto3

    ecs = boto3.client("ecs", region_name=aws_names.region())
    task_arns: list[str] = []
    tok: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "cluster": cluster,
            "startedBy": run_id,
            "desiredStatus": "RUNNING",
            "maxResults": 100,
        }
        if tok:
            kwargs["nextToken"] = tok
        resp = ecs.list_tasks(**kwargs)
        task_arns.extend(resp.get("taskArns", []))
        tok = resp.get("nextToken")
        if not tok:
            break
    if not task_arns:
        return set()

    instance_ids: set[str] = set()
    for i in range(0, len(task_arns), 100):  # DescribeTasks caps at 100 ARNs/call
        resp = ecs.describe_tasks(cluster=cluster, tasks=task_arns[i : i + 100])
        for task in resp.get("tasks", []):
            for override in (task.get("overrides") or {}).get("containerOverrides", []):
                for env in override.get("environment", []):
                    if env.get("name") == "INSTANCE_ID" and env.get("value"):
                        instance_ids.add(env["value"])
    return instance_ids


# ---------------------------------------------------------------------------
# Close-readiness (§8 / BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md §1,§3)
# — "Ready to close = no row for this run, in either phase, is non-terminal."
#
# This used to auto-finalise (stop tasks, revoke keys, mark completed) the
# instant it detected this. Owner decision 2026-08-29: finalising is now a
# deliberate operator action only (POST /runs/{id}/close, control_plane/
# restart.py:close_run) — keys must stay live so a failed instance can be
# reviewed and restarted before the run is treated as actually done. This is
# now a pure READ, safe to call from anywhere, never mutates `runs.status`.
# ---------------------------------------------------------------------------


def non_terminal_row_counts(conn: Any, run_id: str) -> tuple[int, int]:
    """(non_terminal, total) instance_results rows for *run_id*.

    Shared by :func:`is_ready_to_close`, the ``/close`` endpoint's atomic
    re-check (M1), and the nightly guard's Postgres-side equivalent query
    (duplicated there, not imported — see N1: the guard is a standalone
    Lambda that cannot import this module).
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) FILTER (WHERE state IN %s), count(*)
               FROM instance_results WHERE run_id = %s""",
            (tuple(_ALL_NON_TERMINAL_STATES), run_id),
        )
        non_terminal, total = cur.fetchone()
    return non_terminal, total


def is_ready_to_close(conn: Any, run_id: str) -> bool:
    """True iff *run_id* has at least one row and zero non-terminal rows.

    Read-only, derived, never persisted (§3 of the v2 design) — recomputed
    from live ``instance_results`` every call so a restart landing a new
    non-terminal row makes this False again on the very next check, with
    nothing to keep in sync.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM runs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    if row is None or is_run_closed(row[0]):
        return False
    non_terminal, total = non_terminal_row_counts(conn, run_id)
    return total > 0 and non_terminal == 0


# ---------------------------------------------------------------------------
# The reaper tick itself (``_maybe_run_reaper``) — MOVED to run_supervisor.py,
# alongside rules 2 and 3 it drives.  It imports ``is_ready_to_close`` from
# this module for its own "ready for operator close" log line.
# ---------------------------------------------------------------------------
