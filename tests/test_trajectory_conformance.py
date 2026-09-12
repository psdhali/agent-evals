"""Trajectory normalisation contract conformance (review N-1/N-2/N-4).

The normalised contract (architecture §9.2) requires tool calls to be visible to
the stuck detector.  A tool call may appear either as a dedicated ``role:"tool"``
event (custom_minimal, and now codex/opencode after N-2) or as a ``tool_calls``
array on an assistant event (claude_code) — the detector must read both (N-1).

Guard: the adapter list here is DERIVED from ``registry.HARNESS_ADAPTERS``, never
hand-written, so adapter number seven cannot silently skip the contract (the
P4C-2 lesson).  A new adapter whose trajectory writer drops tool calls fails this.
"""

from __future__ import annotations

import importlib
import json as _json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from swebench_eval.harnesses.aider.harness import _parse_chat_history
from swebench_eval.harnesses.base import HarnessInput, ModelConfig
from swebench_eval.harnesses.custom_minimal.harness import CustomMinimalHarness
from swebench_eval.harnesses.mini_swe_agent.harness import _normalize_mini_trajectory
from swebench_eval.harnesses.registry import HARNESS_ADAPTERS
from swebench_eval.harnesses.stuck_detector import StuckState, _event_tool_calls, evaluate

# --- N-1: the detector reads both tool-call shapes ---------------------------


def _tool_event(name: str, command: str) -> dict[str, object]:
    return {"role": "tool", "name": name, "normalized": {"command": command}, "output": ""}


def _assistant_tool_event(name: str, command: str) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"name": name, "input": {"command": command}}],
    }


def test_detector_event_tool_calls_reads_both_shapes() -> None:
    assert _event_tool_calls(_tool_event("bash", "ls")) == [("bash", "ls")]
    assert _event_tool_calls(_assistant_tool_event("bash", "ls")) == [("bash", '{"command": "ls"}')]


def test_detector_evaluates_claude_assistant_tool_calls() -> None:
    """A claude_code-style trajectory (assistant.tool_calls) is evaluable (N-1)."""
    events = [_assistant_tool_event("bash", f"grep x{i}.py") for i in range(10)]
    verdict = evaluate(events)
    assert verdict.state == StuckState.NOT_STUCK  # not insufficient_data
    assert not verdict.stuck


# --- N-2 / N-4: each trajectory-writing adapter records tool calls -------------


def _write_trajectory_of(
    harness_name: str,
) -> Callable[[list[dict[str, Any]], str, str], bool] | None:
    adapter_cls = HARNESS_ADAPTERS[harness_name]
    module = importlib.import_module(adapter_cls.__module__)
    return getattr(module, "_write_trajectory", None)


# Registry-derived: every adapter that has a `_write_trajectory` must record tool
# calls from its native stream in a detector-readable form.
TOOL_NATIVE_EVENTS = {
    # (harness, native event that represents one tool call)
    "claude_code": (
        [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "bash", "input": {"command": "ls"}}]
                },
            }
        ]
    ),
    "codex": (
        [
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "ls -la",
                    "aggregated_output": "",
                    "exit_code": 0,
                },
            }
        ]
    ),
    "opencode": (
        [
            {
                "type": "tool_use",
                "part": {
                    "type": "tool",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "ls -la"},
                        "output": "src/",
                        "time": {},
                    },
                },
            }
        ]
    ),
}


def test_aider_chat_history_recovers_conversation() -> None:
    """`_parse_chat_history` recovers the model reply and ignores invocation echo."""
    ps = "Fix the bug\nConsider: model"
    history = (
        "# aider chat started at 2026-08-12\n"
        "> /bin/aider --message Fix the bug --openai-api-base http://x --model m\n"
        "#### Fix the bug\n#### Consider: model\n"
        "I found the bug: the separability matrix mishandles nesting.\n"
        "Here is the corrected logic.\n"
    )
    events = _parse_chat_history(history, ps)
    assert len(events) == 1
    content = events[0]["content"]
    assert isinstance(content, str)  # the recovered reply body is text
    assert "separability matrix mishandles" in content
    assert events[0]["role"] == "assistant"


