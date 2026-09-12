"""Review §5b + PA-9: the run's config_snapshot captures the alias→provider/model
resolution AND the reproducibility facts.

§8 requires a run's config_snapshot to record which literal provider/model each
model_alias resolved to, so forensics can tell two runs with identical user-facing
config apart.  The resolution is read from the gateway's litellm_config.yaml (the
single source), not duplicated.

PA-9 (persistence): a published RESOLVED number is only meaningful if a run records
what produced it — framework SHA, pinned swebench version, dataset revision, harness
image digest, gateway config hash. Runs made without these can never be retrofitted.
"""

from __future__ import annotations

from unittest import mock

from swebench_eval.orchestrator.control_plane.dispatcher import (
    _build_reproducibility_snapshot,
    _resolve_model_aliases,
)
from swebench_eval.orchestrator.run_config import RunConfig


def test_alias_resolves_to_provider_model() -> None:
    """Every gateway model_list alias maps to a concrete provider/model string."""
    resolved = _resolve_model_aliases()
    assert "cheap-oss-model" in resolved
    entry = resolved["cheap-oss-model"]
    # provider prefix present and the model is a real id, not empty.
    assert entry["provider"]  # e.g. "openrouter"
    assert isinstance(entry["model"], str) and "/" in entry["model"], entry


def test_resolved_models_carry_generation_params() -> None:
    """V7b/DoD 11: gateway-pinned generation params must land in the snapshot.

    Otherwise two runs at different temperatures produce byte-identical
    `config_snapshot["resolved_models"]` — exactly the "can't tell two runs with
    identical user-facing config apart" bug the snapshot exists to prevent, now
    caused by the params being pinned at the gateway.  Proved by mutation:
    reverting the gen-param capture (dropping the `for p in _GEN_PARAMS` loop)
    makes this test fail on the `temperature` assertion.
    """
    import yaml as _yaml

    from swebench_eval.orchestrator.control_plane import dispatcher as disp

    def _fake(hot: float) -> object:
        return {
            "model_list": [
                {
                    "model_name": "qwen3-coder-next",
                    "litellm_params": {
                        "model": "openrouter/qwen/qwen3-coder-next",
                        "temperature": hot,
                        "top_p": 0.95,
                        "top_k": 40,
                        "reasoning_effort": "high",
                    },
                }
            ]
        }

    with mock.patch.object(_yaml, "safe_load", side_effect=lambda *_a, **_k: _fake(1.0)):
        hot = disp._resolve_model_aliases()
    entry = hot["qwen3-coder-next"]
    assert entry["temperature"] == 1.0
    assert entry["top_p"] == 0.95
    assert entry["top_k"] == 40
    assert entry["reasoning_effort"] == "high"

    # A temperature change to the gateway config must change the snapshot.
    with mock.patch.object(_yaml, "safe_load", side_effect=lambda *_a, **_k: _fake(0.2)):
        cold = disp._resolve_model_aliases()
    assert cold["qwen3-coder-next"]["temperature"] == 0.2
    assert cold["qwen3-coder-next"] != entry


def test_config_snapshot_roundtrips_run_config() -> None:
    """dispatch-specific snapshot joins RunConfig fields with resolved models."""
    import dataclasses

    from swebench_eval.orchestrator.run_config import RunConfig

    cfg = RunConfig(model_alias="cheap-oss-model", harness="custom_minimal")
    snapshot = dataclasses.asdict(cfg)
    snapshot["resolved_models"] = _resolve_model_aliases()
    assert snapshot["model_alias"] == "cheap-oss-model"
    assert snapshot["resolved_models"]["cheap-oss-model"]["model"]


def test_pa9_snapshot_records_all_five_reproducibility_facts() -> None:
    """PA-9: a real dispatch writes all five resolved facts, not just intent.

    A row saying RESOLVED is not evidence unless it records what produced it.
    `_build_reproducibility_snapshot` must populate every field that architecture
    §8 requires — framework SHA, pinned swebench version, dataset revision,
    harness image digest and gateway config hash. Runs made without these can
    never be retrofitted, so the keys must be present now.
    """
    cfg = RunConfig(model_alias="cheap-oss-model", harness="custom_minimal")
    snap = _build_reproducibility_snapshot(cfg)

    expected = [
        "framework_sha",
        "swebench_version",
        "dataset_revision",
        "harness_image_digest",
        "gateway_config_hash",
        "harness_cli_versions",  # Stage 3.1: the agent CLI versions baked in
    ]
    assert set(expected) <= set(snap), f"missing PA-9 fields: {set(expected) - set(snap)}"

    # a git SHA is 40 hex chars; the dataset revision is _PINNED_REVISION (40 hex).
    for key in ("framework_sha", "dataset_revision"):
        val = snap[key]
        assert val is None or (isinstance(val, str) and len(val) == 40)
    # the gateway config hash, when present, is a 64-char sha256.
    val = snap["gateway_config_hash"]
    assert val is None or (isinstance(val, str) and len(val) == 64)


def test_pa9_snapshot_resilient_outside_git() -> None:
    """When git/config are unavailable (e.g. a container without them), the snapshot
    still records every field — as None — rather than raising, so a run is never
    blocked from starting by a missing reproducibility fact."""
    # `import subprocess` happens inside the helper, so patch at the source module.
    with mock.patch(
        "subprocess.run",
        side_effect=FileNotFoundError("git not installed"),
    ):
        cfg = RunConfig(model_alias="cheap-oss-model", harness="custom_minimal")
        snap = _build_reproducibility_snapshot(cfg)
    assert snap["framework_sha"] is None
    assert {
        "framework_sha",
        "swebench_version",
        "dataset_revision",
        "harness_image_digest",
        "gateway_config_hash",
        "harness_cli_versions",
    } <= set(snap)


