"""Orchestrator API — run control (ADR-0034 M1.10) + dashboard reads (architecture §10).

The API is the orchestrator's only writer of control state: it owns Aurora
(``control_state``, ``runs.stop_*``) and publishes the same facts to Valkey so
the effect is immediate.  ``/health`` is legacy; everything else falls into two
halves that never touch each other's imports:

* **Control** — pause / resume / abort (the operator's safety mechanism during
  a live run).  This half must always import cleanly (builder2 phase 1 §1 rule 1).
* **Read-only dashboard** — runs, instances, capacity, and S3 artifact proxying.
  None of these mutate anything.
* **Run launch** — ``POST /runs`` + the three read endpoints it needs
  (BUILDER4-RUN-LAUNCH-ORCHESTRATOR-2026-08-26 §3). It *starts a run* and
  spends money; the owner assigned it to builder 4 for this round
  specifically (superseding this file's earlier "no POST /runs" framing) —
  logic lives in ``run_launch_routes.py``, this file carries only the wiring.

The response bodies are declared through ``schemas`` (ADR-0009): FastAPI's
generated OpenAPI spec feeds ``openapi-typescript`` in ``ui/``, so the frontend
types and these models cannot silently drift.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Response

from swebench_eval import aws_names
from swebench_eval.control import state as control_state

from . import artifacts, queries, run_launch_routes
from .schemas import (
    AbortReport,
    AutoscalerDecision,
    CalibrationReviewHistoryItem,
    CalibrationReviewHistoryResponse,
    CalibrationReviewRecorded,
    CalibrationReviewRequest,
    CalibrationSummaryResponse,
    CapacityList,
    CapacityPoint,
    CloseReport,
    ControlMutation,
    ControlView,
    DatasetInstancesResponse,
    DimensionCalibrationItem,
    DiscoverCeilingRequest,
    DiscoverCeilingStarted,
    GatewayPauseReport,
    HarnessesResponse,
    Health,
    ImageValidationReport,
    InstanceCall,
    InstanceCalls,
    InstanceDetail,
    InstanceItem,
    InstancesList,
    InstructionPresetsResponse,
    JudgeCandidateItem,
    JudgeCandidatesResponse,
    JudgeDimensionScoreItem,
    JudgeEstimateResponse,
    JudgeLaunchRequest,
    JudgeLaunchStarted,
    JudgeLiveResponse,
    JudgeLiveState,
    JudgePassesResponse,
    JudgePassItem,
    JudgeResultItem,
    JudgeResultsResponse,
    LimitEditRequest,
    LimitEditResult,
    LimitsGlobalView,
    LimitSpec,
    LimitsRunView,
    LimitsView,
    LiveInstance,
    LlmLiveCall,
    LlmLiveDetail,
    LlmLiveList,
    ManualCeilingRequest,
    ModelCeiling,
    ModelsResponse,
    PacerAliasState,
    PacerLimitEditRequest,
    PacerLimitRow,
    QueuesList,
    QueueView,
    RegradeReport,
    RestartReport,
    RunDetail,
    RunExport,
    RunItem,
    RunLaunchRequest,
    RunList,
    RunLive,
    RunPacer,
    RunProgress,
    RunTimeline,
)

app = FastAPI(title="SWE-bench Eval API", version="0.1.0")


# --- the operator UI, served by this app (adoption Phase 2, option 2) ----------------------
# The SPA calls `/api/...`; in dev Vite proxies that prefix to this app, and here the same
# prefix is stripped in-process so the ONE build works behind the tunnel with no reverse
# proxy. The built files (UI_DIST_DIR, baked by Dockerfile.orchestrator) are mounted at /ui;
# the SPA routes by URL hash, so /ui/ is its only path and no API route is shadowed.
class _ApiPrefixRewrite:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and str(scope.get("path", "")).startswith("/api/"):
            scope = dict(scope)
            scope["path"] = scope["path"][4:]
            raw = scope.get("raw_path")
            if isinstance(raw, bytes) and raw.startswith(b"/api/"):
                scope["raw_path"] = raw[4:]
        await self._inner(scope, receive, send)


def _mount_ui(fastapi_app: FastAPI) -> str | None:
    """Mount the built SPA at /ui when UI_DIST_DIR exists; returns the directory or None."""
    from pathlib import Path

    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    dist = os.environ.get("UI_DIST_DIR", "")
    if not dist or not (Path(dist) / "index.html").exists():
        return None
    fastapi_app.mount("/ui", StaticFiles(directory=dist, html=True), name="ui")

    @fastapi_app.get("/", include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse(url="/ui/")

    return dist


@app.on_event("startup")
def _ensure_spend_log_indexes() -> None:
    """2026-09-09: our indexes on LiteLLM's spend-log table (infra/docker/spend_db_indexes.sql),
    applied in a background thread so the health check is not held behind a CONCURRENTLY build.
    The API is the only service holding LITELLM_SPEND_DATABASE_URL; no DSN → logged, skipped."""
    from swebench_eval.orchestrator.api import llm_live

    llm_live.ensure_spend_log_indexes_in_background()


# F8 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): uvicorn configures ONLY its own loggers
# (uvicorn / uvicorn.error / uvicorn.access, propagate off); the root logger had no handler,
# so every framework INFO line raised inside a request — the launch's "served by every
# replica" wait, gateway.admin's key rotation, run_launch's provisioning — was discarded by
# Python's last-resort WARNING-only handler and never reached the orchestrator-api stream.
# Same bootstrap the python -c entrypoints use; basicConfig is a no-op if a handler exists.
from swebench_eval.logging_bootstrap import configure_logging

configure_logging()

_MAX_PAGE = 500  # a 20k-instance split (ADR-0018) must not be pages of 1000+


def _db() -> Any:
    from swebench_eval.database.connection import get_connection

    return get_connection()


def _opt_int(value: Any) -> int | None:
    """Coerce a Redis-payload number to int, or None when not a number.

    Payload values come from ``json.loads`` — int/float/None, never NaN/Inf —
    so an isinstance check is the whole guard.
    """
    return int(value) if isinstance(value, (int, float)) else None


def _opt_float(value: Any) -> float | None:
    """Coerce a Redis-payload number to float, or None when not a number."""
    return float(value) if isinstance(value, (int, float)) else None


# ── health ───────────────────────────────────────────────────────────────


@app.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok")


# ── control surface (M1.10) ──────────────────────────────────────────────


@app.get("/control", response_model=ControlView)
def get_control() -> ControlView:
    """The live control view: pause flags, published_at, staleness, aborted runs.

    ``stale`` is fail-closed: a missing/expired Valkey key or a dead publisher
    reads as *all pools paused* (control/state.py), and the UI must render that
    as "cannot confirm", never as a confident PAUSED.
    """
    view = control_state.read()
    return ControlView(
        harness_paused=view.harness_paused,
        eval_paused=view.eval_paused,
        gateway_paused=view.gateway_paused,
        published_at=view.published_at,
        stale=view.stale,
        aborted_runs=sorted(view.aborted_runs),
        updated_by=view.updated_by,
        reason=view.reason,
    )


def _set_pause_in_db(pools: list[str], paused: bool, actor: str, reason: str = "") -> None:
    """Persist the pause intent to Aurora (truth), then publish to Valkey.

    The Aurora row's ``updated_at`` (stamped by the UPDATE) is READ BACK and
    carried into the Valkey hash via ``set_pause(updated_at=...)`` — the
    reconcile leg's CAS compares ``hash.updated_at`` against the row, so the
    two must come from the SAME source or clock skew defeats the guard.
    """
    conn = _db()
    cursor = conn.cursor()
    for pool in pools:
        if pool not in control_state.POOLS:
            cursor.close()
            conn.close()
            raise HTTPException(status_code=400, detail=f"unknown pool: {pool}")
        cursor.execute(
            f"UPDATE control_state SET {pool}_paused = %s, updated_at = now(), " "updated_by = %s",
            (paused, actor),
        )
    # Timeline plan §4.3 (2026-09-04): the MOMENT of a pause / resume has no other record —
    # control_state is one current row. Global event (run_id NULL); the timeline read folds
    # it into every run open at the time. Never fails the control action.
    from swebench_eval.orchestrator.control_plane import run_events

    run_events.record(
        conn,
        "pause" if paused else "resume",
        actor=actor,
        reason=reason,
        detail={"pools": list(pools)},
    )
    conn.commit()
    cursor.execute("SELECT updated_at FROM control_state WHERE id = TRUE")
    row = cursor.fetchone()
    if row and row[0] is not None:
        updated_at = row[0].timestamp()
    else:
        updated_at = time.time()
    cursor.close()
    conn.close()
    control_state.set_pause(pools, paused, actor=actor, reason=reason, updated_at=updated_at)


@app.post("/control/pause", response_model=ControlMutation)
def pause(
    pools: list[str],
    reason: str = Query(default="", description="why the operator paused (audit trail)"),
    actor: str = Query(default="operator", description="who issued the pause"),
) -> ControlMutation:
    """Pause the given pools (default harness-only — the money pool, M1 §1.10).

    BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §5: pausing
    "gateway" additionally sweeps every active run's LiteLLM key blocked —
    the flag alone (below) is a UI-visible fact, this is what actually stops
    spend. Runs already individually paused by an operator action are left
    alone (see gateway_pause.sweep_global_pause's docstring — §2's table).
    """
    _set_pause_in_db(pools, True, actor, reason)
    if "gateway" in pools:
        from swebench_eval.orchestrator.control_plane import gateway_pause

        gateway_pause.sweep_global_pause()
    return ControlMutation(pools=pools, paused=True, reason=reason, actor=actor)


@app.post("/control/resume", response_model=ControlMutation)
def resume(
    pools: list[str],
    actor: str = Query(default="operator"),
) -> ControlMutation:
    """Resume the given pools.

    Resuming "gateway" unblocks every run the global sweep blocked — never a
    run an operator individually paused (§2's table: that survives a global
    resume, it needs its own per-run resume).
    """
    _set_pause_in_db(pools, False, actor)
    if "gateway" in pools:
        from swebench_eval.orchestrator.control_plane import gateway_pause

        gateway_pause.sweep_global_resume()
    return ControlMutation(pools=pools, paused=False, actor=actor)


@app.post("/runs/{run_id}/abort", response_model=AbortReport)
def abort_run(run_id: str, body: dict[str, Any] | None = None) -> AbortReport:
    """Abort a run (scope {harness, eval, all}; default harness).

    The report is a *draining* description, not a completion: ``settled`` only
    becomes True once the drain finalises (the UI polls until the run is
    terminal rather than flipping to "aborted" on this 200, phase 1 §4a-b).
    """
    body = body or {}
    scope = str(body.get("scope", "harness"))
    reason = str(body.get("reason", ""))
    actor = str(body.get("actor", "operator"))
    if scope not in ("harness", "eval", "all"):
        raise HTTPException(status_code=400, detail=f"invalid scope: {scope}")
    from swebench_eval.orchestrator.control_plane import abort as abort_executor

    conn = _db()
    try:
        # 2026-09-04: intent + StopTask + drain answer within the api ALB's 60 s idle
        # timeout; settle + sweep + finalise continue on a thread (execute_abort's
        # docstring). settled=False and status='aborting' until the run row flips.
        report = abort_executor.execute_abort(
            connection=conn,
            run_id=run_id,
            scope=scope,
            reason=reason,
            actor=actor,
            background_settle=True,
        )
    finally:
        conn.close()
    return AbortReport(
        run_id=report.run_id,
        status="aborted" if report.settled else "aborting",
        scope=report.scope,
        reason=report.reason,
        actor=report.actor,
        in_flight_stopped=report.in_flight_stopped,
        drained=report.drained,
        drain_skipped=report.drain_skipped,
        drain_skip_reason=report.drain_skip_reason,
        settled=report.settled,
        swept=report.swept,
        abort_not_instant=True,
        note="abort is bounded by stopTimeout + upload + results-queue settle",
    )


# ── manual close + reviewed restart (BUILDER4-MANUAL-RESTART-DESIGN-V2-
# 2026-08-29.md) — the operator's other safety mechanism: no automated
# resume, no auto-close. Keys stay live until /close is called explicitly so
# a failed instance can be reviewed and restarted first.


@app.post("/runs/{run_id}/close", response_model=CloseReport)
def close_run_route(run_id: str) -> CloseReport:
    """Deliberately finalise a run — stop stragglers, revoke both keys,
    release the (harness, model_alias) pair. 409 if the run isn't 'running'
    or still has instances in flight (§1 M1: re-checked under a row lock, not
    just before this call started)."""
    from swebench_eval.orchestrator.control_plane import restart as restart_executor

    try:
        report = restart_executor.close_run(run_id)
    except restart_executor.CloseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return CloseReport(**report)


@app.post("/runs/{run_id}/restart", response_model=RestartReport)
def restart_instances_route(run_id: str, body: dict[str, Any]) -> RestartReport:
    """Restart specific failed instances as attempt N+1, operator-selected —
    never automatic (§2.2). Allowed any time the run isn't closed, whether
    mid-run or fully idle awaiting review (§2.2/§3: there is no separate
    'ready to review' state to gate on)."""
    instance_ids = body.get("instance_ids") or []
    if not isinstance(instance_ids, list) or not all(isinstance(i, str) for i in instance_ids):
        raise HTTPException(status_code=400, detail="instance_ids must be a list of strings")
    actor = str(body.get("actor", "operator"))
    from swebench_eval.orchestrator.control_plane import restart as restart_executor

    try:
        report = restart_executor.restart_instances(run_id, instance_ids, actor=actor)
    except restart_executor.RestartError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RestartReport(**report)


@app.post("/runs/{run_id}/regrade", response_model=RegradeReport)
def regrade_instances_route(run_id: str, body: dict[str, Any]) -> RegradeReport:
    """Re-grade the EXISTING patch of each selected instance as an eval-only
    attempt N+1 (2026-09-01: the eval-side counterpart /restart lacked). No
    model spend — the same captured diff goes back through grading; the
    remedy after EVAL_OOM_KILLED / ABANDONED / dead-lettered eval outcomes,
    a host resize, or a mem_limit change. Instances with no captured patch
    are skipped with a reason pointing at /restart."""
    instance_ids = body.get("instance_ids") or []
    if not isinstance(instance_ids, list) or not all(isinstance(i, str) for i in instance_ids):
        raise HTTPException(status_code=400, detail="instance_ids must be a list of strings")
    actor = str(body.get("actor", "operator"))
    from swebench_eval.orchestrator.control_plane import restart as restart_executor

    try:
        report = restart_executor.regrade_instances(run_id, instance_ids, actor=actor)
    except restart_executor.RestartError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RegradeReport(**report)


@app.post("/images/validate", response_model=ImageValidationReport)
def validate_images_route(body: dict[str, Any]) -> ImageValidationReport:
    """Gold-grade the selected instances' -inst images (image-parity Part B).
    Same body shape as /regrade (``instance_ids``, optional ``actor``); the
    grades run as eval attempts of the synthetic run ``image-validation``."""
    instance_ids = body.get("instance_ids") or []
    if not isinstance(instance_ids, list) or not all(isinstance(i, str) for i in instance_ids):
        raise HTTPException(status_code=400, detail="instance_ids must be a list of strings")
    actor = str(body.get("actor", "operator"))
    from swebench_eval.orchestrator.control_plane import image_validation

    try:
        report = image_validation.validate_images(instance_ids, actor=actor)
    except image_validation.ImageValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ImageValidationReport(**report)


# ── Live LLM-call view (BUILDER4-LITELLM-SPEND-DB-LIVE-VIEW-2026-09-02) ──────


@app.get("/runs/{run_id}/llm-live", response_model=LlmLiveList)
def llm_live_list_route(
    run_id: str,
    instance_id: str | None = None,
    attempt: int | None = None,
    limit: int = 50,
    before: str | None = None,
) -> LlmLiveList:
    """Newest-first LLM calls of a run, straight from LiteLLM's spend-log table
    (rows land live, batched at 5 — seconds behind the call). ``attempt``
    narrows ``instance_id`` to one attempt (the instance page is per attempt).
    503 when the spend DB is not configured/reachable: the view is a
    convenience atop the run, never a dependency of it."""
    from swebench_eval.orchestrator.api import llm_live

    try:
        rows = llm_live.list_llm_calls(
            run_id, instance_id=instance_id, attempt=attempt, limit=limit, before=before
        )
    except llm_live.LlmLiveUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return LlmLiveList(run_id=run_id, items=[LlmLiveCall(**r) for r in rows])


@app.get("/runs/{run_id}/llm-live/{request_id}", response_model=LlmLiveDetail)
def llm_live_detail_route(run_id: str, request_id: str) -> LlmLiveDetail:
    """One call's conversation + response, from proxy_server_request (the
    messages COLUMN is always empty in this LiteLLM version). Run-scoped: a
    request_id belonging to another run 404s, never leaks."""
    from swebench_eval.orchestrator.api import llm_live

    try:
        row = llm_live.get_llm_call(run_id, request_id)
    except llm_live.LlmLiveUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="no such call in this run")
    return LlmLiveDetail(**row)


# ── Pass B — LLM judge (offline-analysis-design.md §9.5/§9.6/§10, 2026-09-01) ──


@app.get("/runs/{run_id}/judge/candidates", response_model=JudgeCandidatesResponse)
def judge_candidates_route(run_id: str) -> JudgeCandidatesResponse:
    """§10.1: the launch UI's instance selector — every harness-phase attempt
    with its eval status (state/verdict/grade_invalid derived into `outcome`)
    and whether a judge_results row already exists, so a re-judge is visibly
    a re-judge. Mirrors LaunchScreen.tsx's data shape, plus the eval-status
    column that screen doesn't need."""
    from swebench_eval.analysis import judge as judge_module

    conn = _db()
    try:
        candidates = judge_module.fetch_candidates(conn, run_id)
        latest = judge_module.latest_judgment_by_key(conn, run_id)
    finally:
        conn.close()

    return JudgeCandidatesResponse(
        run_id=run_id,
        candidates=[
            JudgeCandidateItem(
                instance_id=c.instance_id,
                attempt_number=c.attempt_number,
                harness=c.harness,
                outcome=c.outcome,
                always_judge=c.always_judge,
                already_judged=(c.instance_id, c.attempt_number) in latest,
                last_judgment=latest.get((c.instance_id, c.attempt_number)),
            )
            for c in candidates
        ],
    )


