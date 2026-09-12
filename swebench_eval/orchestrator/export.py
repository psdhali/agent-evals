"""GET /runs/{run_id}/export — the M6.2 publication schema.

``dev/BUILDER1-EXPORT-AND-LIVE-ENDPOINTS-2026-08-31.md`` §1.  Builds the
``publication-site-design.md`` §5.1 shape EXACTLY — that document is the
contract builder 2 builds the publication site against.  Do not redesign it.

The rule that outranks everything in that document:

    *"Every numeric field is nullable.  A null renders as 'not measured' and
    must never be coerced to zero — a harness with no instrumentation would
    otherwise chart as free and infinitely efficient."*

Three things the 2026-08-19 schema predates, handled here per the brief:

  * ``totals.attempted`` comes from the resolve-rate denominator (infra-retry
    collapse), NOT ``run_summary.expected`` (frozen at dispatch) — see
    ``queries.resolve_rate_denominator``;
  * ``pass_at_k`` honours ``retry_reason`` — configured
    ``attempts_per_instance > 1`` slots and ``operator_rerun_pass_at_k``
    restarts are legitimate k, ``operator_infra_retry`` is not (it collapses
    into the attempt it retried);
  * aborted instances are excluded from BOTH denominators.

Everything here is pure — the run row + instance rows are fetched by
``queries`` and assembled here, so the math is unit-testable without a DB.
"""

from __future__ import annotations

import math
from collections import Counter
from statistics import median
from typing import Any

# The abort states (ADR-0034 M1): work that never ran — excluded from every
# denominator (BUILDER1-EXPORT §1 item 3) and never a terminated_reason.
_ABORT_STATES = frozenset({"ABORTED_IN_FLIGHT", "NEVER_DISPATCHED"})
# The retry_reason of an attempt that merely retried a prior infra failure.
_INFRA_RETRY = "operator_infra_retry"

# error_category -> the schema's terminated_reason vocabulary (the harness
# TerminatedReason strings, inverse of map_terminated_reason_to_error_category).
# Categories the schema predates keep their own (lowercased) key rather than
# being silently collapsed into a wrong bucket — a terminated_reasons object
# with an extra key is honest; folding PAUSED_BY_OPERATOR into "crash" is not.
_TERMINATED_REASON_BY_CATEGORY = {
    "HARNESS_TIMEOUT": "timeout",
    "HARNESS_STUCK": "stuck",
    "HARNESS_BUDGET_EXCEEDED": "budget_exceeded",
    "HARNESS_MAX_TURNS_EXCEEDED": "max_turns_exceeded",
    "HARNESS_MAX_TOKENS_TRUNCATED": "max_tokens_truncated",
    "HARNESS_MODEL_REFUSED": "refused",
    "HARNESS_CRASH": "crash",
    "MODEL_API_ERROR": "model_api_error",
    "HARNESS_PATCH_EXTRACT_TIMEOUT": "patch_extract_timeout",
    "HARNESS_MALFORMED_TOOL_CALLS": "malformed_tool_calls",
    "HARNESS_ZERO_MODEL_CALLS": "zero_model_calls",
    "HARNESS_EMPTY_RESPONSE": "empty_response",
    "HARNESS_CONTEXT_EXHAUSTED": "context_exhausted",
    # A harness that completed with no patch is still a completion.
    "EMPTY_PATCH": "completed",
    # Eval/orchestrator-side outcomes the schema predates.
    "EVAL_INFRA_ERROR": "eval_infra_error",
    "EVAL_GRADE_INVALID": "grade_invalid",
    "ORCHESTRATOR_INFRA_ERROR": "orchestrator_infra_error",
    "PAUSED_BY_OPERATOR": "paused_by_operator",
}

# The metering + timing fields that live on the harness-phase row.
_HARNESS_NUMERIC_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "cost_usd",
    "queue_wait_s",
    "provision_s",
    "image_pull_s",
    "repo_prep_s",
    "agent_s",
    "eval_test_s",
    "task_billed_s",
    "task_observed_s",
    "gold_patch_similarity",
)


def _wilson_interval(positive: int, n: int) -> list[float] | None:
    """Wilson score interval for a proportion (``ci95_*``), or None when n == 0.

    A null (n == 0) must never be a fabricated ``[0, 0]`` interval.  95%
    z = 1.96.  Rounded to 4dp for a stable, readable artifact.
    """
    if n <= 0:
        return None
    z = 1.959963984540054
    p = positive / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


