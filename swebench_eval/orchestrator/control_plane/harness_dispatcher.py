"""H3+H4 — the task-per-instance harness dispatcher (ADR-0030/0032/0034).

Receives from ``harness-jobs`` and launches ONE Fargate task per job against a
PRE-REGISTERED task-definition family per env image (H2/H3).  ECS cannot select
an image at run-task time, so the family IS the image: the dispatcher parses
the env hash off the job's ``env_image_key`` and picks ``eval-dev-harness-<hash>``.

ADR-0034 (M1) + H4: **admission control is a gate BEFORE ``receive_message``,
never after.**  A paused consumer that receives and returns a message increments
that message's ``ApproximateReceiveCount``; a poll loop does so in a tight
cycle, so ``maxReceiveCount = 5`` reaches the DLQ within seconds.  The gate
answers "may this dispatcher receive at all" with a *reason* (paused /
at_capacity / stalled), surfaced in logs and ``capacity_snapshot`` so a stopped
dispatcher is distinguishable from an idle one (H4 §7).

Per-instance abort is checked AFTER receive (it needs the job's ``run_id``) —
an aborted run's message is discarded on pick-up as ``NEVER_DISPATCHED`` and
never launched (ADR-0034 M1 gate #2).

The dispatcher does **not** delete the message on success — the launched task
extends visibility via its heartbeat and deletes on completion, so at-least-once
delivery is unchanged (ADR-0030 decision 4).  The ONLY delete is an aborted
run's discarded message.

ADR-0032: ``containerOverrides`` carries a fixed-width job REFERENCE (~200 B),
not the job; the task loads the instance from the mirror.  A1-7 asserts LIVE
EC2 state (the configured harness subnets have no ``0.0.0.0/0``) before launch.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any

from swebench_eval import aws_names
from swebench_eval.control import state as control_state
from swebench_eval.gateway.pacer import overload_key, paced_key, pacer_cfg_key
from swebench_eval.queue.client import delete_message, receive_message, send_message
from swebench_eval.queue.schemas import HarnessJob, JobReference, ResultMessage

logger = logging.getLogger(__name__)

# ADR-0043: one task-definition family per INSTANCE ("eval-dev-harness-<instance_id>",
# pointing at that instance's -inst image).  The 4.1.0-era env-hash families are gone.
_FAMILY_PREFIX = f"{aws_names.name_prefix()}-harness-"
# The dispatcher's own ECS family shares _FAMILY_PREFIX (modules/ecs-service-harness-
# dispatcher: "${name_prefix}-harness-dispatcher"); it is never a harness task.
_DISPATCHER_FAMILY = f"{aws_names.name_prefix()}-harness-dispatcher"


def _is_harness_family_group(group: str | None) -> bool:
    """True for an ECS task ``group`` of a harness family task-def ("family:<family>").

    Service tasks carry "service:<name>" and never match; the dispatcher's own family
    carries the harness prefix and is excluded by name.
    """
    if not group or not group.startswith("family:"):
        return False
    family = group[len("family:") :]
    return family.startswith(_FAMILY_PREFIX) and family != _DISPATCHER_FAMILY


# The family task defs (H2) name their container "harness-worker"; the dispatcher
# overrides its command to the single-job entrypoint.
_CONTAINER_NAME = "harness-worker"
_JOB_COMMAND = ["harness-worker-job"]

# Initial visibility when the dispatcher receives: gives the freshly-launched task
# time to start its heartbeat before the message becomes visible again and risks a
# double-launch.  The task's own heartbeats take over from there.
_INITIAL_VISIBILITY_SECONDS = 600

# H4 §5: how often the dispatcher reconciles its running-task count against
# ListTasks ground truth, and how many launches force a refresh (whichever first).
_GROUND_TRUTH_REFRESH_S = 10
_GROUND_TRUTH_EVERY_N_LAUNCHES = 5


class DispatchRefusedError(RuntimeError):
    """A job cannot be launched (unknown env / no family).  Named, never silent."""


class RunTaskCapacityError(RuntimeError):
    """Transient Fargate capacity — back off and retry, do not spend a receive."""


class RunTaskQuotaError(RuntimeError):
    """The ceiling is wrong / quota moved — stop dispatching, alert."""


class RunTaskThrottleError(RuntimeError):
    """RunTask API throttling (ADR-0030 measured 30-60× headroom; this is a defect)."""


class RunTaskPermanentError(RuntimeError):
    """Bad task definition / subnet / role — permanent; retrying cannot help."""


@dataclass(frozen=True)
class DispatchDecision:
    """Whether the dispatcher may receive at all, and why.

    ``allowed == False`` when any shared M1+H4 gate blocks: pause, at capacity,
    or stalled (ground-truth check failing).  ``reason`` is loggable and feeds
    ``capacity_snapshot.binding_constraint`` (H4 §7).
    """

    allowed: bool
    reason: str = ""
    backoff_s: int = 5

    @classmethod
    def permit(cls) -> DispatchDecision:
        return cls(allowed=True)

    @classmethod
    def block(cls, reason: str, backoff_s: int = 5) -> DispatchDecision:
        return cls(allowed=False, reason=reason, backoff_s=backoff_s)


def _family_for_job(job: HarnessJob) -> str:
    """Resolve the task-definition family to launch for *job*.

    ADR-0043: the family is per INSTANCE — ``eval-dev-harness-<instance_id>``,
    pointing at the ``-inst`` image whose ``/opt/testbed-prepared.json``
    sentinel the worker requires.  There is no env-hash fallback any more (no
    env images exist to fall back to): an unregistered per-instance family is
    a refusal NAMING the family, never a silent default image.

    Existence is checked with ``describe_task_definition``: a registered family
    resolves (no exception); a NOT-FOUND (ECS ClientException, "Unable to
    describe task definition") → refused as missing; any other error → refused
    as unresolvable.  The not-found case is matched by MESSAGE text (not
    exception type) so tests can raise a plain error carrying the same
    signature.
    """
    per_instance = f"{_FAMILY_PREFIX}{job.instance_id}"

    client = _ecs_client()
    try:
        client.describe_task_definition(taskDefinition=per_instance)
        return per_instance
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        if "ClientException" not in text and "not found" not in text.lower():
            raise DispatchRefusedError(
                f"job {job.instance_id}: could not resolve task-definition family " f"({text})"
            ) from exc
        raise DispatchRefusedError(
            f"job {job.instance_id}: no per-instance task-definition family {per_instance} is "
            "registered — regenerate the families from the cache manifest and apply "
            "(no silent default image)"
        ) from exc


def _env_from_env() -> dict[str, Any]:
    """The dispatcher's own environment: cluster + the HARNESS task's network.

    A1 / ADR-0033: the launched task must run in the HARNESS-ISOLATED network —
    dedicated subnets and the harness SG (no ``0.0.0.0/0``).  There is no longer a
    "private subnets" fallback: the posture is structural, not a remembered switch.
    """
    cluster = os.environ.get("CLUSTER", "")
    subnets = json.loads(os.environ.get("HARNESS_SUBNET_IDS", "[]"))
    security_groups = json.loads(os.environ.get("HARNESS_SECURITY_GROUP_IDS", "[]"))
    if not cluster or not subnets or not security_groups:
        raise RuntimeError(
            "harness-dispatcher misconfigured: CLUSTER / HARNESS_SUBNET_IDS / "
            "HARNESS_SECURITY_GROUP_IDS must all be set"
        )
    return {"cluster": cluster, "subnets": subnets, "security_groups": security_groups}


def _assert_isolated_network(env: dict[str, Any]) -> None:
    """Refuse to dispatch unless the configured harness network is actually isolated.

    A1-7 / ADR-0033 decision 4 — the gate reads LIVE state, not a variable file.
    The realistic failure is a value that is SET and WRONG (a tfvars copy-paste of
    the private subnets' ids or the task SG), and a check against the module
    inputs cannot see that.  So at every startup we ask EC2 what routes the
    configured subnets carry and what egress the configured SGs have, and refuse
    to launch anything if either has a default route.
    """
    import boto3

    ec2 = boto3.client("ec2", region_name=aws_names.region())

    subnets_resp = ec2.describe_subnets(SubnetIds=list(env["subnets"]))
    vpc_id = subnets_resp["Subnets"][0].get("VpcId", "") if subnets_resp.get("Subnets") else ""
    for subnet in subnets_resp.get("Subnets", []):
        routes = _subnet_default_routes(ec2, subnet["SubnetId"], vpc_id)
        if routes:
            raise RuntimeError(
                f"A1 gate REFUSES dispatch: harness subnet {subnet['SubnetId']} still has a "
                f"default route {routes} — the harness is not isolated; nothing will be launched"
            )

    sg_resp = ec2.describe_security_groups(GroupIds=list(env["security_groups"]))
    for group in sg_resp.get("SecurityGroups", []):
        for rule in group.get("IpPermissionsEgress", []):
            for ipr in rule.get("IpRanges", []):
                if ipr.get("CidrIp") == "0.0.0.0/0":
                    raise RuntimeError(
                        f"A1 gate REFUSES dispatch: harness security group {group['GroupId']} "
                        "has open egress to 0.0.0.0/0 — the harness is not isolated; "
                        "nothing will be launched"
                    )
            for ip6 in rule.get("Ipv6Ranges", []):
                if ip6.get("CidrIpv6") == "::/0":
                    raise RuntimeError(
                        f"A1 gate REFUSES dispatch: security group {group['GroupId']} "
                        "has open egress to ::/0 — the harness is not isolated; "
                        "nothing will be launched"
                    )


def _subnet_default_routes(ec2: Any, subnet_id: str, vpc_id: str) -> list[str]:
    """Any DEFAULT route that applies to ``subnet_id`` (the leak the gate watches).

    A route table is found by explicit ``association.subnet-id`` first; a subnet
    with no explicit association inherits the VPC MAIN route table. If neither
    resolves, REFUSE — "could not confirm isolation" must never read as "clean".
    Only genuine default routes count: ``0.0.0.0/0`` and ``::/0``.  A route
    keyed by ``DestinationPrefixListId`` is the S3 gateway endpoint — expected.
    """
    rts = ec2.describe_route_tables(
        Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
    ).get("RouteTables", [])
    if not rts and vpc_id:
        rts = ec2.describe_route_tables(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "association.main", "Values": ["true"]},
            ]
        ).get("RouteTables", [])
    if not rts:
        raise RuntimeError(
            f"A1 gate REFUSES dispatch: cannot resolve a route table for harness subnet "
            f"{subnet_id} — cannot prove isolation; nothing will be launched"
        )

    defaults: list[str] = []
    for rt in rts:
        for route in rt.get("Routes", []):
            if route.get("DestinationCidrBlock") == "0.0.0.0/0":
                defaults.append("0.0.0.0/0")
            if route.get("DestinationIpv6CidrBlock") == "::/0":
                defaults.append("::/0")
    return defaults


def _enforce_per_run_key() -> bool:
    """run-launch: the ADR-0035 cutover switch, same convention as
    ``ENFORCE_CACHE_GATE`` (dispatcher.py) — off by default so local/dev runs
    (no minted key, no run_key_cache entry) behave as before; set to "1" once
    the harness-family task definition no longer carries
    ``LITELLM_MASTER_KEY`` and every launch path is confirmed to go through
    ``launch_run()`` (D2).  Until then a missing cached key logs loudly and
    falls back to the master key (routing.gateway_api_key) rather than
    refusing every launch.
    """
    return os.environ.get("ENFORCE_PER_RUN_KEY", "0") == "1"


def _reference_for(job: HarnessJob, receipt_handle: str) -> JobReference:
    from swebench_eval.orchestrator.control_plane import run_key_cache

    litellm_api_key = run_key_cache.fetch(job.run_id)
    if litellm_api_key is None:
        if _enforce_per_run_key():
            raise DispatchRefusedError(
                f"job {job.run_id}/{job.instance_id}: no per-run LiteLLM key cached "
                "(ADR-0035) — refusing to launch with the admin master key "
                "(ENFORCE_PER_RUN_KEY=1). The run either predates launch_run() or its "
                "cache entry expired/was lost."
            )
        logger.warning(
            "no per-run LiteLLM key cached for run %s (job %s/%s attempt %d) — task will "
            "fall back to LITELLM_MASTER_KEY (ENFORCE_PER_RUN_KEY is off); set it once "
            "every launch path is confirmed to go through launch_run()",
            job.run_id,
            job.run_id,
            job.instance_id,
            job.attempt_number,
        )

    return JobReference(
        run_id=job.run_id,
        instance_id=job.instance_id,
        attempt_number=job.attempt_number,
        receipt_handle=receipt_handle,
        harness_name=job.harness_name,
        model_alias=job.model_alias,
        timeout_seconds=job.timeout_seconds,
        max_tokens_per_instance=job.max_tokens_per_instance,
        max_cost_usd_per_instance=job.max_cost_usd_per_instance,
        max_turns_per_instance=job.max_turns_per_instance,
        # Compaction build: the resolved context window MUST travel to the task.
        # Omitting it made every harness read None -> compaction silently disabled
        # (compute_threshold(0) -> 0), so D-1's window never reached the task and
        # instance_results.context_window_tokens stayed NULL regardless of deploy.
        context_window_tokens=job.context_window_tokens,
        # ADR-0037 / M0 §4.3: stamp when RunTask is called — the metadata
        # endpoint cannot give it (provision_s = dispatched_at -> CreatedAt).
        dispatched_at=time.time(),
        # ADR-0037 / M0-6: carried so the deployed task (which never receives)
        # still has queue_wait_s.  Already computed on the job by the loop.
        queue_wait_s=job.queue_wait_s,
        # run-launch (ADR-0035 decision 1): the run's per-run LiteLLM key,
        # cached by PROVISION (run_key_cache.py) — never Aurora, never a log
        # (rule 3).  None falls through to the master key at the worker
        # (routing.gateway_api_key), unless ENFORCE_PER_RUN_KEY refused above.
        litellm_api_key=litellm_api_key,
    )


def _compute_queue_wait(job: HarnessJob, attributes: dict[str, str] | None) -> None:
    """Set ``job.queue_wait_s`` from the SQS attributes of THIS receive (M0-6).

    The deployed task-per-instance path never calls ``receive_message`` (RunTask
    launches it), so queue_wait_s would be permanently NULL there.  The
    dispatcher is the process that received the message and holds its
    attributes — compute the wait here and carry it on the JobReference the same
    way it carries ``dispatched_at``.
    """
    from swebench_eval.workers import timing as tmod

    sent, received = tmod.parse_sqs_attributes(attributes)
    job.queue_wait_s = tmod.seconds_between(sent, received)


def _ecs_client() -> Any:
    import boto3

    return boto3.client("ecs", region_name=aws_names.region())


# ---------------------------------------------------------------------------
# H4 admission — the ceiling, the ground truth, and the failure classes
# ---------------------------------------------------------------------------


def _max_concurrent_from_env() -> int:
    """The dispatcher's concurrency ceiling, from its one Terraform variable.

    H4 §4.1: NO silent default.  A default is how an unbounded dispatcher ships
    looking configured; absent configuration must refuse to start.  The initial
    value is set from the gateway-RPM term (~50), then raised on Stage C's
    evidence (LLM calls per minute).
    """
    raw = os.environ.get("MAX_CONCURRENT_HARNESS_TASKS", "")
    if not raw:
        raise RuntimeError(
            "harness-dispatcher refuses to start: MAX_CONCURRENT_HARNESS_TASKS is unset. "
            "The dispatcher is unbounded without it (H4); set it from the gateway-RPM term."
        )
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"MAX_CONCURRENT_HARNESS_TASKS={raw!r} is not an integer; refusing to start"
        ) from exc
    if value <= 0:
        raise RuntimeError(f"MAX_CONCURRENT_HARNESS_TASKS={value} must be > 0")
    return value


def _read_run_overrides(client: Any = None) -> dict[str, Any]:
    """The §6.7 per-run overrides run_launch publishes at launch (one global hash,
    last-writer-wins, owner-stamped). Guarded: any failure or absent key returns {} —
    the dispatcher then runs on its static env config, the safe direction."""
    try:
        if client is None:
            from swebench_eval.database.redis_client import _get_client

            client = _get_client()
        raw = client.hgetall(_run_overrides_key())
        return {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in raw.items()
        }
    except Exception:
        logger.debug("run-overrides read failed (static config stays in force)", exc_info=True)
        return {}


def _run_overrides_key() -> str:
    # Deferred import so this module never imports run_launch at module load (run_launch
    # already imports nothing from here; keep it acyclic-by-construction anyway).
    from swebench_eval.orchestrator.control_plane.run_launch import AUTOSCALER_OVERRIDES_KEY

    return AUTOSCALER_OVERRIDES_KEY


class _DispatcherAdmission:
    """The dispatcher's admission state: ceiling, local counter, ground truth.

    The ceiling is CLUSTER-WIDE and shared — two concurrent runs share one
    quota (H4 §5).  The local counter learns neither completions nor orphaned
    tasks; ground truth (``ListTasks``) reconciles it periodically.  Bias
    reconciliation toward OVER-counting (ADR-037 §5): an over-count throttles
    (safe); an under-count breaches the quota (unsafe)."""

    def __init__(self, ceiling: int, autoscaler: Autoscaler | None = None) -> None:
        self.ceiling = ceiling
        # §6.7: the per-run max_parallel_harness_tasks cap, refreshed on the same cadence
        # as ground truth. None = no run override published. Min-wins vs the static env
        # ceiling; never widens it.
        self._run_cap: int | None = None
        # The L2 planner (defined below in this same file — part of the dispatcher, not a
        # separate component). None in the isolated test path; observe mode never blocks.
        self.autoscaler = autoscaler
        if autoscaler is not None:
            # Review F5 (2026-09-03): the planner knows the hard cap it is min-wins with, so
            # a cap-bound hold is a recorded verdict (`static_cap`), not a silent early return.
            autoscaler.hard_cap_fn = self.effective_ceiling
        # Running tasks at the last ListTasks refresh (the authoritative count),
        # plus launches since then (over-count-biased within the refresh window
        # — a freshly-launched task is not yet in ListTasks).
        self._gt_in_flight = 0
        self._last_gt_at = 0.0
        self._launches_since_gt = 0
        self._stalled_reason = ""

    def running_count(self) -> int:
        """RUNNING tasks that belong to a HARNESS family.  Raises on failure (fail-closed).

        Bring-up 2026-09-03: ListTasks with only ``cluster`` + ``desiredStatus`` returns
        every task in the cluster — the orchestrator-api, run-supervisor, results-writer,
        gateway, git-mirror and this dispatcher included (8 of them at an idle bring-up).
        In observe mode that only cost eight slots of the static cap; with the planner
        live it read as eight *booting* harness tasks on default budgets, the ceiling
        came out at exactly eight, and every launch was gated forever (no harness task
        ever started, so nothing could raise it).  ListTasks has no prefix filter and
        the ~50 families are one call each, so: ListTasks, then DescribeTasks in batches
        of 100 and keep the tasks whose ``group`` is ``family:eval-dev-harness-<...>``,
        minus the dispatcher's own family (which shares the prefix — the runbook's
        §4.3 quirk).  Failure anywhere raises, as before: the H4 gate fails closed.
        """
        ecs = _ecs_client()
        cluster = os.environ.get("CLUSTER", "")
        arns: list[str] = []
        tok: str | None = None
        while True:
            kw: dict[str, Any] = {
                "cluster": cluster,
                "desiredStatus": "RUNNING",
                "maxResults": 100,
            }
            if tok:
                kw["nextToken"] = tok
            resp = ecs.list_tasks(**kw)
            arns.extend(resp.get("taskArns", []))
            tok = resp.get("nextToken")
            if not tok:
                break
        count = 0
        for i in range(0, len(arns), 100):
            desc = ecs.describe_tasks(cluster=cluster, tasks=arns[i : i + 100])
            count += sum(
                1 for t in desc.get("tasks", []) if _is_harness_family_group(t.get("group"))
            )
        return count

    def _refresh(self) -> None:
        try:
            self._gt_in_flight = self.running_count()
            self._last_gt_at = time.time()
            self._launches_since_gt = 0
            self._stalled_reason = ""
        except Exception:
            self._stalled_reason = "ListTasks failed"
            logger.exception("H4: ListTasks failed; refusing dispatch (fail-closed)")
        # §6.7 run cap — guarded separately: a Redis failure must not stall dispatch (the
        # last-read cap simply stands; absent key clears it back to static-only).
        overrides = _read_run_overrides()
        raw_cap = overrides.get("max_parallel")
        try:
            self._run_cap = int(raw_cap) if raw_cap else None
        except (TypeError, ValueError):
            logger.warning("run-overrides max_parallel=%r unusable; ignoring", raw_cap)
            self._run_cap = None

    def effective_ceiling(self) -> int:
        """min(static env ceiling, per-run cap) — the run override can only tighten."""
        if self._run_cap is not None and self._run_cap > 0:
            return min(self.ceiling, self._run_cap)
        return self.ceiling

    def in_flight(self) -> int:
        """The in-flight count we enforce, over-count-biased (H4 §5).

        ``ground truth + launches since the last refresh``.  The ground truth
        (``ListTasks``) is the authority for how many are actually running —
        it sees completions the dispatcher never observes, AND orphaned tasks
        from a crashed shard the dispatcher never launched.  The ``launches
        since refresh`` term keeps a freshly-launched task (not yet visible in
        ListTasks) bounded — over-counting only within the refresh window is the
        bias ADR-0037 requires (an over-count throttles; an under-count
        breaches the quota).
        """
        return self._gt_in_flight + self._launches_since_gt

    def may_launch(self) -> DispatchDecision:
        """Can this dispatcher receive/launch today?  Shared M1+H4 gate.

        Ordering (each matters, none is optional):
        - refresh ground truth when due (H4 §5) — a stale count is over-count-
          biased; the refresh may set ``_stalled_reason`` (fail-closed).
        - paused (M1 / ADR-0034 §3): halt BEFORE any receive — a paused
          consumer that receives and returns a message DLQs the whole backlog.
        - stalled (H4 §5 fail-closed): cannot confirm capacity -> REFUSE.
        - at capacity (H4 §4): in_flight >= ceiling -> block("at_capacity").
        - the autoscaler's projected gates (exact-design §5) — LAST, and only
          binding in live mode: the static ceiling above stays in force in
          every mode (min-wins, design §6.7.1). Observe mode computes and
          publishes the decision record but never blocks.
        """
        self._maybe_refresh()
        if control_state.is_paused("harness"):
            return DispatchDecision.block("paused")
        if self._stalled_reason:
            return DispatchDecision.block(self._stalled_reason)
        tick = getattr(self.autoscaler, "maybe_tick", None)
        if callable(tick):
            # Review F5 (2026-09-03): tick (and publish) BEFORE the static-cap early return —
            # design §5 says the decision record is emitted every tick including no-ops, and
            # at full fleet the cap is exactly where the record and the UI verdict went stale.
            # maybe_tick rate-limits itself; gate() below reuses this tick's decision.
            tick(self.in_flight())
        if self.in_flight() >= self.effective_ceiling():
            return DispatchDecision.block(
                f"at_capacity ({self.in_flight()}/{self.effective_ceiling()})", backoff_s=5
            )
        if self.autoscaler is not None:
            allowed, reason = self.autoscaler.gate(self.in_flight())
            if not allowed:
                return DispatchDecision.block(reason, backoff_s=5)
        return DispatchDecision.permit()

    def _maybe_refresh(self) -> None:
        """Refresh ground truth when due; a failure sets a stall (fail-closed)."""
        if time.time() - self._last_gt_at <= _GROUND_TRUTH_REFRESH_S:
            return
        self._refresh()

    def note_launch(self) -> None:
        """Record a successful launch: it is in flight until ground truth catches
        up.  Every N launches force an early refresh so the count tracks truth
        even in a burst (H4 §5)."""
        self._launches_since_gt += 1
        if self._launches_since_gt >= _GROUND_TRUTH_EVERY_N_LAUNCHES:
            self._refresh()


def _classify_run_task_failure(reason: str, detail: str) -> Exception:
    """Map a RunTask failure to the class that dictates the correct response (H4 §6)."""
    low = (reason + " " + detail).lower()
    if any(t in low for t in ("capacity", "capacityexceeded", "outofcapacity", "resource")):
        return RunTaskCapacityError(f"RunTask capacity ({reason}: {detail})")
    if any(t in low for t in ("quota", "limitexceeded", "toomanytasks", "ecsthrottling")):
        return RunTaskQuotaError(f"RunTask quota ({reason}: {detail})")
    if "throttl" in low:
        return RunTaskThrottleError(f"RunTask throttled ({reason}: {detail})")
    return RunTaskPermanentError(f"RunTask permanent ({reason}: {detail})")


class _NullAdmission:
    """A no-op admission used by the unit/two-sided dispatcher tests, so the
    existing ``_launch_task(job, receipt)`` seam and the pre-H4 tests keep their
    exact intent (they assert the launch mechanics, not the admission ceiling).
    """

    def note_launch(self) -> None:
        """No-op — no ceiling to count against in the isolated test path."""


def _launch_task(
    job: HarnessJob,
    receipt_handle: str,
    admission: _DispatcherAdmission | _NullAdmission | None = None,
) -> None:
    """Launch the per-env family task for *job*.  Classifies RunTask failures.

    On success the message stays in the queue (invisible, then heartbeated by
    the task) — the dispatcher never deletes it.  On a PERMANENT failure the
    calling loop lets the message time out back to the queue and eventually DLQ;
    the transient/capacity handlers back off instead (H4 §6).  ``admission``
    defaults to a null no-op so callers/tests exercising the launch mechanics
    alone need not provision a ceiling.
    """
    if admission is None:
        admission = _NullAdmission()
    family = _family_for_job(job)

    env = _env_from_env()
    _assert_isolated_network(env)  # A1-7: live state, never a value file
    reference = _reference_for(job, receipt_handle)

    client = _ecs_client()
    resp = client.run_task(
        cluster=env["cluster"],
        taskDefinition=family,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": env["subnets"],
                "securityGroups": env["security_groups"],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": _CONTAINER_NAME,
                    "command": _JOB_COMMAND,
                    "environment": reference.to_env(),
                }
            ]
        },
        # ADR-0034 M1.5: startedBy = run_id, so Abort can enumerate this run's
        # tasks with one ListTasks(startedBy=run_id) instead of scanning the
        # cluster.  26-char ULID fits the 36-char limit.
        startedBy=job.run_id,
    )
    failures = resp.get("failures", [])
    if failures:
        # Abstract over the many RunTask failure shapes: the task def can fail
        # with a reason in *failures* (ECS returns them there) OR the SDK raises.
        raise _classify_run_task_failure(
            failures[0].get("reason", "unknown"), failures[0].get("detail", "")
        )
    admission.note_launch()
    _emit_dispatched(job)


def _emit_dispatched(job: HarnessJob) -> None:
    """D6 / run-launch §6.2: the DISPATCHED ledger notice, emitted after
    RunTask succeeds — a ``ResultMessage`` with that state and no artifacts
    ("no new message type is needed").  Owned entirely by the harness
    dispatcher; no other component ever sets ``instance_results.dispatched_at``
    / ``dispatch_count`` (§6.3a column ownership) or emits state ``DISPATCHED``.
    Best-effort: a failed emit must not fail an otherwise-successful launch —
    the reaper's rule 2 would then simply never fire for this instance (no
    ``dispatched_at`` to measure a deadline from), which is the safe direction
    to be wrong in.
    """
    try:
        send_message(
            "results",
            dataclasses.asdict(
                ResultMessage(
                    run_id=job.run_id,
                    instance_id=job.instance_id,
                    attempt_number=job.attempt_number,
                    phase="harness",
                    state="DISPATCHED",
                )
            ),
        )
    except Exception:
        logger.exception(
            "could not emit DISPATCHED for %s/%s attempt %d (launch itself succeeded)",
            job.run_id,
            job.instance_id,
            job.attempt_number,
        )


def _discard_aborted(
    job: HarnessJob, receipt_handle: str, attributes: dict[str, str] | None = None
) -> None:
    """Discard a queued job of an aborted run: terminal row, delete, never launch.

    ADR-0034 M1 gate #2.  The dispatcher does NOT normally delete; this is the
    single exception — an aborted run's messages must gate off the run, and the
    result row keeps the denominator honest.

    Abort 2026-09-04 (run 01788550040741118596-dd284a4f), two findings from the live
    path:

    * The verdict depends on whether the message was ever launched. A message on its
      FIRST receive was never dispatched -> ``NEVER_DISPATCHED``. A REDELIVERED one
      (``ApproximateReceiveCount`` > 1) was already received once — in the observed case
      a launched task the abort SIGKILLed before it could report, whose message came back
      after its visibility timeout — so the honest terminal state is
      ``ABORTED_IN_FLIGHT``; stamping it NEVER_DISPATCHED recorded a 164-turn, $0.17
      attempt as never having run. (A message returned by a RunTask capacity error is
      also "redelivered"; the error_detail says so, and both states rank terminal.)
    * Nothing here may escape into the poll loop. The delete raised ``AccessDenied``
      (the task role had no ``sqs:DeleteMessage``), the exception took the process down
      and ECS restarted it every visibility timeout — a crash loop on messages that were
      going to be discarded anyway. A failed discard is logged and left to SQS: the
      message returns after its visibility timeout and is discarded again.
    """
    receives = 1
    try:
        receives = int((attributes or {}).get("ApproximateReceiveCount", "1") or 1)
    except (TypeError, ValueError):
        receives = 1
    if receives > 1:
        state = "ABORTED_IN_FLIGHT"
        detail = (
            f"run aborted; message redelivered (receive #{receives}) — the earlier receive "
            "launched a task the abort stopped before it could report, or returned it "
            "on a RunTask capacity error"
        )
    else:
        state = "NEVER_DISPATCHED"
        detail = "run aborted"
    try:
        send_message(
            "results",
            dataclasses.asdict(
                ResultMessage(
                    run_id=job.run_id,
                    instance_id=job.instance_id,
                    attempt_number=job.attempt_number,
                    phase="harness",
                    state=state,
                    error_detail=detail,
                )
            ),
        )
        delete_message("harness-jobs", receipt_handle)
    except Exception:  # a discard must never take the dispatcher down
        logger.exception(
            "discard of queued job %s/%s attempt %d (run aborted) failed; leaving the "
            "message to its visibility timeout",
            job.run_id,
            job.instance_id,
            job.attempt_number,
        )
        return
    logger.info(
        "discarded queued job %s/%s attempt %d (run aborted, receive #%d) -> %s",
        job.run_id,
        job.instance_id,
        job.attempt_number,
        receives,
        state,
    )


def run_harness_dispatcher() -> None:
    """Poll ``harness-jobs`` and dispatch under the admission control gate."""
    from swebench_eval.logging_bootstrap import configure_logging

    configure_logging()

    autoscaler = (
        Autoscaler()
    )  # mode from AUTOSCALER_MODE (default "observe" — publishes, never blocks)
    admission = _DispatcherAdmission(ceiling=_max_concurrent_from_env(), autoscaler=autoscaler)
    logger.info(
        "harness-dispatcher starting; MAX_CONCURRENT_HARNESS_TASKS=%d autoscaler_mode=%s alias=%r",
        admission.ceiling,
        autoscaler.mode,
        autoscaler.model_alias,
    )
    if not autoscaler.model_alias:
        # §2.4 (wiring review): an absent alias must be a VISIBLE degraded state, never a
        # clean-looking record — the planner runs on generic defaults (budgets.source =
        # 'defaults' in every decision record) and §6.6 observation emission stays disabled
        # until a run's launch publishes its alias via the overrides hash.
        logger.warning(
            "AUTOSCALER_MODEL_ALIAS unset and no run override yet — planner on GENERIC "
            "DEFAULT budgets (decision records will carry budgets.source='defaults'); "
            "observation emission disabled until a launched run publishes its alias"
        )

    while True:
        decision = admission.may_launch()
        if not decision.allowed:
            logger.info(
                "harness-dispatcher gated: %s (backoff %ds)", decision.reason, decision.backoff_s
            )
            time.sleep(decision.backoff_s)
            continue

        msg = receive_message(
            "harness-jobs", wait_seconds=20, visibility_timeout=_INITIAL_VISIBILITY_SECONDS
        )
        if msg is None:
            continue

        try:
            job = HarnessJob.from_dict(msg["body"])
            if control_state.is_run_aborted(job.run_id):
                _discard_aborted(job, msg["receipt_handle"], msg.get("attributes"))
                continue
            # ADR-0037 / M0 §4.3 + M0-6 (review): the deployed task-per-instance
            # path never calls receive_message (RunTask launches it), so
            # queue_wait_s would be permanently NULL there.  The DISPATCHER is
            # the process that received the message and holds its SQS
            # attributes — compute the queue wait here and carry it on the
            # reference, exactly as it already carries dispatched_at.
            _compute_queue_wait(job, msg.get("attributes"))
            _launch_task(job, msg["receipt_handle"], admission)
            logger.info(
                "launched task %s/%s attempt %d (family %s)",
                job.run_id,
                job.instance_id,
                job.attempt_number,
                _family_for_job(job),
            )
            # Forecast review §3: space launches out (jittered) — see launch_stagger_s.
            time.sleep(autoscaler.launch_stagger_s())
        except (RunTaskCapacityError, RunTaskThrottleError) as exc:
            # Transient: back off and retry LATER.  Do NOT delete (the message
            # returns after visibility and is retried; spends one receive on the
            # way but is NOT burned to the DLQ by this class).
            logger.warning("harness-dispatcher backoff (%s)", exc)
            time.sleep(10)
        except RunTaskQuotaError:
            # The ceiling is wrong or the quota moved.  Stop the dispatcher
            # and alert — continuing burns the backlog toward the DLQ.
            logger.exception("harness-dispatcher quota error; stopping dispatch")
            raise
        except RunTaskPermanentError as exc:
            # Permanent: the message times out and eventually DLQs (retry cannot
            # help).  Log it and let this job consume no more receives.
            logger.error("harness-dispatcher permanent failure: %s", exc)
        except (DispatchRefusedError, RuntimeError):
            # Unknown env / network / any other launch failure. Do NOT delete:
            # the message returns to the queue and retries.
            logger.exception(
                "harness-dispatcher could not launch for message %s",
                msg.get("message_id"),
            )


# ===========================================================================================
# L2 fleet planner — the autoscaler, PART OF THE HARNESS DISPATCHER (not a control-plane
# component; owner's explicit placement instruction 2026-09-01, and exact-design §8: the
# autoscaler's output IS _DispatcherAdmission.ceiling, so it lives in the process — and the
# file — of the only thing that consumes it). Full rationale + formulas:
# BUILDER4-HARNESS-AUTOSCALER-EXACT-DESIGN-2026-09-01.md §5.
#
# Three projected gates from survival-weighted per-task trajectories, horizon-maxed 0..90min:
#   max_k arrival_tokens/s <= 0.90 x R           else block("arrival_budget")
#   max_k inflight_tokens  <= 0.90 x K_inflight  else block("inflight_budget")
#   max_k call_starts/s    <= 0.85 x R_qps       else block("qps_budget")
# plus the pacer-empty invariant (paced back-pressure), the overload cooldown (owner's §6.4/6.5
# semantics), and +5%-never-more predicted-peak growth. Modes: off | observe (default,
# actuates NOTHING) | live. Curve defaults are the POOLED fit, recorded as such in every
# decision record — never silently authoritative.
# ===========================================================================================


# The key + envelope now live in the SHARED contract (decision_record.py, owner decision 4
# 2026-09-01) — both autoscalers publish the same shape. Re-exported for existing readers.
from swebench_eval.orchestrator.control_plane.decision_record import (
    decision_key as _decision_key,
)
from swebench_eval.orchestrator.control_plane.decision_record import (
    publish as _publish_decision_record,
)

DECISION_KEY = _decision_key("harness")

# --- default demand model (pooled fit; spec §3/§4 measured constants) -----------------------
_POOLED_A = 12_315.0  # tokens/call intercept
_POOLED_B = 515.0  # tokens/call per turn
_TURN_PERIOD_FLOOR_S = 2.0
_MEDIAN_TOOL_TIME_S = 6.8  # median turn 9.1s minus ~2.3s fresh-call latency (observe-phase refit)
# Survival per 20-turn (~3 min) window, measured (§4.1): {window-start-turn: P(survive window)}.
_SURVIVAL_TABLE = {0: 0.79, 40: 0.78, 80: 0.71, 120: 0.86}
# Call latency at context c (two measured points: ~2.3s fresh, 13.4s at 213K real tokens).
_LATENCY_INTERCEPT_S = 1.0
_LATENCY_PER_TOKEN_S = 12.4 / 213_000

_HORIZON_S = 90 * 60
_HORIZON_STEP_S = 5 * 60
_UTILIZATION = 0.90
_QPS_UTILIZATION = 0.85
_PACED_SHARE_LIMIT = 0.05  # >2s waits above this share of admissions => paced back-pressure
_STALE_PROGRESS_S = 120.0
_GROWTH_STEP = 0.05  # +5%, owner-fixed, never more
_STABILIZATION_WINDOW_S = 60.0
# Scaling review 2026-09-03 F2 — the DOWNWARD side of the budget loop, which did not exist:
# growth is bounded to this multiple of the discovered seed (nothing ever lowered a budget, so
# sporadic 429s let r_tok ratchet 1.55x past the only rate measured clean), and a real overload
# lowers r_tok/r_qps to _RECOVERY_MARGIN x the arrival at which the pool overloaded (the
# measured last-60s admission rate when the pacer has it, the projection otherwise), floored at
# _RECOVERY_FLOOR_FACTOR x the seed so a provider hiccup cannot collapse the alias. Applied at
# cooldown ENTRY — the fleet already running is what is 429ing; lowering the admission rate at
# once is the "stop pacer admissions" half of the design's overload response, in the
# proportional form; +5% growth on clean peaks then climbs back (AIMD).
_GROWTH_CAP_FACTOR = 1.5
# F5 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): the survival discount needs evidence. The
# borrowed laguna|mini table (n = 8) said ~40% of tasks finish by turn 80; the qwen tasks ran
# past turn 100+, the survival-weighted horizon-max landed at 5.1K/task and the launch check
# admitted a fleet that measured 12-15K/task. Under a borrowed curve, or a survival table
# resting on fewer attempts than this, the projection uses survival = 1 (horizon peak, no
# discount) and the decision records survival_discount = False.
_SURVIVAL_MIN_ATTEMPTS = 30
_RECOVERY_MARGIN = 0.85
_RECOVERY_FLOOR_FACTOR = 0.5
# Review F4: an alias whose latency curve is BORROWED (no fit for its pool) is not sized past
# this many tasks — a borrowed pool's speed would size this pool's in-flight, the binding axis,
# in the flood direction. Lifted by a refit that gives the pool its own curve.
_BORROWED_CURVE_CAP = int(os.environ.get("AUTOSCALER_BORROWED_CURVE_CAP", "30"))
_MAX_CONTEXT_TOKENS = 262_144.0  # the gateway's max_input_tokens (ceiling_discovery)


def _pool_window_tokens(alias: str | None) -> float:
    """The window the alias's pool serves (its rotatable spec's ``max_input_tokens``) —
    what discovery probed its latency at; ``_MAX_CONTEXT_TOKENS`` for an unknown alias."""
    if not alias:
        return _MAX_CONTEXT_TOKENS
    try:
        from swebench_eval.gateway.rotatable_models import ROTATABLE_MODELS

        spec = ROTATABLE_MODELS.get(alias)
        window = spec.model_info.get("max_input_tokens") if spec is not None else None
        return (
            float(window)
            if isinstance(window, (int, float)) and window > 0
            else _MAX_CONTEXT_TOKENS
        )
    except Exception:  # noqa: BLE001 — a lookup failure must never take the planner down
        return _MAX_CONTEXT_TOKENS


# Forecast review 2026-09-03 §3: a wait-queue head denied longer than this is pressure the
# smoothed >2s share may not show yet (the queue is the pacer's live ledger, un-smoothed).
_QUEUE_HEAD_WAIT_LIMIT_S = 5.0


@dataclass(frozen=True)
class TaskState:
    """One live instance, from its Redis progress key."""

    turn: int
    context_tokens: float  # current per-call prompt size estimate
    # Forecast review 2026-09-03 §3: the payload now names the task's alias + harness so the
    # planner can budget per alias with the right per-harness curve. None = older payload.
    alias: str | None = None
    harness: str | None = None


@dataclass(frozen=True)
class Budgets:
    r_tok: float  # arrival tokens/s
    k_inflight: float  # in-flight tokens
    r_qps: float  # call starts/s
    source: str  # 'pacer_cfg' | 'pacer_cfg_stale' | 'defaults'
    age_s: float | None = None  # seconds since the cfg was seeded; None for defaults
    # Review F6 (2026-09-03): the burst bucket — the axis that denies most calls — belongs in
    # the record a tick is replayed from.
    c_burst: float | None = None
    # Review F2: the DISCOVERED seeds (discovery writes them beside the live values; growth
    # never touches them). Growth is bounded above relative to r_tok_seed, overload recovery
    # floored below it. None = an older cfg with no seed on record.
    r_tok_seed: float | None = None
    k_inflight_seed: float | None = None
    r_qps_seed: float | None = None
    # Review F4: this pool's own max-context call latency from discovery (Phase A median) —
    # the floor under a BORROWED latency curve.
    latency_s_max_context: float | None = None
    # F3 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): what a cached prompt token costs the pool
    # relative to an uncached one (discovery Phase D writes it; 1.0 = full price until then).
    # The projection discounts each curve's cached share by it, matching the shim's charge.
    cached_weight: float = 1.0

    @property
    def utilization_factor(self) -> float:
        """F3 (exact-design review): the pool MOVES (E5 admitted 2.42M, E6 admitted 1.05M one
        minute later) — a cfg seeded hours ago can be ~2x optimistic. The live AIMD
        self-corrects DURING a run; this is the starting-trust policy: a stale cfg halves the
        planner's utilization margins (0.90 -> 0.45) until fresh evidence re-stamps it (a live
        +5% reconciliation rewrites seeded_at; discovery rewrites it outright). The L1 pacer
        still ENFORCES the stale numbers — a stale limit is still a limit; what staleness must
        never buy is full-margin trust in an optimistic old measurement."""
        return 0.5 if self.source == "pacer_cfg_stale" else 1.0


@dataclass(frozen=True)
class Decision:
    """One tick's complete answer — published verbatim as the decision record."""

    decided_at: float
    mode: str
    desired_ceiling: int
    binding_constraint: str
    observed_tasks: int
    projected_arrival_tok_s: float
    projected_inflight_tok: float
    projected_qps: float
    peak_at_s: float  # seconds from now to the projected binding peak
    budgets: Budgets
    overloads_window: int
    paced_share: float
    curve_source: str = "pooled_default"
    would_set: dict[str, float] = field(default_factory=dict)  # observe-mode intended changes
    # Forecast review 2026-09-03: per-alias breakdown (the aggregate above is the dispatcher's
    # single ceiling; this says which alias binds and why), plus the two direct pressure
    # signals the paced share was blind to — hold-cap timeouts and the live wait queue.
    aliases: dict[str, Any] = field(default_factory=dict)
    booting_tasks: int = 0
    paced_timeouts: int = 0
    queue_len: int = 0
    queue_head_wait_s: float = 0.0
    binding_alias: str = ""  # which alias set `binding_constraint` (vocabulary stays plain)
    in_flight_tasks: int = 0  # the ECS ground truth the ceiling was compared against
    # Scaling review 2026-09-03 (F5/F6/F2): the hard cap in force this tick (static env cap
    # min per-run cap) so a replay can tell planner-hold from cap-hold; whether the growth
    # write actually landed (would_set is populated BEFORE the hset); whether growth was
    # refused by the seed-relative clamp; and any overload recovery written this tick
    # ({alias: {field: [old, new]}, ...}).
    static_cap: int | None = None
    growth_applied: bool = False
    growth_clamped: bool = False
    recovery_set: dict[str, Any] = field(default_factory=dict)
    # F5: whether the current alias's projection applied its survival table (False = borrowed
    # curve or a table on < _SURVIVAL_MIN_ATTEMPTS attempts -> survival 1, no discount).
    survival_discount: bool = True
    # Operator limits 2026-09-04: the operator's ceiling override in force (None = the
    # projection) and the global knobs the tick read (empty = every default).
    ceiling_override: int | None = None
    operator_limits: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> str:
        d = {**self.__dict__, "budgets": self.budgets.__dict__}
        return json.dumps(d)


