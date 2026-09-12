"""Acceptance §6 of BUILDER6-SPLIT-SUPERVISOR-AND-RESULTS-WRITER-2026-08-31.md
/ CONTROL-PLANE-DECOMPOSITION-DESIGN-2026-08-31.md:

  "Run two results-writer replicas against a real Postgres with a contended
  results queue and assert: no duplicate instance_results rows, no
  double-enqueued eval jobs, and no lost messages. ADR-0018 has always
  *claimed* this is safe; nothing has ever run more than one. Setting dev to
  2 makes the claim continuously tested rather than theoretical."

  "Kill results-writer mid-run: results accumulate in SQS, drain on restart,
  nothing lost, no duplicate eval jobs."

Requires a live Postgres + SQS (the compose stack — ElasticMQ). Marked
``integration`` and deselected by default, same convention as
``test_results_writer_redelivery.py``.

    uv run pytest tests/test_results_writer_multi_replica.py -m integration
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from swebench_eval.database.connection import get_connection
from swebench_eval.orchestrator.control_plane.dispatcher import register_run
from swebench_eval.orchestrator.control_plane.results_writer import _process_result
from swebench_eval.queue.client import delete_message, receive_message, send_message
from swebench_eval.queue.schemas import ResultMessage

pytestmark = pytest.mark.integration


def _clean(run_id: str) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM instance_results WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM run_targets WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))
    conn.commit()
    conn.close()


def _drain_eval_jobs_for(run_id: str) -> list[dict[str, Any]]:
    """Drain eval-jobs completely, returning only the messages for *run_id*.

    Drains the WHOLE queue (deleting every message it reads, including ones
    belonging to other tests/runs) — the same trade-off
    ``test_results_writer_redelivery.py``'s neighbours accept for this shared
    local queue; a clean run_id keeps this test's own assertions correct
    regardless.
    """
    ours: list[dict[str, Any]] = []
    while True:
        msg = receive_message("eval-jobs", wait_seconds=1)
        if msg is None:
            break
        delete_message("eval-jobs", msg["receipt_handle"])
        if msg["body"].get("run_id") == run_id:
            ours.append(msg["body"])
    return ours


def _harness_row_count(run_id: str, instance_id: str, attempt: int) -> tuple[int, int]:
    """(harness rows, eval rows) for this (run, instance, attempt)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) FILTER (WHERE phase = 'harness'),
                          count(*) FILTER (WHERE phase = 'eval')
                   FROM instance_results
                   WHERE run_id = %s AND instance_id = %s AND attempt_number = %s""",
                (run_id, instance_id, attempt),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return int(row[0]), int(row[1])


# ---------------------------------------------------------------------------
# 1. Two replicas racing on the SAME result — the ADR-0018 claim, proven live.
# ---------------------------------------------------------------------------


def test_two_replicas_processing_the_same_result_concurrently(monkeypatch) -> None:
    """Two threads (standing in for two results-writer replicas — each opens
    its OWN real Postgres connection, exactly as ``_process_result`` does
    inside a real process) call ``_process_result`` on the IDENTICAL
    PATCH_READY message at the same time — the real race a redelivered
    at-least-once SQS message creates when two consumers briefly both hold
    it. Asserts run-launch §6.3b's state-rank guard actually serialises this
    under real concurrent Postgres transactions, not just in the SQL's own
    logic read on paper: exactly one harness row, one seeded eval row, and
    (§6.2/ADR-0016) exactly one eval job enqueued — never two.
    """
    run_id = "b6-multi-replica-race"
    instance_id = "inst-race"
    attempt = 1
    _clean(run_id)
    register_run(run_id, "custom_minimal", "cheap-oss-model")
    _drain_eval_jobs_for(run_id)  # clear any stale leftovers from a prior failed run

    result = ResultMessage(
        run_id=run_id,
        instance_id=instance_id,
        attempt_number=attempt,
        phase="harness",
        state="PATCH_READY",
        patch_s3_key="runs/b6/patch.diff",
        touches_test_files=False,
    )

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _replica() -> None:
        try:
            barrier.wait(timeout=10)
            _process_result(result)
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
            errors.append(exc)

    t1 = threading.Thread(target=_replica)
    t2 = threading.Thread(target=_replica)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert not errors, f"a replica raised: {errors}"

    harness_rows, eval_rows = _harness_row_count(run_id, instance_id, attempt)
    assert harness_rows == 1, f"expected exactly one harness row, got {harness_rows}"
    assert eval_rows == 1, f"expected exactly one seeded eval row, got {eval_rows}"

    enqueued = _drain_eval_jobs_for(run_id)
    assert len(enqueued) == 1, (
        f"expected exactly one eval job enqueued for a race between two replicas "
        f"processing the same result, got {len(enqueued)}"
    )
    assert enqueued[0]["instance_id"] == instance_id
    assert enqueued[0]["attempt_number"] == attempt


def test_two_replicas_do_not_double_enqueue_across_a_real_redelivery(monkeypatch) -> None:
    """Same claim, the OTHER shape a redelivery takes: replica A processes the
    message and (in a real deployment) deletes it, but SQS at-least-once
    still redelivers a second copy to replica B before or during that delete
    (visibility-timeout races are exactly what ADR-0018 relies on the
    state-rank guard, not message-delete ordering, to make safe). Sequential
    here (not threaded) because the property under test is idempotency of a
    SECOND delivery arriving after the FIRST already committed — the
    concurrent-arrival shape is covered by the test above.
    """
    run_id = "b6-multi-replica-redelivery"
    instance_id = "inst-redelivery"
    attempt = 1
    _clean(run_id)
    register_run(run_id, "custom_minimal", "cheap-oss-model")
    _drain_eval_jobs_for(run_id)

    result = ResultMessage(
        run_id=run_id,
        instance_id=instance_id,
        attempt_number=attempt,
        phase="harness",
        state="PATCH_READY",
        patch_s3_key="runs/b6/patch2.diff",
        touches_test_files=False,
    )

    # Replica A's delivery.
    _process_result(result)
    # Replica B's redelivery of the SAME message (SQS at-least-once).
    _process_result(result)

    harness_rows, eval_rows = _harness_row_count(run_id, instance_id, attempt)
    assert harness_rows == 1
    assert eval_rows == 1

    enqueued = _drain_eval_jobs_for(run_id)
    assert len(enqueued) == 1, f"redelivery double-enqueued an eval job: {len(enqueued)}"


# ---------------------------------------------------------------------------
# 2. Kill mid-run: results durable in SQS, drain cleanly, nothing lost/duped.
# ---------------------------------------------------------------------------


def test_kill_mid_run_results_accumulate_and_drain_without_loss_or_duplicates() -> None:
    """Simulates results-writer being DOWN while harness work keeps landing
    results on the ``results`` queue (§2 of the design doc: "in-flight
    harness tasks keep running... messages accumulate in the results queue,
    durable, nothing lost"), then a restart draining them. Sends several
    distinct results directly onto the real queue (standing in for what a
    dead consumer would have left unconsumed), then drives one pass of the
    exact receive→process→delete sequence ``run_results_writer``'s loop uses,
    and asserts every one lands exactly once with no loss and no duplicate
    eval enqueue.
    """
    run_id = "b6-kill-mid-run"
    _clean(run_id)
    register_run(run_id, "custom_minimal", "cheap-oss-model")
    _drain_eval_jobs_for(run_id)
    # Also drain any stale `results` messages a previous failed run of this
    # test left behind, so this run's count is exact.
    while receive_message("results", wait_seconds=1) is not None:
        pass  # best-effort: at-least-once means an odd leftover is tolerable,
        # but this loop only fires when there genuinely is nothing left to
        # read (a None return), so it terminates.

    instances = [f"inst-kill-{i}" for i in range(5)]
    for instance_id in instances:
        send_message(
            "results",
            {
                "run_id": run_id,
                "instance_id": instance_id,
                "attempt_number": 1,
                "phase": "harness",
                "state": "PATCH_READY",
                "patch_s3_key": f"runs/b6/{instance_id}.diff",
                "touches_test_files": False,
            },
        )

    # "Restart": drain the queue exactly the way run_results_writer's loop
    # body does per message (receive, parse+process, delete on success).
    from swebench_eval.orchestrator.control_plane.results_writer import _parse_result

    drained = 0
    while True:
        msg = receive_message("results", wait_seconds=2)
        if msg is None:
            break
        result = _parse_result(msg["body"])
        if result.run_id != run_id:
            continue  # a stray message from another test's queue use
        _process_result(result)
        delete_message("results", msg["receipt_handle"])
        drained += 1

    assert drained == len(instances), f"expected to drain {len(instances)}, drained {drained}"

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM instance_results WHERE run_id = %s AND phase = 'harness'",
                (run_id,),
            )
            harness_count = int(cur.fetchone()[0])
    finally:
        conn.close()
    assert harness_count == len(
        instances
    ), f"expected {len(instances)} distinct harness rows (nothing lost), got {harness_count}"

    enqueued = _drain_eval_jobs_for(run_id)
    assert len(enqueued) == len(
        instances
    ), f"expected exactly one eval job per instance, got {len(enqueued)}"
    assert {m["instance_id"] for m in enqueued} == set(instances)