def _median(values: list[float]) -> float | None:
    """Median (1dp) or None when empty — never a fabricated 0."""
    if not values:
        return None
    return round(median(values), 1)


def _sum_or_none(values: list[Any]) -> int | None:
    """Sum of measured values, or None when nothing was measured (Trap 3).

    A harness with no instrumentation must export ``null`` for its token/cost
    totals, never 0 — otherwise it charts as free and infinitely efficient.
    """
    nums = [v for v in values if isinstance(v, (int, float))]
    return int(sum(nums)) if nums else None


def _terminated_reason(pair: dict[str, Any]) -> str | None:
    """A pair's schema terminated_reason, or None when it never terminated.

    The harness's error_category maps to the TerminatedReason vocabulary when
    present — a run cut off at max_turns is ``max_turns_exceeded`` even when a
    partial patch was still graded (the eval verdict says whether it resolved;
    it does not change WHY the harness ended).  Only when the harness has no
    error category (it completed normally) does an eval verdict make it
    ``completed``.  A pair with neither is still in flight (or aborted).
    """
    category = pair.get("error_category")
    if category:
        return _TERMINATED_REASON_BY_CATEGORY.get(category, str(category).lower())
    if pair.get("verdict") in ("resolved", "unresolved"):
        return "completed"
    return None


def _group_pairs(rows: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    """Merge the phase rows (harness + eval) into one dict per (instance, attempt).

    The harness row carries the metering (shim) + timing + retry_reason +
    error_category; the eval row carries the verdict + integrity flags.  A
    field is kept from whichever phase measured it (last non-None write wins,
    which is phase-correct because the columns are phase-specific).
    """
    pairs: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["instance_id"]), int(row["attempt_number"]))
        pair = pairs.setdefault(
            key,
            {
                "instance_id": str(row["instance_id"]),
                "attempt_number": int(row["attempt_number"]),
                "verdict": None,
                "error_category": None,
                "retry_reason": None,
                "grade_invalid": False,
                "touches_test_files": False,
                "leak_detectable": None,
                "leaked_node_ids": None,
                "gold_patch_similarity": None,
                "aborted": False,
                **{f: None for f in _HARNESS_NUMERIC_FIELDS},
            },
        )
        if row.get("state") in _ABORT_STATES:
            pair["aborted"] = True
        phase = row.get("phase")
        if phase == "harness" and row.get("state"):
            # the harness-phase terminal state: EMPTY_PATCH has no eval row but
            # is a model failure that belongs in `gradeable` (ADR-0038 §4)
            pair["harness_state"] = str(row["state"])
        if phase == "eval":
            if row.get("verdict"):
                pair["verdict"] = str(row["verdict"])
            if row.get("grade_invalid") is True:
                pair["grade_invalid"] = True
            if row.get("leak_detectable") is not None:
                pair["leak_detectable"] = bool(row["leak_detectable"])
            if row.get("leaked_node_ids") is not None:
                pair["leaked_node_ids"] = list(row["leaked_node_ids"])
            if row.get("gold_patch_similarity") is not None:
                pair["gold_patch_similarity"] = float(row["gold_patch_similarity"])
            if row.get("touches_test_files") is not None:
                pair["touches_test_files"] = bool(row["touches_test_files"])
            # The grade's own timing lives on the eval row; without this the
            # schema's timing_p50_s.eval_test was always null (2026-09-06).
            if row.get("eval_test_s") is not None:
                pair["eval_test_s"] = row["eval_test_s"]
        else:
            if row.get("error_category"):
                pair["error_category"] = str(row["error_category"])
            if row.get("retry_reason"):
                pair["retry_reason"] = str(row["retry_reason"])
            if row.get("touches_test_files") is not None:
                pair["touches_test_files"] = bool(row["touches_test_files"])
            for field in _HARNESS_NUMERIC_FIELDS:
                if row.get(field) is not None:
                    pair[field] = row[field]
    return pairs


