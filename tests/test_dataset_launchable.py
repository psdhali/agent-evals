"""GET /dataset/instances — the `launchable` flag (round 2, item 7; family gate 2026-09-05).

Unit tests only: stub ECR + ECS clients and a monkeypatched dataset loader, no
real AWS or dataset mirror needed.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from swebench_eval.orchestrator.api import run_launch_routes


class _StubInstance:
    def __init__(self, instance_id: str, repo: str = "x/y") -> None:
        self.instance_id = instance_id
        self.repo = repo


class _StubLoader:
    def __init__(self, instances: list[_StubInstance]) -> None:
        self._instances = instances

    def load(self) -> list[_StubInstance]:
        return self._instances


class _StubECR:
    """One page of imageDetails, each carrying a list of tags — matches the
    real describe_images(repositoryName=...) shape closely enough for the
    filter logic under test."""

    def __init__(self, tag_pages: list[list[str]]) -> None:
        self._pages = tag_pages
        self._i = 0

    def describe_images(self, **kwargs: Any) -> dict[str, Any]:
        if self._i >= len(self._pages):
            return {"imageDetails": []}
        tags = self._pages[self._i]
        self._i += 1
        token = str(self._i) if self._i < len(self._pages) else None
        return {
            "imageDetails": [{"imageTags": [t]} for t in tags],
            **({"nextToken": token} if token else {}),
        }


class _StubECS:
    """Pages of task-definition family names — the real
    list_task_definition_families(familyPrefix=..., status=ACTIVE) shape."""

    def __init__(self, family_pages: list[list[str]]) -> None:
        self._pages = family_pages
        self._i = 0
        self.calls: list[dict[str, Any]] = []

    def list_task_definition_families(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._i >= len(self._pages):
            return {"families": []}
        families = self._pages[self._i]
        self._i += 1
        token = str(self._i) if self._i < len(self._pages) else None
        return {"families": families, **({"nextToken": token} if token else {})}


def _clients(ecr: Any, ecs: Any):
    """boto3.client patched by service name — both lookups run per request."""
    return mock.patch(
        "boto3.client",
        side_effect=lambda service, **kw: {"ecr": ecr, "ecs": ecs}[service],
    )


def _families(*instance_ids: str) -> list[str]:
    return [f"eval-dev-harness-{i}" for i in instance_ids]


def test_launchable_instance_ids_filters_by_version_and_suffix(monkeypatch) -> None:
    monkeypatch.setenv("SWEBENCH_VERSION", "4.1.0")
    ecr = _StubECR(
        [
            [
                "4.1.0-scikit-learn__scikit-learn-25102-inst",
                "4.1.0-scikit-learn__scikit-learn-25102-hw",  # -hw, not -inst: excluded
                "latest",  # no version prefix: excluded
                "3.9.0-django__django-11099-inst",  # wrong version: excluded
            ]
        ]
    )
    ecs = _StubECS([_families("scikit-learn__scikit-learn-25102", "django__django-11099")])
    with _clients(ecr, ecs):
        ids = run_launch_routes._launchable_instance_ids()
    assert ids == {"scikit-learn__scikit-learn-25102"}


def test_launchable_default_version_is_the_shared_5x_constant(monkeypatch) -> None:
    """2026-09-06: with SWEBENCH_VERSION unset (no task-def sets it) the check
    must count the ``5.0.2-…-inst`` images, not the 41 leftover ``4.1.0`` revert
    tags — the launch screen showed 41 of 500 launchable because this default
    was still "4.1.0" after ADR-0043."""
    from swebench_eval.evaluation.env_image import DEFAULT_SWEBENCH_VERSION

    monkeypatch.delenv("SWEBENCH_VERSION", raising=False)
    assert DEFAULT_SWEBENCH_VERSION == "5.0.2"
    ecr = _StubECR(
        [
            [
                "5.0.2-pylint-dev__pylint-4661-inst",
                "4.1.0-scikit-learn__scikit-learn-25102-inst",  # old revert-path tag: excluded
            ]
        ]
    )
    ecs = _StubECS([_families("pylint-dev__pylint-4661", "scikit-learn__scikit-learn-25102")])
    with _clients(ecr, ecs):
        ids = run_launch_routes._launchable_instance_ids()
    assert ids == {"pylint-dev__pylint-4661"}


def test_launchable_instance_ids_paginates(monkeypatch) -> None:
    monkeypatch.setenv("SWEBENCH_VERSION", "4.1.0")
    ecr = _StubECR([["4.1.0-inst__a-inst"], ["4.1.0-inst__b-inst"]])
    ecs = _StubECS([_families("inst__a"), _families("inst__b")])
    with _clients(ecr, ecs):
        ids = run_launch_routes._launchable_instance_ids()
    assert ids == {"inst__a", "inst__b"}
    assert ecs.calls[0]["familyPrefix"] == "eval-dev-harness-"
    assert ecs.calls[0]["status"] == "ACTIVE"
    assert ecs.calls[1]["nextToken"] == "1"


def test_launchable_requires_the_per_instance_task_family(monkeypatch) -> None:
    """2026-09-05: an -inst image with NO per-instance family is exactly what
    crash-looped 28 matplotlib jobs (dispatcher fell back to the -hw env-hash
    family). Image alone must not read as launchable; env-hash families do not
    count as per-instance."""
    monkeypatch.setenv("SWEBENCH_VERSION", "4.1.0")
    ecr = _StubECR(
        [
            [
                "4.1.0-matplotlib__matplotlib-20488-inst",  # image + family -> launchable
                "4.1.0-matplotlib__matplotlib-24026-inst",  # image, NO family -> not
            ]
        ]
    )
    ecs = _StubECS(
        [
            _families("matplotlib__matplotlib-20488")
            + ["eval-dev-harness-31244378a92e3bcce809ac"]  # env-hash family: ignored
            + _families("django__django-10097")  # family, no image -> not
        ]
    )
    with _clients(ecr, ecs):
        ids = run_launch_routes._launchable_instance_ids()
    assert ids == {"matplotlib__matplotlib-20488"}


def test_launchable_instance_ids_fails_open_on_ecr_error() -> None:
    """An ECR/AWS error must not 500 the whole dataset listing — empty set,
    never a fabricated positive."""
    ecr = mock.Mock()
    ecr.describe_images.side_effect = RuntimeError("boom")
    ecs = _StubECS([_families("inst__a")])
    with _clients(ecr, ecs):
        ids = run_launch_routes._launchable_instance_ids()
    assert ids == set()


def test_launchable_instance_ids_fails_open_on_ecs_error(monkeypatch) -> None:
    """Same rule on the family side: unknown is never rendered as launchable."""
    monkeypatch.setenv("SWEBENCH_VERSION", "4.1.0")
    ecr = _StubECR([["4.1.0-inst__a-inst"]])
    ecs = mock.Mock()
    ecs.list_task_definition_families.side_effect = RuntimeError("boom")
    with _clients(ecr, ecs):
        ids = run_launch_routes._launchable_instance_ids()
    assert ids == set()


def test_dataset_instances_marks_launchable_items(monkeypatch) -> None:
    monkeypatch.setenv("SWEBENCH_VERSION", "4.1.0")
    instances = [
        _StubInstance("inst__launchable"),
        _StubInstance("inst__image-only"),
        _StubInstance("inst__none"),
    ]
    ecr = _StubECR([["4.1.0-inst__launchable-inst", "4.1.0-inst__image-only-inst"]])
    ecs = _StubECS([_families("inst__launchable")])

    with (
        mock.patch(
            "swebench_eval.dataset.swebench_loader.SwebenchLiteLoader",
            return_value=_StubLoader(instances),
        ),
        _clients(ecr, ecs),
    ):
        resp = run_launch_routes.dataset_instances()

    by_id = {item.instance_id: item.launchable for item in resp.items}
    assert by_id == {"inst__launchable": True, "inst__image-only": False, "inst__none": False}
