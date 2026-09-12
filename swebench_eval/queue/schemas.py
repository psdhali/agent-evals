"""Job message schemas for the queue-based pipeline.

Phase 3: job messages carry instance data directly.  If any message
approaches the 256KB SQS cap during the calibration sample (Commit 11),
the messages switch to carrying instance_id only and the worker reads
the record from Postgres (P3-5c, ADR-0007).  That switch is now real on the
dispatcher→task hop: ADR-0032 passes a fixed-width job REFERENCE in
``containerOverrides`` (ECS caps the whole overrides object at ~8 KB), and the
task reads the instance from the mirror.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any, ClassVar

from swebench_eval.orchestrator.run_config import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_MAX_COST_USD_PER_INSTANCE,
    DEFAULT_MAX_TOKENS_PER_INSTANCE,
    DEFAULT_MAX_TURNS_PER_INSTANCE,
    DEFAULT_TIMEOUT_SECONDS,
)


@dataclass
class HarnessJob:
    """Enqueued to ``harness-jobs`` by the dispatcher.  One per (instance, attempt)."""

    run_id: str
    instance_id: str
    repo_url: str
    base_commit: str
    problem_statement: str
    attempt_number: int
    harness_name: str  # "custom_minimal", "aider", "mini_swe_agent"
    model_alias: str  # gateway model alias, e.g. "cheap-oss-model"
    # ADR-0030 H1: the environment image key ("sweb.env.<ext>.<arch>.<hash>")
    # the harness task for THIS instance must run, computed by the dispatcher at
    # enqueue from SWE-bench's own TestSpec — the worker never derives it. The
    # dispatcher reads it back to pick the per-env task-definition family (H3).
    env_image_key: str = ""
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_tokens_per_instance: int | None = DEFAULT_MAX_TOKENS_PER_INSTANCE
    max_cost_usd_per_instance: float = DEFAULT_MAX_COST_USD_PER_INSTANCE
    # master-handover 3.12: per-instance turn cap the shim enforces (all five
    # harnesses sold the same limit). None = unlimited.
    max_turns_per_instance: int | None = DEFAULT_MAX_TURNS_PER_INSTANCE
    # ADR-0037 / M0 §4.3: the dispatcher stamps dispatched_at (epoch-seconds) so
    # provision_s = dispatched_at -> container CreatedAt is computable on the
    # deployed path.  None on the poll/dev path when unknown.
    dispatched_at: float | None = None
    # ADR-0037 / M0-6 (review): queue_wait_s computed by the DISPATCHER (the
    # process that received the message) and carried on the reference, because
    # the deployed task-per-instance path never calls receive_message.  None
    # on the poll/dev path (there the worker computes it directly).
    queue_wait_s: float | None = None
    # Compaction build (Stage 1.2): the resolved per-model context window,
    # threaded like max_turns_per_instance.  None = compaction disabled for this
    # run (deliberate opt-out), mirroring the max_turns convention.
    context_window_tokens: int | None = DEFAULT_CONTEXT_WINDOW_TOKENS

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> HarnessJob:
        """Parse the SQS message body (the dispatcher and worker both use this)."""
        # Fail-closed on the ceilings (ADR-0019): the job message MUST carry
        # them.  A dropped field must raise (KeyError), not silently resolve to
        # "unlimited".  `None` is only legal when the dispatcher explicitly
        # passed it as "deliberately unlimited".
        return cls(
            run_id=str(body["run_id"]),
            instance_id=str(body["instance_id"]),
            repo_url=str(body["repo_url"]),
            base_commit=str(body["base_commit"]),
            problem_statement=str(body["problem_statement"]),
            attempt_number=int(body["attempt_number"]),
            harness_name=str(body["harness_name"]),
            model_alias=str(body["model_alias"]),
            env_image_key=str(body.get("env_image_key", "")),  # ADR-0030 H1
            timeout_seconds=int(body["timeout_seconds"]),
            max_tokens_per_instance=(
                int(body["max_tokens_per_instance"])
                if body["max_tokens_per_instance"] is not None
                else None
            ),
            max_cost_usd_per_instance=float(body["max_cost_usd_per_instance"]),
            max_turns_per_instance=(
                int(body["max_turns_per_instance"])
                if body["max_turns_per_instance"] is not None
                else None
            ),
            dispatched_at=_opt_float(body, "dispatched_at"),
            queue_wait_s=_opt_float(body, "queue_wait_s"),
            # M4 / Stage 1.2: absent -> DEFAULT; explicit JSON null -> None
            # (the .get(k, DEFAULT) two-sided contract, not bare .get(k)).
            context_window_tokens=_int_or_none_default(
                body, "context_window_tokens", DEFAULT_CONTEXT_WINDOW_TOKENS
            ),
        )


def _opt_float(body: dict[str, Any], key: str) -> float | None:
    v = body.get(key)
    return None if v is None else float(v)


def _int_or_none_default(body: dict[str, Any], key: str, default: int | None) -> int | None:
    """M4 two-sided contract: absent key -> ``default``; explicit null -> None.

    Prevents the ContextWindow bug from the turn cap: a bare ``.get(k)`` returns
    None for an ABSENT key, silently disabling the ceiling on every dispatch.
    """
    if key not in body:
        return default
    v = body[key]
    return default if v is None else int(v)


@dataclass
class JobReference:
    """The ADR-0032 dispatcher→task contract: a fixed-width job REFERENCE.

    ECS caps the whole ``overrides`` object at ~8 KB, and ``HarnessJob``
    serialises to up to 26 KB (the ``problem_statement`` is the variance). So
    ``containerOverrides`` carries only these ~200 bytes — every field here is
    fixed-width and instance-independent — and the TASK loads the instance
    (``repo``/``base_commit``/``problem_statement``) itself from the pinned
    mirror via ``load_single_instance(instance_id, include_gold=False)``.
    ``env_image_key`` does not travel: the dispatcher already used it to select
    the task-definition family, so the family encodes it.

    The wire format is environment variables (containerOverrides), defined in
    :attr:`ENV_NAMES` so the dispatcher (writer) and the worker (reader) cannot
    drift apart.
    """

    run_id: str
    instance_id: str
    attempt_number: int
    receipt_handle: str
    harness_name: str
    model_alias: str
    timeout_seconds: int
    max_tokens_per_instance: int | None  # None = deliberately unlimited
    max_cost_usd_per_instance: float
    # master-handover 3.12; None = unlimited (default so legacy references /
    # messages without the field parse as unlimited, matching from_env's
    # empty→None handling).
    max_turns_per_instance: int | None = None
    # Compaction build (Stage 1.2): the resolved context window.  None =
    # compaction disabled (deliberate opt-out), matching the max_turns convention.
    context_window_tokens: int | None = None
    # ADR-0037 / M0 §4.3: the dispatcher stamps when it called RunTask — the
    # provision_s boundary the task-metadata endpoint cannot give (it has no
    # RunTask timestamp).  Epoch seconds; None when unknown.
    dispatched_at: float | None = None
    # ADR-0037 / M0-6 (review): queue_wait_s the dispatcher computed from the
    # SQS attributes it received.  Seconds; None when unknown.
    queue_wait_s: float | None = None
    # run-launch (BUILDER4-RUN-LAUNCH-ORCHESTRATOR §5.2, ADR-0035 decision 1):
    # the run's per-run LiteLLM virtual key — minted at PROVISION, cached in
    # Redis (control_plane/run_key_cache.py) since it is never persisted to
    # Aurora (rule 3), and carried here so the task's own environment gets it
    # via containerOverrides as LITELLM_API_KEY (harnesses/routing.py prefers
    # it over LITELLM_MASTER_KEY).  None when the dispatcher found no cached
    # key for the run (a run that predates run-launch, or a lost cache entry)
    # — the worker then falls back to the master key unless
    # ENFORCE_PER_RUN_KEY=1 refuses the launch outright (harness_dispatcher.py).
    litellm_api_key: str | None = None

    # The wire names (env vars in containerOverrides). Field name -> env name.
    ENV_NAMES: ClassVar[dict[str, str]] = {
        "run_id": "RUN_ID",
        "instance_id": "INSTANCE_ID",
        "attempt_number": "ATTEMPT_NUMBER",
        "receipt_handle": "RECEIPT_HANDLE",
        "harness_name": "HARNESS_NAME",
        "model_alias": "MODEL_ALIAS",
        "timeout_seconds": "TIMEOUT_SECONDS",
        "max_tokens_per_instance": "MAX_TOKENS_PER_INSTANCE",
        "max_cost_usd_per_instance": "MAX_COST_USD_PER_INSTANCE",
        "max_turns_per_instance": "MAX_TURNS_PER_INSTANCE",
        "context_window_tokens": "CONTEXT_WINDOW_TOKENS",
        "dispatched_at": "DISPATCHED_AT",
        "queue_wait_s": "QUEUE_WAIT_S",
        "litellm_api_key": "LITELLM_API_KEY",
    }

    def to_env(self) -> list[dict[str, str]]:
        """The ``containerOverrides[].environment`` list the dispatcher writes."""
        out: list[dict[str, str]] = []
        for f in fields(self):
            name = self.ENV_NAMES[f.name]
            value = getattr(self, f.name)
            rendered = (
                ""
                if value is None
                else (f"{value:.3f}" if isinstance(value, float) else str(value))
            )
            out.append({"name": name, "value": rendered})
        return out

    @staticmethod
    def from_env(environ: dict[str, str] | None = None) -> JobReference:
        """Reconstruct a reference from the task's environment (ADR-0032).

        A missing or empty required field raises (fail-closed) — a task must
        never run with a half reference.
        """
        e = os.environ if environ is None else environ

        def _required(field_name: str) -> str:
            value = e.get(JobReference.ENV_NAMES[field_name], "")
            if not value:
                raise KeyError(f"missing job reference field {JobReference.ENV_NAMES[field_name]}")
            return value

        def _int_or_none(field_name: str) -> int | None:
            value = e.get(JobReference.ENV_NAMES[field_name], "")
            return int(value) if value else None

        def _float_or_none(field_name: str) -> float | None:
            value = e.get(JobReference.ENV_NAMES[field_name], "")
            return float(value) if value else None

        def _str_or_none(field_name: str) -> str | None:
            value = e.get(JobReference.ENV_NAMES[field_name], "")
            return value if value else None

        return JobReference(
            run_id=_required("run_id"),
            instance_id=_required("instance_id"),
            attempt_number=int(_required("attempt_number")),
            receipt_handle=_required("receipt_handle"),
            harness_name=_required("harness_name"),
            model_alias=_required("model_alias"),
            timeout_seconds=int(_required("timeout_seconds")),
            max_tokens_per_instance=_int_or_none("max_tokens_per_instance"),
            max_cost_usd_per_instance=float(_required("max_cost_usd_per_instance")),
            max_turns_per_instance=_int_or_none("max_turns_per_instance"),
            context_window_tokens=_int_or_none("context_window_tokens"),
            dispatched_at=_float_or_none("dispatched_at"),
            queue_wait_s=_float_or_none("queue_wait_s"),
            # run-launch: absent -> None (worker falls back to the master
            # key, see routing.gateway_api_key) — never required here, the
            # ENFORCE_PER_RUN_KEY refusal happens at the DISPATCHER, before
            # a reference with no key is ever built (harness_dispatcher.py).
            litellm_api_key=_str_or_none("litellm_api_key"),
        )


@dataclass
class EvalJob:
    """Enqueued to ``eval-jobs`` by the Results Writer.  One per harness result with a non-empty patch."""

    run_id: str
    instance_id: str
    attempt_number: int
    patch_s3_key: str  # S3 key for patch.diff (uploaded by harness worker)
    fail_to_pass: str
    pass_to_pass: str
    # Image validation (dev/IMAGE-PARITY-ROOT-CAUSE-AND-FIX-2026-09-05 Part B):
    # grade the dataset's GOLD patch instead of a captured one.  The eval
    # worker already loads the instance with include_gold=True (it needs the
    # gold test_patch), so the patch comes from the mirror row and
    # ``patch_s3_key`` is ignored.  Proves an instance image can resolve at
    # all — a gold that fails is an ENVIRONMENT defect, never a model one.
    use_gold_patch: bool = False


@dataclass
class ResultMessage:
    """Pushed to ``results`` by harness and eval workers.  Consumed by the Results Writer."""

    run_id: str
    instance_id: str
    attempt_number: int
    phase: str  # "harness" or "eval"
    state: str  # state machine state (PATCH_READY, RESOLVED, FAILED_HARNESS, …)
    error_category: str = ""
    error_detail: str = ""
    verdict: str = ""  # "resolved" | "unresolved" (eval phase only)
    wall_clock_s: float = 0.0
    # S3 keys for artifacts (not local paths — closes P2-C dangling-path problem).
    patch_s3_key: str = ""
    trajectory_s3_key: str = ""
    raw_log_s3_key: str = ""
    # B6/E11a: mini's NATIVE (pre-normalisation) trajectory, uploaded beside the
    # normalized one. Empty for adapters without a separate native artifact.
    native_trajectory_s3_key: str = ""
    report_json_s3_key: str = ""
    # dev/BUILDER4-EXPOSE-MISSING-ARTIFACTS-2026-08-28.md: SWE-bench's own grade
    # logs (5b review) — uploaded next to eval_report.json by
    # eval_worker._upload_run_logs, but the keys never made it onto the result
    # until now. Empty when grading never reached the point of writing them
    # (e.g. an invalid grade before the runner produced a log dir).
    test_output_s3_key: str = ""
    run_log_s3_key: str = ""
    # Usage summary (harness phase; the SHIM/sole meter — M0 §1).  NULL (None),
    # not 0, when no usage was reported — the eval phase and any run where the
    # shim parsed nothing are distinguishable from one that genuinely used no
    # tokens (R2-4 / Trap 3).
    input_tokens: int | None = None
    output_tokens: int | None = None
    # METERING-COMPLETENESS (2026-08-28): the rollup was a three-field
    # projection of the shim's cumulative Usage — cached/cache_write/reasoning
    # were dropped.  Add them (additive; the shim's Usage carries them) so the
    # report reads the full picture, not just new-input/new-output/cost.
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    # cost_source: "provider" | "local_pricing" (the write-path vocabulary — the
    # per-call llm_calls value, reconciled from Usage.source; see results_writer).
    cost_source: str | None = None
    # METERING-COMPLETENESS: count of calls whose usage could not be parsed — a
    # completeness marker so a shorted token total isn't recorded as complete
    # (e.g. the codex streaming-tail bug).  None when unknown, 0 when all parsed.
    usage_parse_failed_calls: int | None = None
    # BUILDER4-PACER-SEED-AND-FAIRNESS-2026-09-03 §2.5: the L1 pacer's per-instance rollup
    # (per-call facts summed, same shape as usage_parse_failed_calls). paced_wait_ms_total =
    # Σ every admission wait incl. hold-cap timeouts; paced_calls = calls denied at least
    # once; pacer_timeouts = calls that hit the hold cap (each surfaced a 429 to the CLI);
    # overload_retries_total = §8b provider-429 retries absorbed. None on eval rows.
    paced_wait_ms_total: int | None = None
    paced_calls: int | None = None
    pacer_timeouts: int | None = None
    overload_retries_total: int | None = None
    # PERSIST-TURNS-USED (2026-08-28): the shim's serviced-completion turn count —
    # the ONE harness-neutral turn definition (a forwarded model completion; probes /
    # model lists / count_tokens / shim refusals excluded).  harness-phase only,
    # NULL on eval rows.  >= count that always >= COUNT(*) of llm_calls for the
    # instance (non-completions still get a call record) — see results_writer/init.sql.
    turns_used: int | None = None
    cost_usd: float | None = None
    touches_test_files: bool = False
    # ADR-0037 / M0 §1.3: the ADAPTER's own reported usage as a STORED
    # CROSS-CHECK, never added to the shim figure (DoD #2).  None when the
    # adapter has no second meter (Trap 3 / R2-1).
    adapter_input_tokens: int | None = None
    adapter_output_tokens: int | None = None
    adapter_cost_usd: float | None = None

    # M0 §4/§5 phase timing (seconds, harness + eval).  None = not measured
    # (Trap 3) — never a fabricated number.  Populated on a real run; the M0 §8
    # validation gate asserts the phase sum reconciles to task_observed_s.
    queue_wait_s: float | None = None
    provision_s: float | None = None
    image_pull_s: float | None = None
    worker_boot_s: float | None = None
    repo_prep_s: float | None = None
    agent_s: float | None = None
    patch_extract_s: float | None = None
    artifact_upload_s: float | None = None
    task_observed_s: float | None = None
    task_billed_s: float | None = None
    repo_prep_cache_hit: bool | None = None
    image_pull_cold: bool | None = None
    eval_queue_wait_s: float | None = None
    eval_patch_fetch_s: float | None = None
    eval_image_pull_s: float | None = None
    eval_test_s: float | None = None
    eval_log_upload_s: float | None = None
    eval_image_pull_cold: bool | None = None

    # ADR-0038 — contamination + honesty (computed by the workers, persisted
    # here; None/empty = not produced for this attempt).
    stripped_test_paths: tuple[str, ...] = ()
    grade_invalid: bool | None = None
    leaked_node_ids: list[str] | None = None
    leak_detectable: bool | None = None  # N-1: had any absent-at-base FAIL_TO_PASS id
    gold_patch_similarity: float | None = None

    # Compaction build (BUILD-SPEC §6): per-instance compaction measurement —
    # how many passes fired, the tokens before/after the LAST pass, and the
    # resolved context window the run used.  NULL = no pass ran / not measured
    # (Trap 3), never a fabricated 0.
    compactions_fired: int | None = None
    compaction_tokens_before: int | None = None
    compaction_tokens_after: int | None = None
    context_window_tokens: int | None = None
