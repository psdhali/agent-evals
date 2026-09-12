"""Per-instance extra wheels for the offline grade (2026-09-06, pylint-4661)."""

from __future__ import annotations

import json
import sys
from importlib import resources
from pathlib import Path

import pytest

from swebench_eval.dataset import extra_wheels

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import capture_build_reqs as cbr


def test_committed_file_carries_pylint_4661_and_only_valid_shapes() -> None:
    table = extra_wheels.load_extra_wheels()
    assert table["pylint-dev__pylint-4661"] == ["appdirs>=1.4.0"]
    for instance_id, reqs in table.items():
        assert "__" in instance_id
        assert reqs and all(r.strip() == r and r for r in reqs)


def test_extra_wheels_for_is_empty_for_every_other_instance() -> None:
    assert extra_wheels.extra_wheels_for("django__django-10097") == []
    assert extra_wheels.extra_wheels_for("pylint-dev__pylint-4661") == ["appdirs>=1.4.0"]


@pytest.mark.parametrize(
    "doc",
    [
        ["not", "an", "object"],
        {"notaninstanceid": ["x"]},
        {"a__b-1": []},
        {"a__b-1": "appdirs"},
        {"a__b-1": [""]},
    ],
)
def test_malformed_file_fails_loudly(monkeypatch, tmp_path: Path, doc) -> None:
    path = tmp_path / "instance_extra_wheels.json"
    path.write_text(json.dumps(doc))

    class _Files:
        def joinpath(self, name: str) -> Path:
            return tmp_path / name

    # extra_wheels does `from importlib import resources`: patching the module object
    # itself is what the code under test sees.
    monkeypatch.setattr(resources, "files", lambda _pkg: _Files())
    with pytest.raises((ValueError, TypeError)):
        extra_wheels.load_extra_wheels()


def test_capture_appends_extra_wheels_after_build_system_requires(monkeypatch, tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=40", "wheel"]\n'
    )
    (tmp_path / "extra.txt").write_text("appdirs>=1.4.0\n\n  \n")
    out = tmp_path / "build-reqs.txt"
    monkeypatch.setattr(cbr, "PYPROJECT", tmp_path / "pyproject.toml")
    monkeypatch.setattr(cbr, "EXTRA", tmp_path / "extra.txt")
    monkeypatch.setattr(cbr, "OUT", out)
    assert cbr.main() == 0
    assert out.read_text() == "setuptools>=40\nwheel\nappdirs>=1.4.0\n"


def test_capture_without_pyproject_still_writes_the_extras(monkeypatch, tmp_path) -> None:
    (tmp_path / "extra.txt").write_text("appdirs>=1.4.0\n")
    out = tmp_path / "build-reqs.txt"
    monkeypatch.setattr(cbr, "PYPROJECT", tmp_path / "missing.toml")
    monkeypatch.setattr(cbr, "EXTRA", tmp_path / "extra.txt")
    monkeypatch.setattr(cbr, "OUT", out)
    assert cbr.main() == 0
    assert out.read_text() == "appdirs>=1.4.0\n"


def test_capture_with_neither_file_writes_an_empty_list(monkeypatch, tmp_path) -> None:
    out = tmp_path / "build-reqs.txt"
    monkeypatch.setattr(cbr, "PYPROJECT", tmp_path / "missing.toml")
    monkeypatch.setattr(cbr, "EXTRA", tmp_path / "missing.txt")
    monkeypatch.setattr(cbr, "OUT", out)
    assert cbr.main() == 0
    assert out.read_text() == ""
