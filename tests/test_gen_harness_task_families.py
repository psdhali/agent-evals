"""ADR-0030 H2 + ADR-0043 — one task-definition family per INSTANCE image, DERIVED from the manifest.

The load-bearing property: the family set is generated from the warm-cache
manifest's ``instance_images``, not hand-listed in Terraform. Two-sided —
delete one instance image from the manifest and the corresponding family is
not rendered; restore it and it is. A hand-maintained list passes neither half.
"""

from __future__ import annotations

import json

from scripts.gen_harness_task_families import _FAMILY_PREFIX, harness_task_families
from swebench_eval.cache_manifest import CacheManifest

_REPO = "123456789012.dkr.ecr.us-west-2.amazonaws.com/eval-dev-harness-worker"
_ASTROPY_ID = "astropy__astropy-12907"
_DJANGO_ID = "django__django-10914"
_SKLEARN_ID = "scikit-learn__scikit-learn-25102"


def _manifest(instance_ids: list[str]) -> CacheManifest:
    return CacheManifest(
        swebench_version="5.0.2",
        instance_images={
            iid: {
                "tag": f"5.0.2-{iid}-inst",
                "digest": "sha256:" + "c" * 64,
                "base_image_digest": "sha256:" + "e" * 64,
            }
            for iid in instance_ids
        },
    )


def test_family_per_instance_image_points_at_its_inst_tag() -> None:
    families = harness_task_families(_manifest([_ASTROPY_ID, _DJANGO_ID]), _REPO)
    assert set(families) == {f"{_FAMILY_PREFIX}{_ASTROPY_ID}", f"{_FAMILY_PREFIX}{_DJANGO_ID}"}
    assert families[f"{_FAMILY_PREFIX}{_ASTROPY_ID}"] == {
        "image": f"{_REPO}:5.0.2-{_ASTROPY_ID}-inst"
    }


def test_delete_instance_image_removes_family_restore_returns() -> None:
    """The two-sided property: mutate the manifest, the family set follows."""
    both = harness_task_families(_manifest([_ASTROPY_ID, _DJANGO_ID]), _REPO)
    assert f"{_FAMILY_PREFIX}{_ASTROPY_ID}" in both

    reduced = harness_task_families(_manifest([_DJANGO_ID]), _REPO)
    assert f"{_FAMILY_PREFIX}{_ASTROPY_ID}" not in reduced
    assert f"{_FAMILY_PREFIX}{_DJANGO_ID}" in reduced

    restored = harness_task_families(_manifest([_ASTROPY_ID, _DJANGO_ID]), _REPO)
    assert f"{_FAMILY_PREFIX}{_ASTROPY_ID}" in restored


def test_empty_manifest_produces_no_families() -> None:
    assert harness_task_families(_manifest([]), _REPO) == {}


def test_family_name_is_byte_exact_not_sanitised() -> None:
    """The dispatcher probes f"{_FAMILY_PREFIX}{job.instance_id}" verbatim —
    no transformation of the double underscore."""
    families = harness_task_families(_manifest([_SKLEARN_ID]), _REPO)
    assert set(families) == {f"eval-dev-harness-{_SKLEARN_ID}"}
    assert "__" in next(iter(families))


def test_no_env_hash_families_are_ever_rendered() -> None:
    """ADR-0043: a manifest still carrying 4.1.0 env/-hw halves yields NO env-hash
    families — only instance images make families now."""
    m = _manifest([_ASTROPY_ID])
    m.env_images = ["4.1.0-428468730904ff6b4232aa"]
    m.env_images_expected = ["4.1.0-428468730904ff6b4232aa"]
    m.harness_images_expected = ["4.1.0-428468730904ff6b4232aa-hw"]
    families = harness_task_families(m, _REPO)
    assert set(families) == {f"{_FAMILY_PREFIX}{_ASTROPY_ID}"}


def test_inst_instance_ids_only_subtracts() -> None:
    """A hand list can narrow the derived set but never add an unknown image."""
    families = harness_task_families(
        _manifest([_ASTROPY_ID, _DJANGO_ID]), _REPO, inst_instance_ids=[_ASTROPY_ID, "x__x-1"]
    )
    assert set(families) == {f"{_FAMILY_PREFIX}{_ASTROPY_ID}"}


def test_cli_writes_auto_tfvars_json(tmp_path, monkeypatch) -> None:
    """The generator emits the exact auto-loaded tfvars shape the eval env reads."""
    import scripts.gen_harness_task_families as gen

    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(
        json.dumps(
            {
                "swebench_version": "5.0.2",
                "instance_images": {
                    _ASTROPY_ID: {
                        "tag": f"5.0.2-{_ASTROPY_ID}-inst",
                        "digest": "sha256:" + "c" * 64,
                        "base_image_digest": "sha256:" + "e" * 64,
                    }
                },
            }
        )
    )
    out = tmp_path / "harness-task-families.auto.tfvars.json"
    assert gen.main(["--manifest-file", str(manifest_file), "--output", str(out)]) == 0

    payload = json.loads(out.read_text())
    assert payload == {
        "harness_task_families": {
            f"{_FAMILY_PREFIX}{_ASTROPY_ID}": {"image": f"{_REPO}:5.0.2-{_ASTROPY_ID}-inst"}
        }
    }


def test_cli_refuses_a_manifest_without_instance_images(tmp_path) -> None:
    import pytest

    import scripts.gen_harness_task_families as gen

    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps({"swebench_version": "4.1.0", "env_images": ["4.1.0-abc"]}))
    with pytest.raises(SystemExit, match="no instance_images"):
        gen.main(["--manifest-file", str(manifest_file), "--print"])
