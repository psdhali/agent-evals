"""``POST /runs`` + the three read-only endpoints the UI needs (§3).

The owner assigned ``POST /runs`` to builder 4 even though it lives in
builder 2's directory ("their brief says 'no POST /runs' precisely because it
was reserved... the endpoint is assigned to you"). Kept to this ONE new
module plus the minimum wiring ``main.py`` needs — builder 2's own routes,
schemas and queries are untouched.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import HTTPException

from swebench_eval import aws_names
from swebench_eval.orchestrator.control_plane.run_launch import (
    launch_run,
)
from swebench_eval.orchestrator.run_config import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,  # noqa: F401 - re-exported for schemas/tests
    RunConfig,
)

from .schemas import (
    DatasetInstanceItem,
    DatasetInstancesResponse,
    HarnessesResponse,
    InstructionPreset,
    InstructionPresetsResponse,
    ModelItem,
    ModelsResponse,
    RunLaunchRequest,
    RunLaunchResponse,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# POST /runs
# ---------------------------------------------------------------------------


def _resolve_instances(instance_ids: list[str] | str) -> list[Any]:
    """§3.1: ``"all"`` resolves SERVER-SIDE at launch, never client-side.

    Both the explicit-list path and the ``"all"`` path filter/expand against
    the SAME pinned mirror load, so "the client sending 500 ids and the
    server expanding 'all' must produce the identical set" holds by
    construction — there is only one place either path reads instances from.
    """
    from swebench_eval.dataset.swebench_loader import SwebenchLiteLoader

    all_instances = list(SwebenchLiteLoader(include_gold=False).load())
    if instance_ids == "all":
        return all_instances
    wanted = set(instance_ids) if isinstance(instance_ids, list) else {str(instance_ids)}
    by_id = {inst.instance_id: inst for inst in all_instances}
    missing = wanted - by_id.keys()
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                "unknown instance id(s) (not in the pinned dataset mirror): " f"{sorted(missing)}"
            ),
        )
    return [by_id[i] for i in wanted]


def _write_processed_run_config(
    run_id: str, request: RunLaunchRequest, resolved_instance_ids: list[str]
) -> None:
    """§3.1: the S3 copy that keeps a run auditable after Aurora is
    paused/destroyed — "builder 1 added exactly this to the ADR-0024 path...
    the two triggers must produce the same artifact."  Best-effort: the
    bucket name needs the ``RESULTS_BUCKET`` env var wired into the
    orchestrator task definition (terraform change included in this build,
    see builder4-run-launch-response.md) — until that lands, this logs a
    warning and continues rather than failing an otherwise-successful launch;
    ``runs.config_snapshot`` (written unconditionally, see
    :func:`_record_resolved_instances`) is the hard requirement and does not
    depend on this.
    """
    bucket = os.environ.get("RESULTS_BUCKET")
    if not bucket:
        logger.warning(
            "RESULTS_BUCKET not set — skipping runs/processed/%s.json S3 copy "
            "(config_snapshot still carries the resolved instance set)",
            run_id,
        )
        return
    from swebench_eval.queue.client import upload_artifact

    payload = {
        "run_id": run_id,
        "harness": request.harness,
        "model_alias": request.model_alias,
        "budget_cap_usd": request.budget_cap_usd,
        "max_cost_usd_per_instance": request.max_cost_usd_per_instance,
        "max_tokens_per_instance": request.max_tokens_per_instance,
        "max_turns_per_instance": request.max_turns_per_instance,
        "attempts_per_instance": request.attempts_per_instance,
        "timeout_seconds": request.timeout_seconds,
        "context_window_tokens": request.context_window_tokens,
        "instances": resolved_instance_ids,
        "trigger": "POST /runs",
    }
    try:
        upload_artifact(bucket, f"runs/processed/{run_id}.json", json.dumps(payload))
    except Exception:
        logger.exception("could not write runs/processed/%s.json (non-fatal)", run_id)


def _record_resolved_instances(run_id: str, resolved_instance_ids: list[str]) -> None:
    """§3.1: "Record the resolved instance set" into ``runs.config_snapshot``.

    A merge (``||``), not an overwrite — ``dispatch_run()`` (called inside
    :func:`launch_run`) already wrote the RESOLVED config snapshot
    (resolved_models, window, reproducibility facts); this adds one more key
    without racing or clobbering it.  Runs AFTER ``launch_run`` returns
    (dispatch has already started) — config_snapshot is metadata, nothing
    reads it synchronously during dispatch, so the ordering is harmless.
    """
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE runs SET config_snapshot = config_snapshot ||
                   jsonb_build_object('resolved_instance_ids', %s::jsonb)
                   WHERE run_id = %s""",
                (json.dumps(resolved_instance_ids), run_id),
            )
        conn.commit()
    finally:
        conn.close()


