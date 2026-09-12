#!/bin/sh
# Entrypoint dispatcher for the swe-bench-eval framework service images.
#
# One image per service with the subcommand as the first arg (from the ECS
# task definition's `command`, or the image CMD default). This keeps the
# orchestrator to ONE image with two entrypoints (api + control-plane), per
# A-5, and gives the workers/warm-job their own images.
#
#   orchestrator-api            -> uvicorn FastAPI (port $PORT, default 8000)
#   run-supervisor              -> the control-plane SINGLETON: the Aurora->
#                                  Valkey heartbeat + reaper rules 2/3 (moved
#                                  out of results-writer, CONTROL-PLANE-
#                                  DECOMPOSITION-DESIGN-2026-08-31.md — the
#                                  90s heartbeat that gates the whole run must
#                                  never share a process with the busiest
#                                  consumer). REPLACES orchestrator-control-
#                                  plane, which is retired.
#   results-writer               -> results_writer poll loop: the results
#                                  consume loop, the llm_calls daemon, and the
#                                  DLQ consumers (reaper rule 1). N replicas
#                                  (ADR-0018 makes that seam real) — the other
#                                  half of the retired orchestrator-control-
#                                  plane.
#   harness-worker              -> run_harness_worker poll loop (pre-H3 service
#                                  path; local dev / tests)
#   harness-worker-job          -> run ONE job from the ADR-0032 reference then
#                                  exit (the H3 task entrypoint)
#   harness-dispatcher          -> run_harness_dispatcher: poll harness-jobs,
#                                  launch one Fargate task per job (ADR-0030 H3)
#   eval-worker                 -> run_eval_worker poll loop (privileged DinD
#                                  grading via the host docker socket)
#   warm-job                    -> scripts/warm_image_cache.py (placeholder now,
#                                  real job in 5b on the privileged EC2 pool)
#
# Every binary below is invoked by ABSOLUTE venv path (K-2): the images must not
# put /app/.venv/bin on PATH, or the framework interpreter shadows system
# python/pip for every child the harness spawns — including the agent's bash tool.
# The service processes themselves need the venv (openai, boto3...), so they use
# it explicitly; everything they spawn sees a clean system PATH.
set -e