def build_run_export(
    run: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    resolve_rate_denominator: int,
) -> dict[str, Any]:
    """Assemble the §5.1 export from the run row + all instance_results rows.

    ``rows`` is every instance_results row (both phases) for the run.
    ``resolve_rate_denominator`` is ``queries.resolve_rate_denominator()`` —
    the honest denominator after infra-retry collapse (the brief's item 1);
    aborted pairs are then subtracted (item 3).
    """
    pairs = _group_pairs(rows)
    non_aborted = [p for p in pairs.values() if not p["aborted"]]
    # An operator_infra_retry attempt collapses INTO the attempt it retried —
    # it is not a separate trial.  2026-09-06 (first 500-run, django-15098 /
    # 16502): "collapse" used to mean "drop", which left the retried attempt
    # with its crash and no verdict — the export said gradeable 498 / 76.1%
    # while the run's own summary said 500 / 75.8%.  The retry's OUTCOME is the
    # attempt's outcome and its spend is real, so the pair now carries the
    # latest retry's verdict/category/integrity/timing and the chain's summed
    # spend.  One entry per legitimate attempt keeps the totals reconcilable
    # with the instances list (their sum equals the totals — a property
    # builder 2's charts depend on).
    measured = _collapse_infra_retries(non_aborted)

    # ---- totals -----------------------------------------------------------
    # attempted comes FROM the resolve-rate denominator, minus aborted pairs
    # (an aborted pair that is also an infra retry is already excluded by the
    # function itself, so only non-infra aborted pairs are subtracted).  This
    # is exactly len(measured) — the two cannot drift.
    aborted_not_infra = sum(
        1 for p in pairs.values() if p["aborted"] and p.get("retry_reason") != _INFRA_RETRY
    )
    attempted = resolve_rate_denominator - aborted_not_infra
    # gradeable = every attempt the model got a fair shot at: a verdict that was
    # not refused, PLUS an empty submission (EMPTY_PATCH — a model failure with
    # no eval row).  ADR-0038 §4 excludes only infrastructure categories and
    # invalid grades; dropping empty patches flattered the rate (2026-09-06).
    gradeable = sum(
        1
        for p in measured
        if (p["verdict"] in ("resolved", "unresolved") and not p["grade_invalid"])
        or (p["verdict"] is None and p.get("harness_state") == "EMPTY_PATCH")
    )
    resolved = sum(1 for p in measured if p["verdict"] == "resolved")

    rate_attempted = round(resolved / attempted, 4) if attempted else None
    rate_gradeable = round(resolved / gradeable, 4) if gradeable else None

    totals: dict[str, Any] = {
        "attempted": attempted,
        "gradeable": gradeable,
        "resolved": resolved,
        "resolve_rate_attempted": rate_attempted,
        "resolve_rate_gradeable": rate_gradeable,
        "ci95_attempted": _wilson_interval(resolved, attempted),
        "ci95_gradeable": _wilson_interval(resolved, gradeable),
        "pass_at_k": _pass_at_k(measured),
        "cost_usd_total": _cost_total(measured),
        "compute_cost_usd_total": _compute_cost(run),
        "tokens": {
            "input": _sum_or_none([p["input_tokens"] for p in measured]),
            "output": _sum_or_none([p["output_tokens"] for p in measured]),
            "cached": _sum_or_none([p["cached_tokens"] for p in measured]),
            "reasoning": _sum_or_none([p["reasoning_tokens"] for p in measured]),
        },
    }

    # ---- per-termination counts -------------------------------------------
    terminated: Counter[str] = Counter()
    for p in measured:
        reason = _terminated_reason(p)
        if reason:
            terminated[reason] += 1

    return {
        "schema_version": 1,
        "provenance": _provenance(run),
        "totals": totals,
        "terminated_reasons": dict(sorted(terminated.items())),
        "timing_p50_s": _timing_p50(measured),
        "integrity": _integrity(measured),
        "instances": _instances(measured),
    }


# Spend-like numerics: an infra retry's cost/tokens/time ADD to the attempt it
# retried (the crashed attempt's model calls were paid for).  Every other
# numeric (phase timings, gold similarity) is a property of the run that
# produced the outcome, so the retry's value replaces the original's.
_SUMMED_ON_RETRY = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "cost_usd",
        "agent_s",
        "task_billed_s",
        "task_observed_s",
    }
)
# Outcome fields taken wholesale from the retry — including a None, so a crash
# category on the original never survives a retry that completed cleanly.
_OUTCOME_ON_RETRY = (
    "verdict",
    "error_category",
    "harness_state",
    "grade_invalid",
    "touches_test_files",
    "leak_detectable",
    "leaked_node_ids",
)


