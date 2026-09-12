"""Image validation — grade the GOLD patch in an instance's ``-inst`` image.

dev/IMAGE-PARITY-ROOT-CAUSE-AND-FIX-2026-09-05.md Part B: an instance image
is only trusted for scoring once the dataset's own fix RESOLVES in it.  Ten of
the 41-run's 22 unresolved instances were the ENVIRONMENT (pandas/numpy drift,
missing django admin templates, a pytest collection error) — a gold grade
would have flagged every one of them before a single model dollar was spent.

Shape (deliberately the regrade's, not a new pipeline):

* one NAMED validation run per request, ``image-validation-<utc stamp>-<hex>``
  (``status='validation'`` — not ``running``, so the reaper and the close
  path ignore it, and never closed, so it never touches key revocation).
  Owner steer 2026-09-05: a fresh run per click, not one ever-growing
  synthetic run, so each validation batch has its own page + resolve rate;
* one eval-phase ``instance_results`` row per instance (attempt 1) with
  ``retry_reason='image_validation'``, seeded PENDING then sent as an
  ``EvalJob(use_gold_patch=True)``;
* the results writer lands RESOLVED / UNRESOLVED / FAILED_EVAL on it exactly
  as for any eval attempt, so the run detail page, the artifacts (report,
  test_output) and the resolve rate all work unchanged — the validation run's
  "resolve rate" IS the gold pass rate of the images.

Seed-then-send, same as ``restart.regrade_instances``: a committed PENDING row
whose send then fails is a visible non-terminal row, never a silent gap.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

from swebench_eval.orchestrator.control_plane.results_writer import _ALL_NON_TERMINAL_STATES
from swebench_eval.queue.client import send_message
from swebench_eval.queue.schemas import EvalJob

logger = logging.getLogger(__name__)

VALIDATION_RUN_PREFIX = "image-validation"
VALIDATION_RUN_STATUS = "validation"
RETRY_REASON = "image_validation"


def new_validation_run_id() -> str:
    """``image-validation-YYYYMMDDTHHMMSSZ-<4 hex>`` — sortable, readable in the
    run list, unique per click (the hex covers two clicks in one second)."""
    import time
    import uuid

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{VALIDATION_RUN_PREFIX}-{stamp}-{uuid.uuid4().hex[:4]}"


class ImageValidationError(Exception):
    """A validation request could not be carried out as asked."""


def _db() -> Any:
    from swebench_eval.database.connection import get_connection

    return get_connection()


def _provenance_facts(instance_ids: list[str]) -> dict[str, Any]:
    """The dispatcher's reproducibility facts for a validation run, or ``{}``.

    A provenance lookup (ECR digest, git, package metadata) must never stop a
    gold gate from being enqueued — the facts are recorded when readable and
    absent otherwise, exactly as the harness-run snapshot treats them."""
    try:
        from swebench_eval.orchestrator.control_plane.dispatcher import reproducibility_facts

        facts = reproducibility_facts(harness=None, instance_ids=instance_ids)
    except Exception:
        logger.warning("validation run: could not record provenance facts", exc_info=True)
        return {}
    facts.pop("harness_tool_surface", None)
    return dict(facts)


def validate_images(instance_ids: list[str], *, actor: str = "operator") -> dict[str, Any]:
    """Create a fresh named validation run and enqueue a gold-patch grade for
    each instance in it (attempt 1 each).

    Returns ``{"run_id", "validated": [{instance_id, attempt_number}],
    "skipped": [{instance_id, reason}]}``.  An instance whose gold grade is
    still in flight in ANY earlier validation run is skipped (one gold grade
    at a time per instance — the eval host is shared).
    """
    ids = [i for i in dict.fromkeys(instance_ids) if i]
    if not ids:
        raise ImageValidationError("instance_ids must be non-empty")

    run_id = new_validation_run_id()
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT instance_id
                     FROM instance_results
                    WHERE run_id LIKE %s AND instance_id = ANY(%s)
                      AND retry_reason = %s AND state = ANY(%s)""",
                (f"{VALIDATION_RUN_PREFIX}%", ids, RETRY_REASON, sorted(_ALL_NON_TERMINAL_STATES)),
            )
            in_flight = {str(r[0]) for r in cur.fetchall()}

            validated: list[dict[str, Any]] = []
            skipped: list[dict[str, str]] = []
            for instance_id in ids:
                if instance_id in in_flight:
                    skipped.append(
                        {"instance_id": instance_id, "reason": "validation still in flight"}
                    )
                    continue
                validated.append({"instance_id": instance_id, "attempt_number": 1})

            if validated:
                cur.execute(
                    """INSERT INTO runs (run_id, config_snapshot, status)
                       VALUES (%s, %s, %s)""",
                    (
                        run_id,
                        json.dumps(
                            {
                                "purpose": "image-validation",
                                "note": "gold-patch grades of -inst images; not a scored run",
                                "actor": actor,
                                "instance_count": len(validated),
                                # 2026-09-06: the same PA-9 / ADR-0043 facts a harness run
                                # records (framework sha, swebench version, dataset +
                                # revision + digest snapshot, the -inst digest graded) —
                                # a gate result without them cannot be tied to the images
                                # it validated. Never raises (see reproducibility_facts).
                                **_provenance_facts([e["instance_id"] for e in validated]),
                            }
                        ),
                        VALIDATION_RUN_STATUS,
                    ),
                )
                for entry in validated:
                    cur.execute(
                        """INSERT INTO instance_results
                               (run_id, instance_id, attempt_number, phase, state,
                                seeded_at, retry_reason)
                           VALUES (%s, %s, 1, 'eval', 'PENDING', now(), %s)""",
                        (run_id, entry["instance_id"], RETRY_REASON),
                    )
        conn.commit()
    finally:
        conn.close()

    if validated:
        _dispatch_validation(run_id, validated)
        # The supervisor's ticks (eval autoscaler included) idle-gate on the
        # run-activity marker (ADR-0034 §2) — a gold grade landing on an idle
        # fleet needs the eval hosts to come up exactly like a scored run's
        # would.  Same call run_launch makes after a claim; best-effort so a
        # Redis hiccup can never turn an already-enqueued grade into a 500.
        try:
            from swebench_eval.control import state as control_state

            control_state.mark_runs_active()
        except Exception:
            logger.warning("image validation: could not mark runs active", exc_info=True)

    logger.info(
        "image validation: operator %s enqueued %d gold grade(s), skipped %d",
        actor,
        len(validated),
        len(skipped),
    )
    return {"run_id": run_id, "validated": validated, "skipped": skipped}


def _dispatch_validation(run_id: str, validated: list[dict[str, Any]]) -> None:
    """One ``EvalJob(use_gold_patch=True)`` per seeded row, outside the
    transaction (same as ``restart._dispatch_regraded``)."""
    for entry in validated:
        job = EvalJob(
            run_id=run_id,
            instance_id=entry["instance_id"],
            attempt_number=entry["attempt_number"],
            patch_s3_key="",
            fail_to_pass="",
            pass_to_pass="",
            use_gold_patch=True,
        )
        send_message("eval-jobs", dataclasses.asdict(job))
        logger.info(
            "image validation: enqueued %s attempt %d (gold patch)",
            entry["instance_id"],
            entry["attempt_number"],
        )
