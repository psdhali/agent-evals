#!/usr/bin/env python3
"""One instance, end to end, on a laptop — the no-AWS smoke test.

Runs the custom minimal harness against a single SWE-bench Verified instance
INSIDE the official per-instance image (pulled at the digest the committed
snapshot pins, plus the framework — ``scripts/local_instance_image.py``), the
way the deployed harness task does, then grades with the official harness in
the same image and verifies:

1. The harness produces a non-empty patch.
2. The trajectory is written in the normalized JSONL format.
3. The harness's raw log is captured.
4. **Gold patch grading**: the known-good patch grades as ``resolved=True``.
5. **Broken patch grading**: a deliberately-broken patch grades as ``resolved=False``.
6. The harness's own patch is graded and the verdict written to Postgres.

The harness routes through the compose stack's LiteLLM gateway; results are
written to the compose Postgres.  Needs Docker, the compose stack up, and the
dataset mirror seeded into MinIO (``scripts/local_smoke_test.sh`` does all of
it).  Adoption F1–F4 (2026-09-11) replaced the earlier empty-stub-repo run.

Usage::

    python3 scripts/smoke_test.py [--instance-id <id>] [--output-dir DIR]
                                  [--skip-grading] [--harness-output DIR]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

# Ensure the project root is on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swebench_eval.harnesses.base import HarnessOutput


class _NullContext:
    """Context manager that just returns the given path (no cleanup)."""

    def __init__(self, path: str) -> None:
        self._path = path

    def __enter__(self) -> str:
        Path(self._path).mkdir(parents=True, exist_ok=True)
        return self._path

    def __exit__(self, *args: object) -> None:
        pass


def _seed_prepared_repo(path: Path) -> None:
    """A minimal git repo for the forced MODEL_API_ERROR test ONLY.

    That test never reaches the repository (the model call fails first), so an
    empty checkout is enough for the adapter's prepared-repo assert.  The real
    smoke run no longer uses this: the agent works on the official image's
    hardened /testbed (adoption F3)."""
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "smoke@test.local"],
        ["git", "config", "user.name", "Smoke"],
        ["git", "commit", "--allow-empty", "-m", "smoke-prep"],
    ):
        subprocess.run(args, cwd=path, capture_output=True, timeout=10)  # noqa: PLW1510


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2 smoke test")
    parser.add_argument(
        "--instance-id",
        default=None,
        help="Specific SWE-bench instance ID to use (default: first available).",
    )
    parser.add_argument(
        "--skip-grading",
        action="store_true",
        help="Skip the official-harness grading step (useful when Docker is not available).",
    )
    parser.add_argument(
        "--skip-gateway",
        action="store_true",
        help="Skip the LiteLLM gateway and call OpenRouter directly (Phase 1 behaviour).",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory to persist artifacts (trajectory, raw log, patch). "
        "If omitted, a temporary directory is used and cleaned up on exit.",
    )
    parser.add_argument(
        "--force-model-api-error",
        action="store_true",
        help="Run with a deliberately invalid model name to verify MODEL_API_ERROR "
        "handling. Only runs the harness half (no grading).",
    )
    parser.add_argument(
        "--harness-output",
        default=None,
        help="Reuse the artifacts of an earlier in-image harness run (a directory holding "
        "harness_result.json) instead of running the agent again.",
    )
    parser.add_argument(
        "--model",
        default="cheap-oss-model",
        help="Gateway model alias for the agent (default: cheap-oss-model).",
    )
    parser.add_argument("--max-cost-usd", type=float, default=1.0)
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument(
        "--rebuild-image",
        action="store_true",
        help="Rebuild the local per-instance image even if one exists.",
    )
    args = parser.parse_args()

    use_gateway = not args.skip_gateway
    if not use_gateway and not args.force_model_api_error:
        print(
            "ERROR: --skip-gateway is only supported with --force-model-api-error. The in-image "
            "harness run routes through the compose gateway (scripts/local_smoke_test.sh)."
        )
        sys.exit(2)

    # ------------------------------------------------------------------
    # 0. Forced MODEL_API_ERROR case
    # ------------------------------------------------------------------
    if args.force_model_api_error:
        _run_model_api_error_test(use_gateway)
        return

    # ------------------------------------------------------------------
    # 1. Load a single instance
    # ------------------------------------------------------------------
    print("=" * 60)
    phase = "Laptop"
    print("Laptop smoke test — custom harness end-to-end, official image, official grading")
    print("(agent inside the pinned per-instance image; routing through the compose gateway)")
    print("=" * 60)

    # F2: the GRADING path needs the full row (gold patch, hidden tests); the
    # agent's container loads its own gold-stripped public row from the mirror.
    from swebench_eval.dataset.swebench_loader import load_single_instance

    instance_id = args.instance_id or "django__django-11099"
    instance = load_single_instance(instance_id, include_gold=True)
    if instance is None:
        print(f"ERROR: Instance '{instance_id}' not found in dataset split")
        sys.exit(1)

    print(f"\nUsing instance: {instance.instance_id}")
    print(f"  Repo:        {instance.repo}")
    print(f"  Base commit: {instance.base_commit}")
    print(f"  Image:       {instance.image}")
    print(f"  Problem:     {instance.problem_statement[:200]}...")

    # ------------------------------------------------------------------
    # 2. Run the custom harness INSIDE the per-instance image (F3)
    # ------------------------------------------------------------------
    print("\n" + "-" * 40)
    gateway_url = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1")
    print(f"  Gateway:            {gateway_url}")
    print(f"  Model alias:        {args.model}")

    from scripts.local_instance_image import ensure_local_image, run_local_job

    # Use --output-dir for persistent artifacts, tempdir otherwise.
    _ctx = (
        tempfile.TemporaryDirectory() if args.output_dir is None else _NullContext(args.output_dir)
    )
    with _ctx as workdir:
        workdir_path = Path(workdir)
        harness_dir = workdir_path / "harness"

        if args.harness_output:
            harness_dir = Path(args.harness_output)
            print(f"Reusing harness artifacts from {harness_dir}")
        else:
            print("Ensuring the local per-instance image (pinned official base + framework)...")
            image = ensure_local_image(
                instance.instance_id, rebuild=args.rebuild_image, base_commit=instance.base_commit
            )
            print(f"\nRunning custom harness inside {image} ...")
            rc = run_local_job(
                instance.instance_id,
                harness_dir,
                args.model,
                image=image,
                timeout_s=args.timeout_s,
                max_cost_usd=args.max_cost_usd,
                max_turns=args.max_turns,
            )
            if rc != 0:
                print(f"\nERROR: the in-image harness job exited {rc} — see {harness_dir}")
                sys.exit(1)

        output = _harness_output_from_dir(harness_dir)

        # ------------------------------------------------------------------
        # 3. Verify harness output
        # ------------------------------------------------------------------
        print("\n" + "-" * 40)
        print("Harness results:")
        print(f"  Success:            {output.success}")
        print(f"  Terminated reason:  {output.terminated_reason}")
        print(f"  Error category:     {output.error_category or '(none)'}")
        print(f"  Wall clock:         {output.wall_clock_seconds:.1f}s")
        print(f"  Exit code:          {output.exit_code}")
        print(f"  Error:              {output.error or '(none)'}")
        print(f"  Input tokens:       {output.usage.input_tokens}")
        print(f"  Output tokens:      {output.usage.output_tokens}")
        print(f"  Cost (shim meter):  ${output.usage.cost_usd:.4f}")

        if output.patch:
            patch_lines = output.patch.count("\n")
            print(f"  Patch:              {patch_lines} lines")
        else:
            print("  Patch:              (empty)")

        trajectory_ok = False
        if output.trajectory_path and os.path.exists(output.trajectory_path):
            traj_lines = len(Path(output.trajectory_path).read_text().splitlines())
            print(f"  Trajectory:         {traj_lines} events in {output.trajectory_path}")

            # Validate trajectory format.
            with open(output.trajectory_path) as f:
                for line in f:
                    event = json.loads(line)
                    assert "role" in event, f"Missing 'role' in trajectory event: {event}"
                    assert "turn" in event, f"Missing 'turn' in trajectory event: {event}"
                    assert "ts" in event, f"Missing 'ts' in trajectory event: {event}"
            print("  Trajectory schema:  VALID")
            trajectory_ok = True
        else:
            print("  Trajectory:         (missing)")

        if output.raw_log_path and os.path.exists(output.raw_log_path):
            print(f"  Raw log:            {output.raw_log_path}")
        else:
            print("  Raw log:            (missing)")

        # ------------------------------------------------------------------
        # 4. Write to Postgres (gateway mode only)
        # ------------------------------------------------------------------
        if use_gateway:
            _write_to_postgres(instance, output, workdir_path)

        # ------------------------------------------------------------------
        # 5. Grade the gold patch (expected: resolved=True)
        # ------------------------------------------------------------------
        if args.skip_grading:
            print("\n" + "=" * 60)
            print("Grading skipped (--skip-grading). Harness-only smoke test complete.")
            print("=" * 60)
            _check_harness_only(output, trajectory_ok)
            return

        if not instance.patch:
            print("\nERROR: Instance has no gold patch — cannot run grading sanity check")
            sys.exit(1)

        print("\n" + "-" * 40)
        print("Grading gold patch (expected: resolved=True)...")

        from swebench_eval.evaluation.grading_adapter import GradingInput
        from swebench_eval.evaluation.swebench_runner import SwebenchRunner

        # namespace="swebench": the official harness grades in the dataset row's
        # image name, which ensure_local_image tagged to the PINNED digest above —
        # the mutable Docker Hub tag is never pulled (F4).
        runner = SwebenchRunner(namespace="swebench", docker_available=True)

        gold_verdict = runner.grade(
            GradingInput(
                instance_id=instance.instance_id,
                patch=instance.patch,
                fail_to_pass=instance.fail_to_pass,
                pass_to_pass=instance.pass_to_pass,
            ),
            instance=instance,
        )

        print(f"  Resolved:          {gold_verdict.resolved}")
        print(f"  Wall clock:        {gold_verdict.wall_clock_seconds:.1f}s")
        print(f"  Touches test files:{gold_verdict.touches_test_files}")
        if gold_verdict.error:
            print(f"  Error:             {gold_verdict.error}")

        if not gold_verdict.resolved:
            print("\nERROR: Gold patch did not resolve. This means either:")
            print("  1. The evaluation pipeline has a bug (infrastructure error).")
            print("  2. Docker is not running or SWE-bench images are not built.")
            print("  3. The swebench package version is incompatible.")
            sys.exit(1)

        print("  ✓ Gold patch correctly resolved")

        # ------------------------------------------------------------------
        # 6. Grade a broken patch (expected: resolved=False)
        # ------------------------------------------------------------------
        print("\n" + "-" * 40)
        print("Grading broken patch (expected: resolved=False)...")

        broken_patch = _make_broken_patch(instance.patch)
        if broken_patch is None:
            print("ERROR: Could not construct a broken patch from the gold patch")
            sys.exit(1)

        broken_verdict = runner.grade(
            GradingInput(
                instance_id=instance.instance_id,
                patch=broken_patch,
                fail_to_pass=instance.fail_to_pass,
                pass_to_pass=instance.pass_to_pass,
            ),
            instance=instance,
        )

        print(f"  Resolved:          {broken_verdict.resolved}")
        print(f"  Wall clock:        {broken_verdict.wall_clock_seconds:.1f}s")
        if broken_verdict.error:
            print(f"  Error:             {broken_verdict.error}")

        if broken_verdict.resolved:
            print("\nERROR: Broken patch incorrectly resolved. The evaluation pipeline")
            print("is rubber-stamping 'resolved' — this is a grading bug.")
            sys.exit(1)

        print("  ✓ Broken patch correctly rejected")

        # ------------------------------------------------------------------
        # 7. Grade the harness's own patch and write the verdict (gateway mode only)
        # ------------------------------------------------------------------
        if use_gateway and output.patch:
            print("\n" + "-" * 40)
            print("Grading harness patch...")

            harness_verdict = runner.grade(
                GradingInput(
                    instance_id=instance.instance_id,
                    patch=output.patch,
                    fail_to_pass=instance.fail_to_pass,
                    pass_to_pass=instance.pass_to_pass,
                ),
                instance=instance,
            )
            print(f"  Resolved:          {harness_verdict.resolved}")
            print(f"  Wall clock:        {harness_verdict.wall_clock_seconds:.1f}s")
            print(f"  Touches test files:{harness_verdict.touches_test_files}")
            if harness_verdict.error:
                print(f"  Error:             {harness_verdict.error}")

            _update_eval_verdict(instance, harness_verdict)

        # ------------------------------------------------------------------
        # 8. Summary
        # ------------------------------------------------------------------
        print("\n" + "=" * 60)
        failed = False

        if output.success and output.patch:
            print("✓ Harness produced a non-empty patch")
        else:
            print("✗ Harness did not produce a patch")
            failed = True

        if trajectory_ok:
            print("✓ Trajectory written in normalized JSONL format")
        else:
            print("✗ Trajectory missing")
            failed = True

        print("✓ Gold patch:     resolved=True  (sanity check passed)")
        print("✓ Broken patch:   resolved=False (not rubber-stamping)")

        if use_gateway:
            print("✓ Postgres:       run + instance results written")

        if failed:
            print("\nFAIL: harness or trajectory checks failed — see above")
            sys.exit(1)

        print("Smoke test complete — all checks passed")
        print(f"\n{phase} Definition of Done: MET")


def _harness_output_from_dir(harness_dir: Path) -> HarnessOutput:
    """Rebuild a :class:`HarnessOutput` from an in-image ``local-job`` artifact dir."""
    from swebench_eval.harnesses.base import HarnessOutput, Usage

    result_path = harness_dir / "harness_result.json"
    if not result_path.exists():
        print(f"ERROR: no harness_result.json in {harness_dir} — the in-image job did not finish")
        sys.exit(1)
    result = json.loads(result_path.read_text())
    patch_path = harness_dir / "patch.diff"
    patch = patch_path.read_text() if patch_path.exists() else ""
    usage_fields = {f.name for f in dataclasses.fields(Usage)}
    usage = Usage(**{k: v for k, v in (result.get("usage") or {}).items() if k in usage_fields})
    adapter = result.get("adapter_reported_usage")
    adapter_usage = (
        Usage(**{k: v for k, v in adapter.items() if k in usage_fields}) if adapter else None
    )
    return HarnessOutput(
        patch=patch or None,
        success=bool(result.get("success")) and bool(patch),
        trajectory_path=str(harness_dir / "trajectory.jsonl"),
        raw_log_path=str(harness_dir / "harness_stdout.log"),
        usage=usage,
        adapter_reported_usage=adapter_usage,
        wall_clock_seconds=float(result.get("wall_clock_s") or 0.0),
        exit_code=0,
        error=str(result.get("error") or ""),
        terminated_reason=result.get("terminated_reason") or "completed",
        error_category=str(result.get("error_category") or ""),
        compactions_fired=result.get("compactions_fired"),
        context_window_tokens=result.get("context_window_tokens"),
    )


def _check_harness_only(output: HarnessOutput, trajectory_ok: bool) -> None:
    """Validate harness-only results (when grading is skipped)."""
    failed = False
    if output.success and output.patch:
        print("✓ Harness produced a non-empty patch")
    else:
        print("✗ Harness did not produce a patch")
        failed = True
    if trajectory_ok:
        print("✓ Trajectory written in normalized JSONL format")
    else:
        print("✗ Trajectory missing")
        failed = True
    if failed:
        print("\nFAIL: harness or trajectory checks failed — see above")
        sys.exit(1)


def _make_broken_patch(gold_patch: str) -> str | None:
    """Create a deliberately broken patch.

    Replaces the ``+`` (added) lines in the first hunk with a syntax-breaking
    ``raise RuntimeError``.  The result is a valid unified diff that should
    *not* resolve — it breaks the code rather than fixing it.
    """
    lines = gold_patch.split("\n")
    out: list[str] = []
    in_hunk = False

    for line in lines:
        if line.startswith("@@"):
            in_hunk = True
            out.append(line)
        elif in_hunk and line.startswith("+"):
            # Replace the fix with something that clearly breaks things.
            out.append('+    raise RuntimeError("deliberately broken patch")')
        elif in_hunk:
            out.append(line)
        else:
            out.append(line)

    result = "\n".join(out)
    if result == gold_patch:
        return None
    return result


# ---------------------------------------------------------------------------
# Postgres helpers (gateway mode)
# ---------------------------------------------------------------------------


def _generate_run_id() -> str:
    """Generate a ULID-compatible run ID (timestamp-sortable, unique)."""
    ts = time.time_ns()
    suffix = uuid.uuid4().hex[:8]
    return f"{ts:020d}-{suffix}"


def _write_to_postgres(instance: Any, output: Any, workdir_path: Path) -> None:
    """Write harness-phase results to Postgres."""
    from swebench_eval.database.connection import (
        ensure_additional_databases,
        get_connection,
        run_migrations,
    )
    from swebench_eval.database.state_machine import (
        map_terminated_reason_to_error_category,
        map_terminated_reason_to_state,
    )
    from swebench_eval.dataset.swebench_loader import _DATASET_NAME, SwebenchLiteLoader

    # Ensure schema exists, and that ADR-0022's second database (litellm_spend)
    # exists — the control-plane creates it on startup (review N-2); this is the
    # pipeline's equivalent, so spend rows have a home.
    run_migrations()
    ensure_additional_databases()

    run_id = _generate_run_id()
    conn = get_connection()

    # Resolve the pinned dataset revision + identity for config_snapshot. The
    # dataset name comes from the loader's single source of truth (V1), so the
    # smoke config can never name a different dataset than the loader reads.
    loader = SwebenchLiteLoader()
    config = {
        "phase": "2",
        "smoke_test": True,
        "dataset": _DATASET_NAME,
        "dataset_split": "test",
        "dataset_revision": loader.revision,
    }

    try:
        with conn.cursor() as cur:
            # --- runs ---
            cur.execute(
                """INSERT INTO runs (run_id, config_snapshot, status)
                   VALUES (%s, %s, 'running')""",
                (run_id, json.dumps(config)),
            )

            # --- run_targets ---
            cur.execute(
                """INSERT INTO run_targets (run_id, harness, model_alias)
                   VALUES (%s, %s, %s)""",
                (run_id, "custom_minimal", "cheap-oss-model"),
            )

            # --- instance_results (harness phase) ---
            error_category = map_terminated_reason_to_error_category(
                output.terminated_reason, output.patch
            )
            cur.execute(
                """INSERT INTO instance_results
                   (run_id, instance_id, attempt_number, phase, state,
                    error_category, error_detail, wall_clock_harness_s,
                    touches_test_files, patch_path, trajectory_path, raw_log_path)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    run_id,
                    instance.instance_id,
                    1,
                    "harness",
                    map_terminated_reason_to_state(output.terminated_reason, output.patch),
                    error_category or "",
                    output.error or "",
                    output.wall_clock_seconds,
                    False,  # set by eval phase
                    str(workdir_path / "patch.diff") if output.patch else None,
                    output.trajectory_path or "",
                    output.raw_log_path or "",
                ),
            )

            # --- live progress → Redis (ADR-0018; no Postgres write path) ---
            from swebench_eval.database.redis_client import write_progress

            write_progress(
                run_id,
                instance.instance_id,
                1,
                turn_number=0,  # final state; per-turn tracking is the worker's job
                usage=output.usage,
            )

        conn.commit()

        print("\n" + "-" * 40)
        print("Postgres rows written:")
        print(f"  run_id:              {run_id}")
        print("  runs:                1 row")
        print("  run_targets:         1 row (custom_minimal × cheap-oss-model)")
        print("  instance_results:    1 row (harness phase)")
        print("  instance_progress:   Redis key (ADR-0018)")

        # Store run_id for later use.
        _write_to_postgres._run_id = run_id  # type: ignore[attr-defined]

    finally:
        conn.close()