@app.get("/runs/{run_id}/judge/estimate", response_model=JudgeEstimateResponse)
def judge_estimate_route(
    run_id: str,
    prune_mode: str = Query("pruned"),
    instance_ids: str | None = Query(None, description="comma-separated; omit for all eligible"),
    rejudge: bool = Query(False, description="count candidates that already have a judgment"),
    retry_no_verdict: bool = Query(
        False, description="also count attempts whose latest judgment timed out / failed to parse"
    ),
) -> JudgeEstimateResponse:
    """Cost preview BEFORE spend (§10.5's estimate/confirm pattern, same
    shape as GET /model-ceilings/{alias}/estimate). Mirrors the launch's
    resume rule: already-judged candidates are excluded unless ``rejudge``."""
    from swebench_eval.analysis import judge as judge_module

    ids = [i for i in instance_ids.split(",") if i] if instance_ids else None
    conn = _db()
    try:
        candidates = judge_module.fetch_candidates(conn, run_id, ids)
        if not rejudge:
            candidates = judge_module.drop_already_judged(
                conn, run_id, candidates, retry_no_verdict=retry_no_verdict
            )
    finally:
        conn.close()
    cost = judge_module.estimate_pass_cost_usd(len(candidates), prune_mode)
    return JudgeEstimateResponse(
        run_id=run_id,
        candidate_count=len(candidates),
        prune_mode=prune_mode,
        estimated_cost_usd=round(cost, 4),
    )


