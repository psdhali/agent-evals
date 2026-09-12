"""Job dispatcher — enqueues harness jobs to the harness-jobs queue.

The dispatcher only ever enqueues the *initial* harness job, once per
(instance, harness, model_alias, attempt) at run start.  It never enqueues
an eval job directly — the Results Writer does that reactively after
processing a harness result (architecture.md §5.1).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from swebench_eval import aws_names
from swebench_eval.cache_manifest import CacheManifest, load_manifest
from swebench_eval.evaluation.env_image import DEFAULT_SWEBENCH_VERSION
from swebench_eval.orchestrator.run_config import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    RunConfig,
)
from swebench_eval.queue.client import send_message
from swebench_eval.queue.schemas import HarnessJob

if TYPE_CHECKING:
    from swebench_eval.dataset.base import Instance

logger = logging.getLogger(__name__)


# 5b: refuse to dispatch when the warm cache is incomplete.  Off by default so
# local/dev runs (no manifest, no cache) behave as before; the control-plane
# task environment sets ENFORCE_CACHE_GATE=1 in AWS.
class CachePreconditionError(RuntimeError):
    """Raised when a run is refused because the warm cache is not ready."""


class DatasetUnavailableError(RuntimeError):
    """The pinned dataset mirror cannot be read.

    The public loader refuses the HuggingFace fallback (so the gold patch can
    never leak into a dispatcher/harness process), so an unseeded mirror is an
    error, not a silent fallback. Narrowly typed so :func:`dispatch_run` can
    absorb exactly this availability error and let a real loader bug propagate.
    """


def check_cache_precondition(swebench_version: str | None = None) -> CacheManifest | None:
    """Refuse to dispatch when the warm cache has never been built (5b gate).

    ADR-0031: admission is PER-INSTANCE in :func:`dispatch_run` (against the
    manifest's ``env_images``), not whole-run completeness — ``is_complete()``
    no longer gates, or a run of 114 already-warmed instances would be refused
    until all 35 envs exist. This function still refuses when the warm job
    never published a manifest at all, and when it published one with NOTHING
    warmed (``env_images`` empty) — the one precondition the per-instance check
    cannot discover on its own (it would just refuse instance #1 with a
    confusing message).

    Returns the loaded manifest when the gate is enforced (``None`` when it is
    off) so the per-instance env-image check in :func:`dispatch_run` reuses the
    SAME manifest instead of re-loading it (ADR-0030 H1).
    """
    if os.environ.get("ENFORCE_CACHE_GATE", "0") != "1":
        return None
    version = swebench_version
    if version is None:
        try:
            from importlib.metadata import version as _pkg_version

            version = _pkg_version("swebench")
        except Exception:  # noqa: BLE001
            version = "unknown"
    manifest = load_manifest(version)
    if manifest is None:
        raise CachePreconditionError(
            f"warm cache not built: no manifest published for swebench {version}"
        )
    # ADR-0043: admission is per-instance against ``instance_images`` (the
    # ``-inst`` images actually present); ``env_images`` is the 4.1.0-era half
    # and only keeps an old manifest readable.
    if not manifest.instance_images and not manifest.env_images:
        raise CachePreconditionError(
            f"warm cache empty: the manifest for swebench {version} lists no instance images "
            "(build the per-instance images and publish the manifest before dispatching)"
        )
    return manifest


def _resolve_model_aliases() -> dict[str, dict[str, object]]:
    """Map each gateway model alias to its resolved provider/model + gen params.

    §8 requires a run's ``config_snapshot`` to record *which literal provider/model
    each ``model_alias`` resolved to*, so forensics can tell two runs with identical
    user-facing config apart.  The alias→provider mapping lives in
    ``infra/docker/litellm_config.yaml`` (the gateway's single source), so read it
    there rather than duplicating it.

    V7b (switch-to-swebench-verified §9): Pinning ``temperature`` / ``top_p`` /
    ``top_k`` / ``reasoning_effort`` at the gateway means two runs at different
    temperatures produce byte-identical snapshots unless the params are captured
    alongside the model string.  Every published comparison depends on being able
    to state the parameters a number was produced under, so capture them.
    """
    import yaml

    cfg_path = (
        pathlib.Path(__file__).resolve().parents[3] / "infra" / "docker" / "litellm_config.yaml"
    )
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read gateway config for config_snapshot: %s", exc)
        return {}
    _GEN_PARAMS = ("temperature", "top_p", "top_k", "reasoning_effort")
    resolved: dict[str, dict[str, object]] = {}
    for entry in cfg.get("model_list", []):
        name = entry.get("model_name")
        params = entry.get("litellm_params") or {}
        if not name:
            continue
        model = params.get("model", "")
        provider = model.split("/", 1)[0] if "/" in model else ""
        entry_snap: dict[str, object] = {
            "model": model,
            "provider": provider,
            "api_base": params.get("api_base", ""),
        }
        for p in _GEN_PARAMS:
            if p in params:
                entry_snap[p] = params.get(p)
        # Compaction build (Stage 1.1): capture model_info so a run's window can
        # be resolved from the BAKED yaml when the live gateway is unreachable.
        # model_info.max_input_tokens is the provider's window; the gateway keeps
        # it value-shaped so litellm's /model/info reports it faithfully.
        mi = entry.get("model_info") or {}
        if mi.get("max_input_tokens"):
            entry_snap["max_input_tokens"] = int(mi["max_input_tokens"])
        if mi.get("max_output_tokens"):
            entry_snap["max_output_tokens"] = int(mi["max_output_tokens"])
        # O-1: the literal upstream model for per-call model_resolved.
        entry_snap["litellm_params_model"] = model
        resolved[name] = entry_snap
    return resolved


def rotatable_alias_entry(alias: str) -> dict[str, object] | None:
    """The ``resolved_models`` entry for a per-run ROTATABLE alias (2026-09-06).

    Every benchmark alias (``minimax-m2.5-codex``, ``gpt-5-mini-mini``, ...) is a
    db-model registered from :data:`swebench_eval.gateway.rotatable_models.
    ROTATABLE_MODELS`, not a ``litellm_config.yaml`` entry — so
    :func:`_resolve_model_aliases` never saw it and every run's
    ``provenance.model_resolved`` came out null while ``resolved_models`` named
    the three static yaml aliases the run never used (found on the 2026-09-06
    battery exports).  Same shape as the yaml entries, from the spec that
    registers the alias.  ``None`` for an alias this registry does not know
    (a yaml alias, or a typo — the caller keeps whatever it had)."""
    from swebench_eval.gateway.rotatable_models import ROTATABLE_MODELS

    spec = ROTATABLE_MODELS.get(alias)
    if spec is None:
        return None
    params = spec.litellm_params
    model = str(params.get("model", ""))
    entry: dict[str, object] = {
        "model": model,
        "provider": model.split("/", 1)[0] if "/" in model else "",
        "api_base": str(params.get("api_base", "")),
        "source": "rotatable_models",
    }
    for p in ("temperature", "top_p", "top_k", "reasoning_effort"):
        if p in params:
            entry[p] = params[p]
    pin = params.get("extra_body")
    if isinstance(pin, dict) and isinstance(pin.get("provider"), dict):
        entry["provider_pin"] = pin["provider"].get("order")
    for k in ("max_input_tokens", "max_output_tokens"):
        if spec.model_info.get(k):
            entry[k] = int(spec.model_info[k])  # type: ignore[call-overload]
    entry["litellm_params_model"] = model
    return entry


def resolve_model_aliases_for_run(model_alias: str) -> dict[str, dict[str, object]]:
    """``resolved_models`` = the yaml aliases + the RUN's own alias (if rotatable).

    The run's alias comes first so a reader (and ``queries._provenance``'s
    single-model display) finds the model that actually served the run without
    wading through the static entries."""
    resolved = _resolve_model_aliases()
    if model_alias and model_alias not in resolved:
        entry = rotatable_alias_entry(model_alias)
        if entry is not None:
            resolved = {model_alias: entry, **resolved}
    return resolved


def resolve_context_window(
    config: RunConfig,
    resolved_models: dict[str, dict[str, object]] | None = None,
    *,
    live_fetch: Callable[[str], dict[str, object] | None] | None = None,
) -> tuple[int | None, str]:
    """Resolve the run's per-model context window once, per run (BUILD-SPEC §2).

    The window is a property of the MODEL/ENDPOINT, and the five harnesses each
    size their compaction threshold from it.  Order (BUILD-SPEC rev 2 §2):

      1. the run config's explicit ``context_window_tokens`` — operator override;
      2. the LIVE gateway, ``GET {gateway}/model/info`` ``model_info
         .max_input_tokens`` for ``config.model_alias`` (auth-gated by the master
         key; verified serving);
      3. the BAKED ``litellm_config.yaml`` the dispatcher already parsed for
         ``resolved_models`` — fallback when the gateway is unreachable;
      4. ``DEFAULT_CONTEXT_WINDOW_TOKENS`` and log a WARNING — reaching this
         branch means the gateway has no metadata for the model.

    Returns ``(window, source)`` where ``source`` is one of ``"run_config"``,
    ``"gateway"``, ``"baked_config"``, ``"default"``, or ``"none"`` (the last
    only when ``DEFAULT_CONTEXT_WINDOW_TOKENS`` is None — a deliberate
    no-window opt-out).  Prefer (2) over (3): the dispatch image's baked copy and
    the deployed gateway's are built from the same repo but redeployed
    independently, and a recorded window that does not match what actually served
    the tokens is worse than none.

    ``live_fetch`` is injected for tests (defaults to the real /model/info
    client).  This is the ONE place a run's window is resolved — adapters never
    look it up themselves.
    """
    # 1. operator override
    if config.context_window_tokens is not None:
        return config.context_window_tokens, "run_config"

    alias = config.model_alias

    # 2. live gateway
    if live_fetch is not None:
        try:
            info = live_fetch(alias)
        except Exception:
            # baked config / default; provenance must never fail a run.
            logger.warning("live window lookup failed for %s; falling back", alias, exc_info=True)
            info = None
        if info and info.get("max_input_tokens"):
            return _coerce_window(info["max_input_tokens"]), "gateway"

    # 3. baked yaml (the same parsed structure as resolved_models)
    baked = (resolved_models or {}).get(alias) or {}
    if baked.get("max_input_tokens"):
        return _coerce_window(baked["max_input_tokens"]), "baked_config"

    # 4. floor + warning
    if DEFAULT_CONTEXT_WINDOW_TOKENS is not None:
        logger.warning(
            "no context-window metadata for model %s (gateway unreachable and not in "
            "baked config); using DEFAULT_CONTEXT_WINDOW_TOKENS=%s",
            alias,
            DEFAULT_CONTEXT_WINDOW_TOKENS,
        )
        return DEFAULT_CONTEXT_WINDOW_TOKENS, "default"
    return None, "none"


def _coerce_window(value: object) -> int:
    """Coerce a ``max_input_tokens`` value (from gateway or baked yaml) to int.

    The ``resolved_models``/``live_fetch`` values are typed ``object`` — the
    actual runtime values are ints (config numbers), but mypy cannot know that,
    so there is one explicit coercion point rather than a cast at every call site.
    """
    from typing import cast

    return int(cast("int | float | str", value))


def _gateway_model_info_fetch() -> Callable[[str], dict[str, object] | None]:
    """A live ``/model/info`` fetch bound to the run's gateway env.

    Returns a callable matching ``resolve_context_window``'s ``live_fetch``
    contract — it takes the alias, resolves the gateway base + key from the
    environment, and asks ``/model/info`` for that alias.  Built once per run,
    called at most once — the dispatcher is the single place a window is looked
    up.
    """
    from swebench_eval.gateway.model_info import fetch_model_info
    from swebench_eval.harnesses.routing import gateway_api_key, gateway_base_url

    def _fetch(alias: str) -> dict[str, object] | None:
        try:
            base = gateway_base_url()
            key = gateway_api_key()
        except Exception:
            logger.warning("cannot resolve gateway URL/key for window lookup", exc_info=True)
            return None
        return fetch_model_info(base, key, alias)

    return _fetch


def _build_reproducibility_snapshot(
    config: RunConfig,
    instances: list[Instance] | None = None,
) -> dict[str, object]:
    """PA-9 facts for a harness run — see :func:`reproducibility_facts`."""
    return reproducibility_facts(
        harness=config.harness,
        instance_ids=[inst.instance_id for inst in instances or []],
    )


def reproducibility_facts(
    *,
    harness: str | None,
    instance_ids: list[str],
) -> dict[str, object]:
    """PA-9: the resolved-facts a published RESOLVED number needs to mean anything.

    Split out of the harness-run snapshot builder (2026-09-06) so an
    image-validation run — gold-patch grades of the same ``-inst`` images —
    records the same framework/dataset/image facts; its exports had an empty
    provenance block before.  ``harness`` is None for such a run (no tool
    surface).

    ``architecture.md`` §8 requires a run's ``config_snapshot`` to record the
    RESOLVED versions actually used, not just intent — a row saying RESOLVED is
    not evidence unless you can say what produced it. Every field is cheap, and
    a run made without these can never be retrofitted. Includes:

      framework_sha          git HEAD of OUR code that produced the run
      swebench_version       pinned swebench package version (ADR-0005 grader)
      dataset_name           the HF dataset id (ADR-0043: the SWE-bench org)
      dataset_revision       the pinned HF revision (_PINNED_REVISION)
      image_digest_snapshot  the committed instance->digest file the images were built from
      harness_image_digest   the ECR -inst image the task ran
      gateway_config_hash    sha256 of infra/docker/litellm_config.yaml

    The first four plus the snapshot are ADR-0043's "triple": results graded
    under different values are never combined in one table.
    """
    import hashlib
    import subprocess

    snapshot: dict[str, object] = {}

    # 1. framework git SHA — 'which version of our code produced this'.
    #    In the deployed container there is no .git (it is dockerignored), so
    #    the SHA is baked at image build time (Dockerfile ARG -> env
    #    FRAMEWORK_SHA) and read here first; the git fallback covers local runs.
    env_sha = os.environ.get("FRAMEWORK_SHA")
    if env_sha:
        snapshot["framework_sha"] = env_sha
    else:
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            ).stdout.strip()
            snapshot["framework_sha"] = sha or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("config_snapshot: could not read framework SHA: %s", exc)
            snapshot["framework_sha"] = None

    # 2. pinned swebench version (from the installed distribution; falls back to
    #    the package metadata at import if run inside a container without git)
    try:
        from importlib.metadata import version as _pkg_version

        snapshot["swebench_version"] = _pkg_version("swebench")  # may raise
    except Exception:  # noqa: BLE001
        snapshot["swebench_version"] = None

    # 3. dataset name + pinned HF revision + the image-digest snapshot that pairs
    #    with it (ADR-0043: the pin is the pair, so both halves are recorded)
    try:
        from swebench_eval.dataset.swebench_loader import (
            _DATASET_NAME,
            _PINNED_REVISION,
            image_digest_snapshot_name,
            load_image_digest_snapshot,
        )

        snapshot["dataset_name"] = _DATASET_NAME
        snapshot["dataset_revision"] = _PINNED_REVISION
        snapshot["image_digest_snapshot"] = (
            image_digest_snapshot_name() if load_image_digest_snapshot() is not None else None
        )
    except Exception:  # noqa: BLE001
        snapshot.setdefault("dataset_name", None)
        snapshot.setdefault("dataset_revision", None)
        snapshot.setdefault("image_digest_snapshot", None)

    # 4. harness image digest — how the task was built/run; always present in AWS
    #    where the image IS the environment.  D-3 (review 2026-08-26): this was
    #    hardcoded None ("populated at 5b") and 5b never populated it. We look it
    #    up LIVE from ECR rather than reading an env var / terraform literal: the
    #    -inst digest (per instance; both the harness worker AND the eval worker
    #    run the -inst image) pins the build transitively (digests are
    #    content-addressed over the whole layer stack). Only the digest pins the
    #    build — a tag can move, so a pull can succeed and be stale. A permission
    #    (ecr:DescribeImages), not a value, so it never goes stale with a rebuild.
    snapshot["harness_image_digest"] = _resolve_harness_digest(instance_ids)

    # 5. gateway config hash — which alias->model map was live. In the deployed
    #    image the repo's infra/docker is not copied, so the hash is baked at
    #    image build time (Dockerfile ARG -> env GATEWAY_CONFIG_HASH) and read
    #    here first; the file fallback covers local runs.
    env_hash = os.environ.get("GATEWAY_CONFIG_HASH")
    if env_hash:
        snapshot["gateway_config_hash"] = env_hash
    else:
        try:
            cfg_path = (
                pathlib.Path(__file__).resolve().parents[3]
                / "infra"
                / "docker"
                / "litellm_config.yaml"
            )
            if cfg_path.exists():
                snapshot["gateway_config_hash"] = hashlib.sha256(cfg_path.read_bytes()).hexdigest()
            else:
                snapshot["gateway_config_hash"] = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("config_snapshot: could not hash gateway config: %s", exc)
            snapshot["gateway_config_hash"] = None

    # 6. harness CLI versions — the agent CLIs baked into the image at build
    #    time (Stage 3.1). harness_image_digest says WHICH image a run used;
    #    this says WHAT versions it carried, so a mid-experiment rebuild of a
    #    tag is visible instead of silently changing one comparison arm.
    try:
        hv = pathlib.Path("/opt/harness-versions.json")
        if hv.exists():
            snapshot["harness_cli_versions"] = json.loads(hv.read_text())
        else:
            snapshot["harness_cli_versions"] = None  # not a harness-image run
    except Exception as exc:  # noqa: BLE001
        logger.warning("config_snapshot: could not read harness versions: %s", exc)
        snapshot["harness_cli_versions"] = None

    # 7. harness tool surface (B7/E11b) — WHAT the agent was allowed to touch, so
    #    a five-harness cost/score table compares the harnesses, not our
    #    configuration choices. Each adapter may declare tool_surface();
    #    "native" = the CLI's own inventory.
    if harness is None:
        snapshot["harness_tool_surface"] = None  # not a harness run (image validation)
        return snapshot
    try:
        from swebench_eval.harnesses.registry import HARNESS_ADAPTERS

        adapter_cls = HARNESS_ADAPTERS[harness]
        surface = getattr(adapter_cls, "tool_surface", lambda: {"tools": "native"})()
        snapshot["harness_tool_surface"] = (
            surface if isinstance(surface, dict) else {"tools": "native"}
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("config_snapshot: could not read harness tool surface: %s", exc)
        snapshot["harness_tool_surface"] = {"tools": "native"}

    return snapshot


# --- D-3 harness image digest resolution (review 2026-08-27 §5) ---------------
# `config_snapshot.harness_image_digest` should record the digest of the image the
# task ACTUALLY ran, so a run is reproducible. It was formerly a hardcoded
# terraform literal (went stale on the next -hw/-inst rebuild) / an env var that
# nobody wired. Resolve it live from ECR instead — a permission, not a value.
#
# The -inst image is what BOTH the harness worker and the eval worker execute
# (evaluation/env_image.py uses the `-inst` tag), so it transitively pins the
# whole stack (which official base digest, which framework SHA).  ADR-0043:
# there is no -hw/env-hash fallback any more — None when no -inst resolves
# (Trap 3: unknown is honest).
def _digest_for_inst(ecr: Any, repo: str, version: str, instance_id: str) -> str | None:
    """ECR digest of ``<version>-<instance_id>-inst``, or None if it doesn't exist."""
    tag = f"{version}-{instance_id}-inst"
    try:
        details = ecr.describe_images(repositoryName=repo, imageIds=[{"imageTag": tag}])[
            "imageDetails"
        ]
        digest = details[0]["imageDigest"] if details else None
        return str(digest) if digest else None
    except ecr.exceptions.ImageNotFoundException:
        return None  # no -inst built for this instance yet — fallback below
    except Exception:
        # any API/network error -> unknown digest, never raise (grandparent scope
        # treats None as unknown; a digest lookup must not fail a dispatch)
        logger.warning("could not resolve -inst digest for %s", tag, exc_info=True)
        return None


def _resolve_harness_digest(instances: Sequence[Instance | str]) -> str | None:
    """Resolve the ``-inst`` image digest for a run's (first resolvable) instance.

    Scoped to the run's instances (a run is single-instance by plan), so the
    recorded digest is the actual image the task ran.  ``None`` when no
    ``-inst`` exists or ECR cannot be asked — never fabricated.  Takes
    ``Instance`` objects or bare instance ids (the validation-run path).
    """
    instance_ids = [i if isinstance(i, str) else i.instance_id for i in instances]
    import boto3

    # ECR's describe_images(repositoryName=...) takes the BARE repo name, but
    # HARNESS_IMAGE_REPO may hold a full docker URL (the same contract confusion
    # that bit the build script's _ecr_login) — strip any registry prefix.
    _repo = os.environ.get("HARNESS_IMAGE_REPO") or aws_names.named("harness-worker")
    repo = _repo.rsplit("/", 1)[-1] if "/" in _repo else _repo
    version = os.environ.get("SWEBENCH_VERSION", DEFAULT_SWEBENCH_VERSION)
    if not instance_ids:
        return None
    try:
        ecr = boto3.client("ecr", region_name=aws_names.region())
    except Exception:  # noqa: BLE001
        return None
    for instance_id in instance_ids:
        d = _digest_for_inst(ecr, repo, version, instance_id)
        if d:
            return d
    return None


def register_run(
    run_id: str,
    harness_name: str,
    model_alias: str,
    config_snapshot: dict[str, object] | None = None,
) -> None:
    """Idempotently create the ``runs`` and ``run_targets`` rows for a run.

    Every path that can produce an ``instance_results`` row MUST call this
    first (R2-2): the reporting join goes through ``run_targets``, and an
    unattributed result is silently dropped from the resolve-rate/run-summary
    denominator at Phase 8.  Both the dispatcher and the test scripts use this
    so there is exactly one way to start a run.
    """
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                # run-launch (BUILDER4-RUN-LAUNCH-ORCHESTRATOR §4/D2): the
                # claim path (control_plane/run_launch.py) already inserted
                # this row (status='claimed', a RAW config_snapshot) before
                # this call — ON CONFLICT DO NOTHING would then silently
                # discard the RESOLVED snapshot this function computes
                # (resolved_models, window, reproducibility facts), which is
                # the whole point of calling it.  Overwrite config_snapshot
                # on conflict; leave status alone (the claim path's own state
                # machine owns it from here — this INSERT's 'running' in
                # VALUES never applies once the row already exists).
                """INSERT INTO runs (run_id, config_snapshot, status)
                   VALUES (%s, %s, 'running')
                   ON CONFLICT (run_id) DO UPDATE SET config_snapshot = EXCLUDED.config_snapshot""",
                (run_id, json.dumps(config_snapshot or {})),
            )
            cur.execute(
                """INSERT INTO run_targets (run_id, harness, model_alias)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (run_id, harness_name, model_alias),
            )
        conn.commit()
        # ADR-0034 §2 / M1.2: a run starting must open the gates immediately —
        # publish Aurora's control truth to Valkey NOW (the results_writer tick
        # may be idle), and stamp the run-activity marker so the tick stops
        # being idle and keeps the gates fresh every 30s while this run lives.
        from swebench_eval.control import state as control_state

        control_state.publish_from_db(conn)
        control_state.mark_runs_active()
    finally:
        conn.close()


def check_instance_image_admission(
    instance_id: str,
    manifest: CacheManifest,
    snapshot_digests: dict[str, str] | None,
) -> dict[str, str]:
    """ADR-0043 per-instance admission: a present ``-inst`` built FROM the pinned base.

    Refuses (naming the instance and the digests) when the manifest lists no
    ``-inst`` image for it, or when the image's recorded ``base_image_digest``
    differs from the committed snapshot's digest for that instance — the
    image was built from a moved ``:latest`` and would grade in an environment
    the pin does not describe.  A snapshot that carries no entry for the
    instance, or an image entry that recorded no base digest, cannot be
    checked and is admitted with a warning (an absent record is "unknown",
    never "mismatch" — but it is logged so the gap is visible).

    Returns the manifest entry so the caller can stamp its digest on the job.
    """
    entry = manifest.instance_images.get(instance_id)
    if entry is None:
        raise CachePreconditionError(
            f"no -inst image for {instance_id} in the cache manifest "
            "(build it: build_phase0_instances_v2 --base official --promote, then "
            "publish the manifest)"
        )
    built_from = entry.get("base_image_digest", "")
    pinned = (snapshot_digests or {}).get(instance_id, "")
    if built_from and pinned:
        if built_from != pinned:
            raise CachePreconditionError(
                f"-inst image for {instance_id} was built FROM {built_from} but the "
                f"committed image-digest snapshot pins {pinned}; rebuild it from the "
                "pinned digest (ADR-0043) — a moved :latest is not the benchmark"
            )
    else:
        logger.warning(
            "admission for %s could not check the base digest (image recorded %r, "
            "snapshot pins %r); admitted on presence only",
            instance_id,
            built_from,
            pinned,
        )
    return entry


def dispatch_run(
    run_id: str,
    instances: list[Instance],
    config: RunConfig,
) -> int:
    """Enqueue one ``HarnessJob`` per (instance, attempt) to ``harness-jobs``.

    The run config (harness, model alias, ceilings, timeout) comes from a
    single ``RunConfig`` — its defaults are the one source (ADR-0019).

    Returns the number of jobs enqueued.
    """
    # 5b: the warm-cache gate refuses the dispatch before anything is enqueued
    # (env images / mirrors incomplete → a run would fail PIPELINE-side, not at
    # the agent).  Enforced only under ENFORCE_CACHE_GATE=1 (AWS). Returns the
    # manifest when enforced so the per-instance env-image check below reuses
    # the same object rather than re-loading it.
    manifest = check_cache_precondition()

    # §8: persist the RESOLVED config actually used — the RunConfig fields plus
    # which literal provider/model each model_alias resolves to (review §5b),
    # plus the PA-9 reproducibility facts (SHA, versions, digest, config hash).
    snapshot = dataclasses.asdict(config)
    snapshot["resolved_models"] = resolve_model_aliases_for_run(config.model_alias)
    # Compaction build (Stage 1.1): resolve the run's context window ONCE per run
    # and record it + its source so a run's results can be interpreted.  A run
    # whose window we cannot read back cannot have its compaction behaviour
    # audited.  Threaded to every HarnessJob below.
    resolved_models = snapshot["resolved_models"]
    window, window_source = resolve_context_window(
        config,
        resolved_models,
        live_fetch=_gateway_model_info_fetch(),
    )
    snapshot["context_window_tokens"] = window
    snapshot["context_window_source"] = window_source
    snapshot.update(_build_reproducibility_snapshot(config, instances))
    register_run(run_id, config.harness, config.model_alias, config_snapshot=snapshot)

    # ADR-0043 / ADR-0031: the admission list is what is PRESENT — the
    # manifest's ``instance_images`` (the ``-inst`` images the build tier
    # pushed), checked against the committed image-digest snapshot so an image
    # built from a moved ``:latest`` is refused by name.  Dispatch stays
    # all-or-nothing: the first refused instance raises and nothing is
    # enqueued (ADR-0031 decision 4).  Only the REFUSAL is gated
    # (ENFORCE_CACHE_GATE=1 → manifest is not None); a gate-off local dispatch
    # carries on exactly as before.
    snapshot_digests: dict[str, str] | None = None
    if manifest is not None:
        from swebench_eval.dataset.swebench_loader import load_image_digest_snapshot

        snapshot_digests = load_image_digest_snapshot()

    count = 0
    for instance in instances:
        # Refuse AT ENQUEUE when the instance's image is not built or was built
        # from the wrong base — the exact failure a worker would otherwise hit
        # silently at the sentinel check, or grade in the wrong environment.
        if manifest is not None:
            check_instance_image_admission(instance.instance_id, manifest, snapshot_digests)

        for attempt in range(1, config.attempts_per_instance + 1):
            job = HarnessJob(
                run_id=run_id,
                instance_id=instance.instance_id,
                repo_url=f"https://github.com/{instance.repo}",
                base_commit=instance.base_commit,
                # The bare dataset statement. Per-run instructions (2026-09-09) are NOT
                # appended here: the deployed worker rebuilds this field from the mirror
                # row (ADR-0032) — they travel via Redis (run_launch → harness_worker).
                problem_statement=instance.problem_statement,
                attempt_number=attempt,
                harness_name=config.harness,
                model_alias=config.model_alias,
                # ADR-0043: no env images; the per-instance family is selected
                # from the instance id (harness_dispatcher._family_for_job).
                env_image_key="",
                timeout_seconds=config.timeout_seconds,
                max_tokens_per_instance=config.max_tokens_per_instance,
                max_cost_usd_per_instance=config.max_cost_usd_per_instance,
                max_turns_per_instance=config.max_turns_per_instance,
                context_window_tokens=window,
            )
            send_message("harness-jobs", _dataclass_to_dict(job))
            count += 1

    # ADR-0034 M1.8: seed the run summary's `expected` so an abort can report
    # `completed + aborted_in_flight + never_dispatched == expected` (the
    # denominator rule).  `expected` is the number of jobs this dispatch
    # enqueued — the actual denominator for a clean run.
    _seed_expected(run_id, count)

    logger.info("Dispatched %d harness jobs for run %s", count, run_id)
    return count


def _seed_expected(run_id: str, expected: int) -> None:
    """Write ``run_summary.summary_json.expected`` for a freshly dispatched run.

    M1.8's summary is maintained incrementally by the Results Writer; this is
    the one field only the dispatcher knows (it enqueued the jobs).  Idempotent —
    the FIRST write wins so a re-run cannot overwrite a richer summary.
    """
    from swebench_eval.database.connection import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO run_summary (run_id, summary_json)
                   VALUES (%s, jsonb_build_object('expected', %s))
                   ON CONFLICT (run_id) DO NOTHING""",
                (run_id, expected),
            )
        conn.commit()
    except Exception:  # seeding the summary is maintenance, never a dispatch
        # blocker.  The run may not exist yet in a test/read replica, or
        # the summary write may race a teardown; the Results Writer maintains the
        # summary from truth anyway (M1.8).
        logger.warning("could not seed run_summary.expected for run %s", run_id, exc_info=True)
    finally:
        conn.close()


def _dataclass_to_dict(obj: Any) -> dict[str, Any]:
    """Convert a dataclass instance to a JSON-serializable dict."""
    import dataclasses

    return dataclasses.asdict(obj)