def _generate_run_id() -> str:
    """Same ULID-compatible shape as ``harness_worker._generate_run_id`` /
    ``smoke_test._generate_run_id`` (no ``ulid`` package dependency exists in
    this repo — matching the existing convention rather than adding one).
    """
    import time
    import uuid

    ts = time.time_ns()
    suffix = uuid.uuid4().hex[:8]
    return f"{ts:020d}-{suffix}"


def launch(request: RunLaunchRequest) -> RunLaunchResponse:
    """``POST /runs`` (§3.1): dispatch is inline, returns when jobs are queued.

    Raises :class:`DuplicateRunError` / :class:`NoProvisioningKeyError` /
    :class:`GatewayPausedError` (BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-
    2026-08-31.md §4) unhandled — ``main.py``'s route translates them to the
    exact 409/503 response shapes (the 409 body must be FLAT, not FastAPI's
    default ``HTTPException`` ``{"detail": ...}`` wrapper — "the id is the
    payload, not decoration", §3.1).
    """
    instances = _resolve_instances(request.instance_ids)
    resolved_ids = [inst.instance_id for inst in instances]

    config = RunConfig(
        attempts_per_instance=request.attempts_per_instance,
        harness=request.harness,
        model_alias=request.model_alias,
        timeout_seconds=request.timeout_seconds,
        max_tokens_per_instance=request.max_tokens_per_instance,
        max_cost_usd_per_instance=request.max_cost_usd_per_instance,
        max_turns_per_instance=request.max_turns_per_instance,
        # §3.1 warning: None means RESOLVE (gateway -> baked yaml -> default).
        # The UI must not send a value unless the operator deliberately
        # overrides it — that contract lives in the request schema's default,
        # not here; this just carries whatever the request actually said.
        context_window_tokens=request.context_window_tokens,
        max_parallel_harness_tasks=request.max_parallel_harness_tasks,
        initial_budget_override=request.initial_budget_override,
        ramp_step_pct=request.ramp_step_pct,
        ramp_cooldown_seconds=request.ramp_cooldown_seconds,
        autoscaler_enabled=request.autoscaler_enabled,
        # 2026-09-09 efficiency prompt arm: blank collapses to None so a run launched with
        # an empty box is byte-identical to one launched before the field existed.
        harness_instructions=(request.harness_instructions or "").strip() or None,
    )

    run_id = _generate_run_id()
    # DuplicateRunError / NoProvisioningKeyError propagate unhandled — see
    # this function's docstring; main.py's route builds the response shape.
    result = launch_run(run_id, instances, config, request.budget_cap_usd)

    _record_resolved_instances(run_id, resolved_ids)
    _write_processed_run_config(run_id, request, resolved_ids)

    return RunLaunchResponse(
        status="launched",
        run_id=result.run_id,
        dispatched=result.dispatched,
        seeded=result.seeded,
    )


# ---------------------------------------------------------------------------
# GET /dataset/instances, GET /harnesses, GET /models (§3.2)
# ---------------------------------------------------------------------------


def _registered_family_instance_ids() -> set[str]:
    """Which instance_ids have a per-instance ECS task family registered right now.

    2026-09-05 (41-run 31bf2769): 28 matplotlib instances had their ``-inst``
    image in ECR — so the image-only check below called them launchable — but
    no ``eval-dev-harness-<instance_id>`` task family, because the family file
    was never regenerated after builder 5's image build. The dispatcher fell
    back to the env-hash family (the ``-hw`` image), the worker refused it
    (no testbed sentinel, exit 1), and SQS redelivered every 300 s. ECS cannot
    override an image at run-task time, so the family is as much a launch
    precondition as the image: an instance is launchable only when BOTH exist.

    One paginated ``list_task_definition_families`` per request (prefix +
    ACTIVE), same fail-open-to-empty rule as the ECR side.
    """
    import boto3

    from swebench_eval.orchestrator.control_plane.harness_dispatcher import _FAMILY_PREFIX

    try:
        ecs = boto3.client("ecs", region_name=aws_names.region())
    except Exception:
        logger.warning("could not create ECS client for launchable check", exc_info=True)
        return set()

    ids: set[str] = set()
    token: str | None = None
    try:
        while True:
            kwargs: dict[str, Any] = {
                "familyPrefix": _FAMILY_PREFIX,
                "status": "ACTIVE",
                "maxResults": 100,
            }
            if token:
                kwargs["nextToken"] = token
            resp = ecs.list_task_definition_families(**kwargs)
            for family in resp.get("families", []):
                rest = family[len(_FAMILY_PREFIX) :]
                # Per-instance families carry the instance id (``owner__repo-NNN``);
                # env-hash families are bare hex and are NOT per-instance.
                if "__" in rest:
                    ids.add(rest)
            token = resp.get("nextToken")
            if not token:
                break
    except Exception:
        logger.warning("could not list ECS task families for launchable check", exc_info=True)
        return set()
    return ids


