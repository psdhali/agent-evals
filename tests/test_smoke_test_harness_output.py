"""scripts/smoke_test.py reads the in-image local-job artifacts back into a HarnessOutput."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import smoke_test


def _write_result(d: Path, **overrides: object) -> None:
    result = {
        "run_id": "r1",
        "instance_id": "django__django-11099",
        "attempt_number": 1,
        "harness": "custom_minimal",
        "model_alias": "cheap-oss-model",
        "state": "PATCH_READY",
        "terminated_reason": "completed",
        "error_category": "",
        "error": "",
        "success": True,
        "wall_clock_s": 12.5,
        "patch_bytes": 10,
        "turns_used": 7,
        "usage": {"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.01, "source": "gateway"},
        "adapter_reported_usage": {"input_tokens": 99, "output_tokens": 20, "cost_usd": 0.01},
        "compactions_fired": None,
        "context_window_tokens": None,
    }
    result.update(overrides)
    (d / "harness_result.json").write_text(json.dumps(result))


def test_reads_patch_usage_and_reason(tmp_path: Path) -> None:
    _write_result(tmp_path)
    (tmp_path / "patch.diff").write_text("diff --git a/x b/x\n")
    out = smoke_test._harness_output_from_dir(tmp_path)
    assert out.patch == "diff --git a/x b/x\n"
    assert out.success is True
    assert out.usage.input_tokens == 100 and out.usage.cost_usd == 0.01
    assert out.adapter_reported_usage is not None
    assert out.adapter_reported_usage.input_tokens == 99
    assert out.terminated_reason == "completed"
    assert out.wall_clock_seconds == 12.5
    assert out.trajectory_path == str(tmp_path / "trajectory.jsonl")


def test_no_patch_means_not_success(tmp_path: Path) -> None:
    _write_result(tmp_path, success=True, patch_bytes=0)
    out = smoke_test._harness_output_from_dir(tmp_path)
    assert out.patch is None
    assert out.success is False


def test_unknown_usage_keys_are_ignored(tmp_path: Path) -> None:
    _write_result(tmp_path, usage={"input_tokens": 1, "future_field": 3})
    (tmp_path / "patch.diff").write_text("x")
    assert smoke_test._harness_output_from_dir(tmp_path).usage.input_tokens == 1


def test_missing_result_exits(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        smoke_test._harness_output_from_dir(tmp_path)
