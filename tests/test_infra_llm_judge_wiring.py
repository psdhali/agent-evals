"""offline-analysis-design.md §9.5/§10.4 — the llm-judge one-shot ECS task,
wired the same way ceiling-discovery is (test_infra_aws_wiring.py's own
pattern: static string assertions over the terraform source, no `terraform
plan` needed for these checks — that needs real AWS credentials this suite
doesn't have).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ORCH_TF = _ROOT / "infra/terraform/modules/ecs-service-orchestrator/main.tf"
_ENTRYPOINT = _ROOT / "infra/docker/entrypoint.sh"


def _read(path: Path) -> str:
    assert path.exists(), f"missing source: {path}"
    return path.read_text(encoding="utf-8")


def test_llm_judge_task_definition_exists() -> None:
    src = _read(_ORCH_TF)
    assert 'resource "aws_ecs_task_definition" "llm_judge"' in src
    assert '"${var.name_prefix}-llm-judge"' in src


def test_llm_judge_task_has_redis_but_never_touches_control_state() -> None:
    """§10.4's invariant is zero coupling to any run's CONTROL PLANE — no SQS, no reaper, no
    control_state. Until 2026-09-07 that was enforced by rendering no REDIS_URL at all; the
    parallel judge needs Redis for two judge-only things (its live-progress snapshot
    ``judge:live:{run_id}`` and seeding ``pacer:cfg:{judge-model}`` from the pool, without
    which the gateway pacer's defaults cap a pass at ~10 in flight). So: REDIS_URL renders
    from the variable (never a literal ""), and the judge code path still imports nothing
    from the control-state module."""
    src = _read(_ORCH_TF)
    start = src.index('resource "aws_ecs_task_definition" "llm_judge"')
    end = src.index("\n}\n", start)
    block = src[start:end]
    assert '{ name = "REDIS_URL", value = var.redis_endpoint }' in block
    assert 'REDIS_URL", value = ""' not in block

    root = Path(__file__).resolve().parents[1] / "swebench_eval"
    judge_sources = [
        root / "analysis" / "judge.py",
        root / "analysis" / "judge_live.py",
        root / "orchestrator" / "control_plane" / "llm_judge_task.py",
    ]
    for path in judge_sources:
        text = path.read_text(encoding="utf-8")
        assert "control_state" not in text and "swebench_eval.control" not in text, path.name


def test_llm_judge_task_has_the_dependencies_it_actually_needs() -> None:
    src = _read(_ORCH_TF)
    start = src.index('resource "aws_ecs_task_definition" "llm_judge"')
    end = src.index("\n}\n", start)
    block = src[start:end]
    for needle in (
        "ARTIFACTS_BUCKET",
        "LITELLM_BASE_URL",
        "OPENROUTER_MANAGEMENT_SECRET_ARN",
        "DATABASE_URL",
        "LITELLM_MASTER_KEY",
    ):
        assert needle in block, f"llm-judge task def missing {needle}"


def test_orchestrator_api_can_runtask_the_judge_family_scoped_to_this_cluster() -> None:
    src = _read(_ORCH_TF)
    assert 'sid       = "EcsRunJudgeTask"' in src
    assert (
        "task-definition/${var.name_prefix}-llm-judge:*" in src
    ), 'RunTask IAM must be scoped to the judge family, not resources = ["*"]'


def test_orchestrator_api_env_carries_the_judge_task_family_wiring() -> None:
    src = _read(_ORCH_TF)
    assert '{ name = "JUDGE_TASK_FAMILY", value = "${var.name_prefix}-llm-judge" }' in src
    assert '{ name = "JUDGE_SUBNET_IDS"' in src
    assert '{ name = "JUDGE_SECURITY_GROUP_IDS"' in src


def test_entrypoint_dispatches_llm_judge_subcommand() -> None:
    src = _read(_ENTRYPOINT)
    assert "llm-judge)" in src
    assert "swebench_eval.orchestrator.control_plane.llm_judge_task" in src
    assert "llm-judge" in src.split("usage: entrypoint")[1].splitlines()[0]
