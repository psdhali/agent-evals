"""The ONE place the deployment's region, name prefix and derived resource names live.

Adoption Phase 1a (2026-09-11): the region (``us-west-2``) and the name prefix
(``eval-dev``) used to be literals in ~60 files, which made a second region, a
second environment in one account, or a fresh account impossible without a
tree-wide edit.  Every code path that needs one of them now reads it from here,
and the values come from the environment the task definition / the operator's
shell sets:

    AWS_REGION        (fallback AWS_DEFAULT_REGION, then boto3's own config;
                       last resort ``us-west-2`` so an unset laptop still works)
    EVAL_ENV_PREFIX   the Terraform ``name_prefix`` (default ``eval-dev``)
    AWS_ACCOUNT_ID    optional; resolved once via STS when a bucket name needs it

Terraform has its own copies (``var.region`` / ``var.name_prefix``) and stamps
these environment variables into every task definition, so the two sides agree
by construction rather than by memory.  The CI test ``tests/test_no_hardcoded_
aws_names.py`` greps the tree so a literal cannot creep back in.
"""

from __future__ import annotations

import os
from functools import lru_cache

DEFAULT_REGION = "us-west-2"
DEFAULT_PREFIX = "eval-dev"


def region() -> str:
    """The deployment's AWS region."""
    for key in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    try:
        import boto3

        configured = boto3.session.Session().region_name
        if configured:
            return str(configured)
    except Exception:  # noqa: BLE001 - boto3 absent or unconfigured: fall through
        return DEFAULT_REGION
    return DEFAULT_REGION


def name_prefix() -> str:
    """The Terraform ``name_prefix`` every resource name starts with."""
    return os.environ.get("EVAL_ENV_PREFIX", "").strip() or DEFAULT_PREFIX


@lru_cache(maxsize=1)
def account_id() -> str:
    """The AWS account id, from ``AWS_ACCOUNT_ID`` or one STS call (cached)."""
    value = os.environ.get("AWS_ACCOUNT_ID", "").strip()
    if value:
        return value
    import boto3

    return str(boto3.client("sts", region_name=region()).get_caller_identity()["Account"])


def named(suffix: str) -> str:
    """``<prefix>-<suffix>`` — a cluster, queue, secret, log group, role name."""
    return f"{name_prefix()}-{suffix}"


def account_bucket(kind: str) -> str:
    """``<prefix>-<kind>-<account>-<region>`` — the persistent tier's bucket naming."""
    return f"{name_prefix()}-{kind}-{account_id()}-{region()}"


def dataset_bucket_default() -> str:
    """The dataset mirror bucket when ``DATASET_BUCKET`` is unset."""
    return account_bucket("dataset")


def artifacts_bucket_default() -> str:
    return account_bucket("artifacts")


def ecr_registry() -> str:
    """``<account>.dkr.ecr.<region>.amazonaws.com``."""
    return f"{account_id()}.dkr.ecr.{region()}.amazonaws.com"


def ecr_repository(name: str) -> str:
    """``<registry>/<prefix>-<name>`` — e.g. ``ecr_repository("harness-worker")``."""
    return f"{ecr_registry()}/{named(name)}"