def test_mini_trajectory_normalizes_tool_calls(tmp_path: Path) -> None:
    """mini's native `messages` normalise to role:"tool" events the detector reads (N-3)."""
    raw = {
        "trajectory_format": 1,
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "problem"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "id1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "ls -la"}'},
                    }
                ],
                "reasoning_content": "let me look",
            },
            {"role": "tool", "tool_call_id": "id1", "content": "src/"},
        ],
    }
    import json as _json

    events = _normalize_mini_trajectory(_json.dumps(raw), "problem")
    tool_events = [e for e in events if _event_tool_calls(e)]
    assert tool_events, "mini trajectory produced no detector-readable tool events"
    assert ("bash", "ls -la") in {tc for e in tool_events for tc in _event_tool_calls(e)}
    assert (
        evaluate([e for e in events if e["role"] != "user"]).state != StuckState.INSUFFICIENT_DATA
    )


def test_aider_chat_history_failed_model_call_is_empty() -> None:
    """A run where the model never replied (invocation echo only) is unparsed."""
    history = (
        "# aider chat started\n"
        "> /bin/aider --message fix it --openai-api-base x --model m\n"
        "> litellm.BadRequestError: LLM Provider NOT provided\n"
        "> Open URL for more info? (Y)/(N): y\n"
    )
    assert _parse_chat_history(history, "fix it") == []


def test_registry_adapters_with_trajectory_writer_record_tool_calls(tmp_path: Path) -> None:
    """Every registry adapter with a `_write_trajectory` makes its tool event
    visible to the detector (role:tool or assistant.tool_calls)."""
    writers = {
        name: fn
        for name, fn in ((n, _write_trajectory_of(n)) for n in HARNESS_ADAPTERS)
        if fn is not None
    }
    assert writers, "expected at least one registry adapter with _write_trajectory"
    # The set comes from the registry (N-4), and must include the N-1/N-2 adapters.
    assert {"claude_code", "codex", "opencode"} <= set(writers)

    for harness, writer in writers.items():
        native = TOOL_NATIVE_EVENTS[harness]  # KeyError => a new adapter lacks a fixture
        path = tmp_path / f"{harness}.jsonl"
        assert writer(list(native), str(path), "problem")
        events = [
            __import__("json").loads(ln) for ln in path.read_text().splitlines() if ln.strip()
        ]
        # one of the written events must be a detector-readable tool event
        assert any(
            _event_tool_calls(ev) for ev in events
        ), f"{harness} trajectory did not record its tool call in a readable form"
        # ...and the detector must be able to evaluate it (not insufficient_data)
        v = evaluate(events)
        assert v.state != StuckState.INSUFFICIENT_DATA, f"{harness} still insufficient_data"