class DemandModel:
    """§3/§4: per-task token/latency/survival trajectories. Injectable for tests and for the
    offline refits that replace the pooled defaults (``demand_curves.DemandCurves``).

    Forecast review 2026-09-03 §1: on the real data latency tracks OUTPUT tokens (corr 0.97),
    not context (0.12 — prompts are 90%+ cache hits), and the turn period is best measured as
    the median inter-call gap. A fitted model therefore carries ``latency_s`` and
    ``turn_period_s`` as per-(pool, harness) constants; the context-linear forms below are the
    pooled defaults used only when no fit exists.
    """

    def __init__(
        self,
        a: float = _POOLED_A,
        b: float = _POOLED_B,
        compaction_cap: float = 235_929.0,  # compute_threshold(W=262144), 16_384 reserve
        source: str = "pooled_default",
        latency_s: float | None = None,
        turn_period_s: float | None = None,
        survival: dict[int, float] | None = None,
        cached_share: float = 0.0,
        survival_attempts: int | None = None,
    ) -> None:
        self.a, self.b, self.cap, self.source = a, b, compaction_cap, source
        self._latency_s = latency_s
        self._turn_period_s = turn_period_s
        self.survival_table: dict[int, float] = (
            dict(survival) if survival else dict(_SURVIVAL_TABLE)
        )
        # F5: how many attempts the survival table's first window rests on (the fitted
        # curves carry it); None = not from a fit (an explicit/test model, or the defaults).
        self.survival_attempts = survival_attempts
        # F3: the fitted share of this group's prompt tokens that were cache hits (0 when
        # unknown — the full-price projection is the safe direction).
        self.cached_share = min(1.0, max(0.0, float(cached_share)))

    def tokens_per_call(self, turn: float) -> float:
        return min(self.a + self.b * turn, self.cap)

    def call_latency_s(self, context_tokens: float) -> float:
        if self._latency_s is not None:
            return self._latency_s
        return _LATENCY_INTERCEPT_S + context_tokens * _LATENCY_PER_TOKEN_S

    def turn_period_s(self, context_tokens: float) -> float:
        if self._turn_period_s is not None:
            return max(_TURN_PERIOD_FLOOR_S, self._turn_period_s)
        return max(_TURN_PERIOD_FLOOR_S, self.call_latency_s(context_tokens) + _MEDIAN_TOOL_TIME_S)

    def is_own_curve(self) -> bool:
        """True when latency/period come from THIS pool's own fit (``fitted:<pool>|<harness>``
        possibly with survival fallbacks appended); False for a borrowed curve — another
        pool's fit for the harness, the pooled fit, or the hard-coded defaults."""
        head = self.source.split("+", 1)[0]
        if head.startswith("fitted:harness:") or head == "fitted:pooled":
            return False
        return head.startswith("fitted:") and "|" in head

    def with_latency_floor(self, floor_s: float, max_context_tokens: float) -> DemandModel:
        """Review F4 (2026-09-03): a copy whose call latency is at least this pool's own
        measured max-context latency, scaled by context (prefill-proportional:
        ``floor_s × context / max_context``). Used only under a BORROWED curve, where the
        borrowed pool's speed would otherwise size this pool's in-flight — the binding
        axis — in the flood direction."""
        base = self

        class _Floored(DemandModel):
            def call_latency_s(self, context_tokens: float) -> float:
                borrowed = base.call_latency_s(context_tokens)
                floor = floor_s * min(1.0, max(0.0, context_tokens / max_context_tokens))
                return max(borrowed, floor)

            def turn_period_s(self, context_tokens: float) -> float:
                # 2026-09-04 (deepseek x mini): a serial agent cannot start its next call
                # before the current one returns, so the borrowed pool's MEASURED period
                # (laguna mini: 6.0 s, of which ~1.4 s was laguna's latency) is meaningless
                # on a pool whose call alone takes longer. Keep the borrowed pool's non-LLM
                # time (period minus its own latency) and add THIS pool's floored latency.
                # Without this the projection sent a 236K-token call every 6 s per task on
                # a pool answering in 47 s — 39K tok/s per task, 2.5x the physical maximum —
                # and the ceiling for a 13-instance run came out at 5.
                borrowed_period = base.turn_period_s(context_tokens)
                non_llm = max(0.0, borrowed_period - base.call_latency_s(context_tokens))
                return max(borrowed_period, non_llm + self.call_latency_s(context_tokens))

        m = _Floored(
            a=base.a,
            b=base.b,
            compaction_cap=base.cap,
            source=base.source + "+latency:discovery_floor",
            latency_s=None,
            turn_period_s=base._turn_period_s,
            survival=base.survival_table,
            cached_share=base.cached_share,
            survival_attempts=base.survival_attempts,
        )
        m._base_latency_s = base._latency_s  # type: ignore[attr-defined]
        return m

    def survival(self, turn: float, horizon_s: float) -> float:
        """P(instance still running horizon_s from now | currently at *turn*) — the measured
        per-window table compounded across the windows the horizon spans."""
        p = 1.0
        t = turn
        remaining = horizon_s
        table = self.survival_table
        while remaining > 0:
            period = self.turn_period_s(self.tokens_per_call(t))
            window_s = 20 * period  # the table is per-20-turn window
            step = min(remaining, window_s)
            # nearest table row at or below t
            row = max((k for k in table if k <= t), default=min(table))
            p *= table[row] ** (step / window_s)
            t += step / period
            remaining -= step
        return p