def _launchable_instance_ids() -> set[str]:
    """Which instance_ids can actually launch right now: a built ``-inst`` image
    in ECR AND a registered per-instance task family (see
    :func:`_registered_family_instance_ids` for why both are required).

    Round 2, item 7: ``dispatch_run``'s per-instance env-image gate already
    refuses an instance with no ``-inst`` image — correctly, but silently,
    only discovered at dispatch time. An operator picking freely from ~500
    dataset rows had no way to know in advance which ones would actually
    launch (today: 4 of ~500).

    ONE bulk, paginated ``describe_images`` call per request — not one call
    per instance. ``dispatcher._digest_for_inst`` does per-instance lookups,
    which is right for resolving a single run's digest but wrong for a
    500-row listing ("do not do 500 ECR calls per keystroke"). Same repo/tag
    convention as ``dispatcher._resolve_harness_digest``: tags are
    ``<version>-<instance_id>-inst`` in the harness-worker repo (both -hw and
    -inst tags share one repository, differentiated by suffix).

    Fails open to "nothing launchable" on any AWS/network error — an
    unavailable ECR must not 500 the whole dataset listing, and reporting
    zero launchable instances is the honest, conservative answer when the
    real state can't be confirmed (same Trap-3 direction as the rest of this
    codebase: unknown is never rendered as a fabricated positive).
    """
    import boto3

    _repo = os.environ.get("HARNESS_IMAGE_REPO") or aws_names.named("harness-worker")
    repo = _repo.rsplit("/", 1)[-1] if "/" in _repo else _repo
    # 2026-09-06: the default here was still "4.1.0" after ADR-0043 moved every
    # other component to 5.0.2 (env_image.DEFAULT_SWEBENCH_VERSION); no task-def
    # sets SWEBENCH_VERSION, so the launch screen counted the 41 leftover
    # `4.1.0-<id>-inst` revert-path tags and called 459 of 500 built images
    # unlaunchable. One source of truth for the version, same as the eval side.
    from swebench_eval.evaluation.env_image import DEFAULT_SWEBENCH_VERSION

    version = os.environ.get("SWEBENCH_VERSION", DEFAULT_SWEBENCH_VERSION)
    prefix = f"{version}-"
    suffix = "-inst"

    try:
        ecr = boto3.client("ecr", region_name=aws_names.region())
    except Exception:
        logger.warning("could not create ECR client for launchable check", exc_info=True)
        return set()

    instance_ids: set[str] = set()
    token: str | None = None
    try:
        while True:
            kwargs: dict[str, Any] = {"repositoryName": repo, "maxResults": 1000}
            if token:
                kwargs["nextToken"] = token
            resp = ecr.describe_images(**kwargs)
            for detail in resp.get("imageDetails", []):
                for tag in detail.get("imageTags") or []:
                    if tag.startswith(prefix) and tag.endswith(suffix):
                        instance_ids.add(tag[len(prefix) : -len(suffix)])
            token = resp.get("nextToken")
            if not token:
                break
    except Exception:
        logger.warning("could not list ECR images for launchable check", exc_info=True)
        return set()
    if not instance_ids:
        return set()
    return instance_ids & _registered_family_instance_ids()


def dataset_instances() -> DatasetInstancesResponse:
    """The pinned S3 dataset mirror — id + repo + launchable, enough for a
    multi-select.

    The orchestrator task role already has DatasetRead (no IAM work).  Never
    falls back to HuggingFace: ``SwebenchLiteLoader(include_gold=False)``
    already refuses that fallback on its own (the public loader's contract),
    so the gold patch can never leak through this endpoint.
    """
    from swebench_eval.dataset.swebench_loader import SwebenchLiteLoader

    try:
        instances = SwebenchLiteLoader(include_gold=False).load()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"dataset mirror unavailable: {exc}") from exc
    launchable_ids = _launchable_instance_ids()
    items = [
        DatasetInstanceItem(
            instance_id=i.instance_id, repo=i.repo, launchable=i.instance_id in launchable_ids
        )
        for i in instances
    ]
    return DatasetInstancesResponse(items=items, total=len(items))