@app.post("/runs/{run_id}/judge", response_model=JudgeLaunchStarted)
def judge_launch_route(
    run_id: str, body: JudgeLaunchRequest, background_tasks: BackgroundTasks
) -> JudgeLaunchStarted:
    """Launches one judge pass — RunTask when JUDGE_TASK_FAMILY is configured
    (deployed path); the in-process background task remains ONLY the
    local-dev fallback, where no ECS exists (same split as
    discover_model_ceiling_route). A failed RunTask surfaces as 502 — never
    looks started."""
    from swebench_eval.analysis import judge as judge_module
    from swebench_eval.orchestrator.control_plane.llm_judge_task import _generate_pass_id

    conn = _db()
    try:
        if body.synthesis_only:
            # "regenerate report" (2026-09-08): needs recorded judgments, not candidates.
            candidates = []
            n_judged = len(judge_module.fetch_latest_results(conn, run_id))
        else:
            candidates = judge_module.fetch_candidates(conn, run_id, body.instance_ids)
            if not body.rejudge:
                candidates = judge_module.drop_already_judged(
                    conn, run_id, candidates, retry_no_verdict=body.retry_no_verdict
                )
    finally:
        conn.close()
    if body.synthesis_only and n_judged == 0:
        raise HTTPException(
            status_code=400,
            detail=f"run {run_id!r} has no judged attempts yet — nothing to report on",
        )
    if not candidates and not body.synthesis_only:
        raise HTTPException(
            status_code=400,
            detail=(
                f"no eligible candidates for run {run_id!r}"
                + (
                    ""
                    if body.rejudge
                    else " (every selected candidate is already judged — "
                    "launch with rejudge=true to judge them again)"
                )
            ),
        )

    pass_id = _generate_pass_id()
    estimated_cost = (
        judge_module.SYNTHESIS_ESTIMATE_USD
        if body.synthesis_only
        else judge_module.estimate_pass_cost_usd(len(candidates), body.prune_mode)
    )

    task_family = os.environ.get("JUDGE_TASK_FAMILY", "")
    if task_family:
        import boto3

        ecs = boto3.client("ecs", region_name=aws_names.region())
        env = [
            {"name": "JUDGE_RUN_ID", "value": run_id},
            {"name": "JUDGE_PASS_ID", "value": pass_id},
            {"name": "JUDGE_PRUNE_MODE", "value": body.prune_mode},
            {"name": "JUDGE_MODEL_ALIAS", "value": body.model_alias},
            {"name": "JUDGE_SAMPLE_RATE", "value": str(body.sample_rate)},
            {"name": "JUDGE_MIN_PER_STRATUM", "value": str(body.min_per_stratum)},
            {"name": "JUDGE_MAX_SPEND_USD", "value": str(body.max_spend_usd)},
            {"name": "JUDGE_WORKERS", "value": str(body.workers)},
            {"name": "JUDGE_REJUDGE", "value": "1" if body.rejudge else "0"},
            {"name": "JUDGE_SYNTHESIS_ONLY", "value": "1" if body.synthesis_only else "0"},
            {"name": "JUDGE_RETRY_NO_VERDICT", "value": "1" if body.retry_no_verdict else "0"},
        ]
        if body.instance_ids:
            env.append({"name": "JUDGE_INSTANCE_IDS", "value": json.dumps(body.instance_ids)})
        if body.seed is not None:
            env.append({"name": "JUDGE_SEED", "value": str(body.seed)})
        try:
            ecs.run_task(
                cluster=os.environ.get("CLUSTER", ""),
                taskDefinition=task_family,
                launchType="FARGATE",
                startedBy=f"llm-judge:{body.triggered_by}"[:36],
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": json.loads(os.environ.get("JUDGE_SUBNET_IDS", "[]")),
                        "securityGroups": json.loads(
                            os.environ.get("JUDGE_SECURITY_GROUP_IDS", "[]")
                        ),
                        "assignPublicIp": "DISABLED",
                    }
                },
                overrides={"containerOverrides": [{"name": "llm-judge", "environment": env}]},
            )
        except Exception as exc:  # surface honestly — a failed launch must never look started
            raise HTTPException(status_code=502, detail=f"RunTask failed: {exc}") from exc
    else:
        from swebench_eval.orchestrator.control_plane import llm_judge_task

        def _run_in_background() -> None:
            try:
                llm_judge_task.run(
                    run_id,
                    pass_id=pass_id,
                    instance_ids=body.instance_ids,
                    prune_mode=body.prune_mode,
                    model_alias=body.model_alias,
                    sample_rate=body.sample_rate,
                    min_per_stratum=body.min_per_stratum,
                    seed=body.seed,
                    max_spend_usd=body.max_spend_usd,
                    workers=body.workers,
                    rejudge=body.rejudge,
                    synthesis_only=body.synthesis_only,
                    retry_no_verdict=body.retry_no_verdict,
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "llm judge pass %s for run %s failed", pass_id, run_id
                )

        background_tasks.add_task(_run_in_background)

    return JudgeLaunchStarted(
        run_id=run_id, pass_id=pass_id, estimated_cost_usd=round(estimated_cost, 4)
    )


