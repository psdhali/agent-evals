"""Reporting gaps closed 2026-09-06 (battery exports, resume file §6.3).

a. provenance: the run's OWN (rotatable) alias resolves — ``model_resolved`` was
   null on every battery export because ``resolved_models`` only ever held the
   three static yaml aliases; ``dataset_name`` + ``image_digest_snapshot`` ride
   along and the export prints the ADR-0043 triple; validation runs record the
   same facts instead of an empty block.
b. live LLM calls: the list is scoped per ATTEMPT and carries LiteLLM's row
   ``status`` so a provider-rejected call reads as "failed call".
c. run detail: one state bucket per INSTANCE (latest attempt, eval row first).
"""

from __future__ import annotations

from typing import Any, Self

import pytest

from swebench_eval.orchestrator import export
from swebench_eval.orchestrator.api import llm_live, queries
from swebench_eval.orchestrator.control_plane import dispatcher, image_validation

# --- a. provenance ------------------------------------------------------------


def test_rotatable_alias_entry_has_the_yaml_entry_shape() -> None:
    entry = dispatcher.rotatable_alias_entry("minimax-m2.5-codex")
    assert entry is not None
    assert entry["model"] == "openrouter/minimax/minimax-m2.5"
    assert entry["litellm_params_model"] == "openrouter/minimax/minimax-m2.5"
    assert entry["provider"] == "openrouter"
    assert entry["api_base"] == "https://openrouter.ai/api/v1"
    assert entry["source"] == "rotatable_models"
    assert entry["temperature"] == 1.0 and entry["top_p"] == 0.95 and entry["top_k"] == 40
    assert entry["reasoning_effort"] == "high"
    assert entry["provider_pin"] == ["minimax"]
    assert entry["max_input_tokens"] == 204_800


def test_rotatable_alias_entry_claude_code_shape_and_unknown_alias() -> None:
    entry = dispatcher.rotatable_alias_entry("minimax-m2.5-claude_code")
    assert entry is not None
    assert entry["model"] == "anthropic/minimax/minimax-m2.5"
    assert "temperature" not in entry  # the Anthropic-shaped spec sends no sampling params
    assert dispatcher.rotatable_alias_entry("cheap-oss-model") is None  # a yaml alias
    assert dispatcher.rotatable_alias_entry("nope") is None


def test_resolve_model_aliases_for_run_puts_the_run_alias_first(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        dispatcher,
        "_resolve_model_aliases",
        lambda: {"cheap-oss-model": {"model": "openrouter/deepseek/deepseek-v4-flash-0731"}},
    )
    resolved = dispatcher.resolve_model_aliases_for_run("minimax-m2.5-opencode")
    assert next(iter(resolved)) == "minimax-m2.5-opencode"
    assert resolved["cheap-oss-model"]["model"] == "openrouter/deepseek/deepseek-v4-flash-0731"
    # a yaml alias is left exactly as the yaml resolved it
    assert dispatcher.resolve_model_aliases_for_run("cheap-oss-model") == {
        "cheap-oss-model": {"model": "openrouter/deepseek/deepseek-v4-flash-0731"}
    }


def test_api_provenance_prefers_the_run_alias_and_carries_the_pin_halves() -> None:
    snap = {
        "model_alias": "minimax-m2.5-opencode",
        "resolved_models": {
            "minimax-m2.5-opencode": {"litellm_params_model": "openrouter/minimax/minimax-m2.5"},
            "cheap-oss-model": {"litellm_params_model": "openrouter/deepseek/x"},
            "claude-code-model": {"litellm_params_model": "anthropic/deepseek/x"},
        },
        "dataset_name": "SWE-bench/SWE-bench_Verified",
        "dataset_revision": "78f471bf655a3137b2e8a75af1501690ec009ec3",
        "image_digest_snapshot": "SWE-bench_Verified-78f471bf655a.json",
    }
    prov = queries._provenance(snap)
    assert prov["model_resolved"] == "openrouter/minimax/minimax-m2.5"
    assert prov["dataset_name"] == "SWE-bench/SWE-bench_Verified"
    assert prov["image_digest_snapshot"] == "SWE-bench_Verified-78f471bf655a.json"
    # old snapshots (no run alias in the map) keep the joined-literals fallback
    old = {"resolved_models": {"a": {"model": "m1"}, "b": {"model": "m2"}}}
    assert queries._provenance(old)["model_resolved"] == "m1, m2"


