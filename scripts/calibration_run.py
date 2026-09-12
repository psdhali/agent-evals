#!/usr/bin/env python3
"""Phase 3 calibration run — resource usage, cost, wall-clock per instance.

Runs instances through harnesses (in-process, via the same adapters the
worker wrappers use) while measuring:

- peak RSS (psutil)
- peak CPU % (sampled every 5s)
- wall-clock (time.monotonic)
- tokens / cost (from HarnessOutput.usage)
- max encoded HarnessJob message size (P3-5c, against the 256KB SQS cap)

Output feeds design/calibration-note.md, which Phase 5's Terraform sizing
is required to cite, not re-derive.

By default a small sample (--instances 3 --attempts 1 --harness custom_minimal)
is run so the numbers are real but the wall-clock stays reasonable.  The full
45-run matrix (5×3×3) is the documented target.

Usage:
    python3 scripts/calibration_run.py [--instances 3] [--attempts 1]
                                       [--harness custom_minimal]
                                       [--timeout 180]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Self

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Hand-picked instances spanning different repos/difficulties.
DEFAULT_INSTANCES = [
    "astropy__astropy-12907",
    "django__django-15500",
    "matplotlib__matplotlib-22711",
    "scikit-learn__scikit-learn-13779",
    "sympy__sympy-12472",
]


def _seed_prepared_repo(path: Path) -> None:
    """5b: the adapter asserts a PREPARED repo (install_repo_script's job in
    AWS).  Local calibration has no env build, so make a minimal git repo the
    adapter can treat as prepared."""
    import subprocess

    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "calib@test.local"],
        ["git", "config", "user.name", "Calib"],
        ["git", "commit", "--allow-empty", "-m", "calib-prep"],
    ):
        subprocess.run(args, cwd=path, capture_output=True, timeout=10)  # noqa: PLW1510


class _SampleTracker:
    """Sampling wrapper around psutil for peak CPU/memory of the current process."""

    def __init__(self, interval: float = 5.0) -> None:
        self._proc = psutil.Process()
        self._interval = interval
        self.peak_rss_mb: float = 0.0
        self.peak_cpu_percent: float = 0.0
        self._cpu_last: float = 0.0

    def sample(self) -> None:
        try:
            self.peak_rss_mb = max(self.peak_rss_mb, self._proc.memory_info().rss / 1e6)
            self.peak_cpu_percent = max(
                self.peak_cpu_percent, self._proc.cpu_percent(interval=None)
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def __enter__(self) -> Self:
        self._cpu_last = time.monotonic()
        self.sample()
        return self

    def __exit__(self, *exc: object) -> None:
        self.sample()  # final sample at end of run


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 3 calibration run")
    parser.add_argument("--instances", type=int, default=3)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument(
        "--harness", default="custom_minimal", choices=["custom_minimal", "aider", "mini_swe_agent"]
    )
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    from swebench_eval.dataset.swebench_loader import load_single_instance
    from swebench_eval.harnesses.base import HarnessInput, ModelConfig

    instances = DEFAULT_INSTANCES[: args.instances]
    print(f"Calibration: {args.instances} instances × 1 harness × {args.attempts} attempt(s)")
    print(f"Harness: {args.harness}, timeout={args.timeout}s\n")

    results: list[dict[str, Any]] = []

    for instance_id in instances:
        instance = load_single_instance(instance_id)
        if instance is None:
            print(f"  SKIP {instance_id}: not found in dataset")
            continue

        for attempt in range(1, args.attempts + 1):
            print(f"  [{attempt}] {instance_id} ...", end=" ", flush=True)

            # Build the harness adapter (same as the worker wrapper dispatch).
            gateway_url = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1")
            gateway_key = os.environ.get("LITELLM_MASTER_KEY", "sk-local")

            harness: Any

            if args.harness == "custom_minimal":
                from swebench_eval.harnesses.custom_minimal import CustomMinimalHarness

                harness = CustomMinimalHarness(
                    api_base_url=gateway_url, api_key=gateway_key, model="cheap-oss-model"
                )
            elif args.harness == "aider":
                from swebench_eval.harnesses.aider.harness import AiderHarness

                harness = AiderHarness(
                    api_base_url=gateway_url, api_key=gateway_key, model="cheap-oss-model"
                )
            else:
                from swebench_eval.harnesses.mini_swe_agent.harness import MiniSweAgentHarness

                harness = MiniSweAgentHarness(model="cheap-oss-model")

            # Max encoded job message size (P3-5c) — build the HarnessJob as
            # the dispatcher would and measure serialized JSON bytes.
            import dataclasses

            from swebench_eval.queue.schemas import HarnessJob

            job = HarnessJob(
                run_id="calibration",
                instance_id=instance.instance_id,
                repo_url=f"https://github.com/{instance.repo}",
                base_commit=instance.base_commit,
                problem_statement=instance.problem_statement,
                attempt_number=attempt,
                harness_name=args.harness,
                model_alias="cheap-oss-model",
                timeout_seconds=args.timeout,
            )
            job_bytes = len(json.dumps(dataclasses.asdict(job)).encode("utf-8"))

            with tempfile.TemporaryDirectory() as tmpdir, _SampleTracker() as tracker:
                # 5b: the adapter asserts a PREPARED repo (install_repo_script's
                # job in AWS); hand it a minimal git repo here.
                checkout = Path(tmpdir) / "repo"
                _seed_prepared_repo(checkout)

                h_input = HarnessInput(
                    instance_id=instance.instance_id,
                    repo_url=f"https://github.com/{instance.repo}",
                    base_commit=instance.base_commit,
                    problem_statement=instance.problem_statement,
                    attempt_number=attempt,
                    repo_checkout_path=str(checkout),
                    model_config=ModelConfig(
                        gateway_base_url=gateway_url,
                        gateway_api_key=gateway_key,
                        model_name="cheap-oss-model",
                        temperature=0.0,
                    ),
                    timeout_seconds=args.timeout,
                    max_tokens_per_instance=None,
                    max_cost_usd_per_instance=5.0,
                )

                t0 = time.monotonic()
                output = harness.run(h_input)
                wall_clock = time.monotonic() - t0

                # Final samples.
                tracker.sample()

            result = {
                "instance_id": instance.instance_id,
                "harness": args.harness,
                "attempt": attempt,
                "terminated_reason": output.terminated_reason,
                "error_category": output.error_category,
                "patch_present": bool(output.patch),
                "wall_clock_s": round(wall_clock, 1),
                "input_tokens": output.usage.input_tokens,
                "output_tokens": output.usage.output_tokens,
                "cost_usd": round(output.usage.cost_usd, 4),
                "peak_rss_mb": round(tracker.peak_rss_mb, 1),
                "peak_cpu_pct": round(tracker.peak_cpu_percent, 1),
                "job_message_bytes": job_bytes,
            }
            results.append(result)

            print(
                f"→ {output.terminated_reason} wall={wall_clock:.0f}s "
                f"tokens={output.usage.input_tokens + output.usage.output_tokens} "
                f"cost=${output.usage.cost_usd:.4f}"
            )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("Calibration summary:")

    durations = [r["wall_clock_s"] for r in results]
    if durations:
        durations_sorted = sorted(durations)
        n = len(durations_sorted)
        p50 = durations_sorted[max(0, n // 2 - 1)]
        p95 = durations_sorted[min(n - 1, int(n * 0.95))]
        max_d = durations_sorted[-1]
        print(f"  job wall-clock: p50={p50}s, p95={p95}s, max={max_d}s (n={n})")

        # Derive heartbeat cadence / base visibility (P3-1).
        heartbeat = max(int(p95 // 5), 30)  # ≤ 1/5 of p95, at least 30s
        base_timeout = p95 + 2 * heartbeat  # p95 + 2 missed heartbeats
        print(f"  derived heartbeat cadence: {heartbeat}s")
        print(f"  derived base visibility:   {base_timeout:.0f}s")

    costs = [r["cost_usd"] for r in results]
    peak_rss = [r["peak_rss_mb"] for r in results]
    max_job_msg = max((r["job_message_bytes"] for r in results), default=0)
    print(f"  avg cost per run: ${sum(costs) / max(len(costs), 1):.4f}")
    print(f"  peak RSS: {max(peak_rss, default=0):.0f} MB")
    print(f"  max job message size: {max_job_msg} bytes (SQS cap 262144)")

    # Save raw results for the calibration note.
    out = Path("design/calibration-data.json")
    out.write_text(json.dumps(results, indent=2))
    print(f"\n  raw results → {out}")

    print("\nDerived numbers go into design/calibration-note.md (see commit).")


if __name__ == "__main__":
    main()
