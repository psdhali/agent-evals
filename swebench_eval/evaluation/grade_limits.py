"""Resource limits on the grading container (EVAL-GRADE-RESOURCE-LIMITS §3.2).

SWE-bench creates the grading container with neither ``mem_limit`` nor ``nano_cpus``
(``swebench/harness/docker_build.py``: ``client.containers.create(image=..., name=...,
user=..., detach=True, command=..., platform=..., cap_add=...)``) and reads only ``cap_add``
from ``test_spec.docker_specs['run_args']`` — so the limit cannot be configured INTO
SWE-bench; the create call has to be intercepted.  docker-py ≥ 7 builds the ``HostConfig``
inline in ``ContainerCollection.create`` and hands it to ``APIClient.create_container`` —
that is the one seam every container created through this client passes, so the limit is
applied there, on the client the runner hands to ``run_instance``.

Why this exists now (BUILDER4-EVAL-PACKING-2026-09-03.md, owner decision B): the eval host
packs FOUR grades per c5d.2xlarge.  Unlimited, one runaway grade OOMs the host and takes the
other three grades plus their workers with it.  With a cgroup limit, the kernel kills inside
the grade's own cgroup, the daemon emits the ``oom`` container event the
:class:`~swebench_eval.evaluation.resource_sampling.ContainerOOMWatcher` is already listening
for, and the grade lands as ``EVAL_OOM_KILLED`` — a voided, regradable outcome, never a host
loss and never a silent ``unresolved``.

The default (3072 MiB) and its provenance: 31 sampled grades across 6 instances on
c5d.2xlarge (2026-09-02/03, ``resource_usage.json``): peak RSS median 248 MB, max 1,789 MB
(scikit-learn-25102, all five runs within 1,785–1,809 MB).  Stats frames are ~1 s apart, so a
sampled peak understates the true peak; ×1.6 headroom on the max → 2.9 GB → 3 GiB.  At four
per host: 4 × 3 GiB = 12 GiB of the host's 15.2 GiB, leaving ~3 GiB for four workers, the
daemon and the OS even if every grade peaks at its cap at once.  Swap is pinned to the same
value (no swap on Bottlerocket anyway) so a grade at its limit is killed, not slowed into the
test timeout — a too-tight cap that thrashes fails slow and misattributes as a timeout.

CPU is deliberately NOT capped by default.  The measured profile is 0.2–1.2 cores on average
with one brief 8-core spike (scikit-25102), and the kernel's CFS already shares the host
proportionally between containers at equal weight — a hard ``NanoCpus`` cap would only
lengthen the spike phases.  ``EVAL_GRADE_CPUS`` is there for the operator if a cap is ever
wanted; unset means none.

Both knobs are task-definition environment, changeable without an image rebuild.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 3 GiB — provenance in the module docstring.
DEFAULT_GRADE_MEM_LIMIT_MB = 3072


class GradeLimitError(RuntimeError):
    """The limit could not be applied — the grade must not run unlimited on a packed host."""


@dataclass(frozen=True)
class GradeLimits:
    """The limits to apply. ``mem_limit_bytes`` 0 = no memory limit (operator opt-out)."""

    mem_limit_bytes: int
    nano_cpus: int | None  # None = no CPU cap

    @property
    def enabled(self) -> bool:
        return self.mem_limit_bytes > 0 or self.nano_cpus is not None

    def describe(self) -> str:
        mem = f"{self.mem_limit_bytes // 2**20} MiB" if self.mem_limit_bytes else "none"
        cpu = f"{self.nano_cpus / 1e9:g} cpus" if self.nano_cpus else "none"
        return f"mem_limit={mem} cpu_limit={cpu}"


def limits_from_env(environ: dict[str, str] | None = None) -> GradeLimits:
    """Read ``EVAL_GRADE_MEM_LIMIT_MB`` (default 3072; ``0`` disables) and
    ``EVAL_GRADE_CPUS`` (unset/empty = no cap).  A malformed value raises — a limit that
    silently fails to parse is a limit that silently fails to apply."""
    env = os.environ if environ is None else environ
    raw_mem = env.get("EVAL_GRADE_MEM_LIMIT_MB", "").strip()
    try:
        mem_mb = int(raw_mem) if raw_mem else DEFAULT_GRADE_MEM_LIMIT_MB
    except ValueError as exc:
        raise GradeLimitError(f"EVAL_GRADE_MEM_LIMIT_MB={raw_mem!r} is not an integer") from exc
    if mem_mb < 0:
        raise GradeLimitError(f"EVAL_GRADE_MEM_LIMIT_MB={mem_mb} must be >= 0 (0 disables)")
    raw_cpus = env.get("EVAL_GRADE_CPUS", "").strip()
    nano_cpus: int | None = None
    if raw_cpus:
        try:
            cpus = float(raw_cpus)
        except ValueError as exc:
            raise GradeLimitError(f"EVAL_GRADE_CPUS={raw_cpus!r} is not a number") from exc
        if cpus <= 0:
            raise GradeLimitError(f"EVAL_GRADE_CPUS={cpus} must be > 0 (unset for no cap)")
        nano_cpus = int(cpus * 1e9)
    return GradeLimits(mem_limit_bytes=mem_mb * 2**20, nano_cpus=nano_cpus)


def apply_grade_limits(client: Any, limits: GradeLimits | None = None) -> GradeLimits:
    """Wrap ``client.api.create_container`` so every container created through *client*
    carries the limits in its ``HostConfig``.  Keys already present win (SWE-bench sets
    none today; if it ever does, its own value is the more specific one).  Returns the
    limits applied; raises :class:`GradeLimitError` if the client has no ``api`` seam,
    because on a packed host an unlimited grade is the host-loss case this exists to close.
    """
    limits = limits_from_env() if limits is None else limits
    if not limits.enabled:
        logger.warning("grade resource limits DISABLED by env (EVAL_GRADE_MEM_LIMIT_MB=0)")
        return limits
    api = getattr(client, "api", None)
    original = getattr(api, "create_container", None)
    if api is None or original is None:
        raise GradeLimitError(
            "cannot apply grade resource limits: docker client has no api.create_container"
        )
    if getattr(original, "_grade_limits_applied", False):
        return limits  # idempotent: one wrap per client

    def create_container(*args: Any, **kwargs: Any) -> Any:
        host_config = kwargs.get("host_config")
        if host_config is None:
            host_config = api.create_host_config()
            kwargs["host_config"] = host_config
        if limits.mem_limit_bytes:
            host_config.setdefault("Memory", limits.mem_limit_bytes)
            host_config.setdefault("MemorySwap", limits.mem_limit_bytes)
        if limits.nano_cpus is not None:
            host_config.setdefault("NanoCpus", limits.nano_cpus)
        return original(*args, **kwargs)

    create_container._grade_limits_applied = True  # type: ignore[attr-defined]
    api.create_container = create_container
    logger.info("grade resource limits applied to the docker client: %s", limits.describe())
    return limits
