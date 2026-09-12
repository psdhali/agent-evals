"""Reconcile the gateway's db-managed model aliases against the git-tracked spec.

Registers every alias in ``swebench_eval.gateway.rotatable_models.ROTATABLE_MODELS``
as a LiteLLM db-model (idempotent — ``/model/new`` only if not already present,
see ``gateway/admin.py::ensure_model_registered``), so per-run key rotation
(ADR-0035, run-launch §5.1/§5.2) has something to rotate the moment a run
first references that alias, rather than paying the registration latency
inline during that run's PROVISION step.

**Why this exists as a standalone script, not just lazy on-launch
registration:** ``run_launch.py``'s PROVISION step already calls
``ensure_model_registered`` lazily on first use, which is sufficient for an
alias nobody has run against yet (the four new laguna per-harness aliases,
2026-08-26). It is NOT sufficient for migrating an alias that is currently
*only* declared in ``litellm_config.yaml``'s ``model_list`` (the 7 aliases
this build deliberately did not touch — see
``builder4-run-launch-response.md`` §0): removing an alias from the yaml on
a gateway redeploy, with nothing having pre-registered it as a db-model yet,
leaves a window — between the redeploy landing and the first run that
happens to reference that alias — where the alias exists in NEITHER the
yaml NOR the db, and any live traffic against it 404s. Verified locally
2026-08-28: a db-model DOES survive a full container restart with no yaml
entry at all (``store_model_in_db: true`` reloads it from the DB on boot),
but only once it has actually been created — there is no implicit "yaml
entry becomes a db-model" migration on removal.

**The correct sequence for migrating an alias off the yaml, established but
not yet executed for the 7 pre-existing aliases:**
  1. Add its spec to ``rotatable_models.py`` (git-tracked, same shape as the
     four already there).
  2. Run this script against the LIVE gateway BEFORE the next deploy that
     removes the yaml entry — confirms the db-model exists.
  3. Only then remove the yaml `model_list` entry and redeploy.
Steps 1-2 are safe to do at any time (additive; the db-model coexists with
the yaml entry harmlessly until step 3 — LiteLLM round-robins between them,
same load-balancing caveat ``rotatable_models.py`` documents, so step 3
should follow promptly once 1-2 are confirmed, not be left indefinitely).

Usage::

    python -m scripts.reconcile_gateway_models \
        --base-url http://gateway.eval.internal:4000/v1 \
        [--dry-run]

Reads ``LITELLM_MASTER_KEY`` from the environment (never logged, never
printed) — this is an admin-only operation.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("reconcile_gateway_models")


def reconcile(base_url: str, master_key: str, *, dry_run: bool) -> dict[str, str]:
    """Ensure every ROTATABLE_MODELS alias exists as a db-model. Returns
    ``{alias: model_id}`` for what exists (or would exist, in dry-run)."""
    from swebench_eval.gateway import admin
    from swebench_eval.gateway.rotatable_models import ROTATABLE_MODELS

    results: dict[str, str] = {}
    for alias, spec in ROTATABLE_MODELS.items():
        existing = admin.find_db_model_id(base_url, master_key, alias)
        if existing is not None:
            logger.info("%s: already registered (id=%s)", alias, existing)
            results[alias] = existing
            continue
        if dry_run:
            logger.info("%s: NOT registered — would create (dry-run, no change made)", alias)
            continue
        model_id = admin.ensure_model_registered(
            base_url, master_key, alias, spec.litellm_params, spec.model_info
        )
        logger.info("%s: registered (id=%s)", alias, model_id)
        results[alias] = model_id
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LITELLM_BASE_URL", ""),
        help="Gateway base URL (defaults to LITELLM_BASE_URL env).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be registered without making any change.",
    )
    args = parser.parse_args()

    if not args.base_url:
        logger.error("no --base-url given and LITELLM_BASE_URL is unset; refusing to guess")
        return 2
    master_key = os.environ.get("LITELLM_MASTER_KEY", "")
    if not master_key:
        logger.error("LITELLM_MASTER_KEY is unset; refusing to run unauthenticated")
        return 2

    results = reconcile(args.base_url, master_key, dry_run=args.dry_run)
    logger.info("done: %d alias(es) confirmed registered", len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