def test_harness_image_digest_resolved_from_inst(monkeypatch) -> None:
    """D-3 (review 2026-08-27 §5): `harness_image_digest` is the -inst digest,
    resolved LIVE from ECR per instance (not a hardcoded terraform literal / env
    var that goes stale on the next rebuild). The recorded digest must be the
    actual image the task ran.

    Mutation: revert to `snapshot["harness_image_digest"] = None` (or the old
    env read); this test fails."""
    from swebench_eval.dataset.base import Instance
    from swebench_eval.orchestrator.control_plane import dispatcher as disp
    from swebench_eval.orchestrator.control_plane.dispatcher import (
        _build_reproducibility_snapshot,
    )
    from swebench_eval.orchestrator.run_config import RunConfig

    inst = Instance(
        instance_id="scikit-learn__scikit-learn-25102",
        repo="scikit-learn/scikit-learn",
        base_commit="f9a1cf0",
        problem_statement="p",
        fail_to_pass="",
        pass_to_pass="",
    )
    # no instances -> unknown (None), never fabricated (real resolver path)
    snap0 = _build_reproducibility_snapshot(RunConfig(model_alias="cheap-oss-model"))
    assert snap0["harness_image_digest"] is None

    # with instances -> recorded from the (mocked) -inst digest resolution
    monkeypatch.setattr(
        disp,
        "_resolve_harness_digest",
        lambda insts, env_keys=None: "sha256:3f47e2b33d5e44da7dede842e4363037b1b80df885cbec7cedf9c835ad8e6947",
    )
    snap = _build_reproducibility_snapshot(RunConfig(model_alias="cheap-oss-model"), [inst])
    assert snap["harness_image_digest"] == (
        "sha256:3f47e2b33d5e44da7dede842e4363037b1b80df885cbec7cedf9c835ad8e6947"
    )


def test_snapshot_records_the_adr_0043_triple(monkeypatch) -> None:
    """ADR-0043: a run records dataset NAME, revision and the image-digest
    snapshot file it was built from — results graded under different values are
    never combined, so all three must be on the row."""
    from swebench_eval.orchestrator.control_plane import dispatcher as disp
    from swebench_eval.orchestrator.control_plane.dispatcher import (
        _build_reproducibility_snapshot,
    )
    from swebench_eval.orchestrator.run_config import RunConfig

    monkeypatch.setattr(disp, "_resolve_harness_digest", lambda insts: None)
    snap = _build_reproducibility_snapshot(RunConfig(model_alias="cheap-oss-model"))
    assert snap["dataset_name"] == "SWE-bench/SWE-bench_Verified"
    assert snap["dataset_revision"] == "78f471bf655a3137b2e8a75af1501690ec009ec3"
    # The committed snapshot for this pin ships as package data.
    assert snap["image_digest_snapshot"] == "SWE-bench_Verified-78f471bf655a.json"
    assert snap["swebench_version"] == "5.0.2"


def test_resolve_harness_digest_uses_the_inst_image(monkeypatch) -> None:
    """The resolver records the -inst digest when it exists (ADR-0043: no -hw
    fallback — None when no -inst resolves).

    Mutation: make _digest_for_inst return None always (or drop the -inst arm);
    a scikit-25102 -inst (which exists in ECR) must resolve to its digest."""
    from swebench_eval.dataset.base import Instance
    from swebench_eval.orchestrator.control_plane import dispatcher as disp

    inst = Instance(
        instance_id="scikit-learn__scikit-learn-25102",
        repo="scikit-learn/scikit-learn",
        base_commit="c",
        problem_statement="p",
        fail_to_pass="",
        pass_to_pass="",
    )
    inst_digest = "sha256:3f47e2b33d5e44da7dede842e4363037b1b80df885cbec7cedf9c835ad8e6947"

    def _fake_describe_images(**kw):
        tag = kw["imageIds"][0]["imageTag"]
        return {"imageDetails": [{"imageDigest": f"sha256:{tag}"}]}

    class _FakeECR:
        exceptions = type("Exc", (), {"ImageNotFoundException": Exception})
        describe_images = staticmethod(_fake_describe_images)

    monkeypatch.setattr(disp, "_digest_for_inst", lambda ecr, repo, v, iid: inst_digest)
    assert disp._resolve_harness_digest([inst]) == inst_digest
    monkeypatch.setattr(disp, "_digest_for_inst", lambda ecr, repo, v, iid: None)
    assert disp._resolve_harness_digest([inst]) is None  # no fallback, never fabricated


def test_resolve_harness_digest_normalizes_full_url_repo(monkeypatch) -> None:
    """HARNESS_IMAGE_REPO may be a full docker URL; describe_images needs the bare
    repo name. Regression for the live AccessDenied (the URL was passed as the
    repositoryName, so ECR could not match the granted `repository/...` ARN)."""
    from swebench_eval.dataset.base import Instance
    from swebench_eval.orchestrator.control_plane import dispatcher as disp

    inst = Instance(
        instance_id="scikit-learn__scikit-learn-25102",
        repo="x/y",
        base_commit="c",
        problem_statement="p",
        fail_to_pass="",
        pass_to_pass="",
    )
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        disp,
        "_digest_for_inst",
        lambda ecr, repo, v, iid: (seen.update(repo=repo), "sha256:abc")[1],
    )
    monkeypatch.setenv(
        "HARNESS_IMAGE_REPO",
        "123456789012.dkr.ecr.us-west-2.amazonaws.com/eval-dev-harness-worker",
    )
    disp._resolve_harness_digest([inst])
    assert seen.get("repo") == "eval-dev-harness-worker"
