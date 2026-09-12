"""Run ONE instance on a laptop, inside the local per-instance image (adoption F3).

The no-AWS twin of :func:`harness_worker.run_harness_worker_job`: same sentinel
check, same gold-stripped mirror read, same privilege drop, same shim-metered
custom_minimal run — minus the queue, the S3 upload, Redis progress and ECS
timing.  Artifacts land in a bind-mounted directory (``--out``) in the exact
layout the deployed worker uploads under ``runs/<run>/.../<attempt>/``:

    patch.diff              the agent's diff (absent when it made no change)
    trajectory.jsonl        normalised trajectory
    harness_stdout.log      the adapter's raw log
    llm_calls.jsonl         one row per model call (the shim's record)
    compaction_events.json  when a compaction pass fired
    harness_result.json     what the ResultMessage would have carried

Invoked by ``entrypoint.sh local-job`` from ``Dockerfile.local-instance``; driven
by ``scripts/smoke_test.py`` on the host, which then grades the patch with the
official harness in the same digest-pinned image.

Deliberate differences from the deployed path, stated so nobody reads the
laptop run as the AWS one: the container has egress (the isolation controls are
a VPC property — see docs/SETUP.md step 2), the pacer is off (no shared Redis
ledger; ``EVAL_PACER_ENABLED`` defaults to ``0`` here), and there is no
per-run virtual key (the gateway master key is used).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one instance inside the local image")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--model", required=True, help="gateway model alias, e.g. cheap-oss-model")
    parser.add_argument("--out", default="/out", help="artifact directory (bind mount)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument("--max-cost-usd", type=float, default=1.0)
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--context-window-tokens", type=int, default=None)
    return parser.parse_args(argv)


def _generate_run_id() -> str:
    return f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"


def run_local_job(argv: list[str] | None = None) -> int:
    """Entry point.  Returns the process exit code (0 = the harness ran to a result)."""
    from swebench_eval.logging_bootstrap import configure_logging

    configure_logging()
    args = _parse_args(argv)
    # No shared Redis ledger on a laptop: pacing off unless the caller insists.
    os.environ.setdefault("EVAL_PACER_ENABLED", "0")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or _generate_run_id()

    from swebench_eval.dataset.swebench_loader import load_single_instance
    from swebench_eval.harnesses.repo_prep import TESTBED_PATH, verify_testbed_prebaked

    # 1. The image must be THIS instance's (fail closed, as the deployed job does).
    sentinel = verify_testbed_prebaked(args.instance_id)
    logger.info(
        "local-job: %s in image built %s from %s",
        args.instance_id,
        sentinel.get("prepared_at"),
        sentinel.get("base_image_digest") or sentinel.get("base_image") or "<unknown base>",
    )

    # 2. The gold-stripped public row from the mirror — never HF (the loader raises).
    instance = load_single_instance(args.instance_id, include_gold=False)
    if instance is None:
        raise RuntimeError(f"Instance '{args.instance_id}' not found in the dataset mirror")

    # 3. Privilege drop + workdir (the artifact dir doubles as the job workdir).
    from swebench_eval.workers.harness_worker import _testbed_env_prefix, grant_agent_access

    checkout = Path(TESTBED_PATH)
    grant_agent_access([checkout, _testbed_env_prefix(), out])

    # 4. The shim (sole meter), then the adapter — the deployed _run_harness shape.
    from swebench_eval.gateway.local_proxy import LocalProxy
    from swebench_eval.harnesses.base import HarnessInput, HarnessOutput, ModelConfig, Usage
    from swebench_eval.harnesses.custom_minimal import CustomMinimalHarness
    from swebench_eval.harnesses.routing import gateway_api_key, gateway_base_url

    usage = Usage()
    shim = LocalProxy(
        upstream_url=gateway_base_url(),
        run_id=run_id,
        harness="custom_minimal",
        instance_id=args.instance_id,
        attempt_number=args.attempt,
        usage=usage,
        output_dir=str(out),
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=args.max_cost_usd,
        max_turns_per_instance=args.max_turns,
        context_window_tokens=args.context_window_tokens,
    ).start()
    harness = CustomMinimalHarness(model=args.model, max_turns=args.max_turns)
    harness_input = HarnessInput(
        instance_id=instance.instance_id,
        repo_url=f"https://github.com/{instance.repo}",
        base_commit=instance.base_commit,
        problem_statement=instance.problem_statement,
        attempt_number=args.attempt,
        repo_checkout_path=str(checkout),
        output_dir=str(out),
        model_config=ModelConfig(
            gateway_base_url=shim.local_base_url,
            gateway_api_key=gateway_api_key(),
            model_name=args.model,
        ),
        timeout_seconds=args.timeout_s,
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=args.max_cost_usd,
        max_turns_per_instance=args.max_turns,
        context_window_tokens=args.context_window_tokens,
        live_usage=usage,
    )

    started = time.monotonic()
    try:
        output = harness.run(harness_input)
    except Exception as exc:
        logger.exception("harness.run raised for %s", args.instance_id)
        output = HarnessOutput(
            patch="",
            success=False,
            trajectory_path=str(out / "trajectory.jsonl"),
            raw_log_path=str(out / "harness_stdout.log"),
            wall_clock_seconds=time.monotonic() - started,
            error=f"harness.run raised: {exc}",
            terminated_reason="crash",
        )
    finally:
        shim.stop()

    # 5. The worker's post-run classification, verbatim.
    from swebench_eval.database.state_machine import (
        map_terminated_reason_to_error_category,
        map_terminated_reason_to_state,
    )
    from swebench_eval.queue.schemas import HarnessJob
    from swebench_eval.workers.harness_worker import (
        _classify_budget_breach,
        _classify_zero_model_calls,
    )

    job = HarnessJob(
        run_id=run_id,
        instance_id=instance.instance_id,
        repo_url=harness_input.repo_url,
        base_commit=instance.base_commit,
        problem_statement=instance.problem_statement,
        attempt_number=args.attempt,
        harness_name="custom_minimal",
        model_alias=args.model,
        timeout_seconds=args.timeout_s,
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=args.max_cost_usd,
        max_turns_per_instance=args.max_turns,
        context_window_tokens=args.context_window_tokens,
    )
    _classify_zero_model_calls(output, shim)
    _classify_budget_breach(output, usage, job, shim)

    # 6. Artifacts in the deployed layout (the adapter already wrote the
    #    trajectory / raw log / patch into ``out``; make sure the patch is there
    #    even when the adapter wrote it elsewhere).
    if output.patch and not (out / "patch.diff").exists():
        (out / "patch.diff").write_text(output.patch)
    if output.compaction_events:
        (out / "compaction_events.json").write_text(
            json.dumps(output.compaction_events, indent=2) + "\n"
        )
    state = map_terminated_reason_to_state(output.terminated_reason, output.patch)
    error_cat = (
        output.error_category
        or map_terminated_reason_to_error_category(output.terminated_reason, output.patch)
        or ""
    )
    result: dict[str, Any] = {
        "run_id": run_id,
        "instance_id": instance.instance_id,
        "attempt_number": args.attempt,
        "harness": "custom_minimal",
        "model_alias": args.model,
        "state": state,
        "terminated_reason": output.terminated_reason,
        "error_category": error_cat,
        "error": output.error,
        "success": output.success,
        "wall_clock_s": output.wall_clock_seconds,
        "patch_bytes": len(output.patch or ""),
        "turns_used": getattr(shim, "turns_used", None),
        "usage": dataclasses.asdict(usage),
        "adapter_reported_usage": (
            dataclasses.asdict(output.adapter_reported_usage)
            if output.adapter_reported_usage is not None
            else None
        ),
        "compactions_fired": output.compactions_fired,
        "context_window_tokens": output.context_window_tokens,
        "sentinel": sentinel,
        "framework_sha": os.environ.get("FRAMEWORK_SHA"),
    }
    (out / "harness_result.json").write_text(json.dumps(result, indent=2) + "\n")
    logger.info(
        "local-job done: %s state=%s reason=%s patch=%d bytes cost=$%.4f turns=%s",
        instance.instance_id,
        state,
        output.terminated_reason,
        len(output.patch or ""),
        usage.cost_usd,
        result["turns_used"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_local_job())