def test_custom_minimal_trajectory_usage_keys_are_self_describing(
    tmp_path: Path,
) -> None:
    """X3 (trajectory-review-astropy-12907 §5): no mixed semantics in a record.

    The old per-turn dict put per-turn token counts and a CUMULATIVE cost under
    sibling keys — summing cost_usd overstated a run 13× while input_tokens
    meant per-turn in one record and cumulative in another. A real run must
    emit ``turn_input_tokens``/``turn_output_tokens``/``cumulative_cost_usd``
    (sum the turn keys; read the last record's cumulative cost), and the system
    prompts must be in the record.
    """
    import json as _json
    import subprocess
    from unittest import mock

    from swebench_eval.harnesses.base import HarnessInput, ModelConfig
    from swebench_eval.harnesses.custom_minimal.harness import CustomMinimalHarness

    TURN_COSTS = [0.001, 0.002, 0.0005]

    def _make_response(cost: float, has_tool_calls: bool) -> mock.MagicMock:
        resp = mock.MagicMock()
        choice = mock.MagicMock()
        msg = mock.MagicMock()
        msg.content = "DONE" if not has_tool_calls else "ok"
        if has_tool_calls:
            tc = mock.MagicMock()
            tc.id = f"call_{cost}"
            tc.function.name = "bash"
            tc.function.arguments = _json.dumps({"command": "echo hello"})
            msg.tool_calls = [tc]
        else:
            msg.tool_calls = None
        choice.message = msg
        resp.choices = [choice]
        resp.usage = mock.MagicMock()
        resp.usage.prompt_tokens = 100
        resp.usage.completion_tokens = 50
        resp.usage.cost = cost
        resp.usage.prompt_tokens_details = mock.MagicMock()
        resp.usage.prompt_tokens_details.cached_tokens = 0
        resp.usage.prompt_tokens_details.cache_write_tokens = 0
        resp.usage.completion_tokens_details = mock.MagicMock()
        resp.usage.completion_tokens_details.reasoning_tokens = 0
        return resp

    responses = [_make_response(c, i < len(TURN_COSTS) - 1) for i, c in enumerate(TURN_COSTS)]
    fake_client = mock.MagicMock()
    fake_client.chat.completions.create = mock.MagicMock(side_effect=responses)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, timeout=10, check=False)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=repo_dir,
        capture_output=True,
        timeout=10,
        check=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=repo_dir,
        capture_output=True,
        timeout=10,
        check=False,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=repo_dir,
        capture_output=True,
        timeout=10,
        check=False,
    )

    harness_input = HarnessInput(
        instance_id="x3-instance",
        repo_url=f"file://{repo_dir}",
        base_commit="HEAD",
        problem_statement="Test problem.",
        attempt_number=1,
        repo_checkout_path=str(repo_dir),
        model_config=ModelConfig(
            gateway_base_url="http://test/v1",
            gateway_api_key="k",
            model_name="m",
        ),
        timeout_seconds=30,
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=5.0,
    )
    harness = CustomMinimalHarness(api_base_url="http://test/v1", api_key="k", model="m")

    with mock.patch("openai.OpenAI", return_value=fake_client):
        output = harness.run(harness_input)

    records = [
        _json.loads(ln)
        for ln in Path(output.trajectory_path).read_text().splitlines()
        if ln.strip()
    ]
    # X3: the system prompts are recorded (turn 0), before the user statement.
    assert [r["role"] for r in records[:3]] == ["system", "system", "user"]
    assert records[2]["content"] == "Test problem."

    assistant = [r for r in records if r["role"] == "assistant"]
    assert len(assistant) == len(TURN_COSTS)
    for rec in assistant:
        # X3 keys + B5/E10f sub-category keys (reasoning/cached/cache-write, per
        # turn AND cumulative — the fields that were accumulated and dropped).
        assert set(rec["usage"]) == {
            "turn_input_tokens",
            "turn_output_tokens",
            "turn_reasoning_tokens",
            "turn_cached_tokens",
            "turn_cache_write_tokens",
            "cumulative_cost_usd",
            "cumulative_reasoning_tokens",
            "cumulative_cached_tokens",
            "cumulative_cache_write_tokens",
        }, rec["usage"]
        assert "cost_usd" not in rec["usage"]  # the 13× trap is gone

    # cumulative_cost_usd is a RUNNING total, not a per-turn value: the first
    # turn is 0.001, the last is the sum of all three.
    assert assistant[0]["usage"]["cumulative_cost_usd"] == TURN_COSTS[0]
    assert assistant[-1]["usage"]["cumulative_cost_usd"] == sum(TURN_COSTS)
    # And summing the turn_* keys gives the total tokens (per-turn semantics).
    assert sum(r["usage"]["turn_input_tokens"] for r in assistant) == 100 * len(TURN_COSTS)
    assert sum(r["usage"]["turn_output_tokens"] for r in assistant) == 50 * len(TURN_COSTS)


