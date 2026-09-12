"""(V6, switch-to-swebench-verified §8) the ceiling bind-rate report computes
the percentage of attempts that hit the token ceiling, per harness and per
model — so we can say with evidence whether the cap explains any gap vs the
published baselines, instead of not knowing.

The report reads a completed run's ``instance_results`` + ``llm_calls``.  These
tests mock the connection with a fixed rowset and assert the grouping /
percentage arithmetic, so the report is exercised (not a cannot-fail stub) in
CI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import ceiling_bind_rate as report_mod

_ROWS = [
    # harness, model_label, attempts, budget_exceeded, over_ceiling_by_tokens, ceiling_tokens
    ("custom_minimal", "qwen/qwen3-coder-next", 10, 0, 1, 500_000),
    ("mini_swe_agent", "poolside/laguna-xs-2.1", 20, 5, 4, 500_000),
    ("claude_code", "qwen/qwen3-coder-next", 4, 0, 0, 500_000),
]


def _rows() -> list[tuple[str, str, int, int, int, int]]:
    return _ROWS


def _fake_client(rows: list[tuple[str, str, int, int, int, int]]) -> mock.MagicMock:
    cur = mock.MagicMock()
    cur.execute.return_value = None
    cur.fetchall.return_value = rows
    client = mock.MagicMock()
    client.cursor.return_value = mock.MagicMock(__enter__=mock.Mock(return_value=cur))
    return client


def test_report_computes_bind_percentage_per_harness_model(capsys) -> None:
    client = _fake_client(_rows())
    with mock.patch.object(report_mod, "get_connection", return_value=client):
        rc = report_mod.report()

    assert rc == 0
    out = capsys.readouterr().out
    # 10 attempts, 1 over-ceiling -> 10.0% for custom_minimal.
    assert "custom_minimal" in out
    assert "10.0%" in out
    # mini_swe_agent: 5 budget_exceeded (>= 4 over-ceiling) of 20 -> 25.0%.
    assert "25.0%" in out
    # claude_code: nothing bound -> 0.0%.
    assert "0.0%" in out


def test_report_handles_empty_result() -> None:
    client = _fake_client([])
    with mock.patch.object(report_mod, "get_connection", return_value=client):
        rc = report_mod.report()
    assert rc == 0
