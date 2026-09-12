"""S3-triggered dispatch Lambda handler (ADR-0024).

A run is started by dropping a run-config JSON object into ``runs/pending/``
of the durable ``results`` bucket. This handler:

1. Reads the object (S3 event) and parses the run config.
2. Calls the SAME ``register_run()``/``dispatch_run()`` the Phase-7 ``POST
   /runs`` endpoint will call — the ADR's core property: a second *trigger* is
   cheap, a second *dispatcher* is a permanent divergence risk.
3. Moves the object to ``runs/processed/`` (success) or ``runs/failed/``
   (dispatch exception), then deletes the ``pending/`` copy.

Idempotency (ADR-0024 §Consequences): S3 event delivery is at-least-once, and
the guard against a double dispatch is "object no longer in ``pending/``".
If the object is already gone (a duplicate delivery after the first invocation
moved it), the handler exits without dispatching anything.

This function runs inside a container Lambda attached to the VPC (private
subnets + the task security group), so ``database.connection`` reaches Aurora
directly and the existing psycopg path is reused unmodified.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from swebench_eval import aws_names
from swebench_eval.orchestrator.control_plane.run_launch import launch_run
from swebench_eval.orchestrator.run_config import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_MAX_COST_USD_PER_INSTANCE,
    DEFAULT_MAX_TOKENS_PER_INSTANCE,
    DEFAULT_MAX_TURNS_PER_INSTANCE,
    DEFAULT_TIMEOUT_SECONDS,
    RunConfig,
)

logger = logging.getLogger(__name__)

_PENDING_PREFIX = "runs/pending/"
_PROCESSED_PREFIX = "runs/processed/"
_FAILED_PREFIX = "runs/failed/"


def _s3() -> Any:
    import boto3

    return boto3.client("s3", region_name=aws_names.region())


def _bootstrap_db_url() -> None:
    """Set DATABASE_URL from Secrets Manager if not already in the environment.

    The dispatch Lambda is VPC-attached and reaches Aurora directly, but it has
    no ECS ``secrets`` block — it must fetch the connection string at runtime.
    Reading the secret here (never the state file) keeps the value out of
    Terraform state and gives ``database.connection`` the same env seam the ECS
    tasks already use.
    """
    if os.environ.get("DATABASE_URL"):
        return
    secret_arn = os.environ.get("DATABASE_URL_SECRET_ARN")
    if not secret_arn:
        # Local dev / unit tests: no secret plumbing; the compose defaults apply.
        return
    import boto3

    secrets = boto3.client("secretsmanager", region_name=aws_names.region())
    os.environ["DATABASE_URL"] = secrets.get_secret_value(SecretId=secret_arn)["SecretString"]


def _parse_run_config(body: dict[str, Any]) -> tuple[str, RunConfig, list[Any], float]:
    """Extract (run_id, config, instances, budget_cap_usd) from the dropped object body.

    ``instances`` are the full :class:`Instance` fields (the same shape
    ``load_single_instance`` returns) — the trigger object carries them so the
    Lambda does not need HuggingFace access; the dataset bucket/loader is a
    separate (future) input.

    run-launch D2: this trigger predates ``budget_cap_usd`` (ADR-0035 per-run
    keys) — the operator/test object never carried one.  Absent -> a computed
    ceiling (max_cost_usd_per_instance x instances x attempts), the same
    shape the run's own per-instance cap already implies, rather than a flat
    invented constant.  An explicit value in the dropped object always wins.
    """
    run_id = str(body["run_id"])
    instances = body.get("instances", [])
    if not instances:
        raise ValueError(f"run config {run_id} contains no 'instances'")
    # M4 (review 2026-08-25): the turn cap and token ceiling were `None` (=
    # unlimited) on the S3 dispatch path — the old single-arg `body.get("k")`
    # yielded None when the key was ABSENT, overriding RunConfig's DEFAULT_*
    # with None all the way to the shim, so 3.12's 500-turn cap did nothing on
    # the only sanctioned dispatch path unless the operator remembered the key.
    # `.get(k, DEFAULT)` restores the contract: absent → DEFAULT_* constant; an
    # explicit JSON null still means "unlimited" (get returns None for a present
    # null, so the operator's explicit choice is preserved).
    config = RunConfig(
        attempts_per_instance=int(body.get("attempts_per_instance", 1)),
        harness=str(body.get("harness", "custom_minimal")),
        model_alias=str(body.get("model_alias", "cheap-oss-model")),
        timeout_seconds=int(body.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        max_tokens_per_instance=body.get(
            "max_tokens_per_instance", DEFAULT_MAX_TOKENS_PER_INSTANCE
        ),
        max_cost_usd_per_instance=float(
            body.get("max_cost_usd_per_instance", DEFAULT_MAX_COST_USD_PER_INSTANCE)
        ),
        max_turns_per_instance=body.get("max_turns_per_instance", DEFAULT_MAX_TURNS_PER_INSTANCE),
        # Compaction build (Stage 1.2): M4 two-sided contract — absent → the
        # run-config value resolves in the dispatcher (None = "resolve", so the
        # constant is applied there); an explicit JSON null would carry through
        # as None.  The RunConfig default (None) means the DISPATCHER resolves
        # it, which is what we want on this path.
        context_window_tokens=body.get("context_window_tokens", DEFAULT_CONTEXT_WINDOW_TOKENS),
        # 2026-09-09 efficiency prompt arm: absent / blank = nothing appended.
        harness_instructions=(str(body.get("harness_instructions") or "")).strip() or None,
    )
    budget_cap_usd = body.get("budget_cap_usd")
    if budget_cap_usd is None:
        budget_cap_usd = (
            config.max_cost_usd_per_instance * len(instances) * config.attempts_per_instance
        )
    return run_id, config, instances, float(budget_cap_usd)


def _instance_from_dict(data: dict[str, Any]) -> Any:
    """Rehydrate a single Instance dataclass from its dict form."""
    from swebench_eval.dataset.base import Instance

    return Instance(
        instance_id=str(data["instance_id"]),
        repo=str(data["repo"]),
        base_commit=str(data["base_commit"]),
        problem_statement=str(data["problem_statement"]),
        fail_to_pass=str(data.get("fail_to_pass", "")),
        pass_to_pass=str(data.get("pass_to_pass", "")),
        # 5.x public columns (ADR-0043); eval_script is gold material and is
        # never on a dispatch row.
        image=str(data.get("image", "")),
        log_parser=str(data.get("log_parser", "")),
        eval_type=str(data.get("eval_type", "")),
    )


def _read_window_snapshot(run_id: str) -> tuple[object, object]:
    """Read the dispatcher-RESOLVED context window from the run's persisted
    config_snapshot in Aurora, for STEP 6 (write it back into the S3 run config).

    The window is resolved INSIDE ``dispatch_run`` after this handler read the
    pending object; it is authoritative in Aurora's ``runs.config_snapshot``.
    Best-effort: any failure (Aurora paused, connection blip) returns
    (None, None) — the dispatch still succeeds, and the S3 copy still lands; the
    window fields are just absent from it.  Never raises into the handler.
    """
    try:
        from swebench_eval.database.connection import get_connection

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT config_snapshot->>'context_window_tokens', "
                    "config_snapshot->>'context_window_source' FROM runs "
                    "WHERE run_id = %s",
                    (run_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return None, None
        return row[0], row[1]
    except Exception as exc:  # noqa: BLE001
        logger.warning("STEP 6: could not read window for S3 run config %s: %s", run_id, exc)
        return None, None


def _move_to(
    bucket: str,
    key: str,
    dest_prefix: str,
    extra: dict[str, Any] | None = None,
    overlay: dict[str, Any] | None = None,
) -> None:
    """Copy key to dest_prefix/<name> and delete the original (a receipt move).

    ``extra`` (used by the failed- path) rewrites the destination object with
    the error appended, so the artifact alone tells the story — the ADR's
    observable-failure discipline.  ``overlay`` (success path, STEP 6) merges a
    dict of RESOLVED fields (e.g. the context window) into the processed copy so
    the run stays auditable after Aurora is paused/destroyed.
    """
    s3 = _s3()
    name = key.rsplit("/", 1)[-1]
    if extra is not None or overlay is not None:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        content = json.loads(body.decode("utf-8"))
        if extra is not None:
            content["error"] = extra
        if overlay is not None:
            content.update(overlay)
        s3.put_object(Bucket=bucket, Key=f"{dest_prefix}{name}", Body=json.dumps(content))
    else:
        s3.copy_object(
            Bucket=bucket,
            Key=f"{dest_prefix}{name}",
            CopySource={"Bucket": bucket, "Key": key},
        )
    s3.delete_object(Bucket=bucket, Key=key)


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    """Main dispatch entrypoint. Returns {dispatched: N} for log-friendliness.

    S3-triggered lambdas parse the event's ``Records``; the bucket and key come
    from there (the tests build a synthetic record).
    """
    bucket = str(event["Records"][0]["s3"]["bucket"]["name"])
    key = str(event["Records"][0]["s3"]["object"]["key"])

    if not key.startswith(_PENDING_PREFIX):
        logger.info("Ignoring non-pending object %s", key)
        return {"dispatched": 0}

    s3 = _s3()
    # Completion latch (ADR-0024): if the object is already gone, a previous
    # delivery of this at-least-once event moved it — do NOT dispatch again.
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except s3.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] == "404":
            logger.info("Object %s no longer pending; skipping (duplicate delivery)", key)
            return {"dispatched": 0}
        raise

    resp = s3.get_object(Bucket=bucket, Key=key)
    body = json.loads(resp["Body"].read().decode("utf-8"))
    run_id, config, instance_dicts, budget_cap_usd = _parse_run_config(body)

    # VPC-attached lambda: fetch the Aurora connection string for register_run()
    # from Secrets Manager (never from Terraform state).
    _bootstrap_db_url()

    try:

        instances = [_instance_from_dict(d) for d in instance_dicts]
        # D2 (run-launch, BUILDER4-RUN-LAUNCH-ORCHESTRATOR §4): both triggers
        # share ONE claim path — this and POST /runs both call launch_run(),
        # so the (harness, model_alias) mutex cannot be bypassed by dropping
        # a file.  launch_run() calls dispatch_run() internally; no custom
        # enqueueing logic lives here (the ADR-0024 load-bearing property).
        result = launch_run(
            run_id=run_id, instances=instances, config=config, budget_cap_usd=budget_cap_usd
        )
        n = result.dispatched
        # STEP 6 (review 2026-08-26): write the dispatcher-RESOLVED context
        # window into the S3 run config, so the run's window stays auditable
        # after Aurora is paused/destroyed (config_snapshot lives in Aurora; the
        # S3 object survives teardown).  Best-effort read.
        window_tokens, window_source = _read_window_snapshot(run_id)
        _move_to(
            bucket,
            key,
            _PROCESSED_PREFIX,
            overlay={
                "context_window_tokens": window_tokens,
                "context_window_source": window_source,
            },
        )
        logger.info("Dispatched %d jobs for run %s; object -> processed/", n, run_id)
        return {"dispatched": n}
    except Exception as exc:
        logger.exception("Dispatch failed for run %s", run_id)
        try:
            _move_to(bucket, key, _FAILED_PREFIX, extra={"detail": str(exc)})
        except Exception:
            # If even the failed- receipt cannot be written, leave the object in
            # pending/ — the ADR's "pending older than a few minutes" is then the
            # observable failure, which is better than deleting the evidence.
            logger.exception("Could not move %s to failed/; leaving in pending/", key)
        raise
