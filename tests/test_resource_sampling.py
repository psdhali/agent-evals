"""BUILDER3B eval resource instrumentation — sampler + artifact wiring.

dev/BUILDER3B-EVAL-RESOURCE-INSTRUMENTATION-2026-08-31.md.  The unit surface:

* ``_extract_sample`` field paths + delta maths (cgroup v2 semantics: RSS is
  ``memory_stats.usage`` minus ``inactive_file``).
* status honesty — a sampler that never finds the container must report
  ``container_never_seen`` with ``samples_taken == 0``, never a zeroed "ok".
* leak discipline — the stats generator is closed, ``stop()`` is bounded, the
  thread is daemon.
* the kill switch (``EVAL_RESOURCE_SAMPLING``, default on).
* ``GradingOutput.resource_measurement`` is additive (no caller breaks) and
  the eval worker writes ``resource_usage.json`` BESIDE the report — with NO
  ResultMessage field (deliberately; see the doc's "Where the numbers go").

A full real grade against the docker daemon is the acceptance's second half
(``samples_taken > 0`` / ``status == "ok"`` / ``peak_rss_bytes > 100MB``);
that is exercised by ``scripts/probe_resource_sampling.py`` and on the AWS
eval host during the six-instance e2e — a stub is not proof, and the doc says
so.  These tests prove the parsing/state machine that a real grade then feeds.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import docker
import pytest

from swebench_eval.evaluation.grading_adapter import GradingOutput
from swebench_eval.evaluation.resource_sampling import (
    ContainerResourceSampler,
    ResourceMeasurement,
    _extract_sample,
    _resource_sampling_enabled,
)
from swebench_eval.queue.schemas import EvalJob

# A realistic cgroup v2 ``stats(stream=True)`` frame.
FRAME1: dict[str, Any] = {
    "read": "2026-08-31T00:00:00.000000000Z",
    "cpu_stats": {
        "cpu_usage": {
            "total_usage": 5_000_000_000,
            "percpu_usage": [2_000_000_000, 3_000_000_000],
        },
        "system_cpu_usage": 100_000_000_000,
        "online_cpus": 2,
    },
    "precpu_stats": {
        "cpu_usage": {"total_usage": 4_000_000_000},
        "system_cpu_usage": 80_000_000_000,
    },
    "memory_stats": {"usage": 500_000_000, "stats": {"inactive_file": 50_000_000}},
}

# A heavier second frame (peaks must come from here).
FRAME2: dict[str, Any] = {
    "read": "2026-08-31T00:00:02.000000000Z",
    "cpu_stats": {
        "cpu_usage": {"total_usage": 12_000_000_000},
        "system_cpu_usage": 130_000_000_000,
        "online_cpus": 2,
    },
    "precpu_stats": {
        "cpu_usage": {"total_usage": 5_000_000_000},
        "system_cpu_usage": 100_000_000_000,
    },
    "memory_stats": {"usage": 700_000_000, "stats": {"inactive_file": 40_000_000}},
}


def _wait_until(predicate: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# _extract_sample — field paths + delta maths
# ---------------------------------------------------------------------------


def test_extract_sample_realistic_cgroup_v2_frame() -> None:
    e = _extract_sample(FRAME1)
    assert e["rss_bytes"] == 450_000_000  # usage 500MB minus inactive_file 50MB
    assert e["cpu_total_seconds"] == pytest.approx(5.0)  # 5e9 ns
    assert e["cpu_pct"] == pytest.approx(10.0)  # 1e9/20e9 * 2 cores * 100
    assert e["online_cpus"] == 2


def test_extract_sample_no_system_delta_yields_none_pct() -> None:
    """precpu == cpu means there is NO system-time basis for a pct — None
    (not measured), never a fabricated number.  Next frame with a real delta
    sets the value; peak tracking skips None."""
    frame = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 5_000_000_000},
            "system_cpu_usage": 100_000_000_000,
            "online_cpus": 2,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 5_000_000_000},
            "system_cpu_usage": 100_000_000_000,
        },
        "memory_stats": {"usage": 100_000_000, "stats": {"inactive_file": 0}},
    }
    e = _extract_sample(frame)
    assert e["cpu_pct"] is None
    assert e["cpu_total_seconds"] == pytest.approx(5.0)  # cumulative survives


def test_extract_sample_zeroed_precpu_is_finite_not_a_crash() -> None:
    """First streaming frame: precpu is all zeros — no delta basis, but the
    cumulative total is still a real reading.  Must not raise, must not emit
    NaN/inf, and cpu_total_seconds must survive."""
    frame = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 5_000_000_000},
            "system_cpu_usage": 100_000_000_000,
            "online_cpus": 2,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 0},
            "system_cpu_usage": 0,
        },
        "memory_stats": {"usage": 100_000_000, "stats": {"inactive_file": 0}},
    }
    e = _extract_sample(frame)
    assert e["cpu_total_seconds"] == pytest.approx(5.0)
    assert e["cpu_pct"] is not None
    assert e["cpu_pct"] == e["cpu_pct"]  # not NaN
    assert e["cpu_pct"] < 1e9  # not garbage-magnitude


def test_extract_sample_missing_fields_are_none_not_fabricated() -> None:
    e = _extract_sample({})
    assert e == {
        "rss_bytes": None,
        "cpu_total_seconds": None,
        "cpu_pct": None,
        "online_cpus": None,
    }


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_resource_sampling_enabled_defaults_on() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("EVAL_RESOURCE_SAMPLING", raising=False)
        assert _resource_sampling_enabled() is True


def test_resource_sampling_enabled_off_values() -> None:
    for off in ("0", "false", "off", "no"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("EVAL_RESOURCE_SAMPLING", off)
            assert _resource_sampling_enabled() is False
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("EVAL_RESOURCE_SAMPLING", "1")
        assert _resource_sampling_enabled() is True


# ---------------------------------------------------------------------------
# Sampler thread against a stubbed docker client
# ---------------------------------------------------------------------------


class _StubClient:
    """Minimal docker-client stand-in: a container factory + host info.

    ``containers.get`` delegates to the factory so tests can raise
    NotFound (never seen) or return a stub container (seen).
    """

    def __init__(self, container_factory: Any, info: dict[str, Any] | None = None) -> None:
        self._factory = container_factory
        self._info = info or {"ContainersRunning": 2, "MemTotal": 8_589_934_592}

    @property
    def containers(self) -> _StubClient:
        return self

    def get(self, name: str) -> Any:
        return self._factory(name)

    def info(self) -> dict[str, Any]:
        return self._info


def _stub_container(*frames: Any) -> Any:
    """A stub container whose stats stream yields *frames* then ends naturally
    (like a container removed mid-grade) — so the sampler's ``finally`` runs
    ``stream.close()`` and the thread exits on its own."""

    class _Stream:
        def __init__(self) -> None:
            self._frames = iter(frames)
            self._closed = False

        def close(self) -> None:
            self._closed = True

        def __iter__(self) -> _Stream:
            return self

        def __next__(self) -> Any:
            return next(self._frames)  # StopIteration propagates naturally

    class _C:
        def __init__(self) -> None:
            self._stream: _Stream | None = None

        def stats(self, stream: bool = True, decode: bool = False) -> Any:
            self._stream = _Stream()
            return self._stream

    return _C()


def _never_found(name: str) -> Any:
    raise docker.errors.NotFound("container not found")


def test_sampler_container_never_seen_reports_honestly() -> None:
    """A sampler that never finds the container must NOT look like a
    successful measurement: container_never_seen + samples_taken 0."""
    client = _StubClient(_never_found)
    sampler = ContainerResourceSampler(
        client, "sweb.eval.scikit-learn__scikit-learn-25102.abcdef12"
    )
    sampler.start()
    time.sleep(1.0)  # let the polling loop run a couple of NotFound cycles
    m = sampler.stop(timeout=2.0)
    assert m.status == "container_never_seen"
    assert m.samples_taken == 0
    assert m.peak_rss_bytes is None


def test_sampler_tracks_peaks_and_packing_factor() -> None:
    client = _StubClient(lambda name: _stub_container(FRAME1, FRAME2))
    sampler = ContainerResourceSampler(
        client, "sweb.eval.scikit-learn__scikit-learn-25102.abcdef12"
    )
    sampler.start()
    assert _wait_until(lambda: sampler.snapshot().samples_taken >= 2, 3.0)
    m = sampler.stop(timeout=2.0)
    assert m.status == "ok"
    assert m.samples_taken == 2
    # peak RSS is the max (FRAME2), cumulative CPU is the LAST reading.
    assert m.peak_rss_bytes == 700_000_000 - 40_000_000
    assert m.cpu_total_seconds == pytest.approx(12.0)
    assert m.containers_running_peak == 2
    assert m.host_mem_total_bytes == 8_589_934_592
    assert m.online_cpus == 2


def test_sampler_closes_the_stats_stream() -> None:
    """Leak discipline: the stats(stream=True) generator is closed in a finally
    on the normal path (container removed / run done), so nothing holds the
    daemon socket open across grades."""
    holder: dict[str, Any] = {}

    def factory(name: str) -> Any:
        c = _stub_container(FRAME1)
        holder["container"] = c
        return c

    client = _StubClient(factory)
    sampler = ContainerResourceSampler(client, "sweb.eval.x.abcdef12")
    sampler.start()
    assert _wait_until(lambda: sampler.snapshot().samples_taken >= 1, 3.0)
    m = sampler.stop(timeout=2.0)
    assert m.status == "ok"
    # The frames are exhausted -> the for-loop ends normally -> finally closes
    # the stream before the thread exits.
    assert _wait_until(
        lambda: holder["container"]._stream is not None and holder["container"]._stream._closed, 3.0
    )


def test_sampler_zeroed_trailing_frames_do_not_clobber_cpu_total() -> None:
    """The daemon zeroes cpu_stats on frames it emits after the workload exits
    (observed live against the real daemon).  The cumulative CPU-seconds must
    keep the MAX reading, not the last (zeroed) one.

    The zeroed frame carries ``total_usage: 0`` — PRESENT and zero, exactly as
    the real daemon emits it — not an absent counter.  ``_extract_sample``
    maps absent to None (skipped by the is-not-None guard) but maps present-0
    to 0.0 (which only max-tracking protects against).  This is the difference
    the review proved: with an absent frame, last-wins also passes."""
    zeroed: dict[str, Any] = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 0},
            "system_cpu_usage": 0,
            "online_cpus": 2,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 0},
            "system_cpu_usage": 0,
        },
        "memory_stats": {"usage": 100_000_000, "stats": {}},
    }
    client = _StubClient(lambda name: _stub_container(FRAME1, FRAME2, zeroed, zeroed))
    sampler = ContainerResourceSampler(client, "sweb.eval.x.abcdef12")
    sampler.start()
    assert _wait_until(lambda: sampler.snapshot().samples_taken >= 4, 3.0)
    m = sampler.stop(timeout=2.0)
    assert m.cpu_total_seconds == pytest.approx(12.0)  # FRAME2's, not the zeroed tail
    assert m.peak_rss_bytes == 700_000_000 - 40_000_000
    assert m.samples_taken == 4


def test_sampler_stream_error_sets_error_and_never_raises() -> None:
    def boom(name: str) -> Any:
        class _C:
            def stats(self, stream: bool = True, decode: bool = False) -> Any:
                raise RuntimeError("daemon socket gone")

        return _C()

    client = _StubClient(boom)
    sampler = ContainerResourceSampler(
        client, "sweb.eval.scikit-learn__scikit-learn-25102.abcdef12"
    )
    sampler.start()
    time.sleep(0.3)
    m = sampler.stop(timeout=2.0)  # must not raise — a probe never fails a grade
    assert m.status == "error"


def test_sampler_malformed_frame_does_not_kill_the_stream() -> None:
    """A frame that fails to fold (e.g. not a dict) is logged and skipped;
    the next good frame still lands.  One bad frame must not lose the run."""
    client = _StubClient(lambda name: _stub_container("not-a-dict", FRAME1))
    sampler = ContainerResourceSampler(
        client, "sweb.eval.scikit-learn__scikit-learn-25102.abcdef12"
    )
    sampler.start()
    assert _wait_until(lambda: sampler.snapshot().samples_taken >= 1, 3.0)
    m = sampler.stop(timeout=2.0)
    assert m.status == "ok"
    assert m.samples_taken == 1
    assert m.peak_rss_bytes == 450_000_000


def test_sampler_stop_is_bounded_when_stream_blocks() -> None:
    """Leak discipline: stop() must never block on a stuck stream — the bounded
    join returns promptly with the samples taken so far (and the thread is
    daemon, so nothing lingers)."""
    blocked = threading.Event()

    def gen() -> Any:
        yield FRAME1
        blocked.wait()  # never returns

    class _C:
        def stats(self, stream: bool = True, decode: bool = False) -> Any:
            return gen()

    client = _StubClient(lambda name: _C())
    sampler = ContainerResourceSampler(client, "sweb.eval.x.abcdef12")
    sampler.start()
    assert _wait_until(lambda: sampler.snapshot().samples_taken >= 1, 3.0)
    t0 = time.monotonic()
    m = sampler.stop(timeout=0.5)
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0  # returned well within a generous bound
    assert m.samples_taken >= 1
    assert sampler._thread is not None and sampler._thread.daemon is True


# ---------------------------------------------------------------------------
# GradingOutput additive field + eval-worker artifact upload
# ---------------------------------------------------------------------------


def test_grading_output_resource_measurement_is_additive() -> None:
    """The new field defaults to None — every existing constructor keeps
    working, and the dataclass stays frozen/hashable (immutable fields only)."""
    out = GradingOutput(
        instance_id="scikit-learn__scikit-learn-25102",
        resolved=True,
        report_json="{}",
        wall_clock_seconds=1.0,
    )
    assert out.resource_measurement is None
    m = ResourceMeasurement(status="ok", samples_taken=5, peak_rss_bytes=450_000_000)
    out2 = GradingOutput(
        instance_id="scikit-learn__scikit-learn-25102",
        resolved=True,
        report_json="{}",
        wall_clock_seconds=1.0,
        resource_measurement=m,
    )
    assert out2.resource_measurement is m
    hash(out2)  # frozen + hashable


class _FakeInstance:
    repo = "scikit-learn/scikit-learn"
    base_commit = "abcdef"
    patch = ""
    fail_to_pass = ""
    pass_to_pass = ""
    environment_setup_commit = ""
    version = ""


def _fake_patch() -> str:
    return (
        "diff --git a/sklearn/metrics/_classification.py b/sklearn/metrics/_classification.py\n"
        "index 1..2 100644\n"
        "--- a/sklearn/metrics/_classification.py\n"
        "+++ b/sklearn/metrics/_classification.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def accuracy_score(y_true, y_pred):\n"
        "+    return (y_true == y_pred).mean()\n"
    )


def _run_eval_with_grade(grade_output: GradingOutput) -> dict[str, str]:
    """Drive _run_eval with a canned GradingOutput; return uploaded {key: data}."""
    from unittest import mock

    from swebench_eval.workers.eval_worker import _run_eval

    job = EvalJob(
        run_id="r1",
        instance_id="scikit-learn__scikit-learn-25102",
        attempt_number=1,
        patch_s3_key="runs/r1/harness/scikit-learn__scikit-learn-25102/1/patch.diff",
        fail_to_pass="",
        pass_to_pass="",
    )
    uploaded: dict[str, str] = {}

    def fake_upload(bucket: str, key: str, data: str | bytes) -> str:
        uploaded[key] = data.decode() if isinstance(data, bytes) else data
        return key

    with (
        mock.patch(
            "swebench_eval.workers.eval_worker.get_artifact", return_value=_fake_patch().encode()
        ),
        mock.patch(
            "swebench_eval.dataset.swebench_loader.load_single_instance",
            return_value=_FakeInstance(),
        ),
        mock.patch(
            "swebench_eval.workers.eval_worker.SwebenchRunner.grade", return_value=grade_output
        ),
        mock.patch("swebench_eval.workers.eval_worker.upload_artifact", side_effect=fake_upload),
    ):
        _run_eval(job)
    return uploaded


def test_run_eval_uploads_resource_usage_json_beside_the_report() -> None:
    """The measurement lands as its own S3 object next to eval_report.json,
    with the explicit status field + samples_taken the aggregation needs."""
    m = ResourceMeasurement(
        status="ok",
        samples_taken=120,
        peak_rss_bytes=812_345_678,
        cpu_total_seconds=61.5,
        peak_cpu_pct=99.9,
        online_cpus=2,
        containers_running_peak=1,
        host_mem_total_bytes=8_589_934_592,
        image_size_bytes=2_147_483_648,
    )
    out = GradingOutput(
        instance_id="scikit-learn__scikit-learn-25102",
        resolved=True,
        report_json="{}",
        wall_clock_seconds=120.0,
        resource_measurement=m,
    )
    uploaded = _run_eval_with_grade(out)

    resource_key = "runs/r1/eval/scikit-learn__scikit-learn-25102/1/resource_usage.json"
    assert resource_key in uploaded
    parsed = json.loads(uploaded[resource_key])
    assert parsed["status"] == "ok"
    assert parsed["samples_taken"] == 120
    assert parsed["peak_rss_bytes"] == 812_345_678
    assert parsed["image_size_bytes"] == 2_147_483_648
    # No ResultMessage field, no instance_results column — the report key still
    # exists (upload happened) and the artifact is purely S3-side.
    assert "runs/r1/eval/scikit-learn__scikit-learn-25102/1/eval_report.json" in uploaded


def test_run_eval_writes_no_resource_artifact_when_sampling_absent() -> None:
    """resource_measurement None (sampling disabled) -> NO resource_usage.json
    is written at all — absent is 'not measured', never a fabricated zero."""
    out = GradingOutput(
        instance_id="scikit-learn__scikit-learn-25102",
        resolved=True,
        report_json="{}",
        wall_clock_seconds=120.0,
        resource_measurement=None,
    )
    uploaded = _run_eval_with_grade(out)
    assert not any(key.endswith("/resource_usage.json") for key in uploaded)
    assert "runs/r1/eval/scikit-learn__scikit-learn-25102/1/eval_report.json" in uploaded
