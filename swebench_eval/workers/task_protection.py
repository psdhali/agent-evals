"""ECS task scale-in protection for the eval worker (BUILDER4-EVAL-PACKING §6, question 2).

When the eval scaler lowers the service's desired count, ECS chooses which tasks to stop by
availability-zone balance — it has no idea which worker is mid-grade. Task scale-in
protection is the mechanism ECS provides for exactly this: a task that marks itself
protected is skipped by service scale-in (and by deployments) until it clears the flag or
the protection expires. The worker sets it when it picks up a grade and clears it when the
grade is done, so scale-in only ever stops IDLE workers.

The call goes to the ECS agent's task-local endpoint, ``$ECS_AGENT_URI/task-protection/v1/state``
(``ECS_AGENT_URI`` is injected into every container by the agent), with the task role's
``ecs:UpdateTaskProtection`` permission. No boto client, no task ARN lookup — the agent knows
which task it is talking to.

Failure posture: **best-effort, loud.** A failed call means this task can be picked for
scale-in like before, which costs one redelivered re-grade (minutes, no dollars) — never a
reason to fail the grade itself. Every failure is a WARNING with the agent's reply. Outside
ECS (local compose, unit tests) ``ECS_AGENT_URI`` is unset and the module is a logged no-op.

Expiry is a safety net, not the release mechanism: the worker refreshes protection from its
heartbeat thread while grading (``eval_worker._heartbeat``), so a worker that dies silently
leaves a protection that expires on its own instead of pinning a dead task forever.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_ENV = "ECS_AGENT_URI"
_PATH = "/task-protection/v1/state"
_TIMEOUT_S = 5.0
_warned_unavailable = False


def endpoint() -> str | None:
    """The agent's task-protection URL, or None outside ECS."""
    uri = os.environ.get(_ENV, "").strip().rstrip("/")
    return f"{uri}{_PATH}" if uri else None


def _http(method: str, url: str, body: dict[str, Any] | None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        # Agent-local URI handed to us by the ECS agent itself, never user input.
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")


def set_protection(enabled: bool, expires_minutes: int | None = None) -> bool:
    """Set or clear this task's scale-in protection. Returns True only when the agent
    confirms the requested state; False (with a WARNING) on any failure, and False (INFO,
    once) when not running under ECS."""
    global _warned_unavailable
    url = endpoint()
    if url is None:
        if not _warned_unavailable:
            logger.info("%s unset — task scale-in protection unavailable (not under ECS)", _ENV)
            _warned_unavailable = True
        return False
    body: dict[str, Any] = {"ProtectionEnabled": enabled}
    if enabled and expires_minutes is not None:
        body["ExpiresInMinutes"] = int(expires_minutes)
    try:
        status, text = _http("PUT", url, body)
    except Exception as exc:  # noqa: BLE001 — best-effort by design; the grade must not fail
        logger.warning("task protection %s failed: %s", "enable" if enabled else "release", exc)
        return False
    protection: dict[str, Any] = {}
    try:
        payload = json.loads(text) if text else {}
        protection = payload.get("protection") or {}
    except ValueError:
        payload = {}
    ok = status == 200 and bool(protection) and protection.get("ProtectionEnabled") == enabled
    if not ok:
        logger.warning(
            "task protection %s NOT confirmed (HTTP %s): %s",
            "enable" if enabled else "release",
            status,
            text[:300],
        )
    return ok
