"""builder1-aws-wiring-and-apply.md — DoD 3/4/5: the wiring that lets the e2e run.

The reviewer's finding was that NOT ONE of the four gate readers/writers could
reach Valkey (ADR-0039), and the orchestrator's abort could not enumerate/stop
tasks (no ecs:ListTasks/StopTask, no CLUSTER env). These are source assertions
on the Terraform — CI cannot plan against AWS — so they parse the rendered task
definitions the same way test_infra_warm_job_env.py does: they fail if the env
var / IAM action is removed or weakened, and a comment mentioning it does NOT
satisfy them.

DoD 3 — REDIS_URL present AND non-empty on all four of orchestrator-api,
orchestrator-control-plane, harness-dispatcher, eval-worker.  "Non-empty" is
proved by the module rendering it FROM a variable, and the env root supplying a
non-empty value (local.redis_endpoint, which composes rediss://<host>:<port>).
The empty REDIS_URL="" that shipped was a literal in ui/main.tf, not a variable
reference — that shape is what these tests forbid.

CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md: orchestrator-control-plane
is retired, replaced by run-supervisor + results-writer (each its own
terraform module, ecs-service-run-supervisor / ecs-service-results-writer) —
the REDIS_URL/CLUSTER assertions below moved with it onto those two modules;
orchestrator-api's own copy is unaffected and still checked against
ecs-service-orchestrator/main.tf directly.

DoD 4 — CLUSTER present AND non-empty on the orchestrator (both task defs).
abort._cluster() reads os.environ["CLUSTER"]; without it abort fails even with
ecs:ListTasks/StopTask granted.

DoD 5 — the ui/ fallback DEMONSTRATED, not asserted: the expression
try(data.terraform_remote_state.eval.outputs.redis_endpoint, "") yields ""
when eval/'s output is absent. test_ui_fallback_yields_empty_when_eval_down
actually RUNS that exact expression against a real local-backend remote state
that has no redis_endpoint output and asserts the result is "" — a try() that
was never exercised is not evidence (the reviewer's words). Requires the
terraform binary (skips in CI's plain-pytest job, which has none; the
terraform job validates the config itself).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_ORCH = _ROOT / "infra/terraform/modules/ecs-service-orchestrator/main.tf"
_RUN_SUPERVISOR = _ROOT / "infra/terraform/modules/ecs-service-run-supervisor/main.tf"
_RESULTS_WRITER = _ROOT / "infra/terraform/modules/ecs-service-results-writer/main.tf"
_DISPATCHER = _ROOT / "infra/terraform/modules/ecs-service-harness-dispatcher/main.tf"
_EVAL_WORKER = _ROOT / "infra/terraform/modules/ecs-service-eval-worker/main.tf"
_EVAL_TF = _ROOT / "infra/terraform/envs/dev/eval/main.tf"
_UI_TF = _ROOT / "infra/terraform/envs/dev/ui/main.tf"
_EVAL_OUTPUTS = _ROOT / "infra/terraform/envs/dev/eval/outputs.tf"


def _read(path: Path) -> str:
    assert path.exists(), f"missing terraform source: {path}"
    return path.read_text(encoding="utf-8")


def _count(src: str, needle: str) -> int:
    return src.count(needle)


# --- DoD 3: REDIS_URL present + non-empty on all four -------------------------


def test_redis_url_present_on_orchestrator_api() -> None:
    """orchestrator-api renders REDIS_URL from a var (now the module's only task def)."""
    src = _read(_ORCH)
    # The value must be a VARIABLE reference, never a literal "" — the shipped
    # bug was REDIS_URL="" in ui/main.tf (see the fallback test).
    # == 2 since 2026-09-01: the ceiling-discovery one-shot task def (exact-design §6) lives in
    # the same module and legitimately renders the same variable reference — the guarded defect
    # is a literal "", not the count itself. == 3 since 2026-09-07: the llm-judge one-shot task
    # def too (the parallel judge publishes live progress and seeds pacer:cfg:{judge-model}).
    assert _count(src, '{ name = "REDIS_URL", value = var.redis_endpoint }') == 3, (
        "orchestrator-api + ceiling-discovery + llm-judge must render REDIS_URL = "
        'var.redis_endpoint (a literal "" means the gate cannot read)'
    )


def test_redis_url_present_on_run_supervisor_and_results_writer() -> None:
    """The retired orchestrator-control-plane's REDIS_URL wiring moved onto its
    two replacements — run-supervisor (it PUBLISHES control:flags) and
    results-writer (control_state.mark_runs_active(), unconditional per
    message — see that module's own redis_endpoint variable description)."""
    for path in (_RUN_SUPERVISOR, _RESULTS_WRITER):
        src = _read(path)
        assert (
            '{ name = "REDIS_URL", value = var.redis_endpoint }' in src
        ), f"{path.name} must render REDIS_URL = var.redis_endpoint"


def test_redis_url_present_on_harness_dispatcher() -> None:
    src = _read(_DISPATCHER)
    assert (
        '{ name = "REDIS_URL", value = var.redis_endpoint }' in src
    ), "harness-dispatcher must render REDIS_URL = var.redis_endpoint (ADR-0039 gate #1)"


def test_redis_url_present_on_eval_worker() -> None:
    src = _read(_EVAL_WORKER)
    assert (
        '{ name = "REDIS_URL", value = var.redis_endpoint }' in src
    ), "eval-worker must render REDIS_URL = var.redis_endpoint (ADR-0039 gate #3)"


def test_redis_endpoint_wired_from_local_nonempty_in_eval() -> None:
    """eval/ must pass local.redis_endpoint (the composed rediss:// URL) to both
    the eval-worker and the harness-dispatcher module blocks.  That is what makes
    the variable-rendered REDIS_URL non-empty in the deployed task definitions."""
    src = _read(_EVAL_TF)
    # local.redis_endpoint composes rediss://<endpoint>:<port> — never "".
    # At least the two NEW wires (eval_worker + harness_dispatcher) must be
    # present; harness_task_families already carried it (the third occurrence).
    assert _count(src, "redis_endpoint = local.redis_endpoint") >= 2, (
        "both eval_worker and harness_dispatcher modules must receive the endpoint "
        "(harness_task_families is a pre-existing third wire)"
    )


def test_redis_endpoint_wired_in_ui_with_nonempty_fallback() -> None:
    """ui/main.tf must NOT pass a bare literal \"\"; it must read eval/'s output
    through try(..., \"\") so the endpoint is non-empty when eval/ is up and \"\"
    only when eval/ is down (ADR-0039 §Decision 1)."""
    src = _read(_UI_TF)
    assert (
        # Adoption Phase 2: the read is count-switched (read_eval_state), hence eval[0].
        'redis_endpoint = try(data.terraform_remote_state.eval[0].outputs.redis_endpoint, "")'
        in src
    ), (
        "ui/main.tf must read the endpoint from eval/'s remote state with the null-safe "
        'try(..., "") fallback — never a bare literal "" (that was the shipped bug)'
    )
    # A literal empty assignment is now forbidden.
    assert 'redis_endpoint       = ""' not in src


def test_eval_exports_the_composed_endpoint() -> None:
    """eval/outputs.tf (new) must export redis_endpoint from the local so ui/'s
    remote-state read has a key to resolve."""
    src = _read(_EVAL_OUTPUTS)
    assert 'output "redis_endpoint"' in src
    assert "local.redis_endpoint" in src


# --- DoD 4: CLUSTER present + non-empty on the orchestrator -------------------


def test_cluster_present_on_orchestrator_api() -> None:
    """abort._cluster() reads os.environ['CLUSTER']; without it abort fails even
    once ecs:ListTasks/ecs:StopTask are granted. abort runs inside
    orchestrator-api (now the module's only task def)."""
    src = _read(_ORCH)
    assert (
        _count(src, '{ name = "CLUSTER", value = var.cluster_name }') == 1
    ), "orchestrator-api must render CLUSTER = var.cluster_name (abort._cluster() reads it)"


def test_cluster_present_on_run_supervisor() -> None:
    """run-supervisor's reaper rules 2/3 (_running_instance_ids_for_run) need
    CLUSTER for their live-ECS-task DescribeTasks check — the retired
    orchestrator-control-plane's CLUSTER wiring moved here with them.
    results-writer does not call that function, so it does not need CLUSTER."""
    src = _read(_RUN_SUPERVISOR)
    assert (
        '{ name = "CLUSTER", value = var.cluster_name }' in src
    ), "run-supervisor must render CLUSTER = var.cluster_name"


# --- DoD 5: ui/ fallback demonstrated, not asserted ---------------------------


def _ui_fallback_expression() -> str:
    """Pull the exact redis_endpoint expression out of ui/main.tf."""
    src = _read(_UI_TF)
    for line in src.splitlines():
        line = line.strip()
        if line.startswith("redis_endpoint"):
            return line.partition("=")[2].strip()
    raise AssertionError("ui/main.tf has no redis_endpoint assignment")


def test_ui_fallback_source_shape() -> None:
    """The expression exists, is a try() over eval's output, and falls back to \"\"."""
    expr = _ui_fallback_expression()
    assert expr == 'try(data.terraform_remote_state.eval[0].outputs.redis_endpoint, "")'


def _write_scratch_ui(scratch: Path, eval_state_json: dict[str, object]) -> Path:
    """A minimal ui/ clone that reproduces the EXACT expression against a
    local-backend remote state whose outputs we control."""
    state_path = scratch / "eval.tfstate"
    state_path.write_text(json.dumps(eval_state_json), encoding="utf-8")
    ui_main = scratch / "ui" / "main.tf"
    ui_main.parent.mkdir(parents=True, exist_ok=True)
    ui_main.write_text(
        f"""data "terraform_remote_state" "eval" {{
  backend = "local"
  config = {{
    path = "{state_path}"
  }}
}}

output "ui_redis_endpoint" {{
  value = {_ui_fallback_expression()}
}}
""",
        encoding="utf-8",
    )
    return ui_main


def _eval_planned_output(ui_dir: Path) -> object:
    """Run `terraform plan -out` against the scratch and read the planned
    ui_redis_endpoint value — WITHOUT an apply (the brief's no-apply rule governs
    the real stacks; this scratch is a throwaway local-backend dir with zero AWS
    resources, and plan+show never writes state)."""
    plan_path = ui_dir / "demo.plan"
    subprocess.run(
        ["terraform", "plan", f"-out={plan_path}", "-input=false"],
        cwd=ui_dir,
        capture_output=True,
        check=True,
    )
    show = subprocess.run(
        ["terraform", "show", "-json", str(plan_path)],
        cwd=ui_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    plan = json.loads(show.stdout)
    return plan["planned_values"]["outputs"]["ui_redis_endpoint"]["value"]


@pytest.mark.skipif(
    shutil.which("terraform") is None,
    reason="terraform binary not on PATH (CI test job has none; the terraform job validates the config)",
)
def test_ui_fallback_yields_empty_when_eval_down() -> None:
    """DEMONSTRATION (DoD 5): the real expression, evaluated by terraform against
    a real local-backend remote state whose output is ABSENT, yields \"\".  This
    is not an assertion about the source — terraform evaluates the expression."""
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        _write_scratch_ui(scratch, {"version": 4, "terraform_version": "1.11.3", "outputs": {}})
        ui_dir = scratch / "ui"
        subprocess.run(
            ["terraform", "init", "-input=false", "-lockfile=readonly"],
            cwd=ui_dir,
            capture_output=True,
            check=True,
        )
        assert (
            _eval_planned_output(ui_dir) == ""
        ), 'the try(..., "") expression must yield "" when eval/\'s output is absent'


@pytest.mark.skipif(
    shutil.which("terraform") is None,
    reason="terraform binary not on PATH (CI test job has none)",
)
def test_ui_fallback_yields_endpoint_when_eval_up() -> None:
    """The same expression yields the real endpoint when eval/'s output IS present."""
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        _write_scratch_ui(
            scratch,
            {
                "version": 4,
                "terraform_version": "1.11.3",
                "outputs": {
                    "redis_endpoint": {"value": "rediss://cache.example:6379", "type": "string"}
                },
            },
        )
        ui_dir = scratch / "ui"
        subprocess.run(
            ["terraform", "init", "-input=false", "-lockfile=readonly"],
            cwd=ui_dir,
            capture_output=True,
            check=True,
        )
        assert _eval_planned_output(ui_dir) == "rediss://cache.example:6379"


# --- The IAM seams the brief calls out (cheap source guards) -------------------


def test_dispatcher_listtasks_uses_condition_form() -> None:
    """#2: ecs:ListTasks was scoped to a cluster ARN that IAM never matches
    (the resource type is container-instance).  The preferred tighter form is
    resources = ['*'] + a condition on ecs:cluster (observability pattern)."""
    src = _read(_DISPATCHER)
    assert 'actions = ["ecs:ListTasks"]' in src
    assert 'variable = "ecs:cluster"' in src
    assert 'resources = ["*"]' in src


def test_orchestrator_has_abort_and_dlq_and_metric_perms() -> None:
    """#5/#8/#10: abort list/stop, DLQ depth read, and oldest_age_s metric read."""
    src = _read(_ORCH)
    # fmt aligns 'actions' with spaces; match the tokens, not the exact padding.
    assert '"ecs:ListTasks"' in src and '"ecs:StopTask"' in src, "#5 abort list/stop"
    assert 'variable = "ecs:cluster"' in src, "#5 cluster-scoped"
    assert "var.queue_dlq_arns.harness" in src, "#8 DLQ read"
    assert 'sid     = "SqsDlqRead"' in src, "#8 named"
    assert "cloudwatch:GetMetricStatistics" in src, "#10 metric read"


def test_orchestrator_artifacts_can_put() -> None:
    """#7: the abort drain manifest is WRITTEN to the artifacts bucket."""
    src = _read(_ORCH)
    assert (
        'actions = ["s3:GetObject", "s3:ListBucket", "s3:PutObject"]' in src
    ), "#7 ArtifactsRead must include s3:PutObject (abort drain manifest)"


def test_persistent_exports_dlq_arns() -> None:
    """#9: the persistent root forwards the sqs module's dlq_arns output."""
    src = _read(_ROOT / "infra/terraform/envs/dev/persistent/main.tf")
    assert 'output "dlq_arns"' in src
    assert "module.sqs.dlq_arns" in src


def test_warm_job_attests_gateway_config_hash() -> None:
    """§4: the -hw image must carry the gateway config hash it was built against."""
    warm = _read(_ROOT / "infra/terraform/modules/ec2-task-warm-job/main.tf")
    assert '{ name = "GATEWAY_CONFIG_HASH", value = var.gateway_config_hash }' in warm
    eval_src = _read(_EVAL_TF)
    assert "gateway_config_hash = filesha256" in eval_src
