"""offline-analysis-design.md §3.1/§3.5/§3.6 — the one-shot judge call."""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import pytest

from swebench_eval.analysis import judge_llm
from swebench_eval.analysis.rubric import load_rubric


def _fake_response(
    content: str,
    *,
    model: str = "deepseek/deepseek-v4-flash-0731",
    reasoning: str | None = None,
    reasoning_in_psf: bool = False,
) -> mock.MagicMock:
    resp = mock.MagicMock()
    choice = mock.MagicMock()
    msg = mock.MagicMock()
    msg.content = content
    # Explicit, not auto-MagicMock: _message_reasoning must read a real str or None.
    msg.reasoning_content = None
    msg.reasoning = None
    msg.provider_specific_fields = None
    if reasoning is not None:
        if reasoning_in_psf:
            msg.provider_specific_fields = {"reasoning": reasoning}
        else:
            msg.reasoning_content = reasoning
    choice.message = msg
    resp.choices = [choice]
    resp.model = model
    resp.id = "gen-123"
    resp.provider = "deepseek"
    resp.usage = mock.MagicMock()
    resp.usage.prompt_tokens = 1000
    resp.usage.completion_tokens = 200
    resp.usage.prompt_tokens_details = mock.MagicMock()
    resp.usage.prompt_tokens_details.cached_tokens = 0
    return resp


def _rubric():
    return load_rubric()


def test_build_prompt_includes_absent_ids_and_dimensions_never_the_gold_patch() -> None:
    system, user = judge_llm.build_prompt(_rubric(), ["tests/test_x.py::test_absent"], "TRAJ_TEXT")
    assert "contamination" in system
    assert "test_gaming" in system
    assert "tests/test_x.py::test_absent" in user
    assert "TRAJ_TEXT" in user
    assert "gold" not in system.lower() and "gold" not in user.lower()


def test_build_prompt_instructs_ignoring_the_benign_tooling_marker() -> None:
    system, _ = judge_llm.build_prompt(_rubric(), None, "traj")
    assert "benign tooling warning" in system
    assert "environment_problem" in system


def test_build_prompt_never_names_a_harness() -> None:
    """BUILDER3-JUDGE-VERIFIED-AND-UI-BRIEF-2026-09-02.md §3.3/§3.4: the
    marker text used to read "benign codex metadata warning", present in the
    system prompt for all five harnesses but only ever landing in the user
    prompt (via trajectory_assembly's filter) for codex — a deterministic,
    English, harness-identifying string the framework inserted, not
    something the agent wrote. Every harness name must be absent from both
    prompts regardless of which harness produced the trajectory."""
    system, user = judge_llm.build_prompt(_rubric(), None, "traj with no harness name in it")
    for harness_name in ("codex", "claude_code", "mini_swe_agent", "opencode", "aider"):
        assert harness_name not in system.lower()
        assert harness_name not in user.lower()


def test_call_judge_happy_path_parses_and_extracts_usage() -> None:
    valid = json.dumps(
        {
            "rubric_version": 1,
            "dimensions": {"contamination": {"score": 0, "reasoning": "no evidence found"}},
            "summary": "clean run",
        }
    )
    fake_client = mock.MagicMock()
    fake_client.chat.completions.create = mock.MagicMock(return_value=_fake_response(valid))

    with mock.patch("openai.OpenAI", return_value=fake_client):
        result = judge_llm.call_judge(
            api_base_url="http://gateway.local/v1",
            api_key="k",
            model_alias="judge-model",
            rubric=_rubric(),
            absent_node_ids=[],
            trajectory_text="traj",
        )

    assert result.parsed is not None
    assert result.parsed["summary"] == "clean run"
    assert result.parse_error is None
    assert result.raw_response_text == valid
    assert result.model_resolved == "deepseek/deepseek-v4-flash-0731"
    assert result.input_tokens == 1000
    assert result.output_tokens == 200
    assert result.cost_usd is not None and result.cost_usd > 0


def test_call_judge_requests_json_mode_and_the_configured_temperature() -> None:
    fake_client = mock.MagicMock()
    fake_client.chat.completions.create = mock.MagicMock(
        return_value=_fake_response(json.dumps({"dimensions": {}}))
    )
    with mock.patch("openai.OpenAI", return_value=fake_client):
        judge_llm.call_judge(
            api_base_url="http://gateway.local/v1",
            api_key="k",
            model_alias="judge-model",
            rubric=_rubric(),
            absent_node_ids=None,
            trajectory_text="traj",
            temperature=0.0,
        )
    _, kwargs = fake_client.chat.completions.create.call_args
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["temperature"] == 0.0
    assert kwargs["model"] == "judge-model"


