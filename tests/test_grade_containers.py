"""Ownership of the grading container after the worker is gone (eval scaling review F1)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from swebench_eval.evaluation import grade_containers as gc


class _Container:
    def __init__(self, name: str, created_ago_s: float, fail_remove: bool = False) -> None:
        self.name = name
        created = time.time() - created_ago_s
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created))
        self.attrs = {"Created": f"{stamp}.123456789Z"}  # docker's nanosecond RFC 3339
        self.removed: list[bool] = []
        self._fail = fail_remove

    def remove(self, force: bool = False) -> None:
        if self._fail:
            raise RuntimeError("daemon says no")
        self.removed.append(force)


class _Containers:
    def __init__(self, items: list[_Container], list_fails: bool = False) -> None:
        self.items = items
        self._list_fails = list_fails
        self.list_calls: list[dict[str, Any]] = []

    def list(self, all: bool = False, filters: dict[str, Any] | None = None) -> list[_Container]:
        if self._list_fails:
            raise RuntimeError("no daemon")
        self.list_calls.append({"all": all, "filters": filters})
        return list(self.items)

    def get(self, name: str) -> _Container:
        for c in self.items:
            if c.name == name:
                return c
        raise KeyError(name)


class _Client:
    def __init__(self, items: list[_Container], list_fails: bool = False) -> None:
        self.containers = _Containers(items, list_fails)


@pytest.fixture(autouse=True)
def _clear_registry() -> Any:
    gc.set_current(None)
    yield
    gc.set_current(None)


# -- the registered container ----------------------------------------------------------------


def test_remove_current_force_removes_the_registered_container_and_clears() -> None:
    mine = _Container("sweb.eval.django__django-1.abcd1234", 60)
    client = _Client([mine])
    gc.set_current(mine.name)
    assert gc.remove_current(client) is True
    assert mine.removed == [True]  # force
    assert gc.current() is None


def test_remove_current_treats_not_found_as_already_gone() -> None:
    gc.set_current("sweb.eval.gone.0000")
    assert gc.remove_current(_Client([])) is True
    assert gc.current() is None


def test_remove_current_with_nothing_registered_is_a_noop() -> None:
    assert gc.remove_current(_Client([])) is False


def test_remove_current_failure_is_false_and_keeps_the_name_for_the_sweep() -> None:
    mine = _Container("sweb.eval.x.1", 60, fail_remove=True)
    gc.set_current(mine.name)
    assert gc.remove_current(_Client([mine])) is False
    assert gc.current() == mine.name


# -- the sweep -------------------------------------------------------------------------------


def test_sweep_removes_only_old_grading_containers() -> None:
    old = _Container("sweb.eval.astropy__astropy-1.aaaa", 2 * 3600)
    young = _Container("sweb.eval.django__django-1.bbbb", 5 * 60)  # a live grade elsewhere
    other = _Container("some-other-sweb.eval.lookalike", 2 * 3600)  # substring match, not ours
    client = _Client([old, young, other])
    removed = gc.sweep_orphans(client)
    assert removed == [old.name]
    assert old.removed == [True]
    assert young.removed == [] and other.removed == []
    assert client.containers.list_calls == [{"all": True, "filters": {"name": "sweb.eval."}}]


def test_sweep_age_threshold_is_older_than_any_legitimate_grade() -> None:
    assert gc.ORPHAN_AGE_S >= 45 * 60  # > the 30-min SWE-bench test timeout + a pull
    borderline = _Container("sweb.eval.x.1", gc.ORPHAN_AGE_S - 60)
    assert gc.sweep_orphans(_Client([borderline])) == []


def test_sweep_never_raises_on_a_dead_daemon_or_a_stubborn_container() -> None:
    assert gc.sweep_orphans(_Client([], list_fails=True)) == []
    stubborn = _Container("sweb.eval.x.1", 2 * 3600, fail_remove=True)
    assert gc.sweep_orphans(_Client([stubborn])) == []


def test_created_parser_handles_docker_nanoseconds_and_bad_input() -> None:
    from datetime import UTC, datetime

    ten = datetime(2026, 9, 3, 10, 0, 0, tzinfo=UTC).timestamp()
    assert gc._parse_created("2026-09-03T10:00:00.123456789Z") == pytest.approx(
        ten + 0.123456, abs=1e-3
    )
    assert gc._parse_created("2026-09-03T10:00:00Z") == pytest.approx(ten)
    assert gc._parse_created("") is None
    assert gc._parse_created("yesterday") is None


# -- the runner registers what it grades in ---------------------------------------------------


def test_runner_registers_the_container_for_the_duration_of_run_instance(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swebench.harness.constants import END_TEST_OUTPUT, START_TEST_OUTPUT

    from tests.test_oom_detection import _grade

    seen: list[str | None] = []
    real_set = gc.set_current

    def _set(name: str | None) -> None:
        seen.append(name)
        real_set(name)

    monkeypatch.setattr(gc, "set_current", _set)
    output = _grade(
        tmp_path,
        monkeypatch,
        oom_events=0,
        test_output=f"+ : '{START_TEST_OUTPUT}'\ntest_a PASSED\n+ : '{END_TEST_OUTPUT}'\n",
    )
    assert output.oom_killed is False
    assert len(seen) == 2
    assert seen[0] is not None and seen[0].startswith("sweb.eval.")
    assert seen[1] is None
    assert gc.current() is None