def test_custom_minimal_dumps_raw_response_per_turn(tmp_path: Path) -> None:
    """B1 (E11c): the RAW model response per turn reaches harness_stdout.log.

    The rendered assistant line loses the fields that explain a failing run —
    which reasoning field name arrived, finish_reason, refusal, and the token
    details. ``model_dump_json`` on the pydantic response captures all of them;
    the log proves what ARRIVED, which is the first half of every E10a/Z2/E10c
    question. Capped defensively; the dump must never break the harness."""
    import json as _json
    import subprocess
    from unittest import mock

    from swebench_eval.harnesses.base import HarnessInput, ModelConfig
    from swebench_eval.harnesses.custom_minimal.harness import CustomMinimalHarness

    raw_json = '{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"DONE"}}],"usage":{"prompt_tokens":10,"completion_tokens":5}}'

    def _make_response() -> mock.MagicMock:
        resp = mock.MagicMock()
        resp.model_dump_json.return_value = raw_json
        choice = mock.MagicMock()
        msg = mock.MagicMock()
        msg.content = "DONE"
        msg.tool_calls = None
        choice.message = msg
        resp.choices = [choice]
        resp.usage = None
        return resp

    fake_client = mock.MagicMock()
    fake_client.chat.completions.create.return_value = _make_response()

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, timeout=10, check=False)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=repo_dir,
        capture_output=True,
        timeout=10,
        check=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=repo_dir,
        capture_output=True,
        timeout=10,
        check=False,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=repo_dir,
        capture_output=True,
        timeout=10,
        check=False,
    )

    harness_input = HarnessInput(
        instance_id="b1",
        repo_url=f"file://{repo_dir}",
        base_commit="HEAD",
        problem_statement="p",
        attempt_number=1,
        repo_checkout_path=str(repo_dir),
        model_config=ModelConfig(gateway_base_url="http://t", gateway_api_key="k", model_name="m"),
        timeout_seconds=30,
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=5.0,
    )
    harness = CustomMinimalHarness(api_base_url="http://t", api_key="k", model="m")
    with mock.patch("openai.OpenAI", return_value=fake_client):
        output = harness.run(harness_input)

    log = Path(output.raw_log_path).read_text()
    assert "RAW RESPONSE" in log
    assert raw_json in log, "the raw pydantic dump must reach the artifact"
    update = _json.loads(Path(output.raw_log_path).read_text().split("\n\n", 1)[0])
    assert update["terminated_reason"] == "completed"


def _git_repo(tmp_path: Path) -> Path:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, timeout=10, check=False)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=repo,
        capture_output=True,
        timeout=10,
        check=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=repo,
        capture_output=True,
        timeout=10,
        check=False,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=repo,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return repo


def _harness_input(repo: Path, run_id: str = "i") -> HarnessInput:
    return HarnessInput(
        instance_id=run_id,
        repo_url=f"file://{repo}",
        base_commit="HEAD",
        problem_statement="p",
        attempt_number=1,
        repo_checkout_path=str(repo),
        model_config=ModelConfig(gateway_base_url="http://t", gateway_api_key="k", model_name="m"),
        timeout_seconds=30,
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=5.0,
    )


def _one_turn_response(
    *,
    finish_reason: str | None = "stop",
    content: str | None = "DONE",
    tool_calls: Any = None,
    refusal: str | None = None,
    reasoning: str | None = None,
    reasoning_tokens: int = 0,
) -> mock.MagicMock:
    resp = mock.MagicMock()
    resp.model_dump_json.return_value = "{}"
    choice = mock.MagicMock()
    choice.finish_reason = finish_reason
    msg = mock.MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls
    if refusal is not None:
        msg.refusal = refusal
    if reasoning is not None:
        msg.reasoning = reasoning
    choice.message = msg
    resp.choices = [choice]
    resp.usage = mock.MagicMock()
    resp.usage.prompt_tokens = 10
    resp.usage.completion_tokens = 5
    resp.usage.cost = None
    resp.usage.prompt_tokens_details = mock.MagicMock()
    resp.usage.prompt_tokens_details.cached_tokens = 3
    resp.usage.prompt_tokens_details.cache_write_tokens = 2
    resp.usage.completion_tokens_details = mock.MagicMock()
    resp.usage.completion_tokens_details.reasoning_tokens = reasoning_tokens
    return resp


