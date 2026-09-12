"""The LLM judge task — one-shot ECS entrypoint
(offline-analysis-design.md §9.5/§10.4/§10.5).

Runs Pass A (leak backfill) then Pass B (the judge) in ONE invocation —
locked at §10.5: not two tasks, not optional. Free, idempotent, and turns
§5's "Pass A first, then Pass B" ordering requirement into a structural
guarantee instead of operator discipline.

Zero coupling to the orchestrator's other components (§10.4): no SQS, no
reaper, no ledger, no `instance_results.state` writes beyond what Pass A
itself already owns (`leaked_node_ids`/`leak_detectable`/`leak_scan_at`).
Triggered by one direct ``ecs.run_task`` call from the API, same as
``ceiling_discovery`` — never through the harness dispatch path.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import uuid
from typing import Any

from swebench_eval.analysis import judge, leak_pass
from swebench_eval.analysis.rubric import load_rubric

logger = logging.getLogger(__name__)


def _generate_pass_id() -> str:
    """Same ULID-compatible shape as ``run_launch_routes._generate_run_id``
    — no ``ulid`` package dependency in this repo, matching convention."""
    ts = time.time_ns()
    suffix = uuid.uuid4().hex[:8]
    return f"judge-{ts:020d}-{suffix}"


def _make_artifact_fetcher() -> Any:
    from swebench_eval.queue.client import get_artifact

    bucket = os.environ.get("ARTIFACTS_BUCKET", "eval-artifacts")
    return lambda key: get_artifact(bucket, key)


def _make_artifact_store() -> Any:
    """Symmetric writer for the §3.6 raw-response record (ADR-0042). The
    judge stores every cascade attempt's raw response under this key so a
    parse failure stays re-scorable and a recovered judgment stays auditable."""
    from swebench_eval.queue.client import upload_artifact

    bucket = os.environ.get("ARTIFACTS_BUCKET", "eval-artifacts")
    return lambda key, data: upload_artifact(bucket, key, data)


def run(
    run_id: str,
    *,
    pass_id: str | None = None,
    instance_ids: list[str] | None = None,
    prune_mode: str = "pruned",
    model_alias: str = "judge-model",
    sample_rate: float = judge.DEFAULT_SAMPLE_RATE,
    min_per_stratum: int = judge.DEFAULT_MIN_PER_STRATUM,
    seed: int | None = None,
    max_spend_usd: float,
    workers: int = judge.JUDGE_DEFAULT_WORKERS,
    rejudge: bool = False,
    synthesis_only: bool = False,
    stop_event: threading.Event | None = None,
    retry_no_verdict: bool = False,
) -> judge.JudgePassSummary:
    """Step 1 (Pass A) then step 2 (Pass B), both scoped to *instance_ids*
    if given (§10.1). ``workers``: concurrent judge calls (2026-09-07).
    ``rejudge`` (2026-09-08): judge candidates that already have a judge_results
    row too; False (default) skips them — a relaunch resumes a failed pass."""
    from swebench_eval.database.connection import get_connection

    pass_id = pass_id or _generate_pass_id()

    if synthesis_only:
        # "regenerate report" (2026-09-08): no Pass A, no judging — only the synthesis.
        logger.info("llm-judge task %s: synthesis-only (regenerate report)", pass_id)
    else:
        n_scanned = leak_pass.run_leak_pass(run_id, instance_ids)
        logger.info("llm-judge task %s: Pass A scanned %d row(s)", pass_id, n_scanned)

    _seed_pacer_for(model_alias)

    rubric = load_rubric()
    summary = judge.run_pass(
        get_connection,
        run_id,
        pass_id=pass_id,
        instance_ids=instance_ids,
        sample_rate=sample_rate,
        min_per_stratum=min_per_stratum,
        seed=seed,
        prune_mode=prune_mode,
        model_alias=model_alias,
        max_spend_usd=max_spend_usd,
        rubric=rubric,
        fetch_artifact=_make_artifact_fetcher(),
        store_artifact=_make_artifact_store(),
        workers=workers,
        rejudge=rejudge,
        synthesis_only=synthesis_only,
        stop_event=stop_event,
        retry_no_verdict=retry_no_verdict,
    )
    logger.info(
        "llm-judge task %s complete: judged=%d skipped_over_budget=%d parse_failed=%d "
        "call_failed=%d already_judged=%d spend=$%.4f",
        pass_id,
        summary.total_judged,
        summary.total_skipped_over_budget,
        summary.total_parse_failed,
        summary.total_call_failed,
        summary.total_already_judged,
        summary.spend_usd,
    )
    return summary


def _seed_pacer_for(model_alias: str) -> None:
    """Best-effort: copy the judge alias's POOL seeds into ``pacer:cfg:{alias}`` so a
    parallel pass is admitted on the discovered constants, not the pacer's defaults.
    No REDIS_URL / unreachable Valkey = logged, pass proceeds (on defaults)."""
    try:
        from swebench_eval.database import redis_client
        from swebench_eval.orchestrator.control_plane import pacer_seeds

        if not redis_client.is_redis_reachable():
            logger.warning(
                "llm-judge: Redis unreachable — pacer:cfg:%s not seeded (gateway pacer on "
                "defaults) and no live progress will be published",
                model_alias,
            )
            return
        pacer_seeds.seed_alias_from_pool(redis_client._get_client(), model_alias)
    except Exception:  # noqa: BLE001
        logger.warning("llm-judge: pacer seeding for %s failed (pacer on defaults)", model_alias)


def _workers_from_env() -> int:
    raw = os.environ.get("JUDGE_WORKERS", "").strip()
    if not raw:
        return judge.JUDGE_DEFAULT_WORKERS
    try:
        return max(1, min(int(raw), judge.JUDGE_MAX_WORKERS))
    except ValueError as exc:
        raise SystemExit(f"JUDGE_WORKERS must be an integer, got {raw!r}") from exc


def main() -> None:
    """One-shot ECS entrypoint. Inputs via env (RunTask ``containerOverrides``):

    - ``JUDGE_RUN_ID`` (required)
    - ``JUDGE_MAX_SPEND_USD`` (required — real money, no silent default)
    - ``JUDGE_PASS_ID`` (generated if absent)
    - ``JUDGE_INSTANCE_IDS`` (JSON array of instance ids, optional — absent means all)
    - ``JUDGE_PRUNE_MODE`` (``full``/``pruned``/``auto``, default ``pruned``)
    - ``JUDGE_MODEL_ALIAS`` (default ``judge-model``)
    - ``JUDGE_SAMPLE_RATE`` (default ``1.0``)
    - ``JUDGE_MIN_PER_STRATUM`` (default ``5``)
    - ``JUDGE_SEED`` (optional)
    - ``JUDGE_WORKERS`` (concurrent judge calls, default 24, clamped to 1..100)
    - ``JUDGE_REJUDGE`` (``1`` judges already-judged candidates again; default resume)
    - ``JUDGE_SYNTHESIS_ONLY`` (``1`` = regenerate the pass report only, judge nothing)
    - ``JUDGE_RETRY_NO_VERDICT`` (``1`` = also judge attempts whose latest judgment timed
      out or failed to parse; default resume skips them)

    Exits non-zero on any failure — never a silent success, same discipline
    as ``ceiling_discovery.main``.
    """
    from swebench_eval.logging_bootstrap import configure_logging

    configure_logging()

    run_id = os.environ.get("JUDGE_RUN_ID", "")
    if not run_id:
        raise SystemExit("JUDGE_RUN_ID is required")

    max_spend_raw = os.environ.get("JUDGE_MAX_SPEND_USD", "")
    if not max_spend_raw:
        raise SystemExit("JUDGE_MAX_SPEND_USD is required — a judge pass spends real money")
    max_spend_usd = float(max_spend_raw)

    instance_ids_raw = os.environ.get("JUDGE_INSTANCE_IDS", "")
    instance_ids = json.loads(instance_ids_raw) if instance_ids_raw else None

    seed_raw = os.environ.get("JUDGE_SEED", "")
    seed = int(seed_raw) if seed_raw else None

    # 2026-09-08: an ECS stop-task (SIGTERM, then SIGKILL 30 s later) used to leave the
    # per-model pass lock + minted keys behind, refusing the next launch. Now it asks the
    # pass to stop: judged rows kept, in-flight abandoned, ledger written, lock released.
    stop_event = threading.Event()

    def _on_sigterm(signum: int, frame: object) -> None:
        logger.warning("llm-judge task: SIGTERM — stopping the pass (judged rows are kept)")
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_sigterm)

    run(
        run_id,
        pass_id=os.environ.get("JUDGE_PASS_ID") or None,
        instance_ids=instance_ids,
        prune_mode=os.environ.get("JUDGE_PRUNE_MODE", "pruned"),
        model_alias=os.environ.get("JUDGE_MODEL_ALIAS", "judge-model"),
        sample_rate=float(os.environ.get("JUDGE_SAMPLE_RATE", "1.0")),
        min_per_stratum=int(os.environ.get("JUDGE_MIN_PER_STRATUM", "5")),
        seed=seed,
        max_spend_usd=max_spend_usd,
        workers=_workers_from_env(),
        rejudge=os.environ.get("JUDGE_REJUDGE", "0").strip() == "1",
        synthesis_only=os.environ.get("JUDGE_SYNTHESIS_ONLY", "0").strip() == "1",
        stop_event=stop_event,
        retry_no_verdict=os.environ.get("JUDGE_RETRY_NO_VERDICT", "0").strip() == "1",
    )


if __name__ == "__main__":
    main()
