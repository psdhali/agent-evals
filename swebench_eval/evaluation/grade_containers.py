"""Ownership of the grading container once the worker is gone (eval scaling review F1).

The grading container is a sibling on the HOST daemon (the worker bind-mounts
``/var/run/docker.sock``), with its own lifecycle. SWE-bench's only cleanup is a ``finally``
in ``run_instance`` — which never runs when the worker is SIGKILLed at the end of ECS's 30 s
stop grace, and a grade takes minutes. Every other mechanism (the cgroup cap, the reservation,
scale-in protection) manages the container *while the worker is alive*; nothing owned it
afterwards. On a host packed four grades deep, an orphan still running at its 3 GiB cap is the
host-OOM case the cap was built to prevent, re-entered through a different door.

Two closures, both best-effort and loud:

- :func:`remove_current` — the worker registers the name of the container it is grading in
  (:func:`set_current`, from the runner) and force-removes it on SIGTERM, before the SIGKILL.
- :func:`sweep_orphans` — any ``sweb.eval.*`` container on the host older than
  :data:`ORPHAN_AGE_S` with NO running exec is force-removed. Age alone was not enough
  (2026-09-06: a neighbour worker swept a live 67-minute grade); a container whose
  ``/eval.sh`` exec is still running belongs to someone's grade, whatever its age, and is
  skipped. Run at worker start and periodically between grades, so a recycled host heals
  itself whichever worker on it is next to look — the crash path included.

Never raises: an orphan we failed to reap is logged; the grade in hand is never affected.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

ORPHAN_AGE_S = 60 * 60  # exec-less containers only: a live grade has a running exec (see sweep)
_NAME_PREFIX = "sweb.eval."
_lock = threading.Lock()
_current: str | None = None


def set_current(name: str | None) -> None:
    """Record (or clear) the grading container this worker is running."""
    global _current
    with _lock:
        _current = name


def current() -> str | None:
    with _lock:
        return _current


def _client(client: Any) -> Any:
    if client is not None:
        return client
    import docker

    return docker.from_env()


def remove_current(client: Any = None) -> bool:
    """Force-remove the registered grading container. True when it was removed (or was
    already gone); False when the removal failed or nothing was registered."""
    name = current()
    if not name:
        return False
    try:
        c = _client(client)
        try:
            container = c.containers.get(name)
        except Exception:  # noqa: BLE001 — not found = already gone, which is the goal
            logger.info("grading container %s already gone", name)
            set_current(None)
            return True
        container.remove(force=True)
        logger.warning("removed grading container %s (worker stopping mid-grade)", name)
        set_current(None)
        return True
    except Exception:
        logger.warning("could not remove grading container %s", name, exc_info=True)
        return False


_FRACTION = re.compile(r"(\.\d{1,6})\d*")


def _parse_created(raw: str) -> float | None:
    """Docker's ``Created`` is RFC 3339 with nanoseconds ("2026-09-03T10:00:00.123456789Z");
    Python's fromisoformat takes at most six fractional digits."""
    try:
        s = raw.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        s = _FRACTION.sub(r"\1", s, count=1)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def sweep_orphans(
    client: Any = None, *, older_than_s: float = ORPHAN_AGE_S, now: float | None = None
) -> list[str]:
    """Force-remove ``sweb.eval.*`` containers on the host older than *older_than_s*.
    Returns the names removed. Never raises."""
    removed: list[str] = []
    try:
        c = _client(client)
        containers = c.containers.list(all=True, filters={"name": _NAME_PREFIX})
    except Exception:
        logger.warning("orphan sweep: could not list containers", exc_info=True)
        return removed
    now = time.time() if now is None else now
    mine = current()
    for container in containers:
        name = str(getattr(container, "name", "") or "")
        if not name.startswith(_NAME_PREFIX):
            continue  # the daemon's name filter is a substring match; be exact
        if name == mine:
            continue  # this worker's own grade, whatever its age
        attrs = getattr(container, "attrs", {}) or {}
        created = _parse_created(str(attrs.get("Created", "")))
        if created is None or now - created < older_than_s:
            continue
        # 2026-09-06 (run b4338f67, django-10097): a NEIGHBOUR worker on the same
        # host swept a live 67-minute grade at the 60-minute mark — `current()`
        # only knows THIS worker's container.  A container with a running exec
        # (SWE-bench's `/eval.sh` session; docker keeps the id in ExecIDs until
        # it exits) is being graded by someone: skip it.  A true orphan — its
        # worker SIGKILLed — keeps its exec only until the suite ends, so it is
        # still reaped, one suite later.
        if attrs.get("ExecIDs"):
            logger.info(
                "orphan sweep: %s is %.0f min old but has a running exec — a live grade, skipped",
                name,
                (now - created) / 60,
            )
            continue
        try:
            container.remove(force=True)
            removed.append(name)
            logger.warning(
                "orphan sweep: removed grading container %s (age %.0f min, no worker owns it)",
                name,
                (now - created) / 60,
            )
        except Exception:
            logger.warning("orphan sweep: could not remove %s", name, exc_info=True)
    return removed