def test_custom_minimal_truncation_gets_own_reason_not_completed(tmp_path: Path) -> None:
    """B2/Z1: finish_reason 'length' → its own termination, NOT 'completed' →
    EMPTY_PATCH. And the truncated FINAL turn IS recorded in the trajectory."""
    fake = mock.MagicMock()
    fake.chat.completions.create.return_value = _one_turn_response(
        finish_reason="length", content="DONE", tool_calls=None
    )
    harness = CustomMinimalHarness(api_base_url="http://t", api_key="k", model="m")
    with mock.patch("openai.OpenAI", return_value=fake):
        output = harness.run(_harness_input(_git_repo(tmp_path)))

    assert output.terminated_reason == "max_tokens_truncated"
    assert output.exit_code == 2
    # The truncated final turn was recorded (not only in raw_log_lines).
    records = [
        _json.loads(ln)
        for ln in Path(output.trajectory_path).read_text().splitlines()
        if ln.strip()
    ]
    assistants = [r for r in records if r["role"] == "assistant"]
    assert assistants and assistants[-1]["turn"] == 1


def test_custom_minimal_refusal_gets_own_reason(tmp_path: Path) -> None:
    """B3/E10c: a model refusal is NOT a clean finish (not EMPTY_PATCH)."""
    fake = mock.MagicMock()
    fake.chat.completions.create.return_value = _one_turn_response(
        content=None, tool_calls=None, refusal="I won't help with that."
    )
    harness = CustomMinimalHarness(api_base_url="http://t", api_key="k", model="m")
    with mock.patch("openai.OpenAI", return_value=fake):
        output = harness.run(_harness_input(_git_repo(tmp_path)))

    assert output.terminated_reason == "refused"
    assert output.exit_code == 2


def test_custom_minimal_accepts_reasoning_alias_and_replays(tmp_path: Path) -> None:
    """B4/E10a: ``reasoning`` (the gateway's other name) is captured AND replayed."""
    calls: list[dict[str, Any]] = []

    def _bash_call() -> list[Any]:
        tc = mock.MagicMock()
        tc.id = "c1"
        tc.function.name = "bash"
        tc.function.arguments = '{"command": "echo hi"}'
        return [tc]

    def _create(**kwargs: Any) -> Any:
        calls.append({"messages": list(kwargs["messages"])})
        if len(calls) == 1:
            return _one_turn_response(
                reasoning="think-step-1", content="ok", tool_calls=_bash_call()
            )
        return _one_turn_response(content="DONE", tool_calls=None)

    fake = mock.MagicMock()
    fake.chat.completions.create.side_effect = _create
    harness = CustomMinimalHarness(api_base_url="http://t", api_key="k", model="m")
    with mock.patch("openai.OpenAI", return_value=fake):
        output = harness.run(_harness_input(_git_repo(tmp_path)))

    # the 2nd call replays the captured reasoning under reasoning_content
    hist = calls[1]["messages"]
    assistant_msgs = [m for m in hist if m["role"] == "assistant"]
    assert assistant_msgs[-1].get("reasoning_content") == "think-step-1"
    records = [
        _json.loads(l) for l in Path(output.trajectory_path).read_text().splitlines() if l.strip()
    ]
    assistants = [r for r in records if r["role"] == "assistant"]
    assert assistants[0]["reasoning"] == "think-step-1"


