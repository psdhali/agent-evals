"""Adoption Phase 1a guard: no deployment literal (region, account id, name prefix)
outside swebench_eval/aws_names.py.

The region ``us-west-2``, the account id and the ``eval-dev`` prefix used to sit in
~60 files; a fresh account or a second region meant a tree-wide edit.  They now
come from ``swebench_eval.aws_names`` (env-driven: AWS_REGION / EVAL_ENV_PREFIX /
AWS_ACCOUNT_ID or STS).  This test walks every string constant in the package and
the scripts so a literal cannot creep back in.  Comments and docstrings are not
checked (they may name examples); code strings are.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN = [ROOT / "swebench_eval", ROOT / "scripts"]
ALLOW = {
    "swebench_eval/aws_names.py",  # the one place the defaults live
    "scripts/generate_timeline_fixtures.py",  # synthetic fixtures with a fake account id
}
ACCOUNT_ID = "123456789012"
LITERALS = ("us-west-2", ACCOUNT_ID)
PREFIX = "eval-dev"


def _code_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Every str constant that is not a docstring."""
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            out.append((node.lineno, node.value))
    return out


def _offenders() -> list[str]:
    found: list[str] = []
    for base in SCAN:
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOW:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            for lineno, value in _code_strings(tree):
                if any(lit in value for lit in LITERALS) or value.startswith(PREFIX + "-"):
                    found.append(f"{rel}:{lineno}: {value[:80]!r}")
    return found


def test_no_hardcoded_region_account_or_prefix() -> None:
    offenders = _offenders()
    assert not offenders, (
        "deployment literals in code strings — read them from swebench_eval.aws_names "
        "(region() / name_prefix() / named() / account_id()) instead:\n" + "\n".join(offenders)
    )


TF_ROOTS = [ROOT / "infra" / "terraform" / "modules", ROOT / "infra" / "terraform" / "envs" / "dev"]


def _tf_offenders() -> list[str]:
    """Terraform: no literal region / account / prefix outside comments, descriptions and the
    settings.tf defaults (Phase 2 finding 12: five modules still carried
    ``SQS_QUEUE_PREFIX = "eval-dev-"`` after the root sweep)."""
    found: list[str] = []
    for base in TF_ROOTS:
        for path in sorted(base.rglob("*.tf")):
            rel = path.relative_to(ROOT).as_posix()
            if path.name == "settings.tf":
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if code.lstrip().startswith(("*", "/*", "description")):
                    continue
                if (
                    any(lit in code for lit in LITERALS)
                    or f'"{PREFIX}-' in code
                    or f'"{PREFIX}"' in code
                ):
                    found.append(f"{rel}:{lineno}: {line.strip()[:100]}")
    return found


def test_no_hardcoded_literals_in_terraform() -> None:
    offenders = _tf_offenders()
    assert not offenders, (
        "deployment literals in Terraform — use var.region / var.name_prefix / "
        "data.aws_region.current / data.aws_caller_identity.current:\n" + "\n".join(offenders)
    )


def test_guard_catches_a_literal(tmp_path: Path) -> None:
    """The guard must actually trip (DoD: prove the check can fail)."""
    tree = ast.parse('X = "eval-dev-cluster"\nY = f"{1}"\n"""doc"""\n')
    strings = [v for _, v in _code_strings(tree)]
    assert "eval-dev-cluster" in strings