def test_call_judge_malformed_json_is_recorded_not_raised() -> None:
    """§3.6: validate, and on failure record judge_parse_failed with the raw
    response stored — never retry or coerce a partial parse."""
    garbage = "this is not json at all {{{"
    fake_client = mock.MagicMock()
    fake_client.chat.completions.create = mock.MagicMock(return_value=_fake_response(garbage))

    with mock.patch("openai.OpenAI", return_value=fake_client):
        result = judge_llm.call_judge(
            api_base_url="http://gateway.local/v1",
            api_key="k",
            model_alias="judge-model",
            rubric=_rubric(),
            absent_node_ids=[],
            trajectory_text="traj",
        )

    assert result.parsed is None
    assert result.parse_error is not None
    assert result.raw_response_text == garbage  # never lost, even on parse failure


def test_call_judge_non_object_json_is_also_a_parse_failure() -> None:
    """A syntactically valid JSON array or scalar is still not the expected
    {"dimensions": {...}} shape — must not be silently accepted as `parsed`."""
    fake_client = mock.MagicMock()
    fake_client.chat.completions.create = mock.MagicMock(
        return_value=_fake_response(json.dumps([1, 2, 3]))
    )
    with mock.patch("openai.OpenAI", return_value=fake_client):
        result = judge_llm.call_judge(
            api_base_url="http://gateway.local/v1",
            api_key="k",
            model_alias="judge-model",
            rubric=_rubric(),
            absent_node_ids=[],
            trajectory_text="traj",
        )
    assert result.parsed is None
    assert result.parse_error is not None


def test_call_judge_captures_reasoning_content_and_provider_specific_fields() -> None:
    """ADR-0042: the recovery cascade needs the model's chain-of-thought, which
    the gateway exposes under either name. Both must be captured."""
    cases: list[tuple[str, bool]] = [("R1", False), ("R2", True)]
    for reasoning, in_psf in cases:
        fake_client = mock.MagicMock()
        fake_client.chat.completions.create = mock.MagicMock(
            return_value=_fake_response(
                json.dumps({"dimensions": {}}), reasoning=reasoning, reasoning_in_psf=in_psf
            )
        )
        with mock.patch("openai.OpenAI", return_value=fake_client):
            result = judge_llm.call_judge(
                api_base_url="http://gateway.local/v1",
                api_key="k",
                model_alias="judge-model",
                rubric=_rubric(),
                absent_node_ids=[],
                trajectory_text="traj",
            )
        assert result.reasoning_text == reasoning


def test_call_judge_without_force_json_omits_response_format_and_recovers_from_prose() -> None:
    """ADR-0042 step B: dropping the json_object constraint is the whole point,
    so response_format must NOT be sent; the object is then recovered from a
    response that may wrap it in prose or a code fence."""
    wrapped = 'Here is my judgment:\n```json\n{"dimensions": {}, "summary": "ok"}\n```\nDone.'
    fake_client = mock.MagicMock()
    fake_client.chat.completions.create = mock.MagicMock(return_value=_fake_response(wrapped))
    with mock.patch("openai.OpenAI", return_value=fake_client):
        result = judge_llm.call_judge(
            api_base_url="http://gateway.local/v1",
            api_key="k",
            model_alias="judge-model",
            rubric=_rubric(),
            absent_node_ids=[],
            trajectory_text="traj",
            force_json=False,
        )
    _, kwargs = fake_client.chat.completions.create.call_args
    assert "response_format" not in kwargs
    assert result.parsed is not None
    assert result.parsed["summary"] == "ok"


def test_extract_json_object_scans_a_balanced_object_out_of_prose() -> None:
    assert judge_llm._extract_json_object('prefix {"a": {"b": 1}} suffix') == {"a": {"b": 1}}
    # a brace inside a string must not end the scan early
    assert judge_llm._extract_json_object('{"k": "a } b", "n": 2}') == {"k": "a } b", "n": 2}
    assert judge_llm._extract_json_object("no object here") is None
    assert judge_llm._extract_json_object("[1, 2, 3]") is None  # array is not an object