@app.get("/runs/{run_id}/judge/results", response_model=JudgeResultsResponse)
def judge_results_route(run_id: str) -> JudgeResultsResponse:
    """The per-instance rubric tab's data source (§9.6): every dimension,
    reasoning, evidence, and honesty flag — never a collapsed verdict. Latest
    judge_results row per (instance, attempt); a re-judge is a new row, not
    an overwrite, but this endpoint shows the latest by default."""
    from swebench_eval.analysis import judge as judge_module

    conn = _db()
    try:
        results = judge_module.fetch_latest_results(conn, run_id)
    finally:
        conn.close()
    return JudgeResultsResponse(
        run_id=run_id,
        results=[
            JudgeResultItem(
                instance_id=r["instance_id"],
                attempt_number=r["attempt_number"],
                judged_at=r["judged_at"],
                judge_model_resolved=r["judge_model_resolved"],
                rubric_version=r["rubric_version"],
                judge_prune_mode=r["judge_prune_mode"],
                input_truncated=r["input_truncated"],
                tool_output_pruned=r["tool_output_pruned"],
                judge_parse_failed=r["judge_parse_failed"],
                judge_method=r.get("judge_method"),
                judge_attempts=r.get("judge_attempts"),
                summary=r["summary"],
                judge_cost_usd=r["judge_cost_usd"],
                dimensions=[JudgeDimensionScoreItem(**d) for d in r["dimensions"]],
                efficiency_profile=r.get("efficiency_profile"),
            )
            for r in results
        ],
    )


@app.get("/runs/{run_id}/judge/passes", response_model=JudgePassesResponse)
def judge_passes_route(run_id: str) -> JudgePassesResponse:
    """Pass-level completeness (review 2026-09-02 §3.2): judge_sampling was
    written every pass and never read back, so an operator asking for 2,500
    judgments and getting 1,900 rows had no way to tell a complete pass from
    one the budget ceiling truncated. Newest pass first — the launch
    control's banner reads element [0]."""
    from swebench_eval.analysis import judge as judge_module

    conn = _db()
    try:
        passes = judge_module.fetch_pass_summaries(conn, run_id)
    finally:
        conn.close()
    return JudgePassesResponse(run_id=run_id, passes=[JudgePassItem(**p) for p in passes])


@app.get("/runs/{run_id}/judge/live", response_model=JudgeLiveResponse)
def judge_live_route(run_id: str) -> JudgeLiveResponse:
    """The pass in progress (2026-09-07): judge_sampling is written only when a pass ENDS,
    so this is the run screen's only view of a running pass — judged / in flight / spend /
    ETA, from the TTL'd Redis snapshot the pass's writer thread publishes. `live: null`
    means no pass is running (or nothing was published — no Redis), never "healthy"."""
    from swebench_eval.analysis import judge_live

    doc = judge_live.read_judge_live(run_id)
    if not doc:
        return JudgeLiveResponse(run_id=run_id, live=None)
    try:
        live = JudgeLiveState(
            **{k: v for k, v in doc.items() if k in JudgeLiveState.model_fields},
        )
    except Exception:  # noqa: BLE001 — a snapshot from another version reads as absent
        logging.getLogger(__name__).warning(
            "judge live: unreadable snapshot for %s (%s)", run_id, sorted(doc)[:8]
        )
        return JudgeLiveResponse(run_id=run_id, live=None)
    return JudgeLiveResponse(run_id=run_id, live=live)


@app.post(
    "/runs/{run_id}/judge/results/{instance_id}/{attempt_number}/review",
    response_model=CalibrationReviewRecorded,
)
def judge_calibration_review_route(
    run_id: str, instance_id: str, attempt_number: int, body: CalibrationReviewRequest
) -> CalibrationReviewRecorded:
    """offline-analysis-design.md §11: approve/deny + reasoning on one
    dimension of the judge's own verdict — the in-app replacement for §3.9's
    offline hand-labeling. ``body.judged_at`` pins the exact judge_results
    row a re-judge can't silently retarget."""
    from swebench_eval.analysis import calibration

    conn = _db()
    try:
        try:
            calibration.record_review(
                conn,
                run_id=run_id,
                instance_id=instance_id,
                attempt_number=attempt_number,
                judged_at=body.judged_at,
                dimension_id=body.dimension_id,
                decision=body.decision,
                reviewer_reasoning=body.reviewer_reasoning,
                reviewed_by=body.reviewed_by,
                corrected_score_numeric=body.corrected_score_numeric,
                corrected_score_secondary=body.corrected_score_secondary,
                corrected_flag=body.corrected_flag,
            )
        except calibration.ReviewTargetNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()

    return CalibrationReviewRecorded(
        run_id=run_id,
        instance_id=instance_id,
        attempt_number=attempt_number,
        dimension_id=body.dimension_id,
    )


@app.get(
    "/runs/{run_id}/judge/results/{instance_id}/{attempt_number}/reviews",
    response_model=CalibrationReviewHistoryResponse,
)
def judge_review_history_route(
    run_id: str, instance_id: str, attempt_number: int, judged_at: str = Query(...)
) -> CalibrationReviewHistoryResponse:
    """§11: so the per-instance Judge panel can show "already reviewed"
    state — without this, a reviewer has no way to see a prior decision and
    would either re-review blind or have to trust their own memory across a
    page reload. ``judged_at`` pins the exact judge_results row, same rule
    as the POST review route."""
    from swebench_eval.analysis import calibration

    conn = _db()
    try:
        reviews = calibration.fetch_reviews_for_result(
            conn, run_id, instance_id, attempt_number, judged_at
        )
    finally:
        conn.close()
    return CalibrationReviewHistoryResponse(
        run_id=run_id,
        instance_id=instance_id,
        attempt_number=attempt_number,
        reviews=[CalibrationReviewHistoryItem(**r) for r in reviews],
    )


@app.get("/judge/calibration", response_model=CalibrationSummaryResponse)
def judge_calibration_summary_route() -> CalibrationSummaryResponse:
    """offline-analysis-design.md §11: deliberately NOT run-scoped — the
    judge-model alias being calibrated is the same one across every run, so
    the 20-distinct-instance threshold (§3.9's original bar) accumulates
    across runs. The launch control's coverage readout reads this."""
    from swebench_eval.analysis import calibration

    conn = _db()
    try:
        summary = calibration.calibration_summary(conn)
    finally:
        conn.close()
    return CalibrationSummaryResponse(
        dimensions=[DimensionCalibrationItem(**d.__dict__) for d in summary],
        min_distinct_reviewed_instances=calibration.MIN_DISTINCT_REVIEWED_INSTANCES,
    )


# ── per-run gateway pause/resume (BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-
# 2026-08-31.md §5) — sits alongside close/restart/abort: blocks/unblocks
# this run's LiteLLM key specifically, independent of the global "gateway"
# pool flag above (§2's precedence table).


@app.post("/runs/{run_id}/pause-gateway", response_model=GatewayPauseReport)
def pause_gateway_route(run_id: str, body: dict[str, Any] | None = None) -> GatewayPauseReport:
    """Block this run's LiteLLM key. Legal any time the run isn't closed,
    regardless of the current global gateway-pause state — an explicit
    per-run action is always authoritative for that one run."""
    body = body or {}
    actor = str(body.get("actor", "operator"))
    from swebench_eval.orchestrator.control_plane import gateway_pause

    try:
        report = gateway_pause.pause_gateway(run_id, actor=actor)
    except gateway_pause.GatewayPauseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return GatewayPauseReport(**report)