def _update_eval_verdict(instance: Any, gold_verdict: Any) -> None:
    """Update Postgres with the eval-phase verdict."""
    from swebench_eval.database.connection import get_connection
    from swebench_eval.database.state_machine import map_eval_outcome_to_error_category

    run_id: str = getattr(_write_to_postgres, "_run_id", "")
    if not run_id:
        print("  (no run_id cached — skipping eval update)")
        return

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            error_category = map_eval_outcome_to_error_category(gold_verdict.resolved)
            cur.execute(
                """INSERT INTO instance_results
                   (run_id, instance_id, attempt_number, phase, state,
                    error_category, verdict, wall_clock_eval_s,
                    touches_test_files, report_json)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    run_id,
                    instance.instance_id,
                    1,
                    "eval",
                    error_category,
                    error_category,
                    "resolved" if gold_verdict.resolved else "unresolved",
                    gold_verdict.wall_clock_seconds,
                    gold_verdict.touches_test_files,
                    gold_verdict.report_json,
                ),
            )
        conn.commit()
        print("  instance_results:    1 row (eval phase)")
    finally:
        conn.close()


def _run_model_api_error_test(use_gateway: bool) -> None:
    """Run the harness with a deliberately invalid model name and verify the error taxonomy.

    This satisfies the Phase 2 DoD requirement: "a correct error-taxonomy value
    for at least one deliberately-forced failure case (e.g., a bad model name to
    force MODEL_API_ERROR)."
    """
    import tempfile

    from swebench_eval.harnesses.base import HarnessInput, ModelConfig
    from swebench_eval.harnesses.custom_minimal import CustomMinimalHarness

    print("=" * 60)
    print("Phase 2 — forced MODEL_API_ERROR test")
    print("=" * 60)

    if use_gateway:
        gateway_url = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1")
        gateway_key = os.environ.get("LITELLM_MASTER_KEY", "sk-local")
        model_config = ModelConfig(
            gateway_base_url=gateway_url,
            gateway_api_key=gateway_key,
            model_name="nonexistent-model-does-not-exist",
            temperature=0.0,
        )
        print(f"Gateway: {gateway_url}")
        print("Model alias: nonexistent-model-does-not-exist")
    else:
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        model_config = ModelConfig(
            gateway_base_url="https://openrouter.ai/api/v1",
            gateway_api_key=api_key,
            model_name="nonexistent/model",
            temperature=0.0,
        )

    with tempfile.TemporaryDirectory() as workdir:
        workdir_path = Path(workdir)
        checkout_path = workdir_path / "repo"
        _seed_prepared_repo(checkout_path)

        harness_input = HarnessInput(
            instance_id="nonexistent-instance",
            repo_url="https://github.com/astropy/astropy",
            base_commit="d16bfe05a744909de4b27f5875fe0d4ed41ce607",
            problem_statement="This should fail — model does not exist.",
            attempt_number=1,
            repo_checkout_path=str(checkout_path),
            model_config=model_config,
            timeout_seconds=30,
            max_tokens_per_instance=1000,
            max_cost_usd_per_instance=0.01,
        )

        harness = CustomMinimalHarness(
            api_base_url=model_config.gateway_base_url,
            api_key=model_config.gateway_api_key,
            model=model_config.model_name,
        )

        output = harness.run(harness_input)

    print(f"\nTerminated reason:  {output.terminated_reason}")
    print(f"Error category:     {output.error_category}")
    print(f"Error:              {output.error[:200]}")

    # Verify the error taxonomy is correct.
    expected_reasons = {"model_api_error", "crash"}
    expected_categories = {"MODEL_API_ERROR", "HARNESS_CRASH"}

    if not (
        output.terminated_reason in expected_reasons
        and output.error_category in expected_categories
    ):
        print(
            f"\n✗ Unexpected error taxonomy: {output.terminated_reason} → {output.error_category}"
        )
        print(f"Expected one of: {expected_reasons} → {expected_categories}")
        sys.exit(1)

    print(f"\n✓ Error taxonomy correct: {output.terminated_reason} → {output.error_category}")

    # Persist the forced-failure row to Postgres (P2-A).
    # This is the DoD requirement: the row must be queryable in instance_results.
    from swebench_eval.database.connection import (
        ensure_additional_databases,
        get_connection,
        run_migrations,
    )
    from swebench_eval.database.state_machine import (
        map_terminated_reason_to_error_category,
        map_terminated_reason_to_state,
    )

    run_migrations()
    ensure_additional_databases()
    run_id = _generate_run_id()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (run_id, config_snapshot, status) VALUES (%s, %s, 'running')",
                (run_id, json.dumps({"phase": "2", "test": "forced-model-api-error"})),
            )
            cur.execute(
                "INSERT INTO run_targets (run_id, harness, model_alias) VALUES (%s, %s, %s)",
                (run_id, "custom_minimal", "nonexistent-model-does-not-exist"),
            )
            state = map_terminated_reason_to_state(output.terminated_reason, output.patch)
            error_cat = map_terminated_reason_to_error_category(
                output.terminated_reason, output.patch
            )
            cur.execute(
                """INSERT INTO instance_results
                   (run_id, instance_id, attempt_number, phase, state,
                    error_category, error_detail, wall_clock_harness_s)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    run_id,
                    "nonexistent-instance",
                    1,
                    "harness",
                    state,
                    error_cat or "",
                    output.error or "",
                    output.wall_clock_seconds,
                ),
            )
        conn.commit()

        # Verify the row is queryable.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, error_category FROM instance_results WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()
            if row:
                print(f"  Postgres row:       state={row[0]}, error_category={row[1]}")
                print("Forced MODEL_API_ERROR test passed")
            else:
                print("✗ Row not found in Postgres — persistence failed")
                sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
