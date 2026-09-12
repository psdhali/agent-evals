"""Dataclasses matching the app-control-plane Postgres schema.

See ``infra/docker/init.sql`` and architecture.md §8 for the canonical schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class Run:
    """One row in ``runs`` — created by the orchestrator API at POST /runs."""

    run_id: str  # ULID, lexicographically sortable by creation time
    config_snapshot: dict[str, object] = field(default_factory=dict)
    estimated_cost_usd: float | None = None
    cost_confidence_tier: str | None = None  # historical / calibration / default
    compute_cost_estimated_usd: float | None = None
    compute_cost_reconciled_usd: float | None = None
    budget_cap_usd: float | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: str = "pending"


@dataclass
class InstanceResult:
    """One row in ``instance_results`` — per (run, instance, attempt, phase).

    Idempotency key: ``(run_id, instance_id, attempt_number, phase)``.
    """

    run_id: str
    instance_id: str
    attempt_number: int
    phase: str  # "harness" or "eval"
    state: str
    error_category: str | None = None
    error_detail: str | None = None
    verdict: str | None = None
    wall_clock_harness_s: float | None = None
    wall_clock_eval_s: float | None = None
    touches_test_files: bool = False
    patch_path: str | None = None
    trajectory_path: str | None = None
    raw_log_path: str | None = None
    report_path: str | None = None  # S3 key for eval_report.json
    report_json: str = "{}"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# Note: InstanceProgress was removed at ADR-0018/R3-2 — live progress now lives
# on Redis (swebench_eval/database/redis_client.py), not Postgres.  The
# harness worker no longer writes instance_progress; ADR-0007's "workers never
# write directly" has zero exceptions.