@app.post("/runs/{run_id}/resume-gateway", response_model=GatewayPauseReport)
def resume_gateway_route(run_id: str, body: dict[str, Any] | None = None) -> GatewayPauseReport:
    """Unblock this run's LiteLLM key — a carve-out even while the gateway
    pool is globally paused (§2's table: this run resumes, others stay
    blocked)."""
    body = body or {}
    actor = str(body.get("actor", "operator"))
    from swebench_eval.orchestrator.control_plane import gateway_pause

    try:
        report = gateway_pause.resume_gateway(run_id, actor=actor)
    except gateway_pause.GatewayPauseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return GatewayPauseReport(**report)


# ── model ceiling discovery (BUILDER4-AUTOSCALER-TPM-CEILING-DISCOVERY-DESIGN-
# 2026-08-31.md, Part 1). Manual-only per §5: every route below is the ONLY
# way any of this ever runs — nothing else in the system (staleness, run
# launch, the eventual live control law) is ever allowed to self-trigger it.

_KNOWN_MODEL_ALIASES = (
    "laguna-xs-2.1",
    "qwen3-coder-next",
    "deepseek-v4-flash-0731",
    "gpt-5-mini",
    "minimax-m2.5",
)


@app.get("/model-ceilings", response_model=list[ModelCeiling])
def list_model_ceilings() -> list[ModelCeiling]:
    from swebench_eval.orchestrator.control_plane import ceiling_discovery

    return [ModelCeiling(**row) for row in ceiling_discovery.list_ceilings(_KNOWN_MODEL_ALIASES)]


@app.get("/model-ceilings/{model_alias}/estimate", response_model=DiscoverCeilingStarted)
def estimate_discovery_cost_route(
    model_alias: str,
    target_concurrency: int = Query(150, ge=1, le=500),
    ramp_mode: str = Query("target_first", pattern="^(target_first|bottom_up)$"),
    target_tasks: int = Query(60, ge=1, le=500),
) -> DiscoverCeilingStarted:
    """Cost preview for the UI's confirm gate (design §5): the operator sees the number BEFORE
    confirming real spend. Same fields as the started response, status='estimate'."""
    from swebench_eval.orchestrator.control_plane import ceiling_discovery

    try:
        target_tokens = ceiling_discovery.resolve_target_tokens(model_alias)
    except ceiling_discovery.CeilingDiscoveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cost = ceiling_discovery.estimate_cost(
        model_alias, target_tokens, target_concurrency, ramp_mode=ramp_mode
    )
    return DiscoverCeilingStarted(
        model_alias=model_alias,
        target_concurrency=target_concurrency,
        estimated_cost_usd=round(cost, 4),
        status="estimate",
        ramp_mode=ramp_mode,
        target_tasks=target_tasks,
    )


@app.post("/model-ceilings/{model_alias}/discover", response_model=DiscoverCeilingStarted)
def discover_model_ceiling_route(
    model_alias: str, body: DiscoverCeilingRequest, background_tasks: BackgroundTasks
) -> DiscoverCeilingStarted:
    """Kicks off one discovery probe as a background task and returns immediately — a full
    ramp+bisect can take minutes (design doc §2), too long to hold an HTTP request open. The UI
    polls ``GET /model-ceilings`` for the result; a fresh ``discovered_at`` IS the completion
    signal, no separate job-status tracking needed."""
    from swebench_eval.orchestrator.control_plane import ceiling_discovery

    try:
        target_tokens = ceiling_discovery.resolve_target_tokens(model_alias)
    except ceiling_discovery.CeilingDiscoveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.ramp_mode not in ("target_first", "bottom_up"):
        raise HTTPException(status_code=400, detail=f"unknown ramp_mode {body.ramp_mode!r}")
    if body.target_tasks < 1 or body.target_tasks > 500:
        raise HTTPException(status_code=400, detail="target_tasks must be 1..500")
    estimated_cost = ceiling_discovery.estimate_cost(
        model_alias, target_tokens, body.target_concurrency, ramp_mode=body.ramp_mode
    )

    # exact-design §6/§8: deployed, the probe runs as its OWN one-shot ECS task (a bursty,
    # ~150-connection, real-money operation must not share the API process). RunTask is used
    # when DISCOVERY_TASK_FAMILY is configured (the terraform wires it); the in-process
    # background task remains ONLY as the local-dev fallback, where no ECS exists.
    task_family = os.environ.get("DISCOVERY_TASK_FAMILY", "")
    if task_family:
        import boto3

        ecs = boto3.client("ecs", region_name=aws_names.region())
        try:
            ecs.run_task(
                cluster=os.environ.get("CLUSTER", ""),
                taskDefinition=task_family,
                launchType="FARGATE",
                startedBy=f"ceiling-discovery:{body.triggered_by}"[:36],
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": json.loads(os.environ.get("DISCOVERY_SUBNET_IDS", "[]")),
                        "securityGroups": json.loads(
                            os.environ.get("DISCOVERY_SECURITY_GROUP_IDS", "[]")
                        ),
                        "assignPublicIp": "DISABLED",
                    }
                },
                overrides={
                    "containerOverrides": [
                        {
                            "name": "ceiling-discovery",
                            "environment": [
                                {"name": "DISCOVERY_MODEL_ALIAS", "value": model_alias},
                                {
                                    "name": "DISCOVERY_FLEET_TARGET",
                                    "value": str(body.target_concurrency),
                                },
                                {"name": "DISCOVERY_TRIGGERED_BY", "value": body.triggered_by},
                                {"name": "DISCOVERY_RAMP_MODE", "value": body.ramp_mode},
                                {"name": "DISCOVERY_TARGET_TASKS", "value": str(body.target_tasks)},
                            ],
                        }
                    ]
                },
            )
        except Exception as exc:  # surface honestly — a failed launch must never look started
            raise HTTPException(status_code=502, detail=f"RunTask failed: {exc}") from exc
    else:

        def _run_in_background() -> None:
            try:
                asyncio.run(
                    ceiling_discovery.run_discovery(
                        model_alias,
                        fleet_target=body.target_concurrency,
                        triggered_by=body.triggered_by,
                        ramp_mode=body.ramp_mode,
                        target_tasks=body.target_tasks,
                    )
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "ceiling discovery for %s failed", model_alias
                )

        background_tasks.add_task(_run_in_background)
    return DiscoverCeilingStarted(
        model_alias=model_alias,
        target_concurrency=body.target_concurrency,
        estimated_cost_usd=round(estimated_cost, 4),
        ramp_mode=body.ramp_mode,
        target_tasks=body.target_tasks,
    )


@app.post("/model-ceilings/{model_alias}/manual", response_model=ModelCeiling)
def manual_model_ceiling_route(model_alias: str, body: ManualCeilingRequest) -> ModelCeiling:
    from swebench_eval.orchestrator.control_plane import ceiling_discovery

    ceiling_discovery.record_manual_ceiling(model_alias, body.tpm_value, notes=body.notes)
    return ModelCeiling(**ceiling_discovery.list_ceilings((model_alias,))[0])


# ── operator limits (owner request 2026-09-04) ───────────────────────────
#
# Runtime edits of the numbers that drive dispatch and pacing: the run-overrides
# hash the dispatcher re-reads every tick (per-run cap, planner ceiling override,
# growth step, cooldown, planner gate), the global operator:limits hash (borrowed-
# curve cap, growth clamp, utilisation, eval max workers / scale-in delay) and
# pacer:cfg fields per alias. Every edit is one operator_limit_edits audit row.
# Logic lives in control_plane/operator_limits.py; this is the wiring.


def _limits_client() -> Any:
    from swebench_eval.database import redis_client as redis_reads

    if not redis_reads.is_redis_reachable():
        raise HTTPException(status_code=503, detail="redis unreachable — limits unavailable")
    return redis_reads._get_client()


