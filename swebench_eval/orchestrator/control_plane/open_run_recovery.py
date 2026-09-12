"""Recover the per-run Valkey state of every OPEN run after an eval-tier cycle (2026-09-08).

Valkey is an ``eval``-tier resource: every destroy/recreate wipes it. The pool-level pacer seeds
are rehydrated from Aurora at supervisor startup (``pacer_seeds.rehydrate_known_pools``), but
two things are per RUN and were written exactly once, at launch:

1. **The run's raw LiteLLM virtual key** (``run_key_cache``, rule 3: never Aurora). Without it
   the harness dispatcher refuses every restarted job for that run
   (``DispatchRefusedError: no per-run LiteLLM key cached (ADR-0035)``) — found live on the
   opencode restarts of 2026-09-08: 28 jobs refused, recovered by hand.
2. **The run alias's ``pacer:cfg`` hash** (pool -> alias copy in
   ``run_launch._publish_autoscaler_overrides``). Without it the gateway admits the run's calls
   against its DEFAULTS (20k tok/s); an operator r_qps edit recreates the hash with that one
   field and the defaults still apply to the rest — 19 of 24 admissions waited > 2 s.

This module re-creates both for every run that is still ``running`` and went through
run-launch (``openrouter_key_hash IS NOT NULL`` — the stale pre-run-launch e2e rows have none
and are skipped by design). The LiteLLM key is re-MINTED (the old raw value is unrecoverable;
``delete_key`` by alias is idempotent, the new key keeps the run_id alias so the spend log's
attribution is unchanged) and its non-secret id is written back to ``runs.litellm_key_id``. The
OpenRouter key and the rotated db-model row live in Aurora / on OpenRouter and need nothing.

Best-effort, per run: a failure logs and leaves that run exactly as it was (refusing, as
today). Called from run-supervisor startup, after the pool rehydration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OpenRunRecovery:
    runs_seen: int = 0
    keys_reminted: list[str] = field(default_factory=list)
    keys_present: list[str] = field(default_factory=list)
    pacer_filled: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"open runs: {self.runs_seen}; litellm key re-minted: {len(self.keys_reminted)}, "
            f"already cached: {len(self.keys_present)}; pacer alias fields filled: "
            f"{sum(self.pacer_filled.values())} across {len(self.pacer_filled)} run(s); "
            f"failed: {len(self.failed)}"
        )


def _open_runs(conn: Any) -> list[tuple[str, dict[str, Any], str | None]]:
    """(run_id, config_snapshot, litellm_key_id) for every run still running that went
    through run-launch."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run_id, config_snapshot, litellm_key_id FROM runs "
            "WHERE status = 'running' AND openrouter_key_hash IS NOT NULL ORDER BY run_id"
        )
        rows = cur.fetchall()
    out: list[tuple[str, dict[str, Any], str | None]] = []
    for run_id, snapshot, key_id in rows:
        out.append((str(run_id), snapshot if isinstance(snapshot, dict) else {}, key_id))
    return out


def _remint_key(conn: Any, run_id: str, harness: str, model_alias: str) -> str:
    """Delete the run's LiteLLM key by alias and mint a replacement under the SAME alias;
    cache the raw key; record the new id. Returns the new (non-secret) key id."""
    from swebench_eval.gateway import admin as gateway_admin
    from swebench_eval.harnesses.routing import gateway_api_key, gateway_base_url
    from swebench_eval.orchestrator.control_plane import run_key_cache

    base, master = gateway_base_url(), gateway_api_key()
    gateway_admin.delete_key(base, master, run_id)
    raw, key_id = gateway_admin.generate_key(
        base,
        master,
        key_alias=run_id,
        models=[model_alias],
        max_budget=None,  # owner decision 2026-09-06: the OpenRouter per-run key is the cap
        metadata={"run_id": run_id, "harness": harness, "reminted": "open-run-recovery"},
    )
    run_key_cache.store(run_id, raw)
    with conn.cursor() as cur:
        cur.execute("UPDATE runs SET litellm_key_id = %s WHERE run_id = %s", (key_id, run_id))
    conn.commit()
    return key_id


def recover_open_runs(conn: Any | None = None, client: Any | None = None) -> OpenRunRecovery:
    """Re-create the per-run Valkey state of every open run. Never raises."""
    report = OpenRunRecovery()
    try:
        from swebench_eval.database.redis_client import _get_client, write_harness_instructions
        from swebench_eval.orchestrator.control_plane import pacer_seeds, run_key_cache
        from swebench_eval.orchestrator.control_plane.run_launch import _db

        own_conn = conn is None
        conn = conn or _db()
        client = client or _get_client()
        try:
            runs = _open_runs(conn)
            report.runs_seen = len(runs)
            for run_id, snapshot, _old_id in runs:
                harness = str(snapshot.get("harness", ""))
                model_alias = str(snapshot.get("model_alias", ""))
                try:
                    if not model_alias:
                        raise ValueError("config_snapshot has no model_alias")
                    if run_key_cache.fetch(run_id):
                        report.keys_present.append(run_id)
                    else:
                        key_id = _remint_key(conn, run_id, harness, model_alias)
                        report.keys_reminted.append(run_id)
                        logger.warning(
                            "open-run recovery: %s (%s / %s) had no cached LiteLLM key — "
                            "re-minted under the same alias (id %s…)",
                            run_id,
                            harness,
                            model_alias,
                            key_id[:10],
                        )
                    filled = pacer_seeds.seed_alias_from_pool(
                        client, model_alias, fill_missing=True
                    )
                    if filled:
                        report.pacer_filled[run_id] = filled
                    # 2026-09-09 efficiency prompt arm: the worker reads the run's
                    # instructions from Valkey once per job — re-publish from the snapshot
                    # so attempts dispatched after a cycle get the same prompt.
                    write_harness_instructions(run_id, snapshot.get("harness_instructions"))
                except Exception as exc:
                    report.failed[run_id] = f"{type(exc).__name__}: {exc}"[:200]
                    logger.exception(
                        "open-run recovery: %s failed — run left as is (dispatch keeps "
                        "refusing until fixed)",
                        run_id,
                    )
        finally:
            if own_conn:
                conn.close()
    except Exception:
        logger.exception("open-run recovery failed before it could inspect any run")
    logger.info("open-run recovery: %s", report.summary())
    return report
