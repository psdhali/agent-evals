"""Per-instance extra wheels for the offline grade (2026-09-06, pylint-4661).

The ``-inst`` image's last layer bakes ``PIP_NO_INDEX=1`` with a wheelhouse at
``/opt/wheels`` (``infra/docker/Dockerfile.instance-layer``), so the grade can
never phone PyPI.  The wheelhouse holds the repo's ``build-system.requires``
(what a PEP 517 re-install needs) — and nothing else.  That is one package
short for exactly one SWE-bench_Verified instance, ``pylint-dev__pylint-4661``:
its gold patch ADDS a runtime dependency (``appdirs>=1.4.0`` in
``install_requires``), the eval script re-runs ``pip install -e .`` after
applying the patch, pip finds no ``appdirs`` offline, and the test patch itself
``import appdirs`` — so NO patch can pass in the image as built.  Upstream
grades it with network and never notices.

``instance_extra_wheels.json`` (package data next to this module) maps an
instance id to the extra requirement strings whose wheels are downloaded into
that instance's wheelhouse at build time.  Nothing is installed into the
testbed env: the frozen environment stays exactly what the leaderboard graded
in, and the package only lands if a patch asks for it.  Every other instance
gets an empty list.
"""

from __future__ import annotations

import json
from importlib import resources

_FILE = "instance_extra_wheels.json"


def load_extra_wheels() -> dict[str, list[str]]:
    """``instance_id -> [requirement, ...]`` from the package-data file.

    Validates the shape loudly: a malformed file must fail the build, not
    silently give an instance an empty wheelhouse.
    """
    path = resources.files("swebench_eval.dataset").joinpath(_FILE)
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise TypeError(f"{_FILE}: top level must be an object")
    out: dict[str, list[str]] = {}
    for instance_id, reqs in doc.items():
        if not isinstance(instance_id, str) or "__" not in instance_id:
            raise ValueError(f"{_FILE}: {instance_id!r} is not an instance id")
        if not isinstance(reqs, list) or not reqs:
            raise ValueError(f"{_FILE}: {instance_id}: expected a non-empty list of requirements")
        for req in reqs:
            if not isinstance(req, str) or not req.strip():
                raise ValueError(f"{_FILE}: {instance_id}: bad requirement {req!r}")
        out[instance_id] = [r.strip() for r in reqs]
    return out


def extra_wheels_for(instance_id: str) -> list[str]:
    """The extra requirements for *instance_id* (``[]`` for almost every instance)."""
    return list(load_extra_wheels().get(instance_id, []))