@app.get("/limits", response_model=LimitsView)
def get_limits(run_id: str | None = Query(default=None)) -> LimitsView:
    """Every knob with its effective value and source; with ``run_id`` also the pacer cfg
    of each alias the run targets (and of its pool). ``state='unknown'`` when Redis is
    unreachable — nothing here is trustworthy then, and the UI must not render zeros."""
    from swebench_eval.database import redis_client as redis_reads
    from swebench_eval.orchestrator.api import pacer_view
    from swebench_eval.orchestrator.control_plane import operator_limits

    if not redis_reads.is_redis_reachable():
        return LimitsView(state="unknown")
    aliases: list[tuple[str, str]] = []
    if run_id:
        conn = _db()
        try:
            if queries.get_run_status(conn, run_id) is None:
                raise HTTPException(status_code=404, detail=f"run {run_id} not found")
            aliases = pacer_view.resolve_pacer_aliases(queries.list_run_targets(conn, run_id))
        finally:
            conn.close()
    view = operator_limits.effective_view(redis_reads._get_client(), aliases)
    return LimitsView(
        state="ok",
        run=LimitsRunView(**view["run"]),
        global_=LimitsGlobalView(**view["global"]),
        static=view["static"],
        pacer=[PacerLimitRow(**row) for row in view["pacer"]],
        specs=[LimitSpec(**s) for s in view["specs"]],
    )


@app.post("/limits/run", response_model=LimitEditResult)
def set_run_limit(body: LimitEditRequest) -> LimitEditResult:
    """Set (value) or clear (null) one field of the run-overrides hash. Live within one
    dispatcher tick (15 s) / ground-truth refresh (10 s)."""
    from swebench_eval.orchestrator.control_plane import operator_limits

    try:
        out = operator_limits.set_run(
            _limits_client(), body.field, body.value, body.actor, body.reason
        )
    except operator_limits.LimitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LimitEditResult(
        scope="run",
        target=out.get("run_id") or "",
        field=out["field"],
        old=out["old"],
        new=out["new"],
    )


@app.post("/limits/global", response_model=LimitEditResult)
def set_global_limit(body: LimitEditRequest) -> LimitEditResult:
    """Set (value) or clear (null) one global knob. Live within one dispatcher / eval-scaler
    tick; survives an eval-tier destroy (rehydrated from the audit table at supervisor start)."""
    from swebench_eval.orchestrator.control_plane import operator_limits

    try:
        out = operator_limits.set_global(
            _limits_client(), body.field, body.value, body.actor, body.reason
        )
    except operator_limits.LimitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LimitEditResult(
        scope="global", target="", field=out["field"], old=out["old"], new=out["new"]
    )


@app.post("/limits/pacer/{alias}", response_model=LimitEditResult)
def set_pacer_limit(alias: str, body: PacerLimitEditRequest) -> LimitEditResult:
    """Set one pacer:cfg field on the alias, live on the next admission. Seed-relative fields
    re-base their _seed; seeded_at is re-stamped. ``also_pool`` writes the pool key too and
    persists a pacer_cfg_seeds row so the next launch and bring-up inherit it."""
    from swebench_eval.orchestrator.control_plane import operator_limits

    try:
        out = operator_limits.set_pacer(
            _limits_client(),
            alias,
            body.field,
            body.value,
            body.actor,
            body.reason,
            also_pool=body.also_pool,
        )
    except operator_limits.LimitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LimitEditResult(
        scope="pacer",
        target=alias,
        field=out["field"],
        old=out["old"],
        new=out["new"],
        pool=out.get("pool"),
    )


# ── run launch (builder4, BUILDER4-RUN-LAUNCH-ORCHESTRATOR §3) ────────────
#
# The one mutation this file adds beyond the control surface above: it starts
# a run and spends money.  Logic lives in run_launch_routes.py — this is the
# minimum wiring (§0 lane rule: "touch only the files you add plus the
# minimum wiring in api/main.py").


@app.post("/runs", status_code=201)
def create_run(request: RunLaunchRequest) -> Response:
    """§3.1: 201 on launch; 409 (flat body, "the id is the payload, not
    decoration") on a duplicate (harness, model_alias) pair; 503 (D4: fail
    closed) when the OpenRouter provisioning key is unavailable.  No
    ``response_model`` here — the three outcomes have different shapes, and
    FastAPI's default ``HTTPException`` would wrap the 409 body in
    ``{"detail": ...}``, which is not the contract.
    """
    import json as _json

    from swebench_eval.orchestrator.control_plane.run_launch import (
        DuplicateRunError,
        GatewayPausedError,
        NoProvisioningKeyError,
    )

    from .schemas import DuplicateRunResponse

    try:
        result = run_launch_routes.launch(request)
    except DuplicateRunError as exc:
        body = DuplicateRunResponse(
            run_id=exc.existing_run_id,
            harness=exc.harness,
            model_alias=exc.model_alias,
            message=f"a run is already in progress: {exc.existing_run_id}",
        )
        return Response(
            content=body.model_dump_json(), status_code=409, media_type="application/json"
        )
    except NoProvisioningKeyError as exc:
        return Response(
            content=_json.dumps({"status": "refused", "message": str(exc)}),
            status_code=503,
            media_type="application/json",
        )
    except GatewayPausedError as exc:
        return Response(
            content=_json.dumps({"status": "refused", "message": str(exc)}),
            status_code=503,
            media_type="application/json",
        )
    return Response(
        content=result.model_dump_json(), status_code=201, media_type="application/json"
    )


@app.get("/dataset/instances", response_model=DatasetInstancesResponse)
def get_dataset_instances() -> DatasetInstancesResponse:
    return run_launch_routes.dataset_instances()


@app.get("/harnesses", response_model=HarnessesResponse)
def get_harnesses() -> HarnessesResponse:
    return run_launch_routes.harnesses()


@app.get("/models", response_model=ModelsResponse)
def get_models() -> ModelsResponse:
    return run_launch_routes.models()


@app.get("/launch/instruction-presets", response_model=InstructionPresetsResponse)
def get_instruction_presets() -> InstructionPresetsResponse:
    """2026-09-09 efficiency prompt arm: starting texts for the launch screen's
    harness-instructions field, plus its size cap."""
    return run_launch_routes.instruction_presets()


# ── read-only dashboard endpoints (architecture §10) ──────────────────────
#
# All reads go to Aurora (the truth store for runs/instances/capacity); the
# frontend polls these slowly and stops when idle (§4b) so Aurora auto-pause is
# never defeated by a dashboard nobody is looking at.


@app.get("/runs", response_model=RunList)
def list_runs(
    limit: int = Query(default=50, ge=1, le=_MAX_PAGE),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, description="filter by runs.status"),
) -> RunList:
    """The run list, newest-first.  Each item carries its maintained summary."""
    conn = _db()
    try:
        rows, total = queries.list_runs(conn, limit=limit, offset=offset, status=status)
    finally:
        conn.close()
    return RunList(items=[RunItem(**row) for row in rows], total=total, limit=limit, offset=offset)


