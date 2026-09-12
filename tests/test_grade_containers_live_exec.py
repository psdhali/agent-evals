"""The orphan sweep must not kill a neighbour's live grade (2026-09-06, django-10097)."""

from __future__ import annotations

import time
from typing import Any

from swebench_eval.evaluation import grade_containers as gc


class _Container:
    def __init__(self, name: str, created_ago_s: float, exec_ids: list[str] | None) -> None:
        self.name = name
        created = time.time() - created_ago_s
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created))
        self.attrs: dict[str, Any] = {"Created": f"{stamp}.5Z", "ExecIDs": exec_ids}
        self.removed = 0

    def remove(self, force: bool = False) -> None:
        self.removed += 1


class _Client:
    def __init__(self, items: list[_Container]) -> None:
        self.containers = type(
            "Cs", (), {"list": lambda _s, all=False, filters=None: list(items)}
        )()


def test_sweep_skips_an_old_container_with_a_running_exec() -> None:
    live = _Container("sweb.eval.django__django-10097.abc", 67 * 60, ["exec-1"])
    orphan = _Container("sweb.eval.django__django-11099.def", 67 * 60, None)
    empty = _Container("sweb.eval.django__django-11133.ghi", 67 * 60, [])
    gc.set_current(None)
    removed = gc.sweep_orphans(_Client([live, orphan, empty]))
    assert live.removed == 0
    assert orphan.removed == 1 and empty.removed == 1
    assert sorted(removed) == sorted([orphan.name, empty.name])


def test_sweep_never_touches_this_workers_own_container_whatever_its_age() -> None:
    mine = _Container("sweb.eval.django__django-10097.abc", 5 * 3600, None)
    gc.set_current(mine.name)
    try:
        assert gc.sweep_orphans(_Client([mine])) == []
        assert mine.removed == 0
    finally:
        gc.set_current(None)
