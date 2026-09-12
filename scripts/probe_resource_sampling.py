#!/usr/bin/env python3
"""BUILDER3B eval resource instrumentation — real-daemon proof of the sampler.

dev/BUILDER3B-EVAL-RESOURCE-INSTRUMENTATION-2026-08-31.md "How to prove it
works before the run": *"Run one real grade locally and assert on the
artifact.  A unit test that stubs the docker client proves nothing here — it
would supply exactly the thing production has to produce."*

This script runs the sampler against a REAL container on the real docker
daemon, named with SWE-bench's exact grading-container scheme
(``sweb.eval.<instance_id>.<run_id>``, from ``test_spec.get_instance_container_name``)
and created WITHOUT mem_limit/nano_cpus (as SWE-bench creates it), so the
sampler watches a container it never created, exactly as ``grade()`` does.
The container allocates ~300MB of anonymous memory and burns CPU for ~6s.

Asserts the acceptance thresholds; exits non-zero on any failure:

* ``samples_taken > 0`` and ``status == "ok"``
* ``peak_rss_bytes > 100_000_000`` — a real workload does not run under 100MB
* ``cpu_total_seconds > 0`` and ``cpu_total_seconds / wall_clock`` plausible
  (0.5-8 cores — not 0.001, not 500)
* a deliberately mistyped container name comes back
  ``status="container_never_seen"``, NOT a zeroed "ok"
* no leak: ``threading.active_count()`` returns to baseline between runs

Not part of the pytest suite (it needs a real daemon + a pull); run it
explicitly.  The full acceptance (real SWE-bench grade -> the S3
``resource_usage.json`` artifact) is exercised on the AWS eval host during the
six-instance e2e.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

sys.path.insert(0, ".")

import docker

from swebench_eval.evaluation.resource_sampling import (
    ContainerResourceSampler,
)

INSTANCE_ID = "scikit-learn__scikit-learn-25102"
IMAGE = "python:3.12-alpine"

# Allocates ~300MB anonymous memory + burns CPU for ~6s.  `sum(range(100000))`
# is heavy enough that a single core is busy the whole time.
_WORKLOAD = (
    "import time\n"
    "b = bytearray(300 * 1024 * 1024)\n"
    "end = time.time() + 6\n"
    "while time.time() < end:\n"
    "    sum(range(100000))\n"
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(f"FAIL: {msg}")
    print(f"  ok: {msg}")


def _run_sampled(client: Any, container_name: str, baseline_threads: int) -> dict[str, Any]:
    """Start a sampler, run the workload container, stop, assert + report."""
    sampler = ContainerResourceSampler(client, container_name)
    sampler.start()
    started = time.monotonic()
    container = client.containers.run(
        IMAGE,
        command=["python3", "-c", _WORKLOAD],
        detach=True,
        name=container_name,
        # SWE-bench creates the grading container with no mem_limit and no
        # nano_cpus (docker_build.py:516) — replicate that exactly.
        mem_limit=None,
        nano_cpus=None,
    )
    try:
        container.wait(timeout=60)
    finally:
        wall_clock = time.monotonic() - started
        m = sampler.stop(timeout=5.0)
        container.remove(force=True)

    print(f"measurement: {m}")
    _assert(m.status == "ok", f"status == 'ok' (got {m.status!r})")
    _assert(m.samples_taken > 0, f"samples_taken > 0 (got {m.samples_taken})")
    _assert(
        m.peak_rss_bytes is not None and m.peak_rss_bytes > 100_000_000,
        f"peak_rss_bytes > 100MB (got {m.peak_rss_bytes})",
    )
    _assert(
        m.cpu_total_seconds is not None and m.cpu_total_seconds > 0,
        f"cpu_total_seconds > 0 (got {m.cpu_total_seconds})",
    )
    cores = m.cpu_total_seconds / wall_clock if m.cpu_total_seconds else 0.0
    _assert(
        0.5 <= cores <= 8,
        f"cpu_total_seconds/wall_clock plausible core count (got {cores:.2f})",
    )
    _assert(
        m.containers_running_peak is not None and m.containers_running_peak >= 1,
        f"containers_running_peak >= 1 (got {m.containers_running_peak})",
    )
    _assert(
        m.host_mem_total_bytes is not None and m.host_mem_total_bytes > 0,
        f"host_mem_total_bytes > 0 (got {m.host_mem_total_bytes})",
    )
    # No leak: the sampler thread must be gone (daemon + exited) and the
    # active-thread count back to baseline.
    time.sleep(0.2)
    _assert(
        threading.active_count() <= baseline_threads,
        f"thread count back to baseline ({threading.active_count()} <= {baseline_threads})",
    )
    return {
        "peak_rss_bytes": m.peak_rss_bytes,
        "cpu_total_seconds": m.cpu_total_seconds,
        "cores": cores,
        "samples_taken": m.samples_taken,
    }


def main() -> int:
    print(f"pulling {IMAGE} ...")
    client = docker.from_env()
    try:
        client.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        client.images.pull(IMAGE)

    baseline_threads = threading.active_count()
    print(f"baseline threads: {baseline_threads}")

    run_id = f"probe{int(time.time()) % 100000:05d}"

    print("\n[1] real container, exact SWE-bench naming scheme:")
    good = f"sweb.eval.{INSTANCE_ID}.{run_id}"
    _run_sampled(client, good, baseline_threads)

    print("\n[2] deliberately mistyped name -> container_never_seen (never a zeroed ok):")
    bad = f"sweb.eval.{INSTANCE_ID}.{run_id}-TYPO"
    sampler = ContainerResourceSampler(client, bad)
    sampler.start()
    time.sleep(2.0)
    m = sampler.stop(timeout=2.0)
    print(f"measurement: {m}")
    _assert(
        m.status == "container_never_seen", f"status == 'container_never_seen' (got {m.status!r})"
    )
    _assert(m.samples_taken == 0, f"samples_taken == 0 (got {m.samples_taken})")
    _assert(m.peak_rss_bytes is None, f"peak_rss_bytes is None (got {m.peak_rss_bytes})")

    print("\nALL REAL-DAEMON PROOF CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