@app.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str) -> RunDetail:
    """One run: metadata, maintained summary, per-state buckets, terminal flag."""
    conn = _db()
    try:
        row = queries.get_run(conn, run_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return RunDetail(**row)


@app.get("/runs/{run_id}/progress", response_model=RunProgress)
def get_run_progress(run_id: str) -> RunProgress:
    """Per-phase, per-state counts + expected + denominator for one run (M2.4).

    Counts come from Postgres (the report path is Postgres-truth, M2.5) rather
    than the M2.2 Redis sets that do not exist yet; ``expected``/``denominator``
    come from the maintained ``run_summary`` blob.  They are surfaced as two
    separate, labelled numbers on purpose — an aborted run must never report a
    resolve rate over the dispatch plan.
    """
    conn = _db()
    try:
        row = queries.get_run_progress(conn, run_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return RunProgress(**row)


@app.get("/runs/{run_id}/live", response_model=RunLive)
def get_run_live(run_id: str) -> RunLive:
    """Per-instance in-flight progress from Redis (F11), over the Postgres
    attempt list.

    The shim writes per-turn progress to Redis on every serviced turn
    (``harness_worker`` → ``redis_client.write_progress``); nothing read it.
    This enumerates the run's non-terminal attempts from Postgres
    (``queries.list_active_attempts``) and reads each one's TTL'd key.

    The part that is easy to get wrong: a missing key has at least three
    causes (not started / TTL expired / worker died) and they must not all
    render as "0 turns" — each instance gets an explicit state (running /
    pending / stale), and if Redis itself is unreachable the WHOLE response
    is ``state="unknown"``, never an empty list that reads as "nothing is
    running".
    """
    from swebench_eval.database import redis_client as redis_reads

    conn = _db()
    try:
        status = queries.get_run_status(conn, run_id)
        if status is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        attempts = queries.list_active_attempts(conn, run_id)
        # 2026-09-08: attempts the reaper ABANDONED but a worker may still be grading (a
        # SIGTERM-handed-back grade re-run under the same attempt; a straggler past the
        # deadline). Shown ONLY when their progress key is live — see list_reaped_attempts.
        reaped = queries.list_reaped_attempts(conn, run_id)
    finally:
        conn.close()

    if not redis_reads.is_redis_reachable():
        return RunLive(
            run_id=run_id,
            status=status,
            state="unknown",
            reason="redis_unreachable",
            items=[],
        )

    now = time.time()
    items: list[LiveInstance] = []
    for a, revived in [(a, False) for a in attempts] + [(r, True) for r in reaped]:
        payload = redis_reads.read_progress(run_id, a["instance_id"], a["attempt_number"])
        if payload is None:
            if revived:
                continue  # terminal in Postgres AND silent in Redis: not live, as the ledger says
            # Missing key — explicit state, never a fabricated "0 turns".
            # Every number is None (not measured), matching the null rule.
            items.append(
                LiveInstance(
                    instance_id=a["instance_id"],
                    attempt_number=a["attempt_number"],
                    state="stale" if a["running"] else "pending",
                    turn_number=None,
                    input_tokens=None,
                    output_tokens=None,
                    cached_tokens=None,
                    reasoning_tokens=None,
                    cost_usd=None,
                    observed_at=None,
                    age_s=None,
                )
            )
            continue
        observed_at = payload.get("updated_at")
        age_s = max(0.0, now - observed_at) if isinstance(observed_at, (int, float)) else None
        items.append(
            LiveInstance(
                instance_id=a["instance_id"],
                attempt_number=a["attempt_number"],
                state="running",
                turn_number=_opt_int(payload.get("turn_number")),
                input_tokens=_opt_int(payload.get("input_tokens")),
                output_tokens=_opt_int(payload.get("output_tokens")),
                cached_tokens=_opt_int(payload.get("cached_tokens")),
                reasoning_tokens=_opt_int(payload.get("reasoning_tokens")),
                cost_usd=_opt_float(payload.get("cost_usd")),
                observed_at=observed_at if isinstance(observed_at, (int, float)) else None,
                age_s=age_s,
                # Pacer footprint (design doc §2.5) — None when the payload predates it.
                paced_wait_ms_total=_opt_int(payload.get("paced_wait_ms_total")),
                paced_calls=_opt_int(payload.get("paced_calls")),
                pacer_timeouts=_opt_int(payload.get("pacer_timeouts")),
                overload_retries_total=_opt_int(payload.get("overload_retries_total")),
                pacer_last_deny_axis=(
                    str(payload["pacer_last_deny_axis"])
                    if payload.get("pacer_last_deny_axis")
                    else None
                ),
                pacer_last_queue_len=_opt_int(payload.get("pacer_last_queue_len")),
                # eval-phase live progress (grade_progress tee) — None on harness payloads
                phase=str(payload["phase"]) if payload.get("phase") else None,
                eval_elapsed_s=_opt_float(payload.get("eval_elapsed_s")),
                eval_lines=_opt_int(payload.get("eval_lines")),
                eval_last_line=(
                    str(payload["eval_last_line"]) if payload.get("eval_last_line") else None
                ),
                eval_silent_s=_opt_float(payload.get("eval_silent_s")),
                revived_after_reap=revived,
            )
        )
    return RunLive(run_id=run_id, status=status, state="ok", items=items)


@app.get("/runs/{run_id}/pacer", response_model=RunPacer)
def get_run_pacer(run_id: str) -> RunPacer:
    """Live L1 pacer state per alias of this run, straight from the Redis ledger the shims
    share (BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03.md §2.5): cfg, bucket fill, in-flight
    volume, the WAIT QUEUE (each denied call's size + wait), and the last 60 s of the
    pacer's own admission / over-2s / overload counters. Same health contract as /live:
    Redis unreachable → whole response ``unknown``, never an empty list."""
    from swebench_eval.database import redis_client as redis_reads
    from swebench_eval.orchestrator.api import pacer_view

    conn = _db()
    try:
        status = queries.get_run_status(conn, run_id)
        if status is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        targets = queries.list_run_targets(conn, run_id)
    finally:
        conn.close()
    if not redis_reads.is_redis_reachable():
        return RunPacer(run_id=run_id, state="unknown", reason="redis_unreachable", items=[])
    client = redis_reads._get_client()
    items = [
        PacerAliasState(**pacer_view.read_alias_state(client, alias, harness))
        for harness, alias in pacer_view.resolve_pacer_aliases(targets)
    ]
    return RunPacer(run_id=run_id, state="ok", items=items)


@app.get("/autoscaler/{pool}", response_model=AutoscalerDecision)
def get_autoscaler_decision(pool: str) -> AutoscalerDecision:
    """The pool's live L2 decision record from Redis (forecast review 2026-09-03): mode,
    desired ceiling vs ECS in-flight, binding constraint (+ which alias), per-alias
    ceilings / bindings / curve sources, booting tasks, hold-cap timeouts, wait-queue depth.
    Absent and unknown are distinct states — an expired record must never read as 'planner
    says go'."""
    if pool not in ("harness", "eval"):
        raise HTTPException(status_code=400, detail="pool must be harness or eval")
    from swebench_eval.database import redis_client as redis_reads
    from swebench_eval.orchestrator.control_plane.decision_record import decision_key

    if not redis_reads.is_redis_reachable():
        return AutoscalerDecision(pool=pool, state="unknown")
    try:
        raw = redis_reads._get_client().get(decision_key(pool))
    except Exception:  # noqa: BLE001 — a read failure is 'unknown', never 'absent'
        return AutoscalerDecision(pool=pool, state="unknown")
    if not raw:
        return AutoscalerDecision(pool=pool, state="absent")
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        return AutoscalerDecision(pool=pool, state="unknown")
    decided_at = record.get("decided_at")
    age = (
        max(0.0, time.time() - float(decided_at)) if isinstance(decided_at, (int, float)) else None
    )
    return AutoscalerDecision(
        pool=pool, state="ok", age_s=round(age, 1) if age is not None else None, record=record
    )


@app.get(
    "/runs/{run_id}/instances/{instance_id}/{attempt_number}/calls",
    response_model=InstanceCalls,
)
def get_instance_calls(run_id: str, instance_id: str, attempt_number: int) -> InstanceCalls:
    """The attempt's ``llm_calls`` rows in call order — per-call wall-clock decomposition
    (preflight / paced wait / retried round-trips / backoff / final latency) + the pacer's
    diagnostics (design doc §2.6). Empty list = no rows landed (yet); the writer ingests
    ``llm_calls.jsonl`` after the attempt finishes, so an in-flight attempt reads empty here
    — the live view is /live and /pacer."""
    conn = _db()
    try:
        if queries.get_run_status(conn, run_id) is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        rows = queries.list_instance_calls(conn, run_id, instance_id, attempt_number)
    finally:
        conn.close()
    return InstanceCalls(
        run_id=run_id,
        instance_id=instance_id,
        attempt_number=attempt_number,
        items=[InstanceCall(**row) for row in rows],
    )


@app.get("/runs/{run_id}/timeline", response_model=RunTimeline)
def get_run_timeline(
    run_id: str,
    since: str | None = Query(
        default=None, description="ISO timestamp; ticks and capacity rows at or after it only"
    ),
    include_calls: bool = Query(
        default=True, description="include the run's llm_calls rows (the per-lane timelines)"
    ),
) -> RunTimeline:
    """The undecimated, un-scrubbed timeline read for one run (timeline plan §4.4):
    the per-run ticks (``run_timeline_tick``), both pools' ``capacity_snapshot`` rows in the
    run's window, every event source (runs.*_at stamps, run_events, operator_limit_edits,
    model_tpm_observations), the instance rows the lanes are built from, and the llm_calls
    rows. Aurora only — works during and after the run. ``scripts/export_run_timeline.py``
    turns this into the site's files (columnar, decimated, scrubbed); nothing here is
    shaped for the site directly.
    """
    conn = _db()
    try:
        stamps = queries.get_run_stamps(conn, run_id)
        if stamps is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        start = stamps.get("dispatched_at") or stamps.get("created_at")
        end = stamps.get("finalised_at") or stamps.get("stopped_at")
        targets = queries.list_run_targets(conn, run_id)
        aliases = sorted({alias for _h, alias in targets})
        from swebench_eval.gateway.rotatable_models import pool_alias_for

        pools = sorted({pool_alias_for(a) or a for a in aliases})
        ticks = queries.list_timeline_ticks(conn, run_id, since=since)
        capacity = queries.list_capacity_between(conn, start, end, since=since)
        events = queries.list_run_events_between(conn, run_id, start, end)
        limit_edits = queries.list_limit_edits_between(conn, run_id, start, end)
        discovery = queries.list_discovery_between(conn, pools, start, end)
        lane_rows = queries.list_lane_rows(conn, run_id)
        calls = queries.list_run_calls(conn, run_id) if include_calls else []
    finally:
        conn.close()
    return RunTimeline(
        run_id=run_id,
        status=str(stamps.get("status")),
        window_start=start,
        window_end=end,
        stamps=stamps,
        targets=[{"harness": h, "model_alias": a} for h, a in targets],
        tick_interval_s=30,
        ticks=ticks,
        capacity=capacity,
        events=events,
        limit_edits=limit_edits,
        discovery=discovery,
        lane_rows=lane_rows,
        calls=calls,
    )


@app.get("/runs/{run_id}/export", response_model=RunExport)
def get_run_export(run_id: str) -> RunExport:
    """The M6.2 publication artifact — the contract in publication-site-design
    §5.1, built EXACTLY (builder 2 builds the site against it).

    Every numeric field is nullable: a null renders as "not measured" and is
    never coerced to zero.  ``totals.attempted`` comes from the resolve-rate
    denominator (infra-retry collapse), not the frozen dispatch ``expected``;
    ``pass_at_k`` honours ``retry_reason`` (operator_rerun_pass_at_k and
    configured attempts_per_instance slots are legitimate k, operator_infra_
    retry is not); aborted instances are excluded from both denominators.
    """
    from swebench_eval.orchestrator import export as export_assembly

    conn = _db()
    try:
        run = queries.fetch_export_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        rows = queries.fetch_export_instances(conn, run_id)
        denominator = queries.resolve_rate_denominator(conn, run_id)
    finally:
        conn.close()

    payload = export_assembly.build_run_export(run, rows, resolve_rate_denominator=denominator)
    return RunExport(**payload)


@app.get("/runs/{run_id}/instances", response_model=InstancesList)
def list_run_instances(
    run_id: str,
    state: str | None = Query(default=None, description="filter by instance state"),
    error_category: str | None = Query(
        default=None, description="filter by error taxonomy category (§9.3)"
    ),
    limit: int = Query(default=50, ge=1, le=_MAX_PAGE),
    offset: int = Query(default=0, ge=0),
) -> InstancesList:
    """Paginated, filterable instance rows for one run."""
    conn = _db()
    try:
        rows, total = queries.list_instances(
            conn, run_id, state=state, error_category=error_category, limit=limit, offset=offset
        )
    finally:
        conn.close()
    return InstancesList(
        items=[InstanceItem(**row) for row in rows], total=total, limit=limit, offset=offset
    )


@app.get("/instances/{run_id}/{instance_id}/{attempt}", response_model=InstanceDetail)
def get_instance(run_id: str, instance_id: str, attempt: int) -> InstanceDetail:
    """The phase rows for one (run, instance, attempt) — harness + eval side by side.

    ``instance_id`` may contain a slash (some SWE-bench ids are
    ``repo/owner``-shaped); FastAPI path conversion keeps this unambiguous.
    """
    conn = _db()
    try:
        rows = queries.get_instance(conn, run_id, instance_id, attempt)
    finally:
        conn.close()
    if not rows:
        raise HTTPException(status_code=404, detail="instance not found")
    return InstanceDetail(
        run_id=run_id,
        instance_id=instance_id,
        attempt_number=attempt,
        rows=[InstanceItem(**row) for row in rows],
    )


@app.get("/capacity", response_model=CapacityList)
def list_capacity(
    pool: str | None = Query(default=None, pattern="^(harness|eval)$"),
    since: str | None = Query(default=None, description="ISO timestamp; fetch only newer"),
    limit: int = Query(default=500, ge=1, le=_MAX_PAGE * 2),
) -> CapacityList:
    """Recent capacity_snapshot ticks, oldest-first, for the Axis A/B charts."""
    conn = _db()
    try:
        rows = queries.list_capacity(conn, pool=pool, since=since, limit=limit)
    finally:
        conn.close()
    return CapacityList(items=[CapacityPoint(**row) for row in rows])


@app.get("/queues", response_model=QueuesList)
def list_queues() -> QueuesList:
    """Depth + DLQ reading for the three work queues (M2.1 / M2.4).

    ``visible`` vs ``not_visible`` is the incident-vs-capacity signal and is
    never collapsed; ``oldest_age_s`` is CloudWatch-derived and ``None`` (not 0)
    when unavailable.  A DLQ depth above zero is an alarm, not a stat.
    """
    from swebench_eval.queue import client as queue_client

    items: list[QueueView] = []
    for name in ("harness-jobs", "eval-jobs", "results"):
        depth = queue_client.get_queue_depth(name)
        items.append(
            QueueView(
                queue=name,
                visible=depth.visible,
                not_visible=depth.not_visible,
                oldest_age_s=depth.oldest_age_s,
                dlq_depth=queue_client.get_dlq_depth(name),
            )
        )
    return QueuesList(items=items)


@app.get("/artifacts/{run_id}/{instance_id}/{attempt}/{kind}")
def get_artifact(
    run_id: str,
    instance_id: str,
    attempt: int,
    kind: str,
) -> Response:
    """Proxy one stored artifact (patch / trajectory / log / report) from S3.

    The S3 key comes from THIS attempt's own instance_results row — a caller
    can only ever fetch an artifact the run actually produced.
    """
    conn = _db()
    try:
        rows = queries.get_instance(conn, run_id, instance_id, attempt)
    finally:
        conn.close()
    if not rows:
        raise HTTPException(status_code=404, detail="instance not found")

    try:
        # each kind lives on the phase row that produced it (patch/trajectory/
        # log on the harness row, report on the eval row) — rows come back
        # ORDER BY phase, so resolve the key from the row owning this kind's
        # path column instead of assuming rows[0].
        path_column = artifacts.path_column_for(kind)
        row = next((r for r in rows if r.get(path_column)), rows[0])
        key = artifacts.artifact_key_for_row(kind, row)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not key:
        kind_note = {
            "patch": "the attempt produced no patch",
            "trajectory": "no trajectory was captured",
            "log": "no harness log was captured",
            "report": "the evaluation did not run (no report)",
            "native_trajectory": "the harness had no separate native trajectory",
            "test_output": "the grade did not produce a test output log",
            "run_log": "the grade did not produce a run log",
        }.get(kind, f"no stored object key for '{kind}'")
        raise HTTPException(status_code=404, detail=kind_note)

    try:
        data = artifacts.fetch(kind, row)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    media_type = artifacts.media_type_for(kind)
    filename = key.rsplit("/", 1)[-1]
    return Response(
        content=data,
        media_type=media_type,
        headers={
            # the key's basename, surfaced so the browser can save it sensibly
            "Content-Disposition": f'inline; filename="{filename}"'
        },
    )


# The served UI (see _ApiPrefixRewrite / _mount_ui above): mounted last so every API route
# above wins first, then wrapped so `/api/...` reaches those routes. `app` stays the FastAPI
# object for tests and the routes; uvicorn serves `application`.
_UI_DIST = _mount_ui(app)
application = _ApiPrefixRewrite(app)