case "${1:-}" in
  orchestrator-api)
    shift
    # main:application = the API wrapped with the /api prefix rewrite + the served UI.
    exec /app/.venv/bin/uvicorn swebench_eval.orchestrator.api.main:application \
      --host 0.0.0.0 --port "${PORT:-8000}" "$@"
    ;;
  run-supervisor)
    # CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md: the control-plane
    # SINGLETON — heartbeat + reaper rules 2/3, deliberately the only thing
    # in this process. Replaces orchestrator-control-plane.
    shift
    exec /app/.venv/bin/python -c \
      "from swebench_eval.orchestrator.control_plane.run_supervisor import run_run_supervisor; run_run_supervisor()" \
      "$@"
    ;;
  results-writer)
    # The other half of the retired orchestrator-control-plane: the results
    # consume loop, the llm_calls daemon, and the DLQ consumers (reaper rule
    # 1). N replicas — ADR-0018 already made this seam safe.
    shift
    exec /app/.venv/bin/python -c \
      "from swebench_eval.orchestrator.control_plane.results_writer import run_results_writer; run_results_writer()" \
      "$@"
    ;;
  harness-worker)
    shift
    exec /app/.venv/bin/python -c \
      "from swebench_eval.workers.harness_worker import run_harness_worker; run_harness_worker()" \
      "$@"
    ;;
  harness-worker-job)
    shift
    exec /app/.venv/bin/python -c \
      "from swebench_eval.workers.harness_worker import run_harness_worker_job; run_harness_worker_job()" \
      "$@"
    ;;
  local-job)
    # Adoption F3: ONE instance on a laptop, inside the local per-instance image
    # (Dockerfile.local-instance) — no queue, no S3 upload, artifacts to a bind
    # mount.  Args pass through (--instance-id, --model, --out, ...).
    shift
    exec /app/.venv/bin/python -m swebench_eval.workers.local_job "$@"
    ;;
  ceiling-discovery)
    # exact-design §6: the one-shot RunTask-launched discovery probe. Model
    # alias + fleet target arrive via env (RunTask containerOverrides).
    shift
    exec /app/.venv/bin/python -m swebench_eval.orchestrator.control_plane.ceiling_discovery "$@"
    ;;
  llm-judge)
    # offline-analysis-design.md §9.5/§10.4/§10.5: the one-shot RunTask-launched
    # judge pass — Pass A (leak backfill) then Pass B (the judge), same task,
    # same discipline as ceiling-discovery. Run id / instance scope / prune mode /
    # model alias / spend ceiling all arrive via env (RunTask containerOverrides).
    shift
    exec /app/.venv/bin/python -m swebench_eval.orchestrator.control_plane.llm_judge_task "$@"
    ;;
  harness-dispatcher)
    shift
    exec /app/.venv/bin/python -c \
      "from swebench_eval.orchestrator.control_plane.harness_dispatcher import run_harness_dispatcher; run_harness_dispatcher()" \
      "$@"
    ;;
  eval-worker)
    shift
    exec /app/.venv/bin/python -c \
      "from swebench_eval.workers.eval_worker import run_eval_worker; run_eval_worker()" \
      "$@"
    ;;
  warm-job)
    shift
    exec /app/.venv/bin/python /app/warm_image_cache.py "$@"
    ;;
  phase0-instances)
    # ADR-0043: the 4.1.0-era six-instance builder (build_phase0_instances.py,
    # which built env images from the harness's own scripts) is gone with the
    # harness API that generated those scripts.  Only the v2 route remains.
    echo "phase0-instances was removed (ADR-0043); use phase0-instances-v2 --base official" >&2
    exit 64
    ;;
  phase0-instances-v2)
    # builder5-image-build-tier Stage 3: the 500-scale extension (ENV_SHARD,
    # INSTANCE_MAX_WORKERS pool, resume-from-ECR-state, the disk-guard fix) —
    # a SEPARATE script (scripts/build_phase0_instances_v2.py), not a
    # replacement, so phase0-instances above keeps working exactly as before
    # for builder 1's six. Same privileged/docker-socket/EC2-pool
    # requirements as phase0-instances. Args pass through verbatim.
    shift
    exec /app/.venv/bin/python /app/scripts/build_phase0_instances_v2.py "$@"
    ;;
  create-run-rows)
    # R4.1/R4.2 (builder1-r-batch-review-2026-08-24): create the runs +
    # run_targets rows for HAND-launched dispatches / DLQ redrives (the missing
    # runs row is why eval never ran — results FK-failed and DLQ'd).  One arg
    # per run as `run_id:harness:model_alias`, e.g.
    #   create-run-rows run-a:claude_code:claude-code-model run-b:mini_swe_agent:cheap-oss-model
    # Idempotent (ON CONFLICT DO NOTHING).  Intended to be run as a one-off
    # run-task (Fargate) so the DB is reached from inside the VPC — the operator
    # laptop has no Aurora ingress.
    shift
    rc=0
    for spec in "$@"; do
      rid="${spec%%:*}"; rest="${spec#*:}"
      harness="${rest%%:*}"; alias="${rest#*:}"
      /app/.venv/bin/python /app/scripts/create_run_row.py \
        --run-id "$rid" --harness "$harness" --model-alias "$alias" || rc=$?
    done
    exit "$rc"
    ;;
  *)
    # Unknown subcommand: if args were given, exec them verbatim; otherwise
    # fail with usage.  The exec-verbatim path is what lets SWE-bench's eval
    # container (built FROM a harness image whose ENTRYPOINT is this script)
    # stay alive: run_evaluation.py starts it with `command="tail -f /dev/null"`,
    # docker runs `entrypoint.sh tail -f /dev/null`, and this branch execs the
    # tail instead of exiting (a container that exits on start → every
    # exec_run fails 409 "container is not running").  The known harness/
    # service subcommands above still dispatch normally.
    if [ $# -gt 0 ]; then
      exec "$@"
    fi
    echo "usage: entrypoint {orchestrator-api|run-supervisor|results-writer|harness-worker|harness-worker-job|harness-dispatcher|ceiling-discovery|llm-judge|eval-worker|warm-job|create-run-rows|phase0-instances|phase0-instances-v2}"
    exit 1
    ;;
esac
