"""Per-run timeline sampler — the second writer on the capacity observer's tick.

dev/LIVE-RUN-TIMELINE-SITE-DATA-CONTRACT-AND-BUILD-PLAN-2026-09-04.md §4 (owner request
2026-09-04). The publication site wants to show the system responding to load over the
whole of a real run — concurrency climbing against the planner's ceiling, cost accumulating,
the pause banner firing — and the fleet-level control signals were already durable
(``capacity_snapshot``, the observer's own row). What was NOT durable is everything per run:
the in-flight count, the live token / cost sums the shim writes to ``instance_progress:*``
(300 s TTL) and the pacer ledger per alias. This module records those, one row per ACTIVE
run per tick, into ``run_timeline_tick``.

Rules inherited from the observer (they are the same tick, same thread):

  1. **Reads Redis + Aurora, writes Aurora. Decides nothing, actuates nothing.**
  2. **Zero extra Redis reads for the aggregation.** The observer already scans every
     ``instance_progress:*`` key each tick to count live workers; :func:`scan_progress`
     does that ONE scan and returns both the fleet count and the per-run sums, so the
     per-run split costs nothing. The pacer reads are the same handful of HGETALLs
     ``/runs/{id}/pacer`` does.
  3. **NULL is "not measured", never 0.** A failed source leaves its columns NULL and the
     rest of the row still lands; the tick never raises.
  4. **Only active runs.** No row for an idle system — a gap in the chart is correct.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Same threshold the observer and the eval autoscaler use for a dead/dying instance.
STALE_PROGRESS_S = 120.0

# runs.status values with work outstanding (run_launch's ledger + the abort pair's first leg).
ACTIVE_RUN_STATUSES = ("provisioning", "seeding", "dispatching", "running", "aborting")

# The progress-key fields summed per run. Every one is optional in the payload (a pre-pacer
# shim wrote fewer); a run whose live keys carried none of a field sums to None.
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")

# The per-alias pacer fields kept per tick (the ledger the shims share, read exactly the way
# the live /pacer view reads it). The waiter list is deliberately not persisted.
_PACER_FIELDS = (
    "r_tok",
    "r_qps",
    "k_inflight",
    "c_burst",
    "bucket_fill",
    "req_fill",
    "inflight_calls",
    "inflight_tokens",
    "inflight_fill",
    "queue_len",
    "head_waiting_s",
    "admits_60s",
    "over_2s_60s",
    "mean_wait_ms_60s",
    "overloads_60s",
)


@dataclass
class RunProgressAggregate:
    """What the progress-key scan knows about one run at the tick."""

    live: int = 0
    stale: int = 0
    tokens: dict[str, int | None] = field(
        default_factory=lambda: dict.fromkeys(_TOKEN_FIELDS, None)
    )
    cost_usd: float | None = None


def _parse_key(key: Any) -> tuple[str, str, int] | None:
    """``instance_progress:{run_id}:{instance_id}:{attempt}`` -> (run_id, instance_id, attempt).
    Instance ids never carry a colon (``owner__repo-NNN``); run ids never do either."""
    key_s = key.decode() if isinstance(key, bytes) else str(key)
    parts = key_s.split(":")
    if len(parts) < 4 or parts[0] != "instance_progress":
        return None
    try:
        attempt = int(parts[-1])
    except ValueError:
        return None
    return parts[1], ":".join(parts[2:-1]), attempt


def scan_progress(
    client: Any, *, now: float | None = None, stale_s: float = STALE_PROGRESS_S
) -> tuple[int, dict[str, RunProgressAggregate]]:
    """ONE pass over ``instance_progress:*``: (fleet-wide live count, per-run aggregates).

    A key is live when its ``updated_at`` is within *stale_s*; only live keys contribute to
    the token / cost sums (a stale key's numbers are history, not in-flight state). A key
    that does not parse counts for nothing.
    """
    now = time.time() if now is None else now
    per_run: dict[str, RunProgressAggregate] = {}
    fleet_live = 0
    for key in client.scan_iter("instance_progress:*", count=200):
        parsed = _parse_key(key)
        if parsed is None:
            continue
        raw = client.get(key)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            updated_at = float(payload.get("updated_at", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        agg = per_run.setdefault(parsed[0], RunProgressAggregate())
        if now - updated_at > stale_s:
            agg.stale += 1
            continue
        agg.live += 1
        fleet_live += 1
        for f in _TOKEN_FIELDS:
            v = payload.get(f)
            if isinstance(v, (int, float)):
                agg.tokens[f] = int(v) + (agg.tokens[f] or 0)
        c = payload.get("cost_usd")
        if isinstance(c, (int, float)):
            agg.cost_usd = float(c) + (agg.cost_usd or 0.0)
    return fleet_live, per_run


def _counts_to_scalars(phases: list[dict[str, Any]]) -> dict[str, int]:
    """The handful of derived counters the chart draws directly, from the verbatim
    per-phase / per-state counts (which are persisted alongside as ``counts``)."""
    out = {
        "pending": 0,
        "harness_running": 0,
        "eval_running": 0,
        "resolved": 0,
        "unresolved": 0,
        "aborted": 0,
    }
    for p in phases:
        phase, state, n = p.get("phase"), p.get("state"), int(p.get("count") or 0)
        if phase == "harness":
            if state in ("PENDING", "DISPATCHED"):
                out["pending"] += n
            elif state == "HARNESS_RUNNING":
                out["harness_running"] += n
            elif state in ("NEVER_DISPATCHED", "ABORTED_IN_FLIGHT"):
                out["aborted"] += n
        elif phase == "eval":
            if state == "EVAL_RUNNING":
                out["eval_running"] += n
            elif state == "RESOLVED":
                out["resolved"] += n
            elif state == "UNRESOLVED":
                out["unresolved"] += n
    return out


class RunTimelineSampler:
    """Owned by the capacity observer; ``tick`` is called once per observer pass with the
    per-run aggregates its scan produced. Every read guarded; never raises."""

    def __init__(
        self,
        redis_client: Any = None,
        conn_factory: Any = None,
        control_reader: Any = None,
    ) -> None:
        self.enabled = os.environ.get("RUN_TIMELINE_ENABLED", "1") != "0"
        self._redis = redis_client
        self._conn_factory = conn_factory
        self._control_reader = control_reader
        self.rows_written = 0

    # -- sources -----------------------------------------------------------------------------

    def _redis_client(self) -> Any:
        if self._redis is None:
            from swebench_eval.database.redis_client import _get_client

            self._redis = _get_client()
        return self._redis

    def _conn(self) -> Any:
        if self._conn_factory is not None:
            return self._conn_factory()
        from swebench_eval.database.connection import get_connection

        return get_connection()

    @staticmethod
    def _active_runs(conn: Any) -> list[tuple[str, str]]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, status FROM runs WHERE status IN %s ORDER BY created_at",
                (ACTIVE_RUN_STATUSES,),
            )
            rows = cur.fetchall() or []
        out: list[tuple[str, str]] = []
        for r in rows:
            if isinstance(r, dict):
                out.append((str(r["run_id"]), str(r["status"])))
            else:
                out.append((str(r[0]), str(r[1])))
        return out

    def _progress(self, conn: Any, run_id: str) -> dict[str, Any] | None:
        try:
            from swebench_eval.orchestrator.api import queries

            return queries.get_run_progress(conn, run_id)
        except Exception:
            logger.debug("run-timeline: progress read failed for %s", run_id, exc_info=True)
            return None

    @staticmethod
    def _landed(conn: Any, run_id: str) -> tuple[float | None, dict[str, int | None]]:
        """Cost + tokens already landed in instance_results (harness rows). None when no row
        carried the figure — a run with no instrumented harness must not read as free."""
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT SUM(cost_usd), SUM(input_tokens), SUM(output_tokens),
                              SUM(cached_tokens), SUM(reasoning_tokens),
                              COUNT(cost_usd), COUNT(input_tokens)
                       FROM instance_results
                       WHERE run_id = %s AND phase = 'harness'""",
                    (run_id,),
                )
                row = cur.fetchone()
        except Exception:
            logger.debug("run-timeline: landed read failed for %s", run_id, exc_info=True)
            return None, dict.fromkeys(_TOKEN_FIELDS, None)
        if not row:
            return None, dict.fromkeys(_TOKEN_FIELDS, None)
        vals = list(row.values()) if isinstance(row, dict) else list(row)
        cost = float(vals[0]) if vals[5] and vals[0] is not None else None
        toks: dict[str, int | None] = dict.fromkeys(_TOKEN_FIELDS, None)
        if vals[6]:
            for f, v in zip(_TOKEN_FIELDS, vals[1:5], strict=True):
                toks[f] = int(v) if v is not None else None
        return cost, toks

    def _pacer(self, conn: Any, run_id: str) -> dict[str, Any] | None:
        try:
            from swebench_eval.orchestrator.api import pacer_view, queries

            targets = queries.list_run_targets(conn, run_id)
            client = self._redis_client()
            out: dict[str, Any] = {}
            for harness, alias in pacer_view.resolve_pacer_aliases(targets):
                st = pacer_view.read_alias_state(client, alias, harness)
                if not st.get("measured"):
                    out[alias] = None  # no pacer keys: not measured, never zeros
                    continue
                out[alias] = {k: st.get(k) for k in _PACER_FIELDS}
            return out
        except Exception:
            logger.debug("run-timeline: pacer read failed for %s", run_id, exc_info=True)
            return None

    def _control(self) -> dict[str, bool | None]:
        try:
            if self._control_reader is not None:
                view = self._control_reader()
            else:
                from swebench_eval.control import state as control_state

                view = control_state.read()
            return {
                "harness_paused": bool(view.harness_paused),
                "eval_paused": bool(view.eval_paused),
                "gateway_paused": bool(view.gateway_paused),
                "control_stale": bool(view.stale),
            }
        except Exception:
            logger.debug("run-timeline: control read failed", exc_info=True)
            return {
                "harness_paused": None,
                "eval_paused": None,
                "gateway_paused": None,
                "control_stale": None,
            }

    # -- the row -----------------------------------------------------------------------------

    def sample(
        self, conn: Any, run_id: str, status: str, agg: RunProgressAggregate | None
    ) -> dict[str, Any]:
        """One run's row at this tick. Pure assembly over the guarded sources."""
        agg = agg or RunProgressAggregate()
        progress = self._progress(conn, run_id)
        phases = list(progress.get("phases") or []) if progress else []
        scalars = _counts_to_scalars(phases) if progress else {}
        landed_cost, landed_tok = self._landed(conn, run_id)

        def _plus(a: float | None, b: float | None) -> int | float | None:
            if a is None and b is None:
                return None
            return (a or 0) + (b or 0)

        cost_live = _plus(landed_cost, agg.cost_usd)
        row: dict[str, Any] = {
            "run_id": run_id,
            "run_status": status,
            "in_flight": agg.live if agg is not None else None,
            "stale": agg.stale if agg is not None else None,
            "pending": scalars.get("pending"),
            "harness_running": scalars.get("harness_running"),
            "eval_running": scalars.get("eval_running"),
            "resolved": scalars.get("resolved"),
            "unresolved": scalars.get("unresolved"),
            "aborted": scalars.get("aborted"),
            "expected": progress.get("expected") if progress else None,
            "denominator": progress.get("denominator") if progress else None,
            "tok_in": _plus(landed_tok["input_tokens"], agg.tokens["input_tokens"]),
            "tok_out": _plus(landed_tok["output_tokens"], agg.tokens["output_tokens"]),
            "tok_cached": _plus(landed_tok["cached_tokens"], agg.tokens["cached_tokens"]),
            "tok_reasoning": _plus(landed_tok["reasoning_tokens"], agg.tokens["reasoning_tokens"]),
            "cost_usd_live": round(cost_live, 6) if cost_live is not None else None,
            "cost_usd_landed": landed_cost,
            "counts": phases if progress else None,
            "pacer": self._pacer(conn, run_id),
        }
        row.update(self._control())
        return row

    _INSERT = """INSERT INTO run_timeline_tick
        (ts, run_id, run_status, in_flight, stale, pending, harness_running, eval_running,
         resolved, unresolved, aborted, expected, denominator,
         tok_in, tok_out, tok_cached, tok_reasoning, cost_usd_live, cost_usd_landed,
         harness_paused, eval_paused, gateway_paused, control_stale, counts, pacer)
        VALUES (now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
        ON CONFLICT (run_id, ts) DO NOTHING"""

    def _write(self, conn: Any, rows: list[dict[str, Any]]) -> None:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    self._INSERT,
                    (
                        r["run_id"],
                        r["run_status"],
                        r["in_flight"],
                        r["stale"],
                        r["pending"],
                        r["harness_running"],
                        r["eval_running"],
                        r["resolved"],
                        r["unresolved"],
                        r["aborted"],
                        r["expected"],
                        r["denominator"],
                        r["tok_in"],
                        r["tok_out"],
                        r["tok_cached"],
                        r["tok_reasoning"],
                        r["cost_usd_live"],
                        r["cost_usd_landed"],
                        r["harness_paused"],
                        r["eval_paused"],
                        r["gateway_paused"],
                        r["control_stale"],
                        json.dumps(r["counts"]) if r["counts"] is not None else None,
                        json.dumps(r["pacer"]) if r["pacer"] is not None else None,
                    ),
                )
        conn.commit()

    def tick(self, per_run: dict[str, RunProgressAggregate]) -> list[dict[str, Any]]:
        """Sample every active run and write the rows. NEVER raises; returns what it wrote
        (empty when nothing is active — the correct gap)."""
        if not self.enabled:
            return []
        try:
            conn = self._conn()
        except Exception:
            logger.debug("run-timeline: no Aurora connection this tick", exc_info=True)
            return []
        try:
            runs = self._active_runs(conn)
            if not runs:
                return []
            rows = [
                self.sample(conn, run_id, status, per_run.get(run_id)) for run_id, status in runs
            ]
            self._write(conn, rows)
            self.rows_written += len(rows)
            return rows
        except Exception:
            logger.exception("run-timeline tick failed (transient); continues next tick")
            return []
        finally:
            try:
                conn.close()
            except Exception:
                logger.debug("run-timeline: connection close failed", exc_info=True)
