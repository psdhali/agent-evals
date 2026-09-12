"""Grading-container resource limits (EVAL-GRADE-RESOURCE-LIMITS §3.2, BUILDER4-EVAL-PACKING).

The limit is applied on the docker client's ``api.create_container`` seam — the one call
every container created through the client passes in docker-py ≥ 7.  These tests drive that
seam with a fake API and assert the HostConfig SWE-bench's create ends up sending.
"""

from __future__ import annotations

from typing import Any

import pytest

from swebench_eval.evaluation import grade_limits as gl

MiB = 2**20


class _Api:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_host_config(self, **kwargs: Any) -> dict[str, Any]:
        return dict(kwargs)

    def create_container(self, *args: Any, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"Id": "c1"}


class _Client:
    def __init__(self) -> None:
        self.api = _Api()


# -- env parsing ---------------------------------------------------------------------------


def test_default_is_3_gib_and_no_cpu_cap() -> None:
    limits = gl.limits_from_env({})
    assert limits.mem_limit_bytes == 3072 * MiB
    assert limits.nano_cpus is None
    assert limits.enabled
    assert limits.describe() == "mem_limit=3072 MiB cpu_limit=none"


def test_env_overrides_and_zero_disables() -> None:
    assert gl.limits_from_env({"EVAL_GRADE_MEM_LIMIT_MB": "2048"}).mem_limit_bytes == 2048 * MiB
    off = gl.limits_from_env({"EVAL_GRADE_MEM_LIMIT_MB": "0"})
    assert off.mem_limit_bytes == 0 and not off.enabled
    cpus = gl.limits_from_env({"EVAL_GRADE_CPUS": "2.5"})
    assert cpus.nano_cpus == 2_500_000_000
    assert cpus.describe() == "mem_limit=3072 MiB cpu_limit=2.5 cpus"


@pytest.mark.parametrize(
    "env",
    [
        {"EVAL_GRADE_MEM_LIMIT_MB": "lots"},
        {"EVAL_GRADE_MEM_LIMIT_MB": "-1"},
        {"EVAL_GRADE_CPUS": "0"},
        {"EVAL_GRADE_CPUS": "two"},
    ],
)
def test_malformed_env_raises_rather_than_silently_running_unlimited(env: dict[str, str]) -> None:
    with pytest.raises(gl.GradeLimitError):
        gl.limits_from_env(env)


# -- the seam --------------------------------------------------------------------------------


def test_limits_land_in_host_config_of_a_swebench_style_create() -> None:
    client = _Client()
    applied = gl.apply_grade_limits(client, gl.limits_from_env({}))
    assert applied.mem_limit_bytes == 3072 * MiB
    # SWE-bench's create: docker-py builds host_config from cap_add and passes it through.
    client.api.create_container(
        image="img", name="sweb.eval.x", host_config={"CapAdd": ["SYS_PTRACE"]}
    )
    (call,) = client.api.calls
    hc = call["host_config"]
    assert hc["CapAdd"] == ["SYS_PTRACE"]  # SWE-bench's own config untouched
    assert hc["Memory"] == 3072 * MiB
    assert hc["MemorySwap"] == 3072 * MiB  # swap pinned: killed at the limit, never thrashing
    assert "NanoCpus" not in hc  # no CPU cap by default


def test_host_config_is_created_when_absent_and_cpu_cap_applies_when_set() -> None:
    client = _Client()
    gl.apply_grade_limits(
        client, gl.GradeLimits(mem_limit_bytes=2 * 1024 * MiB, nano_cpus=4 * 10**9)
    )
    client.api.create_container(image="img")
    hc = client.api.calls[0]["host_config"]
    assert hc == {"Memory": 2048 * MiB, "MemorySwap": 2048 * MiB, "NanoCpus": 4 * 10**9}


def test_existing_keys_win_over_ours() -> None:
    client = _Client()
    gl.apply_grade_limits(client, gl.limits_from_env({}))
    client.api.create_container(image="img", host_config={"Memory": 1 * MiB})
    hc = client.api.calls[0]["host_config"]
    assert hc["Memory"] == 1 * MiB  # a more specific caller value is respected
    assert hc["MemorySwap"] == 3072 * MiB


def test_disabled_limits_leave_the_client_untouched() -> None:
    client = _Client()
    original = client.api.create_container
    gl.apply_grade_limits(client, gl.GradeLimits(mem_limit_bytes=0, nano_cpus=None))
    assert client.api.create_container == original  # bound methods: == not is


def test_apply_is_idempotent_per_client() -> None:
    client = _Client()
    gl.apply_grade_limits(client, gl.limits_from_env({}))
    wrapped = client.api.create_container
    gl.apply_grade_limits(client, gl.limits_from_env({}))
    assert client.api.create_container is wrapped
    client.api.create_container(image="img")
    assert client.api.calls[0]["host_config"]["Memory"] == 3072 * MiB


def test_client_without_the_seam_refuses_to_grade_unlimited() -> None:
    class _NoApi:
        pass

    with pytest.raises(gl.GradeLimitError):
        gl.apply_grade_limits(_NoApi(), gl.limits_from_env({}))


def test_runner_applies_limits_to_the_client_it_grades_with(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """grade() applies the limits on the docker client it hands to run_instance — the
    OOM-detection grade path, with the limits call recorded."""
    from swebench.harness.constants import END_TEST_OUTPUT, START_TEST_OUTPUT

    from swebench_eval.evaluation import swebench_runner as sr
    from tests.test_oom_detection import _grade

    seen: list[Any] = []

    def _record(client: Any) -> gl.GradeLimits:
        seen.append(client)
        return gl.apply_grade_limits(client, gl.limits_from_env({}))

    monkeypatch.setattr(sr, "apply_grade_limits", _record)
    output = _grade(
        tmp_path,
        monkeypatch,
        oom_events=0,
        test_output=f"+ : '{START_TEST_OUTPUT}'\ntest_a PASSED\n+ : '{END_TEST_OUTPUT}'\n",
    )
    assert output.oom_killed is False
    (client,) = seen
    assert getattr(client.api.create_container, "_grade_limits_applied", False) is True
