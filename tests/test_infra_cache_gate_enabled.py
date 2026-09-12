"""ADR-0030 review R1 — the 5b cache gate is actually ON in the deployed dispatch path.

``check_cache_precondition`` defaults OFF in code (local/dev must not be
gated), which is exactly how the gate went years without firing in AWS: the
switch was never in the Terraform. So the deployable task definitions must
carry ``ENFORCE_CACHE_GATE=1``, and that enablement must itself be asserted —
a gate whose enablement is not tested is how this recurs.

These are source assertions on the Terraform (CI cannot plan against AWS): they
fail if someone removes or weakens the env var from the dispatch Lambda or the
orchestrator-api task definition. A comment mentioning the gate must not
satisfy them — only the actual ``= "1"`` / ``value = "1"`` assignment does.

CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md: the gate is read by
``dispatcher.py`` (``register_run``'s warm-cache precondition check) and
``restart.py`` — both run inside orchestrator-api, never inside the retired
orchestrator-control-plane's replacements (run-supervisor, results-writer),
which never call either. This file used to also assert the retired "control"
task definition carried the var; that assertion is dropped, not relocated —
it was never actually load-bearing for those two new services.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DISPATCH = _ROOT / "infra/terraform/modules/dispatch-lambda/main.tf"
_ORCHESTRATOR = _ROOT / "infra/terraform/modules/ecs-service-orchestrator/main.tf"

# The ECS task-definition container-environment form:
#   { name = "ENFORCE_CACHE_GATE", value = "1" }
_ECS_ENV = '{ name = "ENFORCE_CACHE_GATE", value = "1" }'


def _read(path: Path) -> str:
    assert path.exists(), f"missing terraform source: {path}"
    return path.read_text(encoding="utf-8")


def test_dispatch_lambda_enables_the_gate() -> None:
    """The S3-triggered dispatch Lambda (the current dispatch_run path) is ON."""
    src = _read(_DISPATCH)
    # The Lambda's environment block uses variables, not the ECS list form.
    assert re.search(
        r'ENFORCE_CACHE_GATE\s*=\s*"1"', src
    ), 'dispatch-lambda must set ENFORCE_CACHE_GATE = "1" in its environment'


def test_orchestrator_api_enables_the_gate() -> None:
    """orchestrator-api (register_run's dispatch path, restart.py) carries the
    gate. The module now has only ONE task definition (the retired
    orchestrator-control-plane's replacements never read this var)."""
    src = _read(_ORCHESTRATOR)
    assert _ECS_ENV in src, "orchestrator-api task definition is ungated"


def test_gate_value_is_not_weakened_to_off() -> None:
    """Guard against a half-enabled gate: any '0'/empty assignment anywhere in
    the dispatch path would silently disable it."""
    for path in (_DISPATCH, _ORCHESTRATOR):
        src = _read(path)
        assert not re.search(
            r'ENFORCE_CACHE_GATE[^\n]*=\s*"0"', src
        ), f'{path.name} sets ENFORCE_CACHE_GATE to "0"'
        # The only mentions must be the enabled form (plus comments).
        mentions = re.findall(r"ENFORCE_CACHE_GATE\s*=\s*\"(\d)\"", src) + re.findall(
            r'ENFORCE_CACHE_GATE"\s*,\s*value\s*=\s*"(\d)"', src
        )
        assert mentions, f"{path.name} must reference ENFORCE_CACHE_GATE"
        assert all(v == "1" for v in mentions), f"{path.name} weakens the gate"