def _collapse_infra_retries(non_aborted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One pair per LEGITIMATE attempt, each carrying its infra-retry chain.

    Attempts are walked per instance in attempt order.  A legitimate attempt
    (``retry_reason`` is not ``operator_infra_retry``) starts a new entry; an
    infra retry folds into the most recent legitimate attempt of the same
    instance — outcome fields replaced by the retry's, spend summed, the
    original ``attempt_number`` kept (the schema's ``attempt`` is the trial
    slot, not the DB row).  An infra retry with no legitimate predecessor
    (cannot happen through the API) has nothing to collapse into and is
    dropped, as before.
    """
    by_instance: dict[str, list[dict[str, Any]]] = {}
    for p in sorted(non_aborted, key=lambda x: (x["instance_id"], x["attempt_number"])):
        by_instance.setdefault(p["instance_id"], []).append(p)
    out: list[dict[str, Any]] = []
    for attempts in by_instance.values():
        chain: dict[str, Any] | None = None
        for p in attempts:
            if p.get("retry_reason") != _INFRA_RETRY:
                chain = dict(p)
                out.append(chain)
                continue
            if chain is None:
                continue
            for field in _OUTCOME_ON_RETRY:
                chain[field] = p.get(field)
            for field in _HARNESS_NUMERIC_FIELDS:
                value = p.get(field)
                if field in _SUMMED_ON_RETRY:
                    prior = chain.get(field)
                    if value is not None or prior is not None:
                        chain[field] = (prior or 0) + (value or 0)
                elif value is not None:
                    chain[field] = value
    return out


def _timing_p50(measured: list[dict[str, Any]]) -> dict[str, float | None]:
    """timing_p50_s: the median of each phase timing across the run."""
    fields = (
        ("queue_wait", "queue_wait_s"),
        ("provision", "provision_s"),
        ("image_pull", "image_pull_s"),
        ("repo_prep", "repo_prep_s"),
        ("agent", "agent_s"),
        ("eval_test", "eval_test_s"),
    )
    return {
        name: _median([p[col] for p in measured if isinstance(p.get(col), (int, float))])
        for name, col in fields
    }


def _integrity(measured: list[dict[str, Any]]) -> dict[str, Any]:
    """integrity: contamination counts + gold-patch-similarity median."""
    return {
        "leak_detectable_instances": sum(1 for p in measured if p["leak_detectable"]),
        "leaked_instances": sum(1 for p in measured if p.get("leaked_node_ids")),
        "touches_test_files": sum(1 for p in measured if p["touches_test_files"]),
        "grade_invalid": sum(1 for p in measured if p["grade_invalid"]),
        "gold_patch_similarity_p50": _median(
            [
                p["gold_patch_similarity"]
                for p in measured
                if isinstance(p.get("gold_patch_similarity"), (int, float))
            ]
        ),
    }


def _pass_at_k(non_aborted: list[dict[str, Any]]) -> dict[str, float | None]:
    """pass@k over the run's instances (the brief's item 2).

    For each instance, its LEGITIMATE attempts are those with
    ``retry_reason != 'operator_infra_retry'`` — configured
    ``attempts_per_instance`` slots (retry_reason NULL) and
    ``operator_rerun_pass_at_k`` restarts both count, infra retries do not
    (they collapse into the attempt they retried).  ``pass_at_k[k]`` = the
    fraction of instances solved within their first k legitimate attempts
    (an instance is solved if any legitimate attempt <= k resolved).
    """
    by_instance: dict[str, list[tuple[int, bool]]] = {}
    for p in non_aborted:
        if p.get("retry_reason") == _INFRA_RETRY:
            continue
        by_instance.setdefault(p["instance_id"], []).append(
            (p["attempt_number"], p["verdict"] == "resolved")
        )
    max_k = max((k for inst in by_instance.values() for k, _ in inst), default=0)
    if max_k == 0:
        return {}
    out: dict[str, float | None] = {}
    for k in range(1, max_k + 1):
        solved = sum(
            1 for inst in by_instance.values() if any(attempt <= k and ok for attempt, ok in inst)
        )
        out[str(k)] = round(solved / len(by_instance), 4)
    return out


def _cost_total(non_aborted: list[dict[str, Any]]) -> float | None:
    values = [p["cost_usd"] for p in non_aborted if isinstance(p.get("cost_usd"), (int, float))]
    return round(sum(values), 6) if values else None


def _compute_cost(run: dict[str, Any]) -> float | None:
    """The run-level compute cost (the schema's compute_cost_usd_total).

    instance_results has no per-instance compute-cost column; the runs row
    carries the estimate + reconciliation.  Prefer the reconciled figure when
    present, else the estimate; None (never 0) when neither exists.
    """
    for key in ("compute_cost_reconciled_usd", "compute_cost_estimated_usd"):
        value = run.get(key)
        if isinstance(value, (int, float)):
            return round(float(value), 6)
    return None


def _pin_line(snapshot: dict[str, Any]) -> str | None:
    """``swebench 5.0.2 · SWE-bench/SWE-bench_Verified@78f471bf655a · <snapshot file>``.

    The ADR-0043 triple in the form the publication site prints it (2026-09-06):
    harness version, dataset@revision (12 hex, like the snapshot file name),
    and the committed image-digest snapshot.  ``None`` unless all three are
    recorded — a partial pin printed as a pin would be the lie the ADR forbids.
    """
    version = snapshot.get("swebench_version")
    dataset = snapshot.get("dataset_name")
    revision = snapshot.get("dataset_revision")
    digest_file = snapshot.get("image_digest_snapshot")
    if not (version and dataset and revision and digest_file):
        return None
    return f"swebench {version} · {dataset}@{str(revision)[:12]} · {digest_file}"


def _provenance(run: dict[str, Any]) -> dict[str, Any]:
    """Provenance from runs.config_snapshot + the recorded digests (PA-9).

    ``model_resolved`` is NEVER omitted (publication-site-design §6) — the key
    is always present, null only when the alias's literal upstream model cannot
    be resolved from the snapshot (the site's Provenance component then fails
    loudly rather than shipping a number without it).
    """
    snapshot = run.get("config_snapshot") or {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    alias = snapshot.get("model_alias")
    resolved_models = snapshot.get("resolved_models") or {}
    model_entry = (
        resolved_models.get(alias) or {} if isinstance(resolved_models, dict) and alias else {}
    )

    harness = snapshot.get("harness")
    cli_versions = snapshot.get("harness_cli_versions")
    harness_cli_version = None
    if isinstance(cli_versions, dict):
        harness_cli_version = cli_versions.get(harness)
    elif isinstance(cli_versions, str):
        harness_cli_version = cli_versions

    return {
        "run_id": run.get("run_id"),
        "created_at": run.get("created_at"),
        "framework_sha": snapshot.get("framework_sha"),
        "swebench_version": snapshot.get("swebench_version"),
        "dataset_name": snapshot.get("dataset_name"),
        "dataset_revision": snapshot.get("dataset_revision"),
        "image_digest_snapshot": snapshot.get("image_digest_snapshot"),
        # ADR-0043's pin as one line — "results graded under different values
        # are never combined in one table"; null when any half is unknown.
        "pin": _pin_line(snapshot),
        "harness_image_digest": snapshot.get("harness_image_digest"),
        "gateway_config_hash": snapshot.get("gateway_config_hash"),
        "model_alias": alias,
        "model_resolved": model_entry.get("model"),
        "harness": harness,
        "harness_cli_version": harness_cli_version,
        # ADR-0033: every harness this framework runs is network-isolated (the
        # dispatcher refuses to launch a non-isolated harness); the schema's
        # provenance posture is a constant.
        "network_posture": "isolated",
        "limits": {
            "max_tokens": snapshot.get("max_tokens_per_instance"),
            "max_cost_usd_per_instance": snapshot.get("max_cost_usd_per_instance"),
            "attempts_per_instance": snapshot.get("attempts_per_instance"),
            "temperature": model_entry.get("temperature"),
        },
    }


def _instances(non_aborted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per (instance, attempt) — the schema's ``instances`` list.

    Infra-retry attempts are excluded (they collapse into the attempt they
    retried — listing them would double-count the same work).
    """
    out: list[dict[str, Any]] = []
    for p in sorted(non_aborted, key=lambda x: (x["instance_id"], x["attempt_number"])):
        if p.get("retry_reason") == _INFRA_RETRY:
            continue
        leaked = None
        if p.get("leaked_node_ids") is not None:
            leaked = len(p["leaked_node_ids"]) > 0
        out.append(
            {
                "instance_id": p["instance_id"],
                "attempt": p["attempt_number"],
                "verdict": p["verdict"],
                "error_category": p["error_category"],
                "terminated_reason": _terminated_reason(p),
                "input_tokens": p["input_tokens"],
                "output_tokens": p["output_tokens"],
                "cost_usd": p["cost_usd"],
                "agent_s": p["agent_s"],
                "task_billed_s": p["task_billed_s"],
                "leak_detectable": p["leak_detectable"],
                "leaked": leaked,
                "touches_test_files": p["touches_test_files"],
                "gold_patch_similarity": p["gold_patch_similarity"],
            }
        )
    return out