def harnesses() -> HarnessesResponse:
    """The adapter registry — the names a run may use."""
    from swebench_eval.harnesses.registry import HARNESS_ADAPTERS

    return HarnessesResponse(harnesses=sorted(HARNESS_ADAPTERS))


def instruction_presets() -> InstructionPresetsResponse:
    """GET /launch/instruction-presets — the starting texts for the harness-instructions
    field (2026-09-09 efficiency prompt arm), plus the field's size cap."""
    from swebench_eval.orchestrator.harness_instructions import (
        HARNESS_INSTRUCTIONS_MAX_CHARS,
        PRESETS,
    )

    return InstructionPresetsResponse(
        presets=[InstructionPreset(**p) for p in PRESETS],
        max_chars=HARNESS_INSTRUCTIONS_MAX_CHARS,
    )


def models() -> ModelsResponse:
    """The gateway's live ``/model/info`` — alias + max_input_tokens.

    Deliberately live, not the baked yaml: "a hardcoded list drifts from the
    deployed config, and drift there is invisible until a run produces wrong
    numbers" (§3.2).  Same field the dispatcher resolves the run's context
    window from (``gateway/model_info.py``'s ``MAX_INPUT_TOKENS_KEY``), so the
    UI shows what the run will actually use.  Fails loud (502) rather than
    silently serving stale data — the whole point of reading live.
    """
    import httpx

    from swebench_eval.harnesses.routing import gateway_api_key, gateway_base_url

    base = gateway_base_url()
    try:
        resp = httpx.get(
            f"{base.rstrip('/')}/model/info",
            headers={"Authorization": f"Bearer {gateway_api_key()}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"gateway /model/info unreachable: {exc}"
        ) from exc

    seen: set[str] = set()
    items: list[ModelItem] = []
    for entry in data.get("data") or []:
        if not isinstance(entry, dict):
            continue
        alias = entry.get("model_name")
        if not alias or alias in seen:
            continue
        seen.add(alias)
        info = entry.get("model_info") or {}
        max_input = info.get("max_input_tokens")
        ratio, seeded_at = _pacer_consistency(alias)
        items.append(
            ModelItem(
                alias=alias,
                max_input_tokens=int(max_input) if max_input else None,
                consistency_ratio=ratio,
                pacer_seeded_at=seeded_at,
            )
        )
    return ModelsResponse(items=sorted(items, key=lambda m: m.alias))


def _pacer_consistency(alias: str) -> tuple[float | None, float | None]:
    """Item 14: ``(consistency_ratio, seeded_at)`` from the alias's POOL pacer:cfg — the
    same r_tok / (k_inflight / latency_s_max_context) discovery logs (§2.3). (None, None)
    when the pool has no seeded cfg, a field is missing, or Redis is unreachable: the
    launch screen must never show a fabricated 'consistent'. Best-effort, never raises —
    the model list is the gateway's, not Redis's."""
    try:
        from swebench_eval.database import redis_client as redis_reads
        from swebench_eval.gateway.pacer import pacer_cfg_key
        from swebench_eval.gateway.rotatable_models import pool_alias_for

        if not redis_reads.is_redis_reachable():
            return None, None
        pool = pool_alias_for(alias) or alias
        raw = redis_reads._get_client().hgetall(pacer_cfg_key(pool)) or {}
        cfg = {
            (k.decode() if isinstance(k, bytes) else str(k)): float(
                v.decode() if isinstance(v, bytes) else v
            )
            for k, v in raw.items()
        }
    except Exception:  # noqa: BLE001 — unreadable cfg reads as unknown, never as a number
        return None, None
    seeded_at = cfg.get("seeded_at")
    r_tok, k_inflight, latency = (
        cfg.get("r_tok"),
        cfg.get("k_inflight"),
        cfg.get("latency_s_max_context"),
    )
    if not (r_tok and k_inflight and latency) or latency <= 0 or k_inflight <= 0:
        return None, seeded_at
    return round(r_tok / (k_inflight / latency), 3), seeded_at