def test_custom_minimal_warns_when_reasoning_tokens_but_no_capture(tmp_path, caplog) -> None:
    """B4/E10a: reasoning_tokens > 0 with no captured reasoning text is a WARNING."""
    import logging

    fake = mock.MagicMock()
    fake.chat.completions.create.return_value = _one_turn_response(
        content="DONE", tool_calls=None, reasoning_tokens=50
    )
    harness = CustomMinimalHarness(api_base_url="http://t", api_key="k", model="m")
    with (
        caplog.at_level(logging.WARNING, logger="swebench_eval.harnesses.custom_minimal.harness"),
        mock.patch("openai.OpenAI", return_value=fake),
    ):
        output = harness.run(_harness_input(_git_repo(tmp_path)))

    assert output.terminated_reason == "completed"  # capture failing is NOT a crash
    assert any(
        "reasoning tokens yet no reasoning content was ever captured" in r for r in caplog.messages
    )


def test_custom_minimal_emits_subcategory_tokens_per_turn_and_cumulative(tmp_path: Path) -> None:
    """B5/E10f: reasoning/cached/cache-write tokens reach the trajectory per turn AND cumulative."""
    fake = mock.MagicMock()
    fake.chat.completions.create.return_value = _one_turn_response(
        content="DONE", tool_calls=None, reasoning_tokens=40
    )
    harness = CustomMinimalHarness(api_base_url="http://t", api_key="k", model="m")
    with mock.patch("openai.OpenAI", return_value=fake):
        output = harness.run(_harness_input(_git_repo(tmp_path)))

    records = [
        _json.loads(l) for l in Path(output.trajectory_path).read_text().splitlines() if l.strip()
    ]
    u = [r["usage"] for r in records if r["role"] == "assistant"][-1]
    assert u["turn_reasoning_tokens"] == 40
    assert u["cumulative_reasoning_tokens"] == 40
    assert u["turn_cached_tokens"] == 3
    assert u["cumulative_cached_tokens"] == 3
    assert u["turn_cache_write_tokens"] == 2
    assert u["cumulative_cache_write_tokens"] == 2


def test_custom_minimal_replays_and_records_reasoning(tmp_path: Path) -> None:
    """Reasoning content is BOTH replayed into the next call AND recorded.

    A reasoning model's chain-of-thought is part of its context: the assistant
    message sent on turn N+1 must carry the prior reasoning back, or the model
    loses its thread. The harness must not silently drop it (found live)."""
    import json as _json
    import subprocess
    from unittest import mock

    from swebench_eval.harnesses.base import HarnessInput, ModelConfig
    from swebench_eval.harnesses.custom_minimal.harness import CustomMinimalHarness

    calls: list[dict[str, Any]] = []

    def _make_response(reasoning: str, *, has_tools: bool, content: str) -> mock.MagicMock:
        resp = mock.MagicMock()
        choice = mock.MagicMock()
        msg = mock.MagicMock()
        msg.content = content
        msg.reasoning_content = reasoning
        if has_tools:
            tc = mock.MagicMock()
            tc.id = "c2"
            tc.function.name = "bash"
            tc.function.arguments = _json.dumps({"command": "echo hi"})
            msg.tool_calls = [tc]
        else:
            msg.tool_calls = None
        choice.message = msg
        resp.choices = [choice]
        resp.usage = mock.MagicMock()
        resp.usage.prompt_tokens = 100
        resp.usage.completion_tokens = 50
        resp.usage.cost = None
        resp.usage.prompt_tokens_details = None
        resp.usage.completion_tokens_details = None
        return resp

    responses = [
        _make_response("reasoning-one", has_tools=True, content="ok"),
        _make_response("reasoning-two", has_tools=False, content="done"),  # terminates
    ]

    def _fake_create(**kwargs: Any) -> object:
        # Snapshot the messages list per call — the harness appends to it in
        # place, so a live reference would show the final state for every call.
        calls.append({"messages": list(kwargs["messages"])})
        return responses.pop(0)

    fake_client = mock.MagicMock()
    fake_client.chat.completions.create = mock.MagicMock(side_effect=_fake_create)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, timeout=10, check=False)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=repo_dir,
        capture_output=True,
        timeout=10,
        check=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=repo_dir,
        capture_output=True,
        timeout=10,
        check=False,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=repo_dir,
        capture_output=True,
        timeout=10,
        check=False,
    )

    harness_input = HarnessInput(
        instance_id="rz",
        repo_url=f"file://{repo_dir}",
        base_commit="HEAD",
        problem_statement="p",
        attempt_number=1,
        repo_checkout_path=str(repo_dir),
        model_config=ModelConfig(gateway_base_url="http://t", gateway_api_key="k", model_name="m"),
        timeout_seconds=30,
        max_tokens_per_instance=None,
        max_cost_usd_per_instance=5.0,
    )
    harness = CustomMinimalHarness(api_base_url="http://t", api_key="k", model="m")
    with mock.patch("openai.OpenAI", return_value=fake_client):
        output = harness.run(harness_input)

    # REPLAY: the 2nd model call's message history must carry the 1st reasoning.
    assert len(calls) == 2
    hist = calls[1]["messages"]
    assistant_msgs = [m for m in hist if m["role"] == "assistant"]
    assert assistant_msgs, "expected a replayed assistant message on the 2nd call"
    # The replayed assistant message carries reasoning_content.
    assert assistant_msgs[-1].get("reasoning_content") == "reasoning-one", assistant_msgs[-1]

    # RECORD: the trajectory assistant records carry each turn's reasoning.
    records = [
        _json.loads(l) for l in Path(output.trajectory_path).read_text().splitlines() if l.strip()
    ]
    assistants = [r for r in records if r["role"] == "assistant"]
    assert [r.get("reasoning") for r in assistants] == ["reasoning-one", "reasoning-two"]


