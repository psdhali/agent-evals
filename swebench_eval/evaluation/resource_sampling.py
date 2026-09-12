"""Per-grade resource sampling for the eval (DinD grading) container.

``dev/BUILDER3B-EVAL-RESOURCE-INSTRUMENTATION-2026-08-31.md``: we need to size
the eval host before the 500-instance runs. SWE-bench's guidance is a
whole-machine average ("at least 120GB free storage, 16GB RAM, 8 CPU cores",
``max_workers <= min(0.75 * os.cpu_count(), 24)``) — an average for their
architecture (threads in one process), not ours (one ECS task per grade, each
reserving 2 GiB *on top of* its grading container). Nothing is published
per-instance or per-split, so the only measurement route that is currently
open is instrumenting the grade itself.

The grading container is created by ``run_instance`` with **no ``mem_limit``
and no ``nano_cpus``** (``swebench/harness/docker_build.py``) — a sibling on
the host daemon, invisible to ECS accounting. One heavy grade therefore takes
the whole host and every grade packed onto it. This module watches the
container **from outside** (nothing is added into the thing being measured),
recording per-grade peak RSS, cumulative CPU-seconds, the observed packing
factor (``containers_running``), and the host's total memory so the numbers
are interpretable without knowing which host type ran it.

Leak discipline is mandatory: the eval worker is a long-lived process that
handles every job in the run sequentially, so anything per-job that leaks a
thread, a socket, or an unclosed generator accumulates 500x over a full run
— and a leak would NOT show up in the six-instance e2e. The thread is
``daemon=True``, the stats stream generator is closed in a ``finally`` on
every path, and ``stop()`` uses a bounded join.

Every failure mode is bounded: a sampler must NEVER fail a grade. Any
exception in the thread body logs, sets ``status="error"``, and grading
continues.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# How long the sampler waits between ``containers.get`` retries while the
# grading container does not exist yet (it is created inside run_instance,
# after the sampler starts).
_POLL_SECONDS = 0.5


@dataclass(frozen=True)
class ResourceMeasurement:
    """One grade's observed resource usage (sizing probe, not a product field).

    Carried on :class:`GradingOutput` and serialised by the eval worker to
    ``runs/<run_id>/eval/<instance_id>/<attempt>/resource_usage.json`` beside
    the report.  **No schema change, no ResultMessage field** — a sizing
    probe, not a product feature (BUILDER3B, "Where the numbers go").

    ``status`` is explicit because *unknown must never render as healthy*: a
    sampler that never finds the container (wrong name, or the container
    exited before the first poll) keeps ``samples_taken == 0`` and reports
    ``"container_never_seen"`` — NOT a zeroed ``"ok"``.  The aggregation
    script must refuse to emit a sizing recommendation from any record with
    ``samples_taken == 0``.
    """

    # "ok" | "container_never_seen" | "error"
    status: str = "container_never_seen"
    samples_taken: int = 0
    # Max over the run of (memory_stats.usage - memory_stats.stats.inactive_file).
    # cgroup v2 ``usage`` includes page cache; the raw number overstates real
    # demand, sometimes by GBs.
    peak_rss_bytes: int | None = None
    # Cumulative ``cpu_stats.cpu_usage.total_usage`` / 1e9 (seconds), read
    # once — no delta maths — and tracked as the MAX reading, because the
    # daemon zeroes cpu_stats on the frames it emits after the workload exits
    # (observed live against Docker Desktop), so "the last reading" would
    # record 0.  This is the CPU number that matters: a grade needs roughly
    # the same CPU-seconds whether it gets them fast or slow, so it is a
    # sizing constant, unlike peak_cpu_pct (soft/elastic).
    cpu_total_seconds: float | None = None
    # Max over the run of the per-frame CPU% (delta over system delta,
    # scaled by online_cpus).  Secondary — useful for spotting a
    # single-threaded stall, not for sizing.
    peak_cpu_pct: float | None = None
    # How many cores the container could see when it ran (last frame).
    online_cpus: int | None = None
    # Max over the run of host ``ContainersRunning`` — the packing factor the
    # grade was observed at.  Peak RSS measured at concurrency 1 tells us
    # nothing about whether it degrades under packing; this is what turns six
    # measurements into a sizing rule.
    containers_running_peak: int | None = None
    # Host total memory (bytes) at the last frame, so peak_rss_bytes is
    # interpretable without knowing which host type ran it.
    host_mem_total_bytes: int | None = None
    # The instance image's on-disk size (bytes) — the disk-per-grade figure
    # upstream's "120GB free storage" is a proxy for.  Read by the runner
    # BEFORE run_instance (rm_image removes the image in its finally); None
    # when the image is not local yet (the Docker Hub pull path builds it
    # inside run_instance).  Never fabricated.
    image_size_bytes: int | None = None
    # EVAL-GRADE-RESOURCE-LIMITS (2026-09-01) §3.3: the kernel OOM-killed a
    # process inside this grade's container (docker ``oom`` event, observed by
    # :class:`ContainerOOMWatcher`, stamped in by the runner).  Recorded here
    # so the sizing aggregation can tell "peaked at X and survived" apart from
    # "peaked at X and was killed" — the latter's peak is a floor, not a peak.
    oom_killed: bool = False


def _oom_watch_enabled() -> bool:
    """Kill switch for the OOM-event watcher (task-def env, default ON).

    Deliberately SEPARATE from ``EVAL_RESOURCE_SAMPLING``: the sampler is a
    sizing probe, the OOM watcher is a grade-integrity check (an OOM-killed
    grade must never land as a silent ``unresolved`` — EVAL-GRADE-RESOURCE-
    LIMITS §3.3).  Turning the probe off must not also turn off correctness.
    """
    raw = os.environ.get("EVAL_OOM_WATCH", "1")
    return raw.strip() not in ("", "0", "false", "off", "no")


def _resource_sampling_enabled() -> bool:
    """Kill switch, read from the task definition (BUILDER3B §"Kill switch").

    Default ON — the env var only exists so the sampler can be disabled
    mid-run without an image rebuild (same reasoning the terraform already
    records for the OMP vars: "Task-def env, no -hw rebuild").
    """
    raw = os.environ.get("EVAL_RESOURCE_SAMPLING", "1")
    return raw.strip() not in ("", "0", "false", "off", "no")


def _extract_sample(sample: dict[str, Any]) -> dict[str, Any]:
    """Pull the per-frame fields out of one ``stats(stream=True)`` dict.

    Kept pure and module-level so the field paths + delta maths are unit
    testable without a docker daemon.  Returns only the fields that parse;
    every value can be None (never a fabricated 0).
    """
    cpu = sample.get("cpu_stats", {}) or {}
    precpu = sample.get("precpu_stats", {}) or {}
    mem = sample.get("memory_stats", {}) or {}
    cpu_usage = cpu.get("cpu_usage", {}) or {}
    precpu_usage = precpu.get("cpu_usage", {}) or {}

    total_ns = cpu_usage.get("total_usage")
    cpu_total_seconds = None
    if isinstance(total_ns, (int, float)) and total_ns >= 0:
        cpu_total_seconds = total_ns / 1e9

    # peak CPU% over the frame: delta of total_usage vs precpu, over the
    # system_cpu_usage delta, scaled by online_cpus.  A zeroed precpu (the
    # first frame) has nothing to delta against — skip, never emit garbage.
    peak_cpu_pct = None
    prev_total = precpu_usage.get("total_usage")
    prev_sys = precpu.get("system_cpu_usage")
    sys_total = cpu.get("system_cpu_usage")
    if (
        isinstance(total_ns, (int, float))
        and isinstance(prev_total, (int, float))
        and total_ns >= prev_total
        and isinstance(prev_sys, (int, float))
        and isinstance(sys_total, (int, float))
        and sys_total > prev_sys
    ):
        online = cpu.get("online_cpus")
        if not isinstance(online, int) or online <= 0:
            percpu = cpu_usage.get("percpu_usage", [])
            online = len(percpu) if percpu else 1
        delta_total = total_ns - prev_total
        delta_system = sys_total - prev_sys
        peak_cpu_pct = (delta_total / delta_system) * online * 100.0

    usage = mem.get("usage")
    rss = None
    if isinstance(usage, (int, float)):
        # cgroup v2: usage includes page cache; subtract the reclaimable file
        # cache so we measure demand, not disk-as-RAM.
        inactive_file = (mem.get("stats") or {}).get("inactive_file") or 0
        rss = max(0, int(usage) - int(inactive_file))

    online_cpus = cpu.get("online_cpus")
    if not isinstance(online_cpus, int):
        online_cpus = None

    return {
        "rss_bytes": rss,
        "cpu_total_seconds": cpu_total_seconds,
        "cpu_pct": peak_cpu_pct,
        "online_cpus": online_cpus,
    }


class ContainerResourceSampler:
    """Watch a single DinD grading container's resources while it runs.

    The container is named ``test_spec.get_instance_container_name(run_id)``
    = ``sweb.eval.<instance_id>.<run_id>``, and the runner generates
    ``run_id`` itself before calling ``run_instance`` — so we can watch a
    container we never create.  The container may not exist yet when the
    sampler starts; ``_wait_for_container`` polls until it appears.

    Mirrors the eval worker's own SQS-heartbeat thread shape (daemon thread +
    stop event + bounded join) rather than inventing a second one.
    """

    def __init__(self, client: Any, container_name: str) -> None:
        self._client = client
        self._container_name = container_name
        self._stop = threading.Event()
        # Running state, mutated only by the sampler thread under the lock.
        # ResourceMeasurement is frozen (an immutable value); these plain
        # attributes are the mutable working set, materialised into a fresh
        # ResourceMeasurement by snapshot()/stop() under the same lock.
        self._status = "container_never_seen"
        self._samples_taken = 0
        self._peak_rss_bytes: int | None = None
        self._cpu_total_seconds: float | None = None
        self._peak_cpu_pct: float | None = None
        self._online_cpus: int | None = None
        self._containers_running_peak: int | None = None
        self._host_mem_total_bytes: int | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"eval-resource-sampler-{self._container_name[:24]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> ResourceMeasurement:
        """Stop the sampler and return the measurement.  Never raises.

        Bounded join only — never an unbounded join, which would add the
        grade time the instrumentation claims not to add.  If the thread is
        mid-``next(stream)`` it may outlive the join by a moment; it is
        daemon and its stream is closed when the grading container is removed,
        so nothing accumulates.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        return self.snapshot()

    def snapshot(self) -> ResourceMeasurement:
        """Current measurement without stopping the sampler (tests / health)."""
        with self._lock:
            return ResourceMeasurement(
                status=self._status,
                samples_taken=self._samples_taken,
                peak_rss_bytes=self._peak_rss_bytes,
                cpu_total_seconds=self._cpu_total_seconds,
                peak_cpu_pct=self._peak_cpu_pct,
                online_cpus=self._online_cpus,
                containers_running_peak=self._containers_running_peak,
                host_mem_total_bytes=self._host_mem_total_bytes,
            )

    # -- thread body ------------------------------------------------------

    def _run(self) -> None:
        try:
            container = self._wait_for_container()
            if container is None:
                # Stop arrived (or a fatal read error already set "error")
                # before the container appeared — status is whatever was set.
                return
            stream = container.stats(stream=True, decode=True)
            try:
                for sample in stream:
                    self._ingest(sample)
                    if self._stop.is_set():
                        break
            finally:
                stream.close()
        except Exception:
            # A sampler must never fail a grade: log, record error, stop.
            logger.exception("resource sampler failed for %s", self._container_name)
            self._set_status("error")

    def _wait_for_container(self) -> Any:
        """Poll ``containers.get`` until the grading container exists.

        NotFound is the normal "not created yet" state — keep waiting.  Any
        other exception propagates to the thread body's handler (status=error)
        rather than being mistaken for "container_never_seen".
        """
        import docker

        while not self._stop.is_set():
            try:
                return self._client.containers.get(self._container_name)
            except docker.errors.NotFound:
                time.sleep(_POLL_SECONDS)
        return None

    def _ingest(self, sample: dict[str, Any]) -> None:
        """Fold one stats frame into the running measurement (never raises).

        A single malformed frame must not lose the whole measurement — parse
        defensively, log, and keep going.
        """
        try:
            extracted = _extract_sample(sample)
            try:
                host = self._client.info()
            except Exception:
                logger.warning(
                    "resource sampler: client.info() failed (host fields skipped)",
                    exc_info=True,
                )
                host = {}
            containers_running = host.get("ContainersRunning")
            host_mem = host.get("MemTotal")

            with self._lock:
                rss = extracted["rss_bytes"]
                if rss is not None and (self._peak_rss_bytes is None or rss > self._peak_rss_bytes):
                    self._peak_rss_bytes = rss
                # Cumulative counter — read once, no delta maths.  Track the
                # MAX: the daemon zeroes cpu_stats on the frames it emits after
                # the workload exits (observed live), so "the last reading"
                # would record 0.  A cumulative counter never legitimately
                # decreases, so max is the true final figure.
                cpu_total = extracted["cpu_total_seconds"]
                if cpu_total is not None and (
                    self._cpu_total_seconds is None or cpu_total > self._cpu_total_seconds
                ):
                    self._cpu_total_seconds = cpu_total
                pct = extracted["cpu_pct"]
                if pct is not None and (self._peak_cpu_pct is None or pct > self._peak_cpu_pct):
                    self._peak_cpu_pct = pct
                if extracted["online_cpus"] is not None and (
                    self._online_cpus is None or extracted["online_cpus"] > self._online_cpus
                ):
                    self._online_cpus = extracted["online_cpus"]
                if isinstance(containers_running, int) and (
                    self._containers_running_peak is None
                    or containers_running > self._containers_running_peak
                ):
                    self._containers_running_peak = containers_running
                if isinstance(host_mem, int) and host_mem > 0:
                    self._host_mem_total_bytes = host_mem
                self._samples_taken += 1
                if self._status != "ok":
                    self._status = "ok"
        except Exception:
            # Defensive: a frame that fails to fold must not kill the stream.
            logger.exception("resource sampler: could not fold stats frame")

    def _set_status(self, status: str) -> None:
        with self._lock:
            self._status = status


