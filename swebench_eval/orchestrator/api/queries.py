"""Pure read-side SQL for the dashboard endpoints (builder2 phase 1 §3).

Read-only by construction: every function here is a SELECT.  The orchestrator's
Aurora is the truth store for runs/instances/capacity (the Valkey read path
covers live *control* state only, §4b), so these reads go straight to Postgres
and the frontend polls them slowly and stops when idle.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from psycopg2.extras import RealDictCursor

# columns for one instance_results row — the list view and the detail view share
# this projection so the two endpoints can't drift apart.
_INSTANCE_COLUMNS = """
    run_id, instance_id, attempt_number, phase, state, error_category,
    error_detail, verdict, wall_clock_harness_s, wall_clock_eval_s,
    touches_test_files, patch_path, trajectory_path, raw_log_path, report_path, report_json,
    native_trajectory_s3_key, test_output_s3_key, run_log_s3_key,
    retry_reason,
    created_at, input_tokens, output_tokens, cost_usd, turns_used,
    paced_wait_ms_total, paced_calls, pacer_timeouts, overload_retries_total,
    adapter_input_tokens, adapter_output_tokens, adapter_cost_usd,
    agent_s, task_observed_s, task_billed_s, repo_prep_s, eval_test_s,
    queue_wait_s, provision_s, image_pull_s, worker_boot_s,
    patch_extract_s, artifact_upload_s, repo_prep_cache_hit, image_pull_cold,
    eval_queue_wait_s, eval_patch_fetch_s, eval_image_pull_s, eval_log_upload_s,
    eval_image_pull_cold, cost_source,
    stripped_test_paths, grade_invalid, leaked_node_ids, gold_patch_similarity,
    leak_detectable
"""

# instance states that mean "work still outstanding for this run".  Anything
# else is terminal for that instance; a run with zero such rows (and at least
# one row) is finished.  (runs.status never records a clean completion — the
# only transition after 'running' is the abort pair.)
# run-launch §6.2/§6.3: DISPATCHED is the new intermediate ledger state
# between the pre-seeded PENDING row and HARNESS_RUNNING — still active, not
# terminal.  ABANDONED is deliberately NOT here: the reaper gave up waiting,
# which is a terminal outcome for reporting purposes even though state_rank()
# still lets a later straggler result overwrite it (self-correcting, §7).
_ACTIVE_STATES = ("PENDING", "DISPATCHED", "HARNESS_RUNNING", "EVAL_RUNNING")

# column names whose Postgres type is NUMERIC — psycopg2 hands those back as
# Decimal, which a JSON float field must never choke on.
_NUMERIC_KEYS = frozenset(
    {
        "estimated_cost_usd",
        "compute_cost_estimated_usd",
        "compute_cost_reconciled_usd",
        "budget_cap_usd",
        "cost_usd",
        "adapter_cost_usd",
        "gold_patch_similarity",
        "wall_clock_harness_s",
        "wall_clock_eval_s",
        "agent_s",
        "task_observed_s",
        "task_billed_s",
        "repo_prep_s",
        "eval_test_s",
    }
)


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce psycopg2 native types to JSON-safe ones for this row."""
    for key in _NUMERIC_KEYS:
        value = row.get(key)
        if isinstance(value, Decimal):
            row[key] = float(value)
    return row


# dev/BUILDER4-RUNS-STATUS-FILTER-500-ISSUE-2026-08-28.md: params also
# accepts a dict for %(name)s-style queries (psycopg2 supports both). Prefer
# named params over positional ones once a query has more than one optional
# clause — see list_runs's own comment for exactly what positional binding
# got wrong here.


