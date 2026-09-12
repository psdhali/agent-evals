"""Unit tests for the pure-logic pieces of scripts/build_phase0_instances_v2.py
(builder5-image-build-tier Stage 3) — sharding, filtering, resume — none of
which touch AWS or docker, so they run in CI.

T2's "sharding is disjoint and complete" and "resume does not rebuild" live
assertions are the real proof at scale; these are the fast, no-cloud version
of the same properties, run on every commit.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_phase0_instances_v2 as p0

# ---------------------------------------------------------------------------
# _lpt_shard — disjoint, complete, deterministic, balanced
# ---------------------------------------------------------------------------

# Mirrors §3's real shape: one big group (repo), several small ones.
_ENV_MAP = {
    "envA": [f"a-{i}" for i in range(75)],  # sympy-sized
    "envB": [f"b-{i}" for i in range(44)],  # sphinx-sized
    "envC": [f"c-{i}" for i in range(43)],
    "envD": [f"d-{i}" for i in range(36)],
    "envE": [f"e-{i}" for i in range(5)],
    "envF": [f"f-{i}" for i in range(4)],
    "envG": [f"g-{i}" for i in range(1)],
}


def _all_instances(env_map: dict[str, list[str]]) -> set[str]:
    return {i for instances in env_map.values() for i in instances}


def test_lpt_shard_disjoint_and_complete() -> None:
    num_shards = 3
    shards = [set(p0._lpt_shard(_ENV_MAP, i, num_shards)) for i in range(num_shards)]

    # complete: union == every instance
    union: set[str] = set()
    for s in shards:
        union |= s
    assert union == _all_instances(_ENV_MAP)

    # disjoint: no instance in two shards
    for i in range(num_shards):
        for j in range(i + 1, num_shards):
            assert not (shards[i] & shards[j]), f"shard {i} and {j} overlap"


def test_lpt_shard_never_splits_an_env() -> None:
    """Shard by ENV, never by instance (brief §5.4) — every env's instances
    land in exactly one shard."""
    num_shards = 4
    shard_of: dict[str, int] = {}
    for i in range(num_shards):
        for iid in p0._lpt_shard(_ENV_MAP, i, num_shards):
            shard_of[iid] = i
    for env_hash, instances in _ENV_MAP.items():
        shards_seen = {shard_of[iid] for iid in instances}
        assert len(shards_seen) == 1, f"{env_hash} split across shards {shards_seen}"


def test_lpt_shard_deterministic() -> None:
    """Two independent computations (e.g. two ENV_SHARD workers on different
    hosts) must produce byte-identical partitions with no coordination."""
    a = [p0._lpt_shard(_ENV_MAP, i, 3) for i in range(3)]
    b = [p0._lpt_shard(_ENV_MAP, i, 3) for i in range(3)]
    assert a == b


def test_lpt_shard_preserves_total_at_various_shard_counts() -> None:
    """Every shard count from 1 to len(_ENV_MAP) still partitions completely
    (no instance gained or lost) — the property that actually matters for
    T2, independent of how well-balanced any particular n happens to be.
    (Balance itself is a property of the REAL 40-env distribution, measured
    live in §3 of the brief — not something a 7-env synthetic fixture, with
    fewer big items than bins at n=6, can meaningfully assert.)"""
    total = len(_all_instances(_ENV_MAP))
    for num_shards in range(1, len(_ENV_MAP) + 1):
        loads = [len(p0._lpt_shard(_ENV_MAP, i, num_shards)) for i in range(num_shards)]
        assert sum(loads) == total, f"n={num_shards}: {loads} does not sum to {total}"


def test_lpt_shard_rejects_bad_shard_index() -> None:
    for bad in (-1, 3):
        try:
            p0._lpt_shard(_ENV_MAP, bad, 3)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for shard_index={bad}")


def test_lpt_shard_rejects_zero_shards() -> None:
    try:
        p0._lpt_shard(_ENV_MAP, 0, 0)
    except ValueError:
        return
    raise AssertionError("expected ValueError for num_shards=0")


# ---------------------------------------------------------------------------
# _repo_weight_seconds / weighted _lpt_shard — 2026-09-04, T1 + 500-build
# planning: balance by real per-repo build-time, not raw instance count.
# ---------------------------------------------------------------------------


def test_repo_weight_seconds_known_repos() -> None:
    assert p0._repo_weight_seconds("matplotlib__matplotlib-25287") == 742.0
    assert p0._repo_weight_seconds("django__django-10097") == 448.0
    assert p0._repo_weight_seconds("scikit-learn__scikit-learn-25102") == 466.0


def test_repo_weight_seconds_unknown_repo_falls_back_to_default() -> None:
    # sympy has no real sample yet — must fall back, not KeyError or 0.
    assert p0._repo_weight_seconds("sympy__sympy-12345") == p0._DEFAULT_REPO_WEIGHT_S


def test_repo_weight_seconds_malformed_id_falls_back_cleanly() -> None:
    # No "__" at all (shouldn't happen with real SWE-bench ids, but must not
    # crash) — .split("__", 1)[0] on a string with no "__" returns the whole
    # string, which then simply misses _REPO_WEIGHT_S and falls back.
    assert p0._repo_weight_seconds("not-a-real-instance-id") == p0._DEFAULT_REPO_WEIGHT_S


# A mix that mirrors the real T1 finding: one small env of a HEAVY repo next
# to a bigger env of a LIGHT repo — same instance count won't mean same wall
# time, which is exactly the property the weighted mode has to fix.
_WEIGHTED_ENV_MAP = {
    "env-mpl": [f"matplotlib__matplotlib-{i}" for i in range(5)],  # 5 * 742s = 3710s
    "env-django-a": [f"django__django-{i}" for i in range(5)],  # 5 * 448s = 2240s
    "env-django-b": [f"django__django-{100 + i}" for i in range(5)],  # another 2240s
}


def test_lpt_shard_unweighted_still_balances_by_raw_count() -> None:
    # Baseline: with no weight_fn, 3 envs of 5 instances each split evenly
    # across 3 shards, one env per shard — the ORIGINAL behavior, unchanged.
    shards = [p0._lpt_shard(_WEIGHTED_ENV_MAP, i, 3) for i in range(3)]
    assert sorted(len(s) for s in shards) == [5, 5, 5]


def test_lpt_shard_weighted_isolates_the_heavy_env() -> None:
    # With real weights, the 5-instance matplotlib env (3710s) alone
    # outweighs EITHER django env (2240s) — LPT must give it its own shard
    # rather than pairing it with a django env, which would make that shard
    # the long pole. This is the concrete property raw-count balancing gets
    # wrong and weighted balancing gets right.
    shards = [
        p0._lpt_shard(_WEIGHTED_ENV_MAP, i, 3, weight_fn=p0._repo_weight_seconds) for i in range(3)
    ]
    mpl_shard = next(s for s in shards if any("matplotlib" in iid for iid in s))
    assert all(
        "matplotlib" in iid for iid in mpl_shard
    ), "the heavy matplotlib env must not share a shard with anything else"


def test_lpt_shard_weighted_disjoint_and_complete() -> None:
    num_shards = 3
    shards = [
        set(p0._lpt_shard(_WEIGHTED_ENV_MAP, i, num_shards, weight_fn=p0._repo_weight_seconds))
        for i in range(num_shards)
    ]
    union: set[str] = set()
    for s in shards:
        assert not (s & union), "weighted shards must stay disjoint"
        union |= s
    assert union == _all_instances(_WEIGHTED_ENV_MAP)


def test_lpt_shard_weighted_deterministic() -> None:
    a = [
        p0._lpt_shard(_WEIGHTED_ENV_MAP, i, 3, weight_fn=p0._repo_weight_seconds) for i in range(3)
    ]
    b = [
        p0._lpt_shard(_WEIGHTED_ENV_MAP, i, 3, weight_fn=p0._repo_weight_seconds) for i in range(3)
    ]
    assert a == b


# ---------------------------------------------------------------------------
# _apply_env_filter / _apply_env_limit
# ---------------------------------------------------------------------------

_MIRROR = {
    "a-0": {"repo": "django/django"},
    "b-0": {"repo": "sympy/sympy"},
    "c-0": {"repo": "astropy/astropy"},
}
_SMALL_MAP = {"envA": ["a-0"], "envB": ["b-0"], "envC": ["c-0"]}


def test_apply_env_filter_keeps_matching_repo_prefix(monkeypatch) -> None:
    monkeypatch.setenv("ENV_FILTER", "django,sympy")
    kept = p0._apply_env_filter(_SMALL_MAP, _MIRROR)
    assert set(kept) == {"envA", "envB"}


def test_apply_env_filter_empty_is_noop(monkeypatch) -> None:
    monkeypatch.delenv("ENV_FILTER", raising=False)
    kept = p0._apply_env_filter(_SMALL_MAP, _MIRROR)
    assert kept == _SMALL_MAP


def test_apply_env_filter_case_insensitive(monkeypatch) -> None:
    monkeypatch.setenv("ENV_FILTER", "Django")
    kept = p0._apply_env_filter(_SMALL_MAP, _MIRROR)
    assert set(kept) == {"envA"}


def test_apply_env_limit_keeps_largest_envs_deterministically(monkeypatch) -> None:
    monkeypatch.setenv("ENV_LIMIT", "2")
    kept = p0._apply_env_limit(_ENV_MAP)
    assert set(kept) == {"envA", "envB"}  # the two biggest (75, 44)


def test_apply_env_limit_ties_break_alphabetically(monkeypatch) -> None:
    tied = {"envZ": ["z-0"], "envA": ["a-0"], "envM": ["m-0"]}
    monkeypatch.setenv("ENV_LIMIT", "2")
    kept = p0._apply_env_limit(tied)
    assert set(kept) == {"envA", "envM"}  # alphabetically first two of equal size


def test_apply_env_limit_unset_is_noop(monkeypatch) -> None:
    monkeypatch.delenv("ENV_LIMIT", raising=False)
    assert p0._apply_env_limit(_ENV_MAP) == _ENV_MAP


# ---------------------------------------------------------------------------
# _apply_resume — the resume-from-ECR-state contract
# ---------------------------------------------------------------------------


def test_apply_resume_skips_already_pushed() -> None:
    targets = [("a", "envA"), ("b", "envA"), ("c", "envB")]
    todo = p0._apply_resume(targets, already={"a", "c"}, forced=set())
    assert todo == [("b", "envA")]


def test_apply_resume_force_instances_overrides_already_pushed() -> None:
    targets = [("a", "envA"), ("b", "envA")]
    todo = p0._apply_resume(targets, already={"a", "b"}, forced={"a"})
    assert todo == [("a", "envA")]


def test_apply_resume_nothing_already_built_keeps_everything() -> None:
    targets = [("a", "envA"), ("b", "envB")]
    todo = p0._apply_resume(targets, already=set(), forced=set())
    assert todo == targets


def test_apply_resume_rejects_force_instances_outside_targets() -> None:
    targets = [("a", "envA")]
    try:
        p0._apply_resume(targets, already=set(), forced={"not-a-target"})
    except SystemExit:
        return
    raise AssertionError("expected SystemExit for an unmatched FORCE_INSTANCES id")


def test_apply_resume_only_always_rebuilds_even_if_already_pushed() -> None:
    """The specific regression this guards: --only predates Stage 3 and
    always meant 'rebuild whatever I named, right now' — e.g. `phase0-
    instances --only scikit-learn__scikit-learn-25102` to rebuild one
    instance after a framework fix, where the existing tag being STALE is
    the whole reason it was named. only=True must never let the resume-skip
    silently turn that into a no-op."""
    targets = [("a", "envA"), ("b", "envA")]
    todo = p0._apply_resume(targets, already={"a", "b"}, forced=set(), only=True)
    assert todo == targets


def test_apply_resume_only_false_still_skips_already_pushed() -> None:
    """The default (ENV_SHARD / full-run path) keeps resume-skip — only=True
    is exclusive to --only, not the general case."""
    targets = [("a", "envA"), ("b", "envA")]
    todo = p0._apply_resume(targets, already={"a"}, forced=set(), only=False)
    assert todo == [("b", "envA")]


# ---------------------------------------------------------------------------
# --base official (dev/IMAGE-PARITY-ROOT-CAUSE-AND-FIX-2026-09-05): name
# mapping, digest resolution, and the backup-before-overwrite promotion.
# ---------------------------------------------------------------------------


def test_official_image_name_rewrites_the_double_underscore() -> None:
    assert (
        p0.official_image_name("matplotlib__matplotlib-23314")
        == "swebench/sweb.eval.x86_64.matplotlib_1776_matplotlib-23314"
    )
    assert p0.official_image_name("django__django-10097") == (
        "swebench/sweb.eval.x86_64.django_1776_django-10097"
    )


from typing import Self


class _HubResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        import json

        return json.dumps(self._payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _hub(monkeypatch, payload: dict[str, object]) -> list[str]:
    import urllib.request

    urls: list[str] = []

    def _open(url: str, timeout: float = 0) -> _HubResponse:
        urls.append(url)
        return _HubResponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    return urls


def test_resolve_official_digest_reads_the_latest_tag(monkeypatch) -> None:
    urls = _hub(
        monkeypatch,
        {"digest": "sha256:" + "a" * 64, "images": [{"architecture": "amd64"}]},
    )
    assert p0.resolve_official_digest("django__django-10097") == "sha256:" + "a" * 64
    assert urls == [
        (
            "https://hub.docker.com/v2/repositories/swebench/"
            "sweb.eval.x86_64.django_1776_django-10097/tags/latest"
        )
    ]


def test_resolve_official_digest_refuses_a_non_amd64_or_digestless_tag(monkeypatch) -> None:
    import pytest

    _hub(monkeypatch, {"digest": "sha256:" + "b" * 64, "images": [{"architecture": "arm64"}]})
    with pytest.raises(RuntimeError, match="no amd64"):
        p0.resolve_official_digest("django__django-10097")
    _hub(monkeypatch, {"images": [{"architecture": "amd64"}]})
    with pytest.raises(RuntimeError, match="no manifest digest"):
        p0.resolve_official_digest("django__django-10097")


class _AlreadyExists(Exception):
    pass


class _StubECR:
    """Just enough of the ECR client for promote_inst_tag: tag -> (manifest, digest)."""

    def __init__(self, tags: dict[str, tuple[str, str]]) -> None:
        self.tags = dict(tags)
        self.exceptions = type("E", (), {"ImageAlreadyExistsException": _AlreadyExists})

    def batch_get_image(
        self, *, repositoryName: str, imageIds: list[dict[str, str]]
    ) -> dict[str, object]:
        tag = imageIds[0]["imageTag"]
        if tag not in self.tags:
            return {"images": [], "failures": [{"imageId": imageIds[0]}]}
        manifest, digest = self.tags[tag]
        return {
            "images": [
                {
                    "imageManifest": manifest,
                    "imageManifestMediaType": "application/vnd.docker.distribution.manifest.v2+json",
                    "imageId": {"imageTag": tag, "imageDigest": digest},
                }
            ]
        }

    def put_image(
        self,
        *,
        repositoryName: str,
        imageManifest: str,
        imageTag: str,
        imageManifestMediaType: str = "",
    ) -> None:
        digest = next(d for m, d in self.tags.values() if m == imageManifest)
        if self.tags.get(imageTag) == (imageManifest, digest):
            raise _AlreadyExists()
        self.tags[imageTag] = (imageManifest, digest)


def _with_ecr(monkeypatch, stub: _StubECR) -> None:
    import sys
    import types

    boto3_mod = types.ModuleType("boto3")
    boto3_mod.client = lambda *a, **k: stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3_mod)


def test_promote_backs_up_the_old_inst_before_moving_the_tag(monkeypatch) -> None:
    inst = f"{p0._VERSION}-django__django-10097-inst"
    stub = _StubECR({inst: ("OLD", "sha256:old"), inst + "-official": ("NEW", "sha256:new")})
    _with_ecr(monkeypatch, stub)
    out = p0.promote_inst_tag("django__django-10097", now=lambda: 1)
    assert stub.tags[inst] == ("NEW", "sha256:new")
    assert stub.tags[inst + "-prev"] == ("OLD", "sha256:old")
    assert out == {
        "instance": "django__django-10097",
        "new_digest": "sha256:new",
        "previous_digest": "sha256:old",
        "backup_tag": inst + "-prev",
    }


def test_promote_never_clobbers_an_existing_backup(monkeypatch) -> None:
    inst = f"{p0._VERSION}-django__django-10097-inst"
    stub = _StubECR(
        {
            inst: ("OLD2", "sha256:old2"),
            inst + "-prev": ("OLD1", "sha256:old1"),
            inst + "-official": ("NEW", "sha256:new"),
        }
    )
    _with_ecr(monkeypatch, stub)
    out = p0.promote_inst_tag("django__django-10097", now=lambda: 1234)
    assert stub.tags[inst + "-prev"] == ("OLD1", "sha256:old1")  # untouched
    assert stub.tags[inst + "-prev-1234"] == ("OLD2", "sha256:old2")
    assert stub.tags[inst] == ("NEW", "sha256:new")
    assert out["backup_tag"] == inst + "-prev-1234"


def test_promote_is_a_no_op_when_inst_already_points_at_official(monkeypatch) -> None:
    inst = f"{p0._VERSION}-django__django-10097-inst"
    stub = _StubECR({inst: ("NEW", "sha256:new"), inst + "-official": ("NEW", "sha256:new")})
    _with_ecr(monkeypatch, stub)
    out = p0.promote_inst_tag("django__django-10097")
    assert "backup_tag" not in out
    assert inst + "-prev" not in stub.tags


def test_promote_with_no_existing_inst_just_tags(monkeypatch) -> None:
    inst = f"{p0._VERSION}-django__django-10097-inst"
    stub = _StubECR({inst + "-official": ("NEW", "sha256:new")})
    _with_ecr(monkeypatch, stub)
    out = p0.promote_inst_tag("django__django-10097")
    assert stub.tags[inst] == ("NEW", "sha256:new")
    assert out["previous_digest"] == ""


def test_promote_refuses_when_official_is_missing(monkeypatch) -> None:
    import pytest

    _with_ecr(monkeypatch, _StubECR({}))
    with pytest.raises(RuntimeError, match="not in ECR"):
        p0.promote_inst_tag("django__django-10097")


def test_empty_only_is_refused_not_everything() -> None:
    """--only "" (a shell slip) must not silently become the full 500-build."""
    import argparse

    import pytest

    mirror = {"a__a-1": {"repo": "a/a"}, "b__b-2": {"repo": "b/b"}}
    with pytest.raises(SystemExit, match="names no instance ids"):
        p0._resolve_targets(argparse.Namespace(only=""), mirror)
    # and a real id list still resolves through the same path (ADR-0043: the
    # second element is the repo, the build's grouping unit)
    assert p0._resolve_targets(argparse.Namespace(only="a__a-1"), mirror) == [("a__a-1", "a/a")]


# ---------------------------------------------------------------------------
# ADR-0043: repo grouping, the snapshot-pinned base digest, the manifest record
# ---------------------------------------------------------------------------


def test_compute_repo_map_groups_by_repo_deterministically() -> None:
    mirror = {
        "b__b-2": {"repo": "b/b"},
        "a__a-1": {"repo": "a/a"},
        "a__a-3": {"repo": "a/a"},
        "z__z-9": {},  # no repo column -> the id's org prefix
    }
    assert p0._compute_repo_map(mirror) == {
        "a/a": ["a__a-1", "a__a-3"],
        "b/b": ["b__b-2"],
        "z": ["z__z-9"],
    }


def test_pinned_official_digest_comes_from_the_snapshot_not_the_hub(monkeypatch) -> None:
    import urllib.request

    import pytest

    from swebench_eval.dataset import swebench_loader as loader

    # Any Hub call is a contract violation: the build must never resolve :latest live.
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Hub!"))
    )
    pinned = "sha256:" + "e" * 64
    monkeypatch.setattr(
        loader, "load_image_digest_snapshot", lambda: {"django__django-10097": pinned}
    )
    assert p0._pinned_official_digest("django__django-10097") == pinned
    with pytest.raises(RuntimeError, match="not in the image-digest snapshot"):
        p0._pinned_official_digest("astropy__astropy-1")
    monkeypatch.setattr(loader, "load_image_digest_snapshot", lambda: None)
    with pytest.raises(RuntimeError, match="no image-digest snapshot"):
        p0._pinned_official_digest("django__django-10097")


def test_the_committed_snapshot_pins_every_verified_instance() -> None:
    """The real package-data snapshot resolves for the real pin: 500 digests."""
    from swebench_eval.dataset import swebench_loader as loader

    snap = loader.load_image_digest_snapshot()
    assert snap is not None and len(snap) == 500
    assert p0._pinned_official_digest("django__django-10097").startswith("sha256:")


def test_write_instance_record_carries_the_gate_fields(monkeypatch) -> None:
    from swebench_eval import cache_manifest as cm

    seen: dict[str, object] = {}

    def _fake(version: str, instance_id: str, record: dict[str, str], bucket: str) -> str:
        seen.update(version=version, instance_id=instance_id, record=record, bucket=bucket)
        return "k"

    monkeypatch.setattr(cm, "write_instance_record", _fake)
    prov = {"BASE_IMAGE_REF": "swebench/x:latest", "BASE_IMAGE_DIGEST": "sha256:" + "a" * 64}
    p0._write_instance_record("a__a-1", "sha256:" + "b" * 64, prov)
    rec = seen["record"]
    assert isinstance(rec, dict)
    assert rec["tag"] == f"{p0._VERSION}-a__a-1-inst"
    assert rec["digest"] == "sha256:" + "b" * 64
    assert rec["base_image_digest"] == "sha256:" + "a" * 64
    assert rec["base_image"] == "swebench/x:latest"
    assert seen["version"] == p0._VERSION