class ContainerOOMWatcher:
    """Watch the docker event stream for ``oom`` events on one container.

    EVAL-GRADE-RESOURCE-LIMITS (2026-09-01) §3.3: SWE-bench's ``run_instance``
    never reads the eval exec's exit code, and the eval script's test command
    is not its last command (a ``git checkout`` follows it), so a pytest
    OOM-killed mid-run is exit-code-masked AND still echoes the END marker —
    the truncated log then grades as a clean-looking ``unresolved``.  The only
    authoritative signal is the daemon's own ``oom`` container event (emitted
    from the cgroup's OOM notification, regardless of whether PID 1 — ``tail
    -f /dev/null`` — was the victim).  Watched from OUTSIDE, like the sampler:
    nothing is added into the thing being measured.

    Same leak discipline as :class:`ContainerResourceSampler` (this is the
    eval worker, one long-lived process, 500 grades per run): daemon thread,
    the events stream is closed on every path, ``stop()`` uses a bounded join,
    and a watcher must NEVER fail a grade — any exception logs and stops.

    Scope caveat, recorded honestly: a *cgroup-limit* OOM (once ``mem_limit``
    is set) always emits this event.  A *host-pressure* OOM kill emits it on
    cgroup v2 hosts; on cgroup v1 the daemon's notification only fires for the
    container's own limit, so an unlimited container's host-OOM kill can pass
    unseen here — the runner's END-marker truncation check is the belt for the
    bash-killed shape, and the worker-killed shape surfaces as an SQS retry.
    """

    def __init__(self, client: Any, container_name: str) -> None:
        self._client = client
        self._container_name = container_name
        self._lock = threading.Lock()
        self._oom_events = 0
        self._stream: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"eval-oom-watcher-{self._container_name[:24]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> int:
        """Stop watching and return the number of ``oom`` events observed.

        Closes the events stream first (docker-py's stream supports a
        cross-thread ``close()``), then a bounded join — never unbounded.
        """
        with self._lock:
            stream = self._stream
        if stream is not None:
            try:
                stream.close()
            except Exception:
                logger.debug("oom watcher: events stream close raised", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout)
        with self._lock:
            return self._oom_events

    def oom_event_count(self) -> int:
        with self._lock:
            return self._oom_events

    def _run(self) -> None:
        try:
            stream = self._client.events(
                decode=True,
                filters={
                    "type": "container",
                    "event": "oom",
                    "container": self._container_name,
                },
            )
            with self._lock:
                self._stream = stream
            try:
                for event in stream:
                    # The server-side filter already narrows to this
                    # container's oom events; re-check the action defensively
                    # (older daemons have looser filter behaviour) and never
                    # count a frame we can't positively identify.
                    action = event.get("Action") or event.get("status")
                    if action == "oom":
                        with self._lock:
                            self._oom_events += 1
                        logger.error(
                            "docker oom event observed for grading container %s",
                            self._container_name,
                        )
            finally:
                stream.close()
        except Exception:
            # A watcher must never fail a grade — and a dead watcher must not
            # masquerade as "no OOM": it reports whatever it saw before dying.
            logger.exception("oom watcher failed for %s", self._container_name)