def test_export_pin_line_is_all_three_or_nothing() -> None:
    full = {
        "swebench_version": "5.0.2",
        "dataset_name": "SWE-bench/SWE-bench_Verified",
        "dataset_revision": "78f471bf655a3137b2e8a75af1501690ec009ec3",
        "image_digest_snapshot": "SWE-bench_Verified-78f471bf655a.json",
    }
    assert export._pin_line(full) == (
        "swebench 5.0.2 · SWE-bench/SWE-bench_Verified@78f471bf655a · "
        "SWE-bench_Verified-78f471bf655a.json"
    )
    assert export._pin_line({**full, "image_digest_snapshot": None}) is None
    assert export._pin_line({}) is None


def test_export_provenance_carries_dataset_snapshot_and_pin() -> None:
    run = {
        "run_id": "r",
        "created_at": "t",
        "config_snapshot": {
            "model_alias": "minimax-m2.5-codex",
            "resolved_models": {"minimax-m2.5-codex": {"model": "openrouter/minimax/minimax-m2.5"}},
            "swebench_version": "5.0.2",
            "dataset_name": "SWE-bench/SWE-bench_Verified",
            "dataset_revision": "78f471bf655a3137b2e8a75af1501690ec009ec3",
            "image_digest_snapshot": "SWE-bench_Verified-78f471bf655a.json",
        },
    }
    prov = export._provenance(run)
    assert prov["model_resolved"] == "openrouter/minimax/minimax-m2.5"
    assert prov["dataset_name"] == "SWE-bench/SWE-bench_Verified"
    assert prov["image_digest_snapshot"] == "SWE-bench_Verified-78f471bf655a.json"
    assert prov["pin"].startswith("swebench 5.0.2 · SWE-bench/SWE-bench_Verified@78f471bf655a")


def test_reproducibility_facts_without_a_harness(monkeypatch: Any) -> None:
    monkeypatch.setattr(dispatcher, "_resolve_harness_digest", lambda ids: f"sha256:{ids[0]}")
    monkeypatch.setenv("FRAMEWORK_SHA", "abc123")
    facts = dispatcher.reproducibility_facts(harness=None, instance_ids=["django__django-1"])
    assert facts["framework_sha"] == "abc123"
    assert facts["harness_image_digest"] == "sha256:django__django-1"
    assert facts["harness_tool_surface"] is None
    assert "dataset_name" in facts and "dataset_revision" in facts
    assert "image_digest_snapshot" in facts


def test_resolve_harness_digest_accepts_ids_or_instances(monkeypatch: Any) -> None:
    seen: list[str] = []

    class _Ecr:
        class exceptions:
            class ImageNotFoundException(Exception):
                pass

        def describe_images(self, repositoryName: str, imageIds: list[dict[str, str]]) -> Any:
            seen.append(imageIds[0]["imageTag"])
            return {"imageDetails": [{"imageDigest": "sha256:ok"}]}

    class _Boto:
        @staticmethod
        def client(*a: Any, **k: Any) -> _Ecr:
            return _Ecr()

    monkeypatch.setitem(__import__("sys").modules, "boto3", _Boto)

    class _Inst:
        instance_id = "astropy__astropy-1"

    assert dispatcher._resolve_harness_digest(["django__django-1"]) == "sha256:ok"
    assert dispatcher._resolve_harness_digest([_Inst()]) == "sha256:ok"  # type: ignore[list-item]
    assert seen == ["5.0.2-django__django-1-inst", "5.0.2-astropy__astropy-1-inst"]


