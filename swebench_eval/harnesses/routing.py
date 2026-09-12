"""Harness model-routing helpers.

Every adapter reads its gateway/shims base URL from this single function rather
than hardcoding a literal (R5-1).  In Phase 4 this resolves to the LiteLLM
gateway; once the per-worker shim exists (ADR-0019, Commit 6b) the worker points
the same env var at the shim address and the adapters follow with no change here.

Also the single place the FRAMEWORK_PAUSED marker is recognised (M1.11): a
paused gateway's ALB answers 503 with ``{"error":{"type":"framework_paused"}}``,
and every consumer — the per-worker shim (gateway/local_proxy.py) and, when the
shim is bypassed, a harness — must classify THAT response as an operator pause,
NOT as a model failure.  Keeping the membership test here means the shim and any
future direct-to-ALB harness agree on what "paused" looks like and cannot drift.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ADR-0034 M1.11 — the stable marker a paused gateway's ALB answers with.
FRAMEWORK_PAUSED_503_BODY = b'{"error":{"type":"framework_paused"}}'


def is_framework_paused_response(status_code: int, body: bytes | str) -> bool:
    """True when *body* is the ALB's stable ``framework_paused`` 503 marker.

    Keys on the MARKER, not on the status alone: a genuinely wedged LiteLLM that
    happens to answer 503 is NOT an operator pause, and classifying it as one
    converts a real MODEL_API_ERROR into PAUSED_BY_OPERATOR — the same data
    corruption M1.11 exists to prevent, run backwards.
    """
    if status_code != 503:
        return False
    if isinstance(body, str):
        body = body.encode("utf-8")
    return b"framework_paused" in body


def is_operator_block_response(status_code: int, body: bytes | str) -> bool:
    """True when *body* is LiteLLM's stable "this key is blocked" 401 marker.

    BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §1/§6: gateway
    pause (global or per-run) blocks the run's LiteLLM virtual key via
    ``/key/block`` rather than the ALB. A blocked key's calls fail with
    ``401 {"error":{"type":"auth_error","message":"...Key is blocked..."}}`` —
    verified live, and distinguishable from a genuinely bad/expired key
    (``type: "token_not_found_in_db"``, no "blocked" substring). Keys on the
    MARKER, not the status alone, same discipline as
    :func:`is_framework_paused_response` and for the same reason: a real auth
    failure must never be misclassified as an operator pause.
    """
    if status_code != 401:
        return False
    if isinstance(body, str):
        body = body.encode("utf-8")
    # Loose substring match (not exact-JSON-shape), same discipline as
    # is_framework_paused_response — tolerant of whitespace/field-order
    # differences across LiteLLM versions, still specific enough that a
    # genuinely bad key (no "blocked" wording, different type) can't match.
    return b"auth_error" in body and b"Key is blocked" in body


def is_real_provider_overload(status_code: int, retry_after_s: float | None) -> bool:
    """True when a 429 is a real upstream provider overload, not LiteLLM throttling us.

    BUILDER4-AUTOSCALER-FULL-2026-08-29.md §2.1: ``_classify_rate_limit`` (local_proxy.py) keys on
    the exception string ``litellm.RateLimitError``, but LiteLLM wraps upstream rate limits in its
    own exception too — the string match mislabels real provider throttles as ``gateway``, and its
    own docstring admits the heuristic was never live-validated. The reliable discriminator, from
    the deployed LiteLLM source: LiteLLM's own self-imposed 429 **always** carries a ``retry-after``
    header; a genuine upstream/provider overload (OpenRouter's ``engine_overloaded`` /
    ``limit_source: upstream_provider_shared_pool``) carries none — measured live,
    ``retry_after_s`` non-null 0 of 2,651 real overload calls.

    This is the shared building block for both the Part-1 ceiling-discovery probe
    (``swebench_eval.gateway.ceiling_discovery``) and the eventual §2 congestion-signal fix — one
    classifier, not two that could quietly disagree.
    """
    if status_code != 429:
        return False
    return retry_after_s is None


def gateway_base_url() -> str:
    """Resolve the base URL a harness should route through.

    Reads ``LITELLM_BASE_URL`` (defaults to the local gateway), so the worker's
    env is the single source of truth and the adapters need no per-adapter
    literals to change when the base moves to a shim.
    """
    return os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1")


def gateway_api_key() -> str:
    """Resolve the gateway/shim bearer token.

    run-launch (ADR-0035 decision 1): prefers the per-run virtual key
    (``LITELLM_API_KEY``, set via ``containerOverrides`` from
    ``JobReference.litellm_api_key`` — ``queue/schemas.py``) over the admin
    master key.  Falls back to ``LITELLM_MASTER_KEY`` when no per-run key was
    cached for the run (a run that predates run-launch, or a lost cache
    entry) — ``harness_dispatcher.py``'s ``ENFORCE_PER_RUN_KEY`` gate is what
    turns that fallback into a hard refusal instead, once enabled.

    This function is shared by the harness worker (wants the per-run key)
    AND the orchestrator's own admin calls in ``dispatcher.py``/
    ``run_launch.py`` (want the master key, to mint/rotate/delete other
    keys) — safe only because the orchestrator's task environment never
    carries ``LITELLM_API_KEY`` (only a harness-worker ``containerOverrides``
    sets it), so it always falls through to the master key there. If the
    orchestrator's own environment ever gains a ``LITELLM_API_KEY`` for some
    other reason, this function needs to split in two.
    """
    return os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY", "sk-local")


# E1 (agent-env-denylist-handover.md): the agent environment filter is a
# DENYLIST, not an allowlist.  An allowlist fails when an unknown-NEEDED variable
# is dropped (confusing breakage mid-run); a denylist fails when an
# unknown-DANGEROUS variable slips through.  We can enumerate the dangerous set
# (our task def) but not the needed set (SWE-bench's per-repo env images), so we
# deny the side we control.
#
# Why the agent must not have these: with the task-role credential URI (AWS_*)
# the agent could fetch task-role credentials and read the dataset mirror; with
# the queue URLs + SendMessage it could enqueue its own verdict; with
# ARTIFACTS_BUCKET it could read other attempts' artifacts.  LITELLM_BASE_URL is
# a second, UNMETERED route to the gateway that bypasses M0's sole meter.
#
# Denying these in the FILTER does NOT break the worker: agent_environment()
# builds a copy for the child subprocess and never mutates os.environ — the
# worker's own in-process clients (Redis, SQS, S3, metadata endpoint) are
# unaffected.  The repo_prep and git_utils subprocesses pass no env= and keep
# the full environment.
#
# The resolved key (the per-run key when run-launch minted one, else the
# master key — routing.gateway_api_key()) still reaches the five subprocess
# CLIs: each adapter re-adds it as ANTHROPIC_AUTH_TOKEN / OPENAI_API_KEY /
# LITELLM_API_KEY so the agent can authenticate to the shim — inherent until
# M3.  It IS removed from custom_minimal's bash tool, which has no adapter
# layer and calls the gateway directly from Python, never through a
# bash-spawned CLI.
_AGENT_ENV_DENY_PREFIXES = ("AWS_", "ECS_")

_AGENT_ENV_DENY = frozenset(
    {
        "LITELLM_MASTER_KEY",
        # run-launch: the raw per-run key, same discipline as the master key
        # above — stripped from the generic passthrough; each adapter still
        # gets it via its own explicit re-injection (self._api_key), never
        # by this var name leaking through untouched.
        "LITELLM_API_KEY",
        "LITELLM_BASE_URL",
        "REDIS_URL",
        "DATABASE_URL",
        "QUEUE_URL",
        "EVAL_QUEUE_URL",
        "RESULTS_QUEUE_URL",
        "SQS_QUEUE_PREFIX",
        # Adoption Phase 1a: the deployment's name prefix (swebench_eval.aws_names) —
        # same class as SQS_QUEUE_PREFIX, the agent has no business knowing it.
        "EVAL_ENV_PREFIX",
        "ARTIFACTS_BUCKET",
        "DATASET_BUCKET",
        # G1 (dev/HARNESS-ISOLATION-AUDIT-2026-09-05 §3): the git mirror is a
        # FULL-history --mirror of every repo — the gold fix is one `git log`
        # away for any agent that can reach it.  The pre-baked -inst never
        # clones at runtime, the task definition no longer sets this, and the
        # mirror's SG no longer admits the harness; the denylist entry is the
        # belt to those braces so the NAME cannot leak back through a stray env.
        "GIT_MIRROR_URL",
    }
)


# ---------------------------------------------------------------------------
# G2 (dev/HARNESS-ISOLATION-AUDIT-2026-09-05 §3): the agent runs as a NON-ROOT
# user.  The env denylist above strips the task credentials and keys from the
# agent's own environment, but a root agent simply reads /proc/1/environ (the
# worker's, PID 1) and has every one of them back — the whole artifacts bucket,
# sqs:SendMessage on the results queue (a self-reported verdict), the gateway
# key.  /proc/<pid>/environ is 0400 root-owned, so a uid-1000 agent cannot.
#
# The image creates user `agent` (Dockerfile.harness-worker-env); the worker
# stays root (it needs the task role) and drops privileges ONLY for the agent
# subprocess (proc.run_streaming / custom_minimal's shell) via Popen(user=).
# Before that it makes /testbed + the conda env + its scratch dir writable for
# the agent (harness_worker.grant_agent_access) — parity with the official
# harness, where the agent is root and pip-installs into the env at will.
#
# HARNESS_AGENT_USER=root (or an image without the user, e.g. a pre-G2 -inst,
# or a non-root dev shell) keeps the old behaviour, logged once — never a
# silent downgrade in the deployed image, which always has the user.
# ---------------------------------------------------------------------------
_AGENT_USER_DEFAULT = "agent"
_agent_user_warned = False


@dataclass(frozen=True)
class AgentUser:
    name: str
    uid: int
    gid: int
    home: str


def agent_user() -> AgentUser | None:
    """The unprivileged identity the agent subprocess runs as, or ``None``
    when privilege separation is off / impossible here (then the agent runs
    as the worker's own user, as before G2)."""
    global _agent_user_warned
    name = os.environ.get("HARNESS_AGENT_USER", _AGENT_USER_DEFAULT).strip()
    if not name or name.lower() in ("root", "off", "0"):
        return None
    if os.geteuid() != 0:
        return None  # cannot switch uid without root (local dev shell)
    try:
        import pwd

        pw = pwd.getpwnam(name)
    except (KeyError, ImportError):
        if not _agent_user_warned:
            logger.warning(
                "HARNESS_AGENT_USER=%s does not exist in this image; the agent runs as root "
                "(pre-G2 image?)",
                name,
            )
            _agent_user_warned = True
        return None
    return AgentUser(name=name, uid=pw.pw_uid, gid=pw.pw_gid, home=pw.pw_dir)


def agent_spawn_kwargs() -> dict[str, Any]:
    """Extra ``subprocess.Popen`` keyword arguments that drop the agent to
    :func:`agent_user` (empty when separation is off)."""
    user = agent_user()
    if user is None:
        return {}
    return {"user": user.uid, "group": user.gid, "extra_groups": []}


def grant_agent_ownership(path: Path) -> None:
    """G2: hand *path* — a file or a small directory tree the worker created as
    root for the agent (an adapter's config dir) — to :func:`agent_user`, so
    the demoted agent process can enter, read and write it.  No-op when
    separation is off or *path* does not exist.

    Found live 2026-09-06 (first opencode run under G2): the adapter's
    ``tempfile.TemporaryDirectory`` is mode 0700 root:root regardless of umask,
    so opencode (running as ``agent``) died with ``EACCES: mkdir
    '<cfg_dir>/opencode'`` on every instance before its first model call.
    The multi-gigabyte /testbed + conda chown stays in
    ``harness_worker.grant_agent_access`` (a subprocess ``chown -R``); this
    helper is for the handful of files an adapter writes itself.
    """
    user = agent_user()
    if user is None or not path.exists():
        return
    os.chown(path, user.uid, user.gid)
    if path.is_dir():
        for root, dirs, files in os.walk(path):
            for name in (*dirs, *files):
                os.chown(os.path.join(root, name), user.uid, user.gid)


def agent_environment() -> dict[str, str]:
    """Return the agent subprocess environment: the worker's env, minus the
    denylist.

    Denies the exact dangerous names + the AWS_*/ECS_* prefixes (ECS injects
    credential/metadata vars we do not author).  Everything else — including the
    testbed-shaped vars (CONDA_PREFIX, LD_LIBRARY_PATH, PYTHONPATH, PATH) the env
    images need — passes through untouched.  Adapters layer their own
    harness-specific vars (ANTHROPIC_*, OPENAI_*, XDG_CONFIG_HOME, ...) on top.

    G2: when the agent runs as the unprivileged user, HOME/USER/LOGNAME name
    THAT user (the worker's /root is 0700 — every CLI that writes under $HOME
    would fail on it), so the agent's own state lands in /home/agent.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _AGENT_ENV_DENY and not k.startswith(_AGENT_ENV_DENY_PREFIXES)
    }
    user = agent_user()
    if user is not None:
        env["HOME"] = user.home
        env["USER"] = user.name
        env["LOGNAME"] = user.name
    return env