def test_transcribe_reasoning_serializes_without_forcing_json() -> None:
    """ADR-0042 step C: hand the model its own analysis, ask ONLY to structure
    it, and never force json_object (that constraint is the trigger)."""
    parsed = json.dumps({"dimensions": {}, "summary": "transcribed"})
    fake_client = mock.MagicMock()
    fake_client.chat.completions.create = mock.MagicMock(return_value=_fake_response(parsed))
    with mock.patch("openai.OpenAI", return_value=fake_client):
        result = judge_llm.transcribe_reasoning(
            api_base_url="http://gateway.local/v1",
            api_key="k",
            model_alias="judge-model",
            rubric=_rubric(),
            reasoning_text="the agent's fix is correct and minimal",
        )
    _, kwargs = fake_client.chat.completions.create.call_args
    assert "response_format" not in kwargs
    system = kwargs["messages"][0]["content"]
    user = kwargs["messages"][1]["content"]
    assert "transcrib" in system.lower()  # a serializer prompt, not a re-judging prompt
    assert "the agent's fix is correct and minimal" in user
    assert result.parsed is not None and result.parsed["summary"] == "transcribed"


def test_every_judge_client_is_built_with_the_10_minute_ceiling() -> None:
    """2026-09-08 (owner): 10 minutes is the ceiling for one judgment — the client
    timeout agrees with the gateway ALB's 600 s idle timeout, explicitly, on every
    client the judge builds (a longer wait per answer was judged not worth it)."""
    rubric = load_rubric()
    seen: list[dict[str, Any]] = []

    def _resp():
        return mock.MagicMock(  # MagicMock: iterable (synthesize_pass streams)
            model="judge-model",
            id="gen-1",
            usage=None,
            choices=[mock.Mock(message=mock.Mock(content='{"dimensions": {}, "summary": "x"}'))],
        )

    def _fake_openai(**kwargs):
        seen.append(kwargs)
        client = mock.MagicMock()
        client.chat.completions.create.return_value = _resp()
        return client

    with mock.patch("openai.OpenAI", side_effect=_fake_openai):
        judge_llm.call_judge(
            api_base_url="http://g/v1",
            api_key="k",
            model_alias="judge-model",
            rubric=rubric,
            absent_node_ids=None,
            trajectory_text="t",
        )
        judge_llm.transcribe_reasoning(
            api_base_url="http://g/v1",
            api_key="k",
            model_alias="judge-model",
            rubric=rubric,
            reasoning_text="r",
        )
        judge_llm.synthesize_pass(
            api_base_url="http://g/v1", api_key="k", model_alias="judge-model", digest_text="d"
        )
    assert len(seen) == 3
    assert all(k["timeout"] == judge_llm.JUDGE_CALL_TIMEOUT_S for k in seen)
    assert judge_llm.JUDGE_CALL_TIMEOUT_S == 600.0


def _gateway_504() -> Exception:
    import httpx
    import openai

    resp = httpx.Response(504, request=httpx.Request("POST", "http://g/v1/chat/completions"))
    return openai.APIStatusError("504 Gateway Time-out", response=resp, body=None)


