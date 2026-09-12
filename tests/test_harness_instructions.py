"""2026-09-09 efficiency prompt arm — operator instructions on the task prompt.

Flow (corrected after smoke run f31723b4): launch records the text in config_snapshot and
publishes it to Redis under the run id; the harness WORKER reads it once per job and
appends it to the problem statement every adapter frames on top of. The dispatcher's job
payload does NOT carry it — on the deployed path the worker rebuilds the statement from the
dataset mirror row (ADR-0032), which is exactly how the first version silently did nothing."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swebench_eval.database import redis_client
from swebench_eval.dataset.base import Instance
from swebench_eval.harnesses.task_framing import (
    INSTRUCTIONS_HEADING,
    framed,
    with_harness_instructions,
)
from swebench_eval.orchestrator import harness_instructions as hi
from swebench_eval.orchestrator.api.main import app
from swebench_eval.orchestrator.api.schemas import RunLaunchRequest
from swebench_eval.orchestrator.control_plane import dispatcher
from swebench_eval.orchestrator.harness_instructions import (
    HARNESS_INSTRUCTIONS_MAX_CHARS,
    OPENCODE_LAGUNA_EFFICIENCY_V1,
    OPENCODE_LAGUNA_EFFICIENCY_V2,
    PRESETS,
)
from swebench_eval.orchestrator.run_config import RunConfig
from swebench_eval.orchestrator.s3_dispatch import _parse_run_config
from swebench_eval.queue.schemas import HarnessJob
from swebench_eval.workers import harness_worker

_STATEMENT = "UsernameValidator allows trailing newline in usernames\nDescription\n..."


# ---- the text itself ----------------------------------------------------------------------


def test_no_instructions_leaves_the_statement_byte_identical() -> None:
    assert with_harness_instructions(_STATEMENT, None) == _STATEMENT
    assert with_harness_instructions(_STATEMENT, "") == _STATEMENT
    assert with_harness_instructions(_STATEMENT, "   \n") == _STATEMENT


def test_instructions_follow_the_statement_under_the_fixed_heading() -> None:
    out = with_harness_instructions(_STATEMENT + "\n\n", "1. Locate, then read windows.")
    assert out.startswith(_STATEMENT)
    assert f"\n\n{INSTRUCTIONS_HEADING}\n1. Locate, then read windows.\n" in out


def test_framing_then_issue_then_rules_is_the_order_the_model_sees() -> None:
    """The adapter frames on top of what the worker hands it: framing, Issue, statement,
    rules — the rules are the last thing in the user turn."""
    prompt = framed(with_harness_instructions(_STATEMENT, "1. Rule."))
    assert prompt.index("text-only") < prompt.index("Issue:") < prompt.index(_STATEMENT)
    assert prompt.index(_STATEMENT) < prompt.index(INSTRUCTIONS_HEADING)
    assert prompt.rstrip().endswith("1. Rule.")


@pytest.mark.parametrize("text", [OPENCODE_LAGUNA_EFFICIENCY_V1, OPENCODE_LAGUNA_EFFICIENCY_V2])
def test_preset_is_within_the_cap_and_names_the_pilot_findings(text: str) -> None:
    assert len(text) <= HARNESS_INSTRUCTIONS_MAX_CHARS
    assert "offset and limit" in text  # unbounded reads
    assert "Never re-read" in text  # repeated reads
    assert "runtests.py" in text and "settings.configure()" in text  # Django runner / probing
    assert "then stop" in text  # the post-green tail
    assert [ln[:2] for ln in text.splitlines() if ln[:1].isdigit()] == [
        "1.",
        "2.",
        "3.",
        "4.",
        "5.",
    ]


def test_v2_rewords_only_rule_4_and_is_served_first() -> None:
    """v2 (owner-approved 2026-09-09) allows ONE self-written reproduction when no existing
    test covers the change — v1's rule 4 had nothing to bind to on django-10880 and produced
    eleven scratch scripts. Rules 1-3 and 5 must be byte-identical to v1."""
    v1, v2 = OPENCODE_LAGUNA_EFFICIENCY_V1, OPENCODE_LAGUNA_EFFICIENCY_V2
    assert v1 != v2
    assert v2.split("4. Verify once")[0] == v1.split("4. Verify once")[0]
    assert v2.split("5. Do not loop")[1] == v1.split("5. Do not loop")[1]
    assert "write ONE reproduction" in v2 and "more than once" in v2
    assert "do not re-read the file you edited" in v2
    assert "write ONE reproduction" not in v1
    assert [p["id"] for p in PRESETS] == [
        "opencode-laguna-efficiency-v2",
        "claude_code-efficiency-v2",
        "codex-efficiency-v2",
        "mini_swe_agent-efficiency-v2",
        "custom_minimal-efficiency-v2",
        "opencode-laguna-efficiency-v1",
    ]


_PER_HARNESS_V2 = {
    "claude_code": (hi.CLAUDE_CODE_EFFICIENCY_V2, ["Grep", "Glob", "Read with offset and limit"]),
    "codex": (hi.CODEX_EFFICIENCY_V2, ["rg -n", "sed -n 'START,ENDp' FILE"]),
    "mini_swe_agent": (
        hi.MINI_SWE_AGENT_EFFICIENCY_V2,
        ["nl -ba FILE | sed -n 'START,ENDp'", "never print a whole test log"],
    ),
    "custom_minimal": (
        hi.CUSTOM_MINIMAL_EFFICIENCY_V2,
        ["str_replace_editor view", "view_range", "str_replace"],
    ),
}


@pytest.mark.parametrize("harness", sorted(_PER_HARNESS_V2))
def test_per_harness_v2_presets_share_rules_3_to_5_and_name_their_own_tools(harness: str) -> None:
    """2026-09-09: every harness gets a v2 preset. Rules 3-5 are byte-identical to opencode's
    v2 (the arms stay comparable); rules 1-2 name the harness's real tools and never opencode's
    (a shell-only agent cannot 'read with offset and limit')."""
    text, must_name = _PER_HARNESS_V2[harness]
    assert len(text) <= HARNESS_INSTRUCTIONS_MAX_CHARS
    assert hi.RULES_3_TO_5_V2 in text  # rules 3-5 verbatim
    rules_1_2 = text.split("3. Run tests")[0]
    for tool in must_name:
        assert tool in rules_1_2, (harness, tool)
    if harness != "claude_code":
        assert "offset and limit" not in rules_1_2  # opencode / claude_code wording only
    assert "Never re-read" in rules_1_2 or "Never re-view" in rules_1_2
    assert "tail -40" in rules_1_2
    numbered = [ln[:2] for ln in text.splitlines() if ln[:1].isdigit()]
    assert numbered[:5] == ["1.", "2.", "3.", "4.", "5."]
    preset = next(p for p in PRESETS if p["harness"] == harness and p["id"].endswith("-v2"))
    assert preset["text"] == text


def test_mini_preset_ends_with_the_submit_command() -> None:
    assert hi.MINI_SWE_AGENT_EFFICIENCY_V2.rstrip().endswith("nothing in between.")
    assert "6. Rule 4 replaces steps 2, 4 and 5" in hi.MINI_SWE_AGENT_EFFICIENCY_V2
    assert (
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` on its own" in hi.MINI_SWE_AGENT_EFFICIENCY_V2
    )
    for other in (
        hi.CLAUDE_CODE_EFFICIENCY_V2,
        hi.CODEX_EFFICIENCY_V2,
        hi.CUSTOM_MINIMAL_EFFICIENCY_V2,
    ):
        assert "COMPLETE_TASK_AND_SUBMIT" not in other


# ---- launch side: config, snapshot shape, S3 trigger, API cap, presets route --------------


def test_run_config_carries_the_field_and_it_lands_in_the_snapshot_shape() -> None:
    cfg = RunConfig(harness_instructions="rules")
    assert dataclasses.asdict(cfg)["harness_instructions"] == "rules"
    assert RunConfig().harness_instructions is None


def test_s3_dispatch_parses_absent_blank_and_present() -> None:
    base = {"run_id": "r1", "harness": "opencode", "instances": [{}]}
    _, cfg, _, _ = _parse_run_config(dict(base))
    assert cfg.harness_instructions is None
    _, cfg, _, _ = _parse_run_config({**base, "harness_instructions": "  "})
    assert cfg.harness_instructions is None
    _, cfg, _, _ = _parse_run_config({**base, "harness_instructions": " rules "})
    assert cfg.harness_instructions == "rules"


def test_launch_request_caps_the_text_length() -> None:
    RunLaunchRequest(
        instance_ids="all",
        harness="opencode",
        model_alias="m",
        budget_cap_usd=1.0,
        harness_instructions="x" * HARNESS_INSTRUCTIONS_MAX_CHARS,
    )
    with pytest.raises(ValueError):
        RunLaunchRequest(
            instance_ids="all",
            harness="opencode",
            model_alias="m",
            budget_cap_usd=1.0,
            harness_instructions="x" * (HARNESS_INSTRUCTIONS_MAX_CHARS + 1),
        )


def test_presets_route_serves_the_texts_and_the_cap() -> None:
    body = TestClient(app).get("/launch/instruction-presets").json()
    assert body["max_chars"] == HARNESS_INSTRUCTIONS_MAX_CHARS
    assert body["presets"][0]["id"] == "opencode-laguna-efficiency-v2"
    assert body["presets"][0]["harness"] == "opencode"
    assert body["presets"][0]["text"] == OPENCODE_LAGUNA_EFFICIENCY_V2
    assert body["presets"][-1]["text"] == OPENCODE_LAGUNA_EFFICIENCY_V1  # v1 kept last
    assert [p["harness"] for p in body["presets"]] == [
        "opencode",
        "claude_code",
        "codex",
        "mini_swe_agent",
        "custom_minimal",
        "opencode",
    ]


# ---- the Redis channel ---------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttl: dict[str, int] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value.encode()
        if ex is not None:
            self.ttl[key] = ex

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


def test_write_then_read_round_trips_and_blank_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(redis_client, "_get_client", lambda: fake)
    redis_client.write_harness_instructions("run-a", "  1. Rule.\n ")
    assert redis_client.read_harness_instructions("run-a") == "1. Rule."
    assert fake.ttl[redis_client.harness_instructions_key("run-a")] > 24 * 3600
    redis_client.write_harness_instructions("run-a", None)
    assert redis_client.read_harness_instructions("run-a") is None
    assert redis_client.read_harness_instructions("never-published") is None


def test_redis_failures_never_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Any:
        raise ConnectionError("valkey down")

    monkeypatch.setattr(redis_client, "_get_client", boom)
    redis_client.write_harness_instructions("run-a", "rules")  # no raise
    assert redis_client.read_harness_instructions("run-a") is None


# ---- dispatch: the payload stays the bare statement ----------------------------------------


def _instance() -> Instance:
    return Instance(
        instance_id="django__django-11099",
        repo="django/django",
        base_commit="abc",
        problem_statement=_STATEMENT,
        fail_to_pass="[]",
        pass_to_pass="[]",
    )


def test_dispatch_does_not_append_to_the_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployed worker never reads the payload's statement (ADR-0032) — appending here
    is the silent no-op the first version shipped. The channel is Redis, not the job."""
    monkeypatch.delenv("ENFORCE_CACHE_GATE", raising=False)
    monkeypatch.setattr(dispatcher, "register_run", lambda *a, **k: None)
    monkeypatch.setattr(dispatcher, "_seed_expected", lambda *a, **k: None)
    monkeypatch.setattr(
        dispatcher, "resolve_model_aliases_for_run", lambda alias: {alias: "provider/model"}
    )
    monkeypatch.setattr(dispatcher, "resolve_context_window", lambda *a, **k: (262_144, "test"))
    monkeypatch.setattr(dispatcher, "_build_reproducibility_snapshot", lambda *a, **k: {})
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(dispatcher, "send_message", lambda queue, body: sent.append(dict(body)))
    dispatcher.dispatch_run(
        "run-instr", [_instance()], RunConfig(harness="opencode", harness_instructions="1. Rule.")
    )
    assert sent[0]["problem_statement"] == _STATEMENT


# ---- the worker: where the text actually joins the prompt ----------------------------------


def _job(run_id: str = "run-w") -> HarnessJob:
    return HarnessJob(
        run_id=run_id,
        instance_id="django__django-11099",
        repo_url="https://github.com/django/django",
        base_commit="abc",
        problem_statement=_STATEMENT,
        attempt_number=1,
        harness_name="opencode",
        model_alias="laguna-xs-2.1-opencode",
    )


def test_worker_appends_the_published_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(redis_client, "_get_client", lambda: fake)
    redis_client.write_harness_instructions("run-w", "1. Rule.")
    out = harness_worker._with_run_instructions(_job("run-w"))
    assert out.startswith(_STATEMENT)
    assert out.endswith(f"{INSTRUCTIONS_HEADING}\n1. Rule.\n")


def test_worker_leaves_a_plain_run_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redis_client, "_get_client", lambda: _FakeRedis())
    assert harness_worker._with_run_instructions(_job("run-plain")) == _STATEMENT