def test_claude_code_emits_role_tool_from_tool_result(tmp_path: Path) -> None:
    """M2 (review 2026-08-25): claude_code's trajectory must carry role:"tool"
    records (output) from tool_result blocks.

    Regression-shaped against the REAL 2.1.234 event stream, not an imagined one:
    Claude Code surfaces tool results as `tool_result` content blocks inside a
    top-level **`user`** event — it NEVER emits a dedicated `tool_result` event.
    The old fixture hand-built `{"type":"tool_result"}` and passed while zero
    role:tool records were produced in production (189 tool_calls, 0 role:tool),
    the same fixture-vs-reality trap as the opencode round. This test feeds the
    actual emitted shape and asserts the tool name is mapped from the preceding
    assistant tool_use, plus is_error / stdout / stderr are carried."""
    from swebench_eval.harnesses.claude_code.harness import _write_trajectory

    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "ls"}}
                ]
            },
        },
        {
            # REAL shape: tool_result lives inside a user event's content blocks,
            # with the raw stdout/stderr on the event-level tool_use_result.
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "src/  tests/",
                        "is_error": False,
                    }
                ]
            },
            "tool_use_result": {"stdout": "src/  tests/", "stderr": "", "interrupted": False},
        },
    ]
    path = str(tmp_path / "claude-traj-test.jsonl")
    _write_trajectory(events, path, "fix it")
    with open(path) as fh:
        tool_recs = [_json.loads(l) for l in fh if _json.loads(l)["role"] == "tool"]
    assert tool_recs, "claude trajectory must contain a role:tool record"
    assert tool_recs[0]["tool_call_id"] == "tu_1"
    assert "src/" in tool_recs[0]["output"]
    # M2: real tool name mapped from the preceding assistant tool_use (Bash,
    # not the old hardcoded "bash"); is_error/stdout/stderr carried.
    assert tool_recs[0]["name"] == "Bash"
    assert tool_recs[0]["is_error"] is False
    assert tool_recs[0]["stdout"] == "src/  tests/"
    assert tool_recs[0]["stderr"] == ""

    # M2: a pure tool_result user event must NOT emit an empty user record.
    with open(path) as fh:
        user_recs = [_json.loads(l) for l in fh if _json.loads(l)["role"] == "user"]
    assert not [r for r in user_recs if not r["content"]], "no empty user records"
