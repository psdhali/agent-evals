"""model_tpm_observations / model_ceilings against a real Postgres —
BUILDER4-AUTOSCALER-TPM-CEILING-DISCOVERY-DESIGN-2026-08-31.md §3 (reviewer F3: a view, not a
consumer-maintained pointer)."""

from __future__ import annotations

import pytest

from swebench_eval.orchestrator.control_plane.ceiling_discovery import record_manual_ceiling

pytestmark = pytest.mark.integration

_PREFIX = "cd-test-"


def _db():
    from swebench_eval.database.connection import get_connection

    return get_connection()


@pytest.fixture(autouse=True)
def _clean_rows():
    def _clean():
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM model_tpm_observations WHERE model_alias LIKE %s", (f"{_PREFIX}%",)
                )
            conn.commit()
        finally:
            conn.close()

    _clean()
    yield
    _clean()


def _view_row(model_alias: str, value_kind: str = "tpm"):
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT value, ceiling_source FROM model_ceilings
                   WHERE model_alias = %s AND value_kind = %s""",
                (model_alias, value_kind),
            )
            return cur.fetchone()
    finally:
        conn.close()


def _insert(model_alias: str, event_type: str, tpm_value: int) -> None:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO model_tpm_observations (model_alias, event_type, tpm_value)
                   VALUES (%s, %s, %s)""",
                (model_alias, event_type, tpm_value),
            )
        conn.commit()
    finally:
        conn.close()


def test_manual_entry_is_visible_through_the_view() -> None:
    model = f"{_PREFIX}laguna"
    record_manual_ceiling(model, 1_500_000, notes="test")

    # Manual entries default to the headline value_kind (burst_admission_tokens, §6).
    row = _view_row(model, value_kind="burst_admission_tokens")
    assert row == (1_500_000, "manual")


def test_an_overload_row_is_never_returned_as_the_current_ceiling() -> None:
    """§3's WHERE clause — an overload is evidence, never itself a ceiling to start from. Even
    though it's the MOST RECENT row, the view must fall back to the prior discovery/manual value."""
    model = f"{_PREFIX}qwen"
    _insert(model, "discovery_initial", 2_000_000)
    _insert(model, "overload", 800_000)

    row = _view_row(model)
    assert row == (2_000_000, "discovery_initial")


def test_the_view_returns_the_most_recent_qualifying_row() -> None:
    """Recency-wins is the default reconciliation policy (F3) — a later reconciliation_peak
    supersedes an earlier discovery_initial for the same model."""
    model = f"{_PREFIX}recency"
    _insert(model, "discovery_initial", 1_000_000)
    _insert(model, "reconciliation_peak", 1_200_000)

    row = _view_row(model)
    assert row == (1_200_000, "reconciliation_peak")


def test_value_kinds_are_isolated_rows_in_the_view() -> None:
    """§6: the view is per (model_alias, value_kind) — a fresh burst-edge value must not
    shadow the paced-rate value, and each kind keeps its own recency."""
    model = f"{_PREFIX}kinds"
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO model_tpm_observations
                       (model_alias, event_type, value_kind, tpm_value)
                   VALUES (%s, 'discovery_initial', 'burst_admission_tokens', 2_000_000),
                          (%s, 'discovery_initial', 'paced_rate_tok_per_s', 35_000)""",
                (model, model),
            )
        conn.commit()
    finally:
        conn.close()

    assert _view_row(model, "burst_admission_tokens") == (2_000_000, "discovery_initial")
    assert _view_row(model, "paced_rate_tok_per_s") == (35_000, "discovery_initial")


def test_two_different_models_never_leak_into_each_others_view_row() -> None:
    model_a = f"{_PREFIX}laguna-iso"
    model_b = f"{_PREFIX}qwen-iso"
    record_manual_ceiling(model_a, 1_000_000)
    record_manual_ceiling(model_b, 2_000_000)

    assert _view_row(model_a, "burst_admission_tokens") == (1_000_000, "manual")
    assert _view_row(model_b, "burst_admission_tokens") == (2_000_000, "manual")