def _rows(
    conn: Any, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _one(
    conn: Any, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
) -> dict[str, Any] | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row is not None else None


def _count(conn: Any, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> int:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return int(row["count"]) if row else 0


def _iso(value: Any) -> Any:
    """ISO-format datetimes for JSON; pass everything else through untouched."""
    if value is not None and hasattr(value, "isoformat"):
        return value.isoformat()
    return value


# The launch-time limits a run was started with (run_launch.RunConfig -> config_snapshot). The
# run endpoint echoes them (2026-09-08) so the operator can see what a run is bounded by
# without opening the launch screen — every field None when the snapshot lacks it (a
# pre-run-launch row), never a default invented here.
_LAUNCH_LIMIT_KEYS = (
    "timeout_seconds",
    "max_tokens_per_instance",
    "max_cost_usd_per_instance",
    "max_turns_per_instance",
    "context_window_tokens",
    "max_parallel_harness_tasks",
    "ramp_step_pct",
    "ramp_cooldown_seconds",
    "autoscaler_enabled",
    "initial_budget_override",
    "harness_instructions",  # 2026-09-09: not a limit, but part of what the run was launched with
)


def _launch_limits(config_snapshot: Any) -> dict[str, Any] | None:
    """The run's launch-time limits from ``runs.config_snapshot``, or None when the row has
    no snapshot at all (a run that predates run-launch)."""
    snap = config_snapshot
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except (TypeError, ValueError):
            snap = None
    if not isinstance(snap, dict):
        return None
    return {key: snap.get(key) for key in _LAUNCH_LIMIT_KEYS}


def _provenance(config_snapshot: Any) -> dict[str, Any]:
    """Compose the run-provenance object the dashboard shows, from
    ``runs.config_snapshot`` (written by the dispatcher at run start —
    architecture.md §8). This is BUILDER2-UI-ISSUES-HANDOVER-2026-09-02.md's
    bucket A, resolved as option (b): the provenance fields live in
    config_snapshot, NOT in run_summary.summary_json, so we read them from
    their real source here rather than teaching the concurrently-written
    results-writer to nest them. Every field is optional — a pre-image or
    partially-recorded run legitimately has some as None (never invented)."""
    snap = config_snapshot
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except (TypeError, ValueError):
            snap = {}
    if not isinstance(snap, dict):
        snap = {}

    # resolved_models is {alias: {model, provider, litellm_params_model, ...}}
    # (dispatcher._resolve_model_aliases). Flatten to the literal upstream
    # model(s) for a single "model_resolved" display; keep the raw map too.
    resolved_models = snap.get("resolved_models")
    resolved_models = resolved_models if isinstance(resolved_models, dict) else {}
    # 2026-09-06: the RUN's alias first. The dispatcher now records the run's own
    # (rotatable) alias in resolved_models; before, the map held only the static
    # yaml aliases and this joined every one of them into a string that named
    # models the run never used. The join survives only as the fallback for old
    # snapshots without the run's alias.
    run_alias = snap.get("model_alias")
    own = resolved_models.get(run_alias) if isinstance(run_alias, str) else None
    own_literal = (
        str(own.get("litellm_params_model") or own.get("model"))
        if isinstance(own, dict) and (own.get("litellm_params_model") or own.get("model"))
        else None
    )
    literals = sorted(
        {
            str(v.get("litellm_params_model") or v.get("model"))
            for v in resolved_models.values()
            if isinstance(v, dict) and (v.get("litellm_params_model") or v.get("model"))
        }
    )
    model_resolved = own_literal or (", ".join(literals) if literals else None)

    return {
        "framework_sha": snap.get("framework_sha"),
        "swebench_version": snap.get("swebench_version"),
        "dataset_name": snap.get("dataset_name"),
        "dataset_revision": snap.get("dataset_revision"),
        "image_digest_snapshot": snap.get("image_digest_snapshot"),
        "harness_image_digest": snap.get("harness_image_digest"),
        "gateway_config_hash": snap.get("gateway_config_hash"),
        "model_resolved": model_resolved,
        "resolved_models": resolved_models or None,
        "context_window_tokens": snap.get("context_window_tokens"),
        "context_window_source": snap.get("context_window_source"),
    }


def list_runs(
    conn: Any,
    *,
    limit: int,
    offset: int,
    status: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """The run list, newest-first, with each run's maintained summary blob.

    ``run_summary.summary_json`` is served straight from the writer's blob
    rather than recomputed per request (§4b) — the detail endpoint recomputes
    state buckets when a single run is opened.
    """
    # dev/BUILDER4-RUNS-STATUS-FILTER-500-ISSUE-2026-08-28.md: this used to
    # bind positionally — `params + (_ACTIVE_STATES, limit, offset)` with
    # `params = (status,)`, so the tuple's order was (status, active_states,
    # limit, offset). But the ROWS query's `ir.state IN %s` placeholder sits
    # in the SELECT list, textually BEFORE `{where}`'s `r.status = %s` gets
    # substituted in after FROM/JOIN — so psycopg2 (which binds %s strictly
    # by left-to-right position in the FINAL rendered SQL, not by the order
    # params happen to be listed in code) put `status` where `_ACTIVE_STATES`
    # belonged: `ir.state IN 'running'` — a bare string where IN needs a
    # list/tuple, `psycopg2.errors.SyntaxError: syntax error at or near
    # 'running'`, exactly the live symptom. This is genuinely correct for the
    # unfiltered case (WHERE is empty, so the only %s's are IN/LIMIT/OFFSET
    # and the 3-tuple lines up) — the mismatch only exists once `status` adds
    # a clause with its own placeholder earlier in the SQL text than the
    # params tuple assumed.
    #
    # Named parameters make the whole bug class structurally impossible —
    # order in the SQL text no longer has to match order in the params
    # collection at all.
    where = "WHERE r.status = %(status)s" if status else ""
    named_params: dict[str, Any] = {"status": status} if status else {}
    total = _count(
        conn,
        f"SELECT count(*) FROM runs r {where}",
        named_params,
    )
    rows = _rows(
        conn,
        f"""SELECT r.run_id, r.status, r.created_at, r.estimated_cost_usd,
                   r.cost_confidence_tier, r.compute_cost_estimated_usd,
                   r.compute_cost_reconciled_usd, r.budget_cap_usd,
                   r.stop_requested_at, r.stop_scope, r.stop_reason, r.stopped_at,
                   r.dispatched_at, r.finalised_at, r.config_snapshot,
                   rs.summary_json AS summary,
                   (SELECT string_agg(DISTINCT rt.harness, ', ')
                      FROM run_targets rt WHERE rt.run_id = r.run_id) AS harness,
                   (SELECT string_agg(DISTINCT rt.model_alias, ', ')
                      FROM run_targets rt WHERE rt.run_id = r.run_id) AS model_alias,
                   (SELECT COALESCE(SUM(ir.cost_usd), 0)
                      FROM instance_results ir WHERE ir.run_id = r.run_id) AS cost_usd_total,
                   (SELECT count(DISTINCT ir.instance_id)
                      FROM instance_results ir WHERE ir.run_id = r.run_id) AS instance_count,
                   (SELECT count(*) FROM instance_results ir
                     WHERE ir.run_id = r.run_id) AS _ir_total,
                   (SELECT count(*) FROM instance_results ir
                     WHERE ir.run_id = r.run_id
                       AND ir.state IN %(active_states)s)
                     AS _ir_active
            FROM runs r
            LEFT JOIN run_summary rs ON rs.run_id = r.run_id
            {where}
            ORDER BY r.created_at DESC
            LIMIT %(limit)s OFFSET %(offset)s""",
        # 2026-08-28: was a hand-typed literal missing DISPATCHED — the
        # ledger's real "in flight" state for the entire image-pull/repo-prep
        # window, and (until the same-day fix) also for HARNESS_RUNNING's
        # whole runtime, since that state was never actually emitted.
        # Reference the one _ACTIVE_STATES tuple instead of a second copy so
        # this can't drift from the query at lines 186/244 again.
        {**named_params, "active_states": _ACTIVE_STATES, "limit": limit, "offset": offset},
    )
    for row in rows:
        _normalize(row)
        for key in (
            "created_at",
            "stop_requested_at",
            "stopped_at",
            "dispatched_at",
            "finalised_at",
        ):
            row[key] = _iso(row.get(key))
        # A(b): provenance is composed from config_snapshot, not summary_json.
        _snapshot = row.pop("config_snapshot", None)
        row["provenance"] = _provenance(_snapshot)
        row["launch_limits"] = _launch_limits(_snapshot)
        cost_total = row.get("cost_usd_total")
        row["cost_usd_total"] = float(cost_total) if cost_total is not None else None
        row["instance_count"] = int(row.get("instance_count") or 0)
        # terminal is computed (same rule as get_run), never read off
        # runs.status — nothing records a clean completion, so polling must
        # stop via "no active instance rows" not via a status that never flips.
        inst_total = int(row.get("_ir_total") or 0)
        inst_active = int(row.get("_ir_active") or 0)
        row.pop("_ir_total", None)
        row.pop("_ir_active", None)
        row["terminal"] = bool(row["status"] == "aborted" or (inst_total > 0 and inst_active == 0))
    return rows, total


def get_run(
    conn: Any,
    run_id: str,
) -> dict[str, Any] | None:
    """One run: metadata + summary blob + per-state buckets + terminal flag."""
    row = _one(
        conn,
        """SELECT r.run_id, r.status, r.created_at, r.config_snapshot,
                  r.estimated_cost_usd, r.cost_confidence_tier,
                  r.compute_cost_estimated_usd, r.compute_cost_reconciled_usd,
                  r.budget_cap_usd, r.stop_requested_at, r.stop_scope,
                  r.stop_reason, r.stopped_at, r.dispatched_at, r.finalised_at,
                  r.gateway_key_blocked_by,
                  rs.summary_json AS summary,
                  (SELECT string_agg(DISTINCT rt.harness, ', ')
                     FROM run_targets rt WHERE rt.run_id = r.run_id) AS harness,
                  (SELECT string_agg(DISTINCT rt.model_alias, ', ')
                     FROM run_targets rt WHERE rt.run_id = r.run_id) AS model_alias,
                  (SELECT COALESCE(SUM(ir.cost_usd), 0)
                     FROM instance_results ir WHERE ir.run_id = r.run_id) AS cost_usd_total,
                  (SELECT count(DISTINCT ir.instance_id)
                     FROM instance_results ir WHERE ir.run_id = r.run_id) AS instance_count
           FROM runs r
           LEFT JOIN run_summary rs ON rs.run_id = r.run_id
           WHERE r.run_id = %s""",
        (run_id,),
    )
    if row is None:
        return None

    state_rows = _rows(
        conn,
        """SELECT state, count(*) AS count
           FROM instance_results WHERE run_id = %s GROUP BY state ORDER BY count DESC""",
        (run_id,),
    )
    states: list[dict[str, Any]] = [
        {"state": str(s["state"]), "count": int(s["count"])} for s in state_rows
    ]
    # 2026-09-06: one bucket per INSTANCE — its latest attempt, the eval row when
    # that attempt reached eval, else the harness row. The per-(attempt, phase)
    # buckets above double-count an instance mid-eval (harness row + eval row)
    # and a restarted one (two attempts), so "41 instances" read as 60+ rows on
    # the run card; these are what an operator means by "how many are where".
    instance_state_rows = _rows(
        conn,
        """SELECT state, count(*) AS count
             FROM (SELECT DISTINCT ON (instance_id) instance_id, state
                     FROM instance_results
                    WHERE run_id = %s
                    ORDER BY instance_id, attempt_number DESC, (phase = 'eval') DESC) latest
            GROUP BY state ORDER BY count DESC, state""",
        (run_id,),
    )
    instance_states: list[dict[str, Any]] = [
        {"state": str(s["state"]), "count": int(s["count"])} for s in instance_state_rows
    ]

    active = sum(s["count"] for s in states if s["state"] in _ACTIVE_STATES)
    row_count = sum(s["count"] for s in states)
    terminal = bool(row["status"] == "aborted" or (row_count > 0 and active == 0))

    _normalize(row)
    for key in ("created_at", "stop_requested_at", "stopped_at", "dispatched_at", "finalised_at"):
        row[key] = _iso(row.get(key))
    # A(b): provenance from config_snapshot (dispatcher-written), not summary_json.
    _snapshot = row.pop("config_snapshot", None)
    row["provenance"] = _provenance(_snapshot)
    row["launch_limits"] = _launch_limits(_snapshot)
    cost_total = row.get("cost_usd_total")
    row["cost_usd_total"] = float(cost_total) if cost_total is not None else None
    row["instance_count"] = int(row.get("instance_count") or 0)
    row["states"] = states
    row["instance_states"] = instance_states
    row["terminal"] = terminal
    # BUILDER4-MANUAL-RESTART-DESIGN-V2-2026-08-29.md M2/§3: the honest
    # resolve-rate denominator (accounts for restarts) and the derived
    # close-readiness flag — both computed live, never stored, so a restart
    # landing between two dashboard polls just makes the next poll answer
    # differently, with nothing to keep in sync.
    row["resolve_rate_denominator"] = resolve_rate_denominator(conn, run_id)
    row["ready_to_close"] = bool(row_count > 0 and active == 0 and row["status"] == "running")
    return row


def get_run_status(conn: Any, run_id: str) -> str | None:
    """The run's ``status`` string, or None when the run does not exist (404 path).

    A lightweight existence check for read endpoints that do not need the full
    ``get_run`` row (e.g. ``/runs/{run_id}/live``, which the frontend polls).
    """
    row = _one(conn, "SELECT status FROM runs WHERE run_id = %s", (run_id,))
    return str(row["status"]) if row else None


# Columns the export aggregation (orchestrator/export.py) reads per phase row.
_EXPORT_COLUMNS = """
    run_id, instance_id, attempt_number, phase, state, error_category,
    retry_reason, verdict, grade_invalid, leaked_node_ids, leak_detectable,
    touches_test_files, gold_patch_similarity,
    input_tokens, output_tokens, cached_tokens, cache_write_tokens,
    reasoning_tokens, cost_usd,
    queue_wait_s, provision_s, image_pull_s, repo_prep_s, agent_s,
    eval_test_s, task_observed_s, task_billed_s
"""


def fetch_export_run(conn: Any, run_id: str) -> dict[str, Any] | None:
    """The runs row the export needs: status, created_at, config_snapshot,
    and the run-level compute-cost figures.

    ``config_snapshot`` comes back as a dict under a jsonb typecaster, else a
    JSON string — both are normalised to a dict here (psycopg2 without the
    typecaster hands back text; same handling as get_run_progress).
    """
    row = _one(
        conn,
        """SELECT run_id, status, created_at, config_snapshot,
                  compute_cost_estimated_usd, compute_cost_reconciled_usd
           FROM runs WHERE run_id = %s""",
        (run_id,),
    )
    if row is None:
        return None
    _normalize(row)
    row["created_at"] = _iso(row.get("created_at"))
    snapshot = row.get("config_snapshot")
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    row["config_snapshot"] = snapshot if isinstance(snapshot, dict) else {}
    return row


def fetch_export_instances(conn: Any, run_id: str) -> list[dict[str, Any]]:
    """All instance_results rows (both phases) the export aggregates over."""
    rows = _rows(
        conn,
        f"""SELECT {_EXPORT_COLUMNS}
            FROM instance_results
            WHERE run_id = %s
            ORDER BY instance_id, attempt_number, phase""",
        (run_id,),
    )
    for row in rows:
        _normalize(row)
    return rows


def list_reaped_attempts(conn: Any, run_id: str) -> list[dict[str, Any]]:
    """``(instance_id, attempt_number)`` pairs the reaper ABANDONED and nothing has
    overwritten yet (2026-09-08). ``/runs/{run_id}/live`` reads their progress keys too: a
    grade that was SIGTERM-handed-back and re-graded under the SAME attempt (or a straggler
    the deadline rule gave up on) is alive in Redis while Postgres says ABANDONED, and used
    to be invisible on the live panel. Only a FRESH key makes such a pair show; a reaped
    attempt with no key stays out (it is terminal, as the ledger says)."""
    return _rows(
        conn,
        """SELECT instance_id, attempt_number
           FROM instance_results
           WHERE run_id = %s
           GROUP BY instance_id, attempt_number
           HAVING bool_or(state = 'ABANDONED') AND NOT bool_or(state IN %s)
           ORDER BY instance_id, attempt_number""",
        (run_id, _ACTIVE_STATES),
    )


def list_active_attempts(conn: Any, run_id: str) -> list[dict[str, Any]]:
    """The run's non-terminal ``(instance_id, attempt_number)`` pairs (F11 live).

    The set of attempts still in flight — ``/runs/{run_id}/live`` reads each
    one's Redis progress key.  An attempt is active when ANY of its phase rows
    is in :data:`_ACTIVE_STATES` (a pair with a terminal harness row but an
    ``EVAL_RUNNING`` eval row is still live).  The ``running`` flag says
    whether the pair ever reached a RUNNING state — the live endpoint uses it
    to tell "not started yet" (PENDING/DISPATCHED, no key → ``pending``) from
    "started but the key is gone" (RUNNING, no key → ``stale``), instead of
    collapsing a missing key into a single misleading "0 turns".
    """
    return _rows(
        conn,
        """SELECT instance_id, attempt_number,
                  bool_or(state IN ('HARNESS_RUNNING', 'EVAL_RUNNING')) AS running
           FROM instance_results
           WHERE run_id = %s AND state IN %s
           GROUP BY instance_id, attempt_number
           ORDER BY instance_id, attempt_number""",
        (run_id, _ACTIVE_STATES),
    )


def get_run_progress(
    conn: Any,
    run_id: str,
) -> dict[str, Any] | None:
    """Per-phase, per-state counts + both denominators for one run (M2.4).

    Live counts come from ``instance_results`` (Postgres is truth for the
    report path, M2.5); ``expected`` and ``denominator`` come from the
    maintained ``run_summary`` blob — the dispatcher seeds ``expected`` at
    dispatch, the results writer maintains ``denominator`` = gradeable
    (ADR-0034 M1.8).  Either is ``None`` when the blob does not exist yet, so
    an aborted-at-60-of-900 run never reports resolves over 900.
    """
    row = _one(
        conn,
        """SELECT r.status, rs.summary_json AS summary
           FROM runs r
           LEFT JOIN run_summary rs ON rs.run_id = r.run_id
           WHERE r.run_id = %s""",
        (run_id,),
    )
    if row is None:
        return None

    phase_rows = _rows(
        conn,
        """SELECT phase, state, count(*) AS count
           FROM instance_results WHERE run_id = %s
           GROUP BY phase, state ORDER BY phase, state""",
        (run_id,),
    )
    phases: list[dict[str, Any]] = [
        {"phase": str(p["phase"]), "state": str(p["state"]), "count": int(p["count"])}
        for p in phase_rows
    ]

    summary = row.get("summary") or {}
    if isinstance(summary, str):  # psycopg2 without a jsonb typecaster hands back text
        try:
            summary = json.loads(summary)
        except (TypeError, ValueError):
            summary = {}
    expected = summary.get("expected")
    denominator = summary.get("denominator")

    # same terminal rule as get_run: aborted, or rows exist and none active.
    active = sum(p["count"] for p in phases if p["state"] in _ACTIVE_STATES)
    total = sum(p["count"] for p in phases)
    terminal = bool(row["status"] == "aborted" or (total > 0 and active == 0))

    return {
        "run_id": run_id,
        "status": str(row["status"]),
        "terminal": terminal,
        "phases": phases,
        "expected": int(expected) if isinstance(expected, int) else None,
        "denominator": int(denominator) if isinstance(denominator, int) else None,
    }


def list_instances(
    conn: Any,
    run_id: str,
    *,
    state: str | None = None,
    error_category: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    """Paginated, filterable instance rows for one run (RUN-state / error_category)."""
    clauses = ["run_id = %s"]
    params: list[Any] = [run_id]
    if state:
        clauses.append("state = %s")
        params.append(state)
    if error_category:
        clauses.append("error_category = %s")
        params.append(error_category)
    where = " AND ".join(clauses)

    total = _count(
        conn,
        f"SELECT count(*) FROM instance_results WHERE {where}",
        tuple(params),
    )
    rows = _rows(
        conn,
        f"""SELECT {_INSTANCE_COLUMNS}
            FROM instance_results
            WHERE {where}
            ORDER BY instance_id, attempt_number, phase
            LIMIT %s OFFSET %s""",
        tuple(params) + (limit, offset),
    )
    for row in rows:
        _normalize(row)
        row["created_at"] = _iso(row.get("created_at"))
    return rows, total


def resolve_rate_denominator(conn: Any, run_id: str) -> int:
    """The honest resolve-rate denominator, BUILDER4-MANUAL-RESTART-DESIGN-
    V2-2026-08-29.md M2: count of DISTINCT (instance_id, attempt_number)
    pairs whose ``retry_reason`` is NOT 'operator_infra_retry'.

    Deliberately separate from ``run_summary.summary_json.expected``, which
    stays frozen at the count the dispatcher originally enqueued (M1.8's
    abort-accounting invariant — unrelated to, and unaffected by, restarts).
    This is a pure read, recomputed at request time, so it can never race a
    concurrent ``/restart`` or ``/close`` the way a stored, mutated counter
    would.

    An 'operator_infra_retry' attempt collapses into whichever earlier
    attempt(s) for that instance it is retrying — it exists only because a
    prior attempt failed on infra grounds, not because it is a new trial.
    An 'operator_regrade' attempt (2026-09-01: /regrade — the SAME patch
    graded again, eval-only) collapses for the same reason, and even more
    strongly: it is not even a new harness run, just the eval half re-done.
    An 'operator_rerun_pass_at_k' attempt (restarting an instance that had
    already reached a terminal, legitimate result) is NOT collapsed — it is
    counted as an additional attempt, same as a configured
    ``attempts_per_instance > 1`` slot (``retry_reason IS NULL`` on more than
    one attempt for the same instance already counts each one).
    """
    return _count(
        conn,
        """SELECT count(DISTINCT (instance_id, attempt_number)) AS count
           FROM instance_results
           WHERE run_id = %s
             AND (retry_reason IS NULL
                  OR retry_reason NOT IN ('operator_infra_retry', 'operator_regrade'))""",
        (run_id,),
    )


def get_instance(
    conn: Any,
    run_id: str,
    instance_id: str,
    attempt_number: int,
) -> list[dict[str, Any]]:
    """The phase rows for one (run, instance, attempt) — harness + eval side by side."""
    rows = _rows(
        conn,
        f"""SELECT {_INSTANCE_COLUMNS}
            FROM instance_results
            WHERE run_id = %s AND instance_id = %s AND attempt_number = %s
            ORDER BY phase""",
        (run_id, instance_id, attempt_number),
    )
    for row in rows:
        _normalize(row)
        row["created_at"] = _iso(row.get("created_at"))
    return rows


def list_capacity(
    conn: Any,
    *,
    pool: str | None = None,
    since: str | None = None,
    limit: int,
) -> list[dict[str, Any]]:
    """Recent ``capacity_snapshot`` ticks, oldest-first, for the Axis A/B charts.

    ``since`` (ISO) lets the UI fetch only what it has not seen, so a chart
    left open can keep up without polling the whole history.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if pool:
        clauses.append("pool = %s")
        params.append(pool)
    if since:
        clauses.append("ts >= %s")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    # 2026-09-09 query audit: without `since` this returned the OLDEST `limit` rows of an
    # append-only table — the Capacity panel (which sends neither pool nor since) was showing the
    # first hours of the table's life, never the live run. Take the newest `limit` rows (the
    # (pool, ts DESC) / (ts, pool) indexes serve that as a backward seek) and hand them back
    # oldest-first, which is what the charts expect. With `since` the window is bounded already.
    cols = """ts, pool, queue_depth, not_visible, gateway_headroom, current_workers,
                   desired, binding_constraint, ceiling_utilization, decision_age_s,
                   eta_low_s, eta_high_s, constants_source,
                   pacer_queue_len, paced_over_2s_share, decision"""
    if since:
        sql = f"SELECT {cols} FROM capacity_snapshot {where} ORDER BY ts ASC LIMIT %s"
    else:
        sql = (
            f"SELECT * FROM (SELECT {cols} FROM capacity_snapshot {where} "
            f"ORDER BY ts DESC LIMIT %s) newest ORDER BY ts ASC"
        )
    rows = _rows(conn, sql, tuple(params) + (limit,))
    for row in rows:
        row["ts"] = _iso(row.get("ts"))
    return rows


def list_run_targets(conn: Any, run_id: str) -> list[tuple[str, str]]:
    """``(harness, model_alias)`` pairs for a run — the aliases the live pacer view keys on."""
    rows = _rows(
        conn,
        "SELECT harness, model_alias FROM run_targets WHERE run_id = %s ORDER BY harness",
        (run_id,),
    )
    return [(str(r["harness"]), str(r["model_alias"])) for r in rows]


def list_instance_calls(
    conn: Any, run_id: str, instance_id: str, attempt_number: int
) -> list[dict[str, Any]]:
    """Every ``llm_calls`` row for one attempt, in call order — the per-call wall-clock
    decomposition + the pacer's diagnostics (BUILDER4-PACER-SEED-AND-FAIRNESS §2.6)."""
    rows = _rows(
        conn,
        """SELECT call_index, started_at, http_status, model_resolved, error_type,
                  rate_limit_scope, shim_preflight_ms, paced_wait_ms, overload_retries,
                  overload_backoff_ms, retry_upstream_ms, ttft_ms, latency_ms,
                  pacer_was_queued, pacer_queue_len, pacer_deny_axis,
                  input_tokens, output_tokens, cached_tokens, cost_usd
           FROM llm_calls
           WHERE run_id = %s AND instance_id = %s AND attempt_number = %s
           ORDER BY call_index ASC""",
        (run_id, instance_id, attempt_number),
    )
    for row in rows:
        row["started_at"] = _iso(row.get("started_at"))
        if row.get("cost_usd") is not None:
            row["cost_usd"] = float(row["cost_usd"])
    return rows


# ── run timeline (timeline plan §4.4, 2026-09-04) ──────────────────────────────


def get_run_stamps(conn: Any, run_id: str) -> dict[str, Any] | None:
    """The run's lifecycle stamps — the timeline's window and its first event sources."""
    row = _one(
        conn,
        """SELECT run_id, status, created_at, dispatched_at, stop_requested_at, stop_scope,
                  stop_reason, stopped_at, finalised_at
           FROM runs WHERE run_id = %s""",
        (run_id,),
    )
    if row is None:
        return None
    for key in ("created_at", "dispatched_at", "stop_requested_at", "stopped_at", "finalised_at"):
        row[key] = _iso(row.get(key))
    return row


def _window_clause(column: str, start: Any, end: Any) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if start:
        clauses.append(f"{column} >= %s")
        params.append(start)
    if end:
        clauses.append(f"{column} <= %s")
        params.append(end)
    return " AND ".join(clauses), params


def list_timeline_ticks(
    conn: Any, run_id: str, *, since: str | None = None, limit: int = 50_000
) -> list[dict[str, Any]]:
    """The run's ``run_timeline_tick`` rows, oldest first (``since`` for incremental reads)."""
    where = "run_id = %s" + (" AND ts >= %s" if since else "")
    params: tuple[Any, ...] = (run_id, since) if since else (run_id,)
    rows = _rows(
        conn,
        f"""SELECT ts, run_status, in_flight, stale, pending, harness_running, eval_running,
                   resolved, unresolved, aborted, expected, denominator,
                   tok_in, tok_out, tok_cached, tok_reasoning, cost_usd_live, cost_usd_landed,
                   harness_paused, eval_paused, gateway_paused, control_stale, counts, pacer
            FROM run_timeline_tick WHERE {where}
            ORDER BY ts ASC LIMIT %s""",
        params + (limit,),
    )
    for row in rows:
        row["ts"] = _iso(row.get("ts"))
        for key in ("counts", "pacer"):
            if isinstance(row.get(key), str):
                try:
                    row[key] = json.loads(row[key])
                except (TypeError, ValueError):
                    row[key] = None
    return rows


def list_capacity_between(
    conn: Any, start: Any, end: Any, *, since: str | None = None, limit: int = 50_000
) -> list[dict[str, Any]]:
    """Both pools' capacity_snapshot rows inside [start, end] (end None = open)."""
    where, params = _window_clause("ts", since or start, end)
    rows = _rows(
        conn,
        f"""SELECT ts, pool, queue_depth, not_visible, current_workers, desired,
                   binding_constraint, ceiling_utilization, decision_age_s, eta_low_s,
                   eta_high_s, constants_source, pacer_queue_len, paced_over_2s_share, decision
            FROM capacity_snapshot {"WHERE " + where if where else ""}
            ORDER BY ts ASC, pool ASC LIMIT %s""",
        tuple(params) + (limit,),
    )
    for row in rows:
        row["ts"] = _iso(row.get("ts"))
        if isinstance(row.get("decision"), str):
            try:
                row["decision"] = json.loads(row["decision"])
            except (TypeError, ValueError):
                row["decision"] = None
    return rows


def list_run_events_between(conn: Any, run_id: str, start: Any, end: Any) -> list[dict[str, Any]]:
    """``run_events`` for this run PLUS global ones (run_id NULL) inside the window."""
    where, params = _window_clause("ts", start, end)
    scope = "(run_id = %s OR run_id IS NULL)"
    rows = _rows(
        conn,
        f"""SELECT id, ts, run_id, kind, actor, reason, detail
            FROM run_events WHERE {scope}{" AND " + where if where else ""}
            ORDER BY ts ASC, id ASC""",
        (run_id, *params),
    )
    for row in rows:
        row["ts"] = _iso(row.get("ts"))
        if isinstance(row.get("detail"), str):
            try:
                row["detail"] = json.loads(row["detail"])
            except (TypeError, ValueError):
                row["detail"] = {}
    return rows


def list_limit_edits_between(conn: Any, run_id: str, start: Any, end: Any) -> list[dict[str, Any]]:
    """operator_limit_edits in the window: this run's overrides + every global / pacer edit."""
    where, params = _window_clause("ts", start, end)
    scope = "(scope <> 'run' OR target = %s)"
    rows = _rows(
        conn,
        f"""SELECT id, ts, scope, target, field, old_value, new_value, actor, reason
            FROM operator_limit_edits WHERE {scope}{" AND " + where if where else ""}
            ORDER BY ts ASC, id ASC""",
        (run_id, *params),
    )
    for row in rows:
        row["ts"] = _iso(row.get("ts"))
    return rows


def list_discovery_between(
    conn: Any, pools: list[str], start: Any, end: Any
) -> list[dict[str, Any]]:
    """model_tpm_observations for the run's pools from three hours before the window
    (a probe usually precedes the launch) to its end."""
    if not pools:
        return []
    clauses = ["model_alias = ANY(%s)"]
    params: list[Any] = [list(pools)]
    if start:
        clauses.append("ts >= (%s::timestamptz - interval '3 hours')")
        params.append(start)
    if end:
        clauses.append("ts <= %s")
        params.append(end)
    rows = _rows(
        conn,
        f"""SELECT id, ts, model_alias, run_id, event_type, tpm_value, at_concurrency,
                   task_id, notes
            FROM model_tpm_observations WHERE {" AND ".join(clauses)}
            ORDER BY ts ASC, id ASC""",
        tuple(params),
    )
    for row in rows:
        row["ts"] = _iso(row.get("ts"))
    return rows


def list_lane_rows(conn: Any, run_id: str) -> list[dict[str, Any]]:
    """The instance_results rows (both phases) the lanes are built from."""
    rows = _rows(
        conn,
        """SELECT instance_id, attempt_number, phase, state, verdict, error_category,
                  retry_reason, grade_invalid, created_at, turns_used,
                  input_tokens, output_tokens, cached_tokens, reasoning_tokens, cost_usd,
                  paced_wait_ms_total, overload_retries_total,
                  task_observed_s, agent_s, eval_test_s, leak_detectable, leaked_node_ids,
                  touches_test_files
           FROM instance_results WHERE run_id = %s
           ORDER BY instance_id, attempt_number, phase""",
        (run_id,),
    )
    for row in rows:
        _normalize(row)
        row["created_at"] = _iso(row.get("created_at"))
        if row.get("leaked_node_ids") is not None:
            row["leaked_node_ids"] = list(row["leaked_node_ids"])
    return rows


def list_run_calls(conn: Any, run_id: str, *, limit: int = 500_000) -> list[dict[str, Any]]:
    """Every llm_calls row of the run — the per-lane call timelines (metadata only,
    never request or response bodies)."""
    rows = _rows(
        conn,
        """SELECT instance_id, attempt_number, call_index, started_at, latency_ms, ttft_ms,
                  paced_wait_ms, overload_retries, http_status, error_type, finish_reason,
                  input_tokens, output_tokens, cached_tokens, reasoning_tokens, cost_usd,
                  model_resolved, provider_name
           FROM llm_calls WHERE run_id = %s
           ORDER BY instance_id, attempt_number, call_index LIMIT %s""",
        (run_id, limit),
    )
    for row in rows:
        row["started_at"] = _iso(row.get("started_at"))
        if row.get("cost_usd") is not None:
            row["cost_usd"] = float(row["cost_usd"])
    return rows