def test_validation_run_provenance_facts_never_raise(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        dispatcher,
        "reproducibility_facts",
        lambda *, harness, instance_ids: {
            "framework_sha": "f6ff628",
            "harness_tool_surface": None,
            "ids": instance_ids,
        },
    )
    facts = image_validation._provenance_facts(["x"])
    assert facts == {"framework_sha": "f6ff628", "ids": ["x"]}

    def _boom(**_: Any) -> dict[str, Any]:
        raise RuntimeError("ecr down")

    monkeypatch.setattr(dispatcher, "reproducibility_facts", _boom)
    assert image_validation._provenance_facts(["x"]) == {}


# --- b. live LLM calls ----------------------------------------------------------


class _Cursor:
    def __init__(self, sink: dict[str, Any]) -> None:
        self.sink = sink
        self.description = [(c,) for c in ("request_id", "status", "attempt")]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, sql: str, params: Any) -> None:
        self.sink["sql"] = sql
        self.sink["params"] = list(params)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [("req-1", "failure", "2")]


class _Conn:
    def __init__(self, sink: dict[str, Any]) -> None:
        self.sink = sink

    def cursor(self) -> _Cursor:
        return _Cursor(self.sink)

    def close(self) -> None:
        return None


def test_list_llm_calls_filters_by_attempt_and_returns_status(monkeypatch: Any) -> None:
    sink: dict[str, Any] = {}
    monkeypatch.setattr(llm_live, "_connect", lambda: _Conn(sink))
    rows = llm_live.list_llm_calls("run-1", instance_id="django__django-1", attempt=2, limit=10)
    assert "eval_attempt" in sink["sql"] and "status" in sink["sql"]
    assert sink["params"] == ["run-1", "django__django-1", "2", 10]  # attempt compared as text
    assert rows[0]["status"] == "failure" and rows[0]["attempt"] == 2


def test_list_llm_calls_without_attempt_is_unchanged(monkeypatch: Any) -> None:
    sink: dict[str, Any] = {}
    monkeypatch.setattr(llm_live, "_connect", lambda: _Conn(sink))
    llm_live.list_llm_calls("run-1", instance_id="django__django-1")
    assert sink["params"] == ["run-1", "django__django-1", 50]


# --- c. per-instance state buckets ---------------------------------------------


def test_get_run_adds_per_instance_buckets(monkeypatch: Any) -> None:
    calls: list[str] = []

    def _one(conn: Any, sql: str, params: Any) -> dict[str, Any]:
        return {
            "run_id": "r",
            "status": "running",
            "created_at": None,
            "config_snapshot": None,
            "summary": None,
            "harness": "codex",
            "model_alias": "minimax-m2.5-codex",
            "cost_usd_total": None,
            "instance_count": 2,
        }

    def _rows(conn: Any, sql: str, params: Any) -> list[dict[str, Any]]:
        calls.append(sql)
        if "DISTINCT ON (instance_id)" in sql:
            return [{"state": "RESOLVED", "count": 1}, {"state": "EVAL_RUNNING", "count": 1}]
        return [
            {"state": "COMPLETED", "count": 2},
            {"state": "RESOLVED", "count": 1},
            {"state": "EVAL_RUNNING", "count": 1},
        ]

    monkeypatch.setattr(queries, "_one", _one)
    monkeypatch.setattr(queries, "_rows", _rows)
    monkeypatch.setattr(queries, "resolve_rate_denominator", lambda conn, run_id: 2)
    row = queries.get_run(object(), "r")
    assert row is not None
    assert row["instance_states"] == [
        {"state": "RESOLVED", "count": 1},
        {"state": "EVAL_RUNNING", "count": 1},
    ]
    assert sum(s["count"] for s in row["states"]) == 4  # the per-row buckets are unchanged
    latest_sql = next(s for s in calls if "DISTINCT ON (instance_id)" in s)
    assert "attempt_number DESC" in latest_sql and "(phase = 'eval') DESC" in latest_sql


@pytest.mark.parametrize("field", ["dataset_name", "image_digest_snapshot"])
def test_api_provenance_schema_has_the_pin_halves(field: str) -> None:
    from swebench_eval.orchestrator.api.schemas import Provenance

    assert field in Provenance.model_fields