def test_a_504_at_the_ceiling_is_a_timed_out_judgment_not_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-08 (owner): the gateway's 504 at its idle timeout means the judge was still
    generating at the 10-min ceiling. Not retried (a second 10 min is not worth it), raised
    at once as timed_out=True so run_pass records a no-verdict judgment."""
    sleeps: list[float] = []
    monkeypatch.setattr(judge_llm, "_sleep", lambda s: sleeps.append(s))
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = _gateway_504()
    with pytest.raises(judge_llm.JudgeCallTransportError) as info:
        judge_llm._create_with_transport_retry(client, model="judge-model", messages=[])
    assert info.value.timed_out is True
    assert info.value.attempts == 1
    assert info.value.last_status == 504
    assert sleeps == []  # no retry ladder


def test_the_sdk_timeout_is_a_timed_out_judgment_too(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    import openai

    monkeypatch.setattr(judge_llm, "_sleep", lambda s: None)
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = openai.APITimeoutError(
        request=httpx.Request("POST", "http://g/v1/chat/completions")
    )
    with pytest.raises(judge_llm.JudgeCallTransportError) as info:
        judge_llm._create_with_transport_retry(client, model="judge-model", messages=[])
    assert info.value.timed_out is True


def test_a_429_is_still_retried_and_never_timed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    import openai

    monkeypatch.setattr(judge_llm, "_sleep", lambda s: None)
    monkeypatch.setattr(judge_llm, "_TRANSPORT_MAX_ATTEMPTS", 2)
    resp = httpx.Response(429, request=httpx.Request("POST", "http://g/v1/chat/completions"))
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = openai.RateLimitError(
        "429", response=resp, body=None
    )
    with pytest.raises(judge_llm.JudgeCallTransportError) as info:
        judge_llm._create_with_transport_retry(client, model="judge-model", messages=[])
    assert info.value.timed_out is False
    assert info.value.attempts == 2


def test_synthesis_streams_and_assembles_content_reasoning_and_usage() -> None:
    """2026-09-08: the report call is streamed so the gateway ALB never sees an idle
    connection (a 490-judgment report 504'd at 600 s on a slow provider). Content
    deltas are joined, reasoning deltas are the fallback for a hollow answer, usage
    comes from the final chunk (stream_options include_usage)."""
    from types import SimpleNamespace

    def chunk(content=None, reasoning=None, usage=None):
        delta = SimpleNamespace(content=content, reasoning_content=reasoning)
        return SimpleNamespace(
            model="judge-model",
            usage=usage,
            choices=[SimpleNamespace(delta=delta)] if (content or reasoning) else [],
        )

    chunks = [
        chunk(reasoning="thinking…"),
        chunk(content="## Overview\n"),
        chunk(content="501 attempts judged."),
        chunk(usage=SimpleNamespace(prompt_tokens=75_000, completion_tokens=1_200)),
    ]
    client = mock.MagicMock()
    client.chat.completions.create.return_value = iter(chunks)
    with mock.patch("openai.OpenAI", return_value=client):
        out = judge_llm.synthesize_pass(
            api_base_url="http://g/v1", api_key="k", model_alias="judge-model", digest_text="d"
        )
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}
    assert kwargs["max_tokens"] == judge_llm.SYNTHESIS_MAX_TOKENS
    assert kwargs["reasoning_effort"] == "low"
    assert out.text == "## Overview\n501 attempts judged."
    assert out.input_tokens == 75_000 and out.output_tokens == 1_200
    assert out.cost_usd is not None and out.cost_usd > 0
    assert out.model_resolved == "deepseek/deepseek-v4-flash-0731"


def test_synthesis_rewrites_a_hollow_answer_from_its_reasoning_never_stores_it() -> None:
    from types import SimpleNamespace

    chunks = [
        SimpleNamespace(
            model="judge-model",
            usage=None,
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None, reasoning_content="all in reasoning")
                )
            ],
        )
    ]
    rewrite = mock.MagicMock()
    rewrite.choices = [mock.Mock(message=mock.Mock(content="## Overview\nRewritten report."))]
    rewrite.usage = SimpleNamespace(prompt_tokens=500, completion_tokens=300)
    rewrite.model = "judge-model"
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = [iter(chunks), rewrite]
    with mock.patch("openai.OpenAI", return_value=client):
        out = judge_llm.synthesize_pass(
            api_base_url="http://g/v1", api_key="k", model_alias="judge-model", digest_text="d"
        )
    # the chain of thought is NEVER the report — a short rewrite call turns it into one
    assert out.text == "## Overview\nRewritten report."
    assert client.chat.completions.create.call_count == 2
    second = client.chat.completions.create.call_args_list[1].kwargs
    assert second.get("stream") is None and second["max_tokens"] == judge_llm.SYNTHESIS_MAX_TOKENS
    assert second["reasoning_effort"] == "low"
    assert "all in reasoning" in second["messages"][1]["content"]
    assert out.output_tokens == 300 and out.cost_usd is not None


def test_build_prompt_places_the_efficiency_profile_before_the_trajectory() -> None:
    rubric = load_rubric()
    profile = "EFFICIENCY PROFILE (computed):\n- LLM calls: 12"
    _system, user = judge_llm.build_prompt(rubric, None, "TRAJ-TEXT", profile)
    assert profile in user
    assert user.index("EFFICIENCY PROFILE") < user.index("TRAJECTORY (patch")
    # and the schema block tells the judge the causes fields
    system, _ = judge_llm.build_prompt(rubric, None, "t")
    assert "avoidable_share" in system and "recommendation" in system
    # 2026-09-09: the causes note must ask for evidence explicitly — the first v3 pass read the
    # field list as exhaustive and returned no citation, so §3.3 demoted every score
    # (token_efficiency is the last dimension; its folded question spans lines)
    causes_block = system.split("- token_efficiency", 1)[1].split("Respond with ONLY", 1)[0]
    assert '"evidence"' in causes_block and "cited turn" in causes_block
    assert "Evidence (turn index + exact quote) is REQUIRED" in causes_block


def test_build_prompt_without_a_profile_is_unchanged() -> None:
    rubric = load_rubric()
    _s, user = judge_llm.build_prompt(rubric, None, "TRAJ-TEXT")
    assert "EFFICIENCY PROFILE" not in user