class Autoscaler:
    """Owned and ticked by the dispatcher. All Redis access is guarded — any failure holds the
    last good decision (fail-closed: never widen admission on missing data)."""

    def __init__(
        self,
        model_alias: str | None = None,
        mode: str | None = None,
        demand: DemandModel | None = None,
        redis_client: Any = None,
        tick_interval_s: float = 15.0,
        curves: Any = None,
    ) -> None:
        # The STATIC alias (constructor arg or env) is the fallback only — the live run's
        # overrides hash carries the authoritative alias (§2.4 of the wiring review: with the
        # env var unset the planner silently ran on generic defaults and §6.6 emitted nothing;
        # the run knows its model, so the launch publishes it and _apply_run_overrides adopts
        # it each tick, falling back here when no run has one published).
        self._static_alias = model_alias or os.environ.get("AUTOSCALER_MODEL_ALIAS", "")
        self.model_alias = self._static_alias
        self.mode = mode or os.environ.get("AUTOSCALER_MODE", "observe")
        # `demand`: an explicit model (tests / a forced curve) wins for every alias. Otherwise
        # the fitted curves document supplies one per (pool, harness) — forecast review §2.
        self._explicit_demand = demand
        self.demand = demand or DemandModel()
        if curves is None and demand is None:
            from swebench_eval.orchestrator.control_plane.demand_curves import DemandCurves

            curves = DemandCurves.load()
        self.curves = curves
        self._demand_cache: dict[tuple[str | None, str | None], DemandModel] = {}
        self._alias_decisions: dict[str, dict[str, Any]] = {}
        self._redis = redis_client
        self._tick_interval_s = tick_interval_s
        self._last_tick_at = 0.0
        self._last_decision: Decision | None = None
        # Cooldown/overload state (§5, owner's semantics)
        self._cooldown_until = 0.0
        self._cooldown_clean_since: float | None = None
        # Paced back-pressure hysteresis: blocked until two consecutive clean windows.
        self._paced_blocked = False
        self._paced_clean_windows = 0
        # Predicted-peak reconciliation bookkeeping (growth happens when a predicted peak
        # arrives clean and utilization is high).
        self._pending_peak: tuple[float, float] | None = None  # (at_monotonic, projected_value)
        # §6.7 per-run overrides, re-read each tick (guarded). _GROWTH_STEP is the HARD MAX
        # (owner-fixed "never more") — an override can only shrink the step. enabled=False
        # disables L2's dynamic gate only; the tick still runs and publishes.
        self._growth_step = _GROWTH_STEP
        self._stabilization_window_s = _STABILIZATION_WINDOW_S
        self._enabled = True
        # §6.6 observation-event bookkeeping: events fire on TRANSITIONS, never per-tick
        # (a 90-tick cooldown is one overload event and one recovery, not 90 rows).
        self._was_in_cooldown = False
        self._was_holding = False
        self._run_id: str | None = None
        # Review F5: the dispatcher's hard cap (static env cap min per-run cap), set by
        # _DispatcherAdmission so a cap-bound tick records `static_cap` instead of nothing.
        self.hard_cap_fn: Any = None
        # Review F4: one WARNING per alias when it runs on a borrowed curve.
        self._borrowed_warned: set[str] = set()
        # F6: the seed a CLAMPED refusal was last logged against — one log line per clamp
        # episode (it printed every tick), reset when a reconciliation is not clamped.
        self._clamp_logged_seed: float | None = None
        # Operator limits 2026-09-04 (control_plane/operator_limits.py), re-read every tick:
        # the run-scope ceiling override and the global knobs that used to be module
        # constants / task-def env. The constants stay the defaults.
        self._ceiling_override: int | None = None
        self._borrowed_curve_cap = _BORROWED_CURVE_CAP
        self._growth_cap_factor = _GROWTH_CAP_FACTOR
        self._utilization = _UTILIZATION
        self._operator_limits: dict[str, float] = {}

    def _apply_run_overrides(self) -> None:
        try:
            overrides = _read_run_overrides(self._client())
        except Exception:  # noqa: BLE001 — a client failure means defaults, never a stall
            overrides = {}
        try:
            step_pct = float(overrides.get("ramp_step_pct", _GROWTH_STEP * 100))
            self._growth_step = min(_GROWTH_STEP, max(0.0, step_pct / 100.0))
        except (TypeError, ValueError):
            self._growth_step = _GROWTH_STEP
        try:
            self._stabilization_window_s = max(
                1.0, float(overrides.get("cooldown_s", _STABILIZATION_WINDOW_S))
            )
        except (TypeError, ValueError):
            self._stabilization_window_s = _STABILIZATION_WINDOW_S
        self._enabled = overrides.get("enabled", "1") != "0"
        self._run_id = overrides.get("run_id") or None
        self.model_alias = overrides.get("model_alias") or self._static_alias
        try:
            raw_override = overrides.get("ceiling_override")
            new_override = int(raw_override) if raw_override not in (None, "") else None
        except (TypeError, ValueError):
            new_override = None
        if new_override != self._ceiling_override:
            logger.warning(
                "autoscaler: operator ceiling override %s -> %s",
                self._ceiling_override,
                new_override,
            )
            self._ceiling_override = new_override
        self._apply_operator_limits()

    def _apply_operator_limits(self) -> None:
        """The global operator knobs (operator:limits), guarded: a failed read keeps the
        last values; an absent field is its default (the module constant / env)."""
        from swebench_eval.orchestrator.control_plane import operator_limits

        try:
            lim = operator_limits.read_global(self._client())
        except Exception:  # noqa: BLE001 — never a stall
            return
        if lim != self._operator_limits:
            logger.warning("autoscaler: operator limits now %s", lim or "{} (defaults)")
        self._operator_limits = lim
        self._borrowed_curve_cap = max(1, int(lim.get("borrowed_curve_cap", _BORROWED_CURVE_CAP)))
        self._growth_cap_factor = max(1.0, float(lim.get("growth_cap_factor", _GROWTH_CAP_FACTOR)))
        self._utilization = min(1.0, max(0.1, float(lim.get("utilization", _UTILIZATION))))

    def _emit_observation(self, event_type: str, tok_per_min: float, at_concurrency: int) -> None:
        """§6.6: publish one observation event to the model-observations queue — the dispatcher
        never writes Aurora from its hot path; the results-writer's daemon INSERTs it into
        model_tpm_observations (append-only; the model_ceilings view reconciles on read).
        Guarded fire-and-forget: a queue failure must never affect admission. Emitted on
        transitions (event-driven subsumes the design's drain-to-zero + every-N-ticks triggers
        — nothing is buffered, so there is nothing left to flush)."""
        if not self.model_alias:
            return  # no alias, no attributable observation
        try:
            send_message(
                "model-observations",
                {
                    "model_alias": self.model_alias,
                    "run_id": self._run_id,
                    "event_type": event_type,
                    # The value is the planner's PROJECTED arrival rate at the event — named
                    # honestly so a reader never mistakes it for a measured admission figure.
                    "value_kind": "projected_arrival_tok_per_min",
                    "tpm_value": round(tok_per_min),
                    "at_concurrency": at_concurrency,
                    "notes": f"mode={self.mode}",
                },
            )
        except Exception:
            logger.debug("observation emit failed (%s)", event_type, exc_info=True)

    # -- data plumbing ------------------------------------------------------------------------

    def _client(self) -> Any:
        if self._redis is None:
            from swebench_eval.database.redis_client import _get_client

            self._redis = _get_client()
        return self._redis

    def demand_for(self, alias: str | None, harness: str | None) -> DemandModel:
        """The per-(pool, harness) fitted model, cached; an explicit `demand` wins."""
        if self._explicit_demand is not None or self.curves is None:
            return self.demand
        key = (alias, harness)
        if key not in self._demand_cache:
            pool: str | None = None
            if alias:
                try:
                    from swebench_eval.gateway.rotatable_models import pool_alias_for

                    pool = pool_alias_for(alias) or alias
                except Exception:  # noqa: BLE001 — registry unavailable reads as the alias itself
                    pool = alias
            self._demand_cache[key] = self.curves.model_for(pool, harness)
        return self._demand_cache[key]

    def _scan_fleet(self) -> list[TaskState]:
        """Live per-task state from the instance_progress keys the workers already write.

        Forecast review 2026-09-03 §3: a key is kept for as long as it EXISTS (the writer's
        TTL is the liveness bound) — the old 120s staleness cut dropped every instance in a
        long tool call or a long pacer hold out of the projection, under-counting demand
        exactly when the fleet was under pressure. Existence is bounded by ECS ground truth
        in the tick (booting = in_flight - fleet, clamped at 0)."""
        client = self._client()
        fleet: list[TaskState] = []
        for key in client.scan_iter("instance_progress:*", count=200):
            raw = client.get(key)
            if not raw:
                continue
            try:
                p = json.loads(raw)
                turn = int(p.get("turn_number", 0))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            alias = p.get("model_alias") or None
            harness = p.get("harness") or None
            d = self.demand_for(alias or self.model_alias, harness)
            fleet.append(
                TaskState(
                    turn=turn,
                    context_tokens=d.tokens_per_call(turn),
                    alias=str(alias) if alias else None,
                    harness=str(harness) if harness else None,
                )
            )
        return fleet

    def _budgets(self, alias: str | None = None) -> Budgets:
        from swebench_eval.gateway import pacer as pacer_mod

        alias = alias or self.model_alias
        defaults = Budgets(
            r_tok=pacer_mod.DEFAULT_R_TOK,
            k_inflight=pacer_mod.DEFAULT_K_INFLIGHT,
            r_qps=pacer_mod.DEFAULT_R_QPS,
            source="defaults",
        )
        if not alias:
            return defaults
        try:
            raw = self._client().hgetall(pacer_cfg_key(alias))
        except Exception:  # noqa: BLE001 — missing cfg degrades to defaults, never crashes
            return defaults
        if not raw:
            return defaults
        cfg = {(k.decode() if isinstance(k, bytes) else k): float(v) for k, v in raw.items()}
        seeded_at = cfg.get("seeded_at")
        age_s = (time.time() - seeded_at) if seeded_at else None
        max_age_s = float(os.environ.get("PACER_CFG_MAX_AGE_S", str(6 * 3600)))
        stale = age_s is None or age_s > max_age_s  # unknown age reads as stale, never as fresh
        return Budgets(
            r_tok=cfg.get("r_tok", defaults.r_tok),
            k_inflight=cfg.get("k_inflight", defaults.k_inflight),
            r_qps=cfg.get("r_qps", defaults.r_qps),
            source="pacer_cfg_stale" if stale else "pacer_cfg",
            age_s=round(age_s, 1) if age_s is not None else None,
            c_burst=cfg.get("c_burst", float(pacer_mod.DEFAULT_C_BURST)),
            r_tok_seed=cfg.get("r_tok_seed"),
            k_inflight_seed=cfg.get("k_inflight_seed"),
            r_qps_seed=cfg.get("r_qps_seed"),
            latency_s_max_context=cfg.get("latency_s_max_context"),
            cached_weight=min(1.0, max(pacer_mod.CACHED_WEIGHT_MIN, cfg.get("cached_weight", 1.0))),
        )

    def _measured_arrival(self, alias: str | None = None) -> tuple[float, float]:
        """(tok/s, calls/s) ADMITTED over the last six complete 10 s buckets — the pacer's own
        ledger of what actually went upstream (review F2), as opposed to the projection.
        (0, 0) when the pacer has published nothing (older shim, or an idle alias)."""
        alias = alias or self.model_alias
        if not alias:
            return 0.0, 0.0
        try:
            client = self._client()
            now_b = int(time.time() // 10)
            tok = n = 0
            for i in range(1, 7):
                raw = client.hgetall(paced_key(alias, now_b - i))
                if raw:
                    d = {(k.decode() if isinstance(k, bytes) else k): v for k, v in raw.items()}
                    tok += int(d.get("tok", 0))
                    n += int(d.get("n", 0))
            return tok / 60.0, n / 60.0
        except Exception:  # noqa: BLE001
            return 0.0, 0.0

    def _overloads_window(self, alias: str | None = None) -> int:
        """§2.3 read shape: the six COMPLETE 10s buckets (60s window)."""
        alias = alias or self.model_alias
        if not alias:
            return 0
        try:
            client = self._client()
            now_b = int(time.time() // 10)
            keys = [overload_key(alias, now_b - i) for i in range(1, 7)]
            vals = client.mget(keys)
            return sum(int(v) for v in vals if v)
        except Exception:  # noqa: BLE001 — unreadable counter reads as 0 overloads for GROWTH
            # gating only; the freeze path is edge-triggered on observed increments, so a Redis
            # outage cannot UN-freeze anything (fail-closed handled by budgets staying put).
            return 0

    def _paced_stats(self, alias: str | None = None) -> tuple[float, int]:
        """(share of last-60s admissions that waited > 2s, hold-cap TIMEOUTS in the window).

        Forecast review 2026-09-03 §3: the share alone was blind to the worst case — a call
        that hits the hold cap never admits, so it never counted. Run 01788405363237319353's
        ten timeouts registered as ZERO paced pressure. The pacer now publishes timeouts into
        the same buckets; any timeout in the window is treated as pressure outright."""
        alias = alias or self.model_alias
        if not alias:
            return 0.0, 0
        try:
            client = self._client()
            now_b = int(time.time() // 10)
            n = over = timeouts = 0
            for i in range(1, 7):
                raw = client.hgetall(paced_key(alias, now_b - i))
                if raw:
                    d = {(k.decode() if isinstance(k, bytes) else k): v for k, v in raw.items()}
                    n += int(d.get("n", 0))
                    over += int(d.get("n_over_2s", 0))
                    timeouts += int(d.get("n_timeout", 0))
            return ((over / n) if n else 0.0), timeouts
        except Exception:  # noqa: BLE001
            return 0.0, 0

    def _paced_share(self) -> float:
        return self._paced_stats()[0]

    def _queue_pressure(self, alias: str | None = None) -> tuple[int, float]:
        """(wait-queue depth, seconds the head of line has waited) — the pacer's live ledger,
        un-smoothed (forecast review §3). A client without ZSET support reads as (0, 0)."""
        alias = alias or self.model_alias
        if not alias:
            return 0, 0.0
        try:
            from swebench_eval.gateway.pacer import pacer_waitest_key, pacer_waitq_key

            client = self._client()
            zcard = getattr(client, "zcard", None)
            zrange = getattr(client, "zrange", None)
            if not callable(zcard) or not callable(zrange):
                return 0, 0.0
            depth = int(zcard(pacer_waitq_key(alias)) or 0)
            if depth <= 0:
                return 0, 0.0
            head = zrange(pacer_waitq_key(alias), 0, 0)
            if not head:
                return depth, 0.0
            head_id = head[0].decode() if isinstance(head[0], bytes) else str(head[0])
            raw = client.hgetall(pacer_waitest_key(alias)) or {}
            est = {(k.decode() if isinstance(k, bytes) else k): v for k, v in raw.items()}
            v = est.get(head_id)
            if v is None:
                return depth, 0.0
            v = v.decode() if isinstance(v, bytes) else str(v)
            first_denied = float(v.split(":", 1)[1])
            # Scaling review 2026-09-03 (cosmetic): first_denied was written from Redis
            # server TIME; compare against the same clock, never the dispatcher's (a >5 s skew
            # would trip the head-wait pressure permanently and freeze the fleet).
            now = time.time()
            redis_time = getattr(client, "time", None)
            if callable(redis_time):
                try:
                    sec, usec = redis_time()
                    now = float(sec) + float(usec) / 1e6
                except Exception:  # noqa: BLE001 — a fake without TIME falls to the local clock
                    now = time.time()
            return depth, max(0.0, now - first_denied)
        except Exception:  # noqa: BLE001
            return 0, 0.0

    # -- the projection (§5 formulas) ---------------------------------------------------------

    def survival_applies(self, demand: DemandModel) -> bool:
        """F5: does this curve's survival table carry enough evidence to discount the
        forecast? An explicitly injected model is the operator's own choice and is taken as
        given; a fitted curve must be this pool's OWN and rest on >= _SURVIVAL_MIN_ATTEMPTS
        attempts; anything borrowed (another pool's fit, the pooled fit, the defaults) or thin
        projects with survival = 1."""
        if self._explicit_demand is not None:
            return True
        if not demand.is_own_curve():
            return False
        return (demand.survival_attempts or 0) >= _SURVIVAL_MIN_ATTEMPTS

    def project(
        self,
        fleet: list[TaskState],
        extra_fresh: int = 0,
        demand: DemandModel | None = None,
        cached_weight: float = 1.0,
    ) -> tuple[float, float, float, float]:
        """Survival-weighted horizon-max of (arrival tok/s, inflight tokens, call starts/s),
        plus the time (s from now) of the binding arrival peak. ``extra_fresh`` models candidate
        launches as fresh instances (§4.2's expected-launch term) — and, since the forecast
        review, the tasks ECS says are in flight but have not written progress yet (booting).

        F3: the ARRIVAL term is in the pacer's weighted units — the curve's cached share of
        each prompt is discounted by *cached_weight* (``1 - share x (1 - w)``), exactly what the
        shim draws from the bucket; in-flight stays the full prompt."""
        max_arrival = max_inflight = max_qps = 0.0
        peak_at = 0.0
        d = demand or self.demand
        weighted = 1.0 - d.cached_share * (1.0 - min(1.0, max(0.0, cached_weight)))
        use_survival = self.survival_applies(d)  # F5
        candidates = fleet + [TaskState(turn=0, context_tokens=d.tokens_per_call(0))] * max(
            0, extra_fresh
        )
        for k in range(0, _HORIZON_S + 1, _HORIZON_STEP_S):
            arrival = inflight = qps = 0.0
            for t in candidates:
                period0 = d.turn_period_s(t.context_tokens)
                turn_k = t.turn + k / period0  # first-order aging
                tok_k = d.tokens_per_call(turn_k)
                period_k = d.turn_period_s(tok_k)
                s = d.survival(t.turn, k) if use_survival else 1.0
                arrival += s * tok_k * weighted / period_k
                inflight += s * tok_k * (d.call_latency_s(tok_k) / period_k)
                qps += s / period_k
            if arrival > max_arrival:
                max_arrival, peak_at = arrival, float(k)
            max_inflight = max(max_inflight, inflight)
            max_qps = max(max_qps, qps)
        return max_arrival, max_inflight, max_qps, peak_at

    def _ceiling_from_budgets(
        self,
        fleet: list[TaskState],
        budgets: Budgets,
        demand: DemandModel | None = None,
        booting: int = 0,
    ) -> tuple[int, str]:
        """Largest task count whose projected demand fits every gate — found by trying
        incremental fresh additions on top of the live fleet PLUS the booting tasks (bounded
        search; the projection is cheap). Returns (ceiling, binding_constraint_if_at_current_size).

        Forecast review 2026-09-03 §3: ``booting`` = tasks ECS counts in flight that have no
        progress key yet (image pull + boot is minutes). They were invisible to the old
        projection, so the ceiling it returned (len(fleet) + extra) undercounted the fleet —
        gating as "at_capacity" during every ramp (artificial hold-back) while their coming
        demand was not in the forecast (flood once they all started calling). They are
        modelled as fresh candidates and the ceiling is based on the ECS count."""
        base = len(fleet) + max(0, booting)
        w = budgets.cached_weight
        arrival, inflight, qps, _ = self.project(
            fleet, extra_fresh=booting, demand=demand, cached_weight=w
        )
        util = budgets.utilization_factor  # F3: halved when the cfg is stale
        binding = "none"
        if arrival > util * self._utilization * budgets.r_tok:
            binding = "arrival_budget"
        elif inflight > util * self._utilization * budgets.k_inflight:
            binding = "inflight_budget"
        elif qps > util * _QPS_UTILIZATION * budgets.r_qps:
            binding = "qps_budget"

        def _fits(n_extra: int) -> bool:
            a, i, q, _ = self.project(
                fleet, extra_fresh=booting + n_extra, demand=demand, cached_weight=w
            )
            return not (
                a > util * self._utilization * budgets.r_tok
                or i > util * self._utilization * budgets.k_inflight
                or q > util * _QPS_UTILIZATION * budgets.r_qps
            )

        # Coarse steps of 5, then refine by 1: the old search stopped at the last multiple of
        # 5 that fit, under-allocating up to 4 tasks whenever the budget sat between steps —
        # an artificial hold-back of up to 4 launches (forecast review §3).
        extra = 0
        step = 5
        while extra < 400 and _fits(extra + step):
            extra += step
        while extra < 400 and _fits(extra + 1):
            extra += 1
        return base + extra, binding

    # -- the tick -----------------------------------------------------------------------------

    def maybe_tick(self, in_flight_tasks: int) -> Decision | None:
        """Called by the dispatcher on its own cadence; runs at most every tick_interval_s.
        NEVER raises — any failure logs and holds the last good decision (a planner bug must not
        take the receive/launch loop down with it; same discipline as _refresh's ListTasks)."""
        if time.monotonic() - self._last_tick_at < self._tick_interval_s:
            return self._last_decision
        self._last_tick_at = time.monotonic()
        try:
            self._last_decision = self._tick(in_flight_tasks)
        except Exception:
            logger.exception("autoscaler tick failed; holding last good decision")
        return self._last_decision

    def _tick(self, in_flight_tasks: int) -> Decision:
        """One planning pass — forecast review 2026-09-03 §3, per ALIAS:

        The fleet is grouped by the alias each task's progress payload names (older payloads
        fall to the run's current alias). Every alias gets its own budgets (its pacer:cfg),
        its own overload / paced / queue signals, and its own fitted (pool, harness) curve;
        the dispatcher's single ceiling is the SUM of the per-alias ceilings, and the whole
        fleet is held while ANY alias is in cooldown or paced (a struggling pool freezes new
        launches — deferring a launch costs nothing, spec §1.3; the per-alias post-receive
        gate would risk SQS receive counts). Tasks ECS counts but that have no progress key
        yet (booting) are modelled as fresh candidates on the current alias."""
        self._apply_run_overrides()
        fleet = self._scan_fleet()
        now = time.monotonic()
        current = self.model_alias or ""

        groups: dict[str, list[TaskState]] = {}
        for t in fleet:
            groups.setdefault(t.alias or current, []).append(t)
        if current and current not in groups:
            groups[current] = []
        if not groups:
            groups[""] = []
        booting = max(0, in_flight_tasks - len(fleet))

        total_ceiling = 0
        agg_arrival = agg_inflight = agg_qps = 0.0
        agg_peak_at = 0.0
        agg_overloads = 0
        agg_timeouts = 0
        agg_queue_len = 0
        agg_head_wait = 0.0
        worst_share = 0.0
        binding = "none"
        binding_alias = ""
        current_budgets: Budgets | None = None
        current_demand: DemandModel | None = None
        current_arrival = 0.0
        aliases: dict[str, Any] = {}
        alias_budgets: dict[str, Budgets] = {}
        alias_overloads: dict[str, int] = {}
        any_overload = False

        for alias, tasks in groups.items():
            harness = next((t.harness for t in tasks if t.harness), None)
            if harness is None and alias:
                # per-harness rotatable aliases end in the harness name
                harness = next(
                    (
                        h
                        for h in (
                            "mini_swe_agent",
                            "claude_code",
                            "custom_minimal",
                            "codex",
                            "opencode",
                            "aider",
                        )
                        if alias.endswith(f"-{h}")
                    ),
                    None,
                )
            demand = self.demand_for(alias or None, harness)
            # The current alias uses the no-arg readers (the existing test seams patch those);
            # every other alias reads its own keys explicitly.
            if alias == current:
                budgets = self._budgets()
                overloads = self._overloads_window()
                share = self._paced_share()
                timeouts = self._paced_stats()[1]
            else:
                budgets = self._budgets(alias or None)
                overloads = self._overloads_window(alias or None)
                share, timeouts = self._paced_stats(alias or None)
            queue_len, head_wait = self._queue_pressure(alias or None)
            my_booting = booting if alias == current else 0
            # Review F4: a borrowed curve sizes this pool's in-flight with another pool's
            # speed. Floor its latency with this pool's own discovery measurement, and do not
            # size past a small fleet until a refit gives the pool its own curve.
            # (An explicitly injected model — tests, a forced curve — is by definition the
            # operator's own choice, never "borrowed".)
            borrowed = self._explicit_demand is None and not demand.is_own_curve()
            if borrowed and budgets.latency_s_max_context:
                # The floor was measured at the POOL's window (discovery probes near max
                # context); scale it by that window, not the 262K default — a 1M pool's 47 s
                # floor scaled by 262K would read as 42 s at a 236K prompt instead of ~11 s.
                demand = demand.with_latency_floor(
                    budgets.latency_s_max_context, _pool_window_tokens(alias)
                )
            if borrowed and alias not in self._borrowed_warned:
                self._borrowed_warned.add(alias)
                logger.warning(
                    "autoscaler: alias %s runs on a BORROWED curve (%s) — fleet capped at %d "
                    "tasks until a refit gives its pool a fitted curve (latency floor from "
                    "discovery: %s)",
                    alias or "(none)",
                    demand.source,
                    self._borrowed_curve_cap,
                    budgets.latency_s_max_context,
                )
            arrival, inflight, qps, peak_at = self.project(
                tasks, extra_fresh=my_booting, demand=demand, cached_weight=budgets.cached_weight
            )
            ceiling, a_binding = self._ceiling_from_budgets(
                tasks, budgets, demand=demand, booting=my_booting
            )
            if borrowed:
                borrowed_cap = max(len(tasks) + my_booting, self._borrowed_curve_cap)
                if ceiling > borrowed_cap:
                    ceiling, a_binding = borrowed_cap, "borrowed_curve"
            alias_budgets[alias] = budgets
            alias_overloads[alias] = overloads
            total_ceiling += ceiling
            agg_arrival += arrival
            agg_inflight += inflight
            agg_qps += qps
            agg_overloads += overloads
            agg_timeouts += timeouts
            agg_queue_len = max(agg_queue_len, queue_len)
            agg_head_wait = max(agg_head_wait, head_wait)
            worst_share = max(worst_share, share)
            any_overload = any_overload or overloads > 0
            if a_binding != "none" and binding == "none":
                binding, binding_alias = a_binding, alias
            if alias == current:
                current_budgets, current_demand, current_arrival = budgets, demand, arrival
                agg_peak_at = peak_at
            aliases[alias or "(none)"] = {
                "tasks": len(tasks),
                "booting": my_booting,
                "ceiling": ceiling,
                "binding": a_binding,
                "arrival_tok_s": round(arrival, 1),
                "inflight_tok": round(inflight, 1),
                "qps": round(qps, 3),
                "overloads": overloads,
                "paced_share": round(share, 4),
                "paced_timeouts": timeouts,
                "queue_len": queue_len,
                "queue_head_wait_s": round(head_wait, 1),
                "budgets_source": budgets.source,
                "curve_source": demand.source,
                "curve_borrowed": borrowed,
                # F3: the weighting the arrival figure above is in.
                "cached_weight": round(budgets.cached_weight, 4),
                "cached_share": round(demand.cached_share, 3),
                # F5: whether the survival table discounted this alias's projection.
                "survival_discount": self.survival_applies(demand),
                "survival_attempts": demand.survival_attempts,
            }

        budgets = current_budgets or self._budgets()
        demand = current_demand or self.demand
        arrival = current_arrival

        # §5.2 veto + §6.4 cooldown (owner's semantics): any overload in the window freezes
        # admission outright; stabilization = a full clean window before anything resumes.
        if any_overload:
            self._cooldown_until = now + self._stabilization_window_s
            self._cooldown_clean_since = None
        in_cooldown = now < self._cooldown_until

        # §6.6 transition events: entering a freeze is one 'overload' observation; leaving it
        # clean is one 'recovery_stabilized' (the new post-dip operating point).
        recovery_set: dict[str, Any] = {}
        if in_cooldown and not self._was_in_cooldown:
            self._emit_observation("overload", agg_arrival * 60.0, len(fleet))
            # Review F2: the downward step, once per cooldown episode, for every alias that
            # overloaded — r_tok/r_qps to _RECOVERY_MARGIN x the arrival at which the pool
            # 429'd, floored relative to the discovered seed.
            recovery_set = self._overload_recovery(aliases, alias_budgets, alias_overloads)
        elif self._was_in_cooldown and not in_cooldown and not any_overload:
            self._emit_observation("recovery_stabilized", agg_arrival * 60.0, len(fleet))
        self._was_in_cooldown = in_cooldown

        # Paced back-pressure with hysteresis (two clean windows to resume). Pressure is any
        # of: the >2s share over the limit, ANY hold-cap timeout in the window, or a wait queue
        # whose head has been denied for longer than _QUEUE_HEAD_WAIT_LIMIT_S.
        pressured = (
            worst_share > _PACED_SHARE_LIMIT
            or agg_timeouts > 0
            or (agg_queue_len > 0 and agg_head_wait > _QUEUE_HEAD_WAIT_LIMIT_S)
        )
        if pressured:
            self._paced_blocked = True
            self._paced_clean_windows = 0
        elif self._paced_blocked:
            self._paced_clean_windows += 1
            if self._paced_clean_windows >= 2:
                self._paced_blocked = False

        ceiling = total_ceiling
        override_applied = False
        if self._ceiling_override is not None:
            # Operator limits 2026-09-04: the operator's number replaces the projection.
            # Cooldown / paced holds and the hard caps below still apply (min-wins).
            ceiling = self._ceiling_override
            override_applied = True
        would_set: dict[str, float] = {}
        growth_applied = False
        growth_clamped = False

        if in_cooldown:
            binding = "cooldown"
            ceiling = min(ceiling, in_flight_tasks)  # zero NEW admission; never kill (§5.5)
        else:
            if self._paced_blocked:
                binding = "paced"
                ceiling = min(ceiling, in_flight_tasks)
            # F4 (BUILDER4-QWEN-MINI-E2E-FINDINGS-2026-09-04): growth used to live only in the
            # unpaced branch, so the loop could never learn past its OWN throttle — "paced"
            # means our bucket is binding, not that the pool is. With zero provider overloads
            # in the window the predicted-peak reconciliation runs while paced too (still
            # clamped at _GROWTH_CAP_FACTOR x the seed); any overload keeps the old behaviour.
            if not self._paced_blocked or agg_overloads == 0:
                would_set, growth_applied, growth_clamped = self._reconcile_growth(
                    now, agg_overloads, arrival, budgets, len(fleet)
                )
            if self._pending_peak is None:
                # Forecast review §3: a mature fleet's peak is NOW (peak_at == 0) — the old
                # `peak_at > 0` guard meant growth could never fire at steady state. F6: the
                # re-arm is at least one STABILIZATION window out (it was the tick interval,
                # so an aged fleet grew every 20 s: 28.1K -> 39.6K in ~3 min) — growth is
                # judged on a window the pool has had time to answer.
                self._pending_peak = (
                    now + max(agg_peak_at, self._stabilization_window_s),
                    arrival,
                )

        # Review F5: the hard cap (static env cap min per-run cap) is min-wins with the
        # planner in every mode; when IT is what binds, the record says so.
        static_cap: int | None = None
        if self.hard_cap_fn is not None:
            try:
                static_cap = int(self.hard_cap_fn())
            except Exception:  # noqa: BLE001 — an unreadable cap is simply not recorded
                static_cap = None
        if static_cap is not None and ceiling > static_cap:
            ceiling = static_cap
            if binding == "none" and ceiling <= in_flight_tasks:
                binding = "static_cap"

        if override_applied and binding == "none" and ceiling <= in_flight_tasks:
            binding = "ceiling_override"
        if binding == "none" and ceiling <= in_flight_tasks:
            binding = "at_capacity"

        self._alias_decisions = aliases
        # Owner 2026-09-03: enough to reason about what happened — one line per tick with the
        # verdict, and a WARNING on every hold/release transition (so the log alone explains a
        # ramp that stalled). The full record goes to Redis (live) and, via the capacity
        # observer, to capacity_snapshot.decision (Aurora, per tick).
        if recovery_set:
            logger.warning(
                "autoscaler: overload RECOVERY lowered budgets (%s mode): %s",
                self.mode,
                recovery_set,
            )
        held = binding in ("cooldown", "paced") or ceiling <= in_flight_tasks
        logger.info(
            "autoscaler tick mode=%s in_flight=%d fleet=%d booting=%d ceiling=%d binding=%s%s "
            "arrival=%.0f tok/s inflight=%.0f qps=%.2f overloads=%d timeouts=%d queue=%d/%.0fs "
            "paced_share=%.3f budgets=%s curve=%s aliases=%s",
            self.mode,
            in_flight_tasks,
            len(fleet),
            booting,
            ceiling,
            binding,
            f" ({binding_alias})" if binding_alias else "",
            agg_arrival,
            agg_inflight,
            agg_qps,
            agg_overloads,
            agg_timeouts,
            agg_queue_len,
            agg_head_wait,
            worst_share,
            budgets.source,
            demand.source,
            {a: (v["ceiling"], v["binding"]) for a, v in aliases.items()},
        )
        if held and not self._was_holding:
            logger.warning(
                "autoscaler HOLDING new launches: %s (in_flight=%d ceiling=%d booting=%d "
                "timeouts=%d queue=%d head_wait=%.0fs overloads=%d)",
                binding,
                in_flight_tasks,
                ceiling,
                booting,
                agg_timeouts,
                agg_queue_len,
                agg_head_wait,
                agg_overloads,
            )
        elif not held and self._was_holding:
            logger.warning(
                "autoscaler RELEASED launches: ceiling=%d in_flight=%d", ceiling, in_flight_tasks
            )
        self._was_holding = held
        decision = Decision(
            decided_at=time.time(),
            mode=self.mode,
            desired_ceiling=ceiling,
            binding_constraint=binding,
            observed_tasks=len(fleet),
            projected_arrival_tok_s=round(agg_arrival, 1),
            projected_inflight_tok=round(agg_inflight, 1),
            projected_qps=round(agg_qps, 3),
            peak_at_s=agg_peak_at,
            budgets=budgets,
            overloads_window=agg_overloads,
            paced_share=round(worst_share, 4),
            curve_source=demand.source,
            would_set=would_set,
            aliases=aliases,
            booting_tasks=booting,
            paced_timeouts=agg_timeouts,
            queue_len=agg_queue_len,
            queue_head_wait_s=round(agg_head_wait, 1),
            binding_alias=binding_alias,
            in_flight_tasks=in_flight_tasks,
            static_cap=static_cap,
            growth_applied=growth_applied,
            growth_clamped=growth_clamped,
            recovery_set=recovery_set,
            survival_discount=self.survival_applies(demand),
            ceiling_override=self._ceiling_override,
            operator_limits=dict(self._operator_limits),
        )
        self._publish(decision)
        return decision

    def _reconcile_growth(
        self,
        now: float,
        agg_overloads: int,
        arrival: float,
        budgets: Budgets,
        fleet_size: int,
    ) -> tuple[dict[str, float], bool, bool]:
        """§6.3 predicted-peak reconciliation -> +5% growth (never more, owner-fixed).
        Returns ``(would_set, growth_applied, growth_clamped)``; a no-op until the armed peak
        has arrived. Review F2: growth is bounded relative to the DISCOVERED seed — the only
        rate ever measured clean; an old cfg with no seed on record adopts its current values
        as the seed (written alongside the first growth). F6: a CLAMPED refusal is logged once
        per episode (once per seed), not every tick."""
        would_set: dict[str, float] = {}
        growth_applied = False
        growth_clamped = False
        if self._pending_peak is None or now < self._pending_peak[0]:
            return would_set, growth_applied, growth_clamped
        self._pending_peak = None
        if not (agg_overloads == 0 and arrival > 0.8 * self._utilization * budgets.r_tok):
            return would_set, growth_applied, growth_clamped
        seed_r = budgets.r_tok_seed or budgets.r_tok
        grown_r = budgets.r_tok * (1 + self._growth_step)
        if grown_r > self._growth_cap_factor * seed_r:
            growth_clamped = True
            if self._clamp_logged_seed != seed_r:
                self._clamp_logged_seed = seed_r
                logger.info(
                    "autoscaler: growth CLAMPED for %s — r_tok %.0f would exceed %.1fx "
                    "the discovered seed %.0f (logged once per episode)",
                    self.model_alias,
                    grown_r,
                    self._growth_cap_factor,
                    seed_r,
                )
            return would_set, growth_applied, growth_clamped
        self._clamp_logged_seed = None  # a reconciliation that is not clamped ends the episode
        would_set = {
            "r_tok": grown_r,
            "k_inflight": budgets.k_inflight * (1 + self._growth_step),
            "r_qps": budgets.r_qps * (1 + self._growth_step),
        }
        if self.mode == "live" and self.model_alias:
            seed_stash = (
                {}
                if budgets.r_tok_seed is not None
                else {
                    "r_tok_seed": budgets.r_tok,
                    "k_inflight_seed": budgets.k_inflight,
                    "r_qps_seed": budgets.r_qps,
                }
            )
            try:
                self._client().hset(
                    pacer_cfg_key(self.model_alias),
                    mapping={
                        **{k: repr(v) for k, v in would_set.items()},
                        **{k: repr(v) for k, v in seed_stash.items()},
                        # F3: a clean reconciliation IS fresh evidence — re-stamp.
                        "seeded_at": repr(time.time()),
                    },
                )
                growth_applied = True
                logger.info(
                    "autoscaler: +%.0f%% growth applied to %s: %s",
                    self._growth_step * 100,
                    self.model_alias,
                    would_set,
                )
                # §6.6: a clean predicted peak that earned growth IS the reconciliation-peak
                # observation.
                self._emit_observation("reconciliation_peak", arrival * 60.0, fleet_size)
            except Exception:
                logger.warning("autoscaler: growth write failed", exc_info=True)
        return would_set, growth_applied, growth_clamped

    def _overload_recovery(
        self,
        aliases: dict[str, Any],
        alias_budgets: dict[str, Budgets],
        alias_overloads: dict[str, int],
    ) -> dict[str, Any]:
        """Review F2: for every alias that overloaded this tick, lower r_tok/r_qps to
        _RECOVERY_MARGIN x the arrival at which it 429'd — the measured last-60 s admission
        rate when the pacer published one, the projection otherwise — floored at
        _RECOVERY_FLOOR_FACTOR x the discovered seed. Live mode writes pacer:cfg (re-stamping
        seeded_at: an overload IS fresh evidence); observe mode records the intended write.
        Returns {alias: {field: [old, new], "basis_tok_s": ..., "measured": bool}}."""
        out: dict[str, Any] = {}
        for alias, n_over in alias_overloads.items():
            if n_over <= 0 or not alias:
                continue
            b = alias_budgets.get(alias)
            if b is None:
                continue
            m_tok, m_qps = self._measured_arrival(alias)
            measured = m_tok > 0
            basis_tok = m_tok if measured else float(aliases.get(alias, {}).get("arrival_tok_s", 0))
            basis_qps = m_qps if measured else float(aliases.get(alias, {}).get("qps", 0))
            changes: dict[str, list[float]] = {}
            if basis_tok > 0:
                floor_r = _RECOVERY_FLOOR_FACTOR * (b.r_tok_seed or b.r_tok)
                new_r = max(floor_r, _RECOVERY_MARGIN * basis_tok)
                if new_r < b.r_tok:
                    changes["r_tok"] = [round(b.r_tok, 1), round(new_r, 1)]
            if basis_qps > 0:
                floor_q = _RECOVERY_FLOOR_FACTOR * (b.r_qps_seed or b.r_qps)
                new_q = max(floor_q, _RECOVERY_MARGIN * basis_qps)
                if new_q < b.r_qps:
                    changes["r_qps"] = [round(b.r_qps, 3), round(new_q, 3)]
            if not changes:
                continue
            record: dict[str, Any] = {
                **changes,
                "basis_tok_s": round(basis_tok, 1),
                "measured": measured,
                "applied": False,
            }
            if self.mode == "live":
                try:
                    self._client().hset(
                        pacer_cfg_key(alias),
                        mapping={
                            **{k: repr(v[1]) for k, v in changes.items()},
                            "seeded_at": repr(time.time()),
                        },
                    )
                    record["applied"] = True
                except Exception:
                    logger.warning("autoscaler: recovery write failed for %s", alias, exc_info=True)
            out[alias] = record
        return out

    def _publish(self, decision: Decision) -> None:
        """One-way broadcast via the SHARED decision-record contract (decision_record.py) —
        never read back, a failed publish never affects admission, emitted every tick."""
        record = {**decision.__dict__, "budgets": decision.budgets.__dict__}
        record["desired_ceiling"] = decision.desired_ceiling  # explicit: the shared envelope field
        _publish_decision_record(self._client(), "harness", record)

    # -- the dispatcher-facing gate -----------------------------------------------------------

    def gate(self, in_flight_tasks: int) -> tuple[bool, str]:
        """(may_launch, reason). In observe/off mode this NEVER blocks — it computes and
        publishes only; live mode enforces. The static MAX_CONCURRENT_HARNESS_TASKS ceiling
        remains in force in every mode (min-wins, design §6.7.1)."""
        decision = self.maybe_tick(in_flight_tasks)
        if decision is None or self.mode != "live":
            return True, ""
        if not self._enabled:
            # §6.7 autoscaler_enabled=false: L2's dynamic ceiling never blocks — the static
            # MAX_CONCURRENT_HARNESS_TASKS gate and the L1 pacer both stay in force.
            return True, ""
        if decision.binding_constraint in ("cooldown", "paced"):
            return False, decision.binding_constraint
        if in_flight_tasks >= decision.desired_ceiling:
            reason = decision.binding_constraint
            return False, reason if reason != "none" else "at_capacity"
        return True, ""

    def launch_stagger_s(self) -> float:
        """Forecast review §3 / exact-design §5.7: launches are spaced (jittered) so a burst
        of fresh tasks does not synchronise its first calls — E12/E13's request-axis massacre
        — and so RunTask itself is not hammered. LAUNCH_STAGGER_S (default 2.0) x U(0.5, 1.5)."""
        try:
            base = float(os.environ.get("LAUNCH_STAGGER_S", "2.0"))
        except ValueError:
            base = 2.0
        return max(0.0, base) * random.uniform(0.5, 1.5)


def survival_check(demand: DemandModel | None = None) -> float:
    """§8 test hook: a fleet near its median length must forecast LOWER than the same fleet
    fresh (spec test 7) — exported so the property is checkable without a full Autoscaler."""
    d = demand or DemandModel()
    return d.survival(60, 600.0) / max(d.survival(0, 600.0), 1e-9)
