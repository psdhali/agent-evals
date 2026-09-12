"""Warm-cache manifest — completeness and the gate's refusal shape (5b).

The precondition gate refuses dispatch when the cache is incomplete, naming
what is missing.  These tests pin the two pieces of that contract that can
silently rot: an incomplete manifest must NOT look complete, and the S3 round-
trip must round-trip a real manifest (loads it back as the same object).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from unittest import mock

import pytest

from swebench_eval.cache_manifest import (
    CacheManifest,
    key_for,
    load_manifest,
    write_manifest,
)


def _complete() -> CacheManifest:
    env_expected = ["4.1.0-0", "4.1.0-1"]
    return CacheManifest(
        swebench_version="4.1.0",
        env_images=list(env_expected),
        env_images_expected=env_expected,
        harness_images=[f"{t}-hw" for t in env_expected],
        harness_images_expected=[f"{t}-hw" for t in env_expected],
        mirrors_ok=["django/django", "astropy/astropy"],
        mirrors_expected=["django/django", "astropy/astropy"],
    )


def test_complete_manifest_is_complete() -> None:
    m = _complete()
    assert m.is_complete()
    # A complete manifest names NOTHING missing. (The git-mirror half is gone
    # from completeness — review S1 — so there is no mirrors_missing key.)
    assert m.missing_summary()["env_images_missing"] == []
    assert m.missing_summary()["harness_images_missing"] == []


def test_incomplete_manifest_is_not_complete() -> None:
    m = _complete()
    m.env_images = m.env_images[:1]  # 1 of 2 env images
    assert not m.is_complete()
    summary = m.missing_summary()
    # The env half now NAMES what is missing (review find #14) instead of a bare count.
    assert summary["env_images_missing"] == ["4.1.0-1"]


def test_mirrors_do_not_gate_completeness() -> None:
    """Review S1: the git-mirror half must NOT block the gate. The 12 repos are
    baked into the git-mirror image, so mirror readiness is not something the
    manifest can assert — an empty mirrors_ok must not make the cache
    'incomplete'."""
    m = _complete()
    m.mirrors_ok = []  # the live EFS mirror tree is gone
    assert m.is_complete()


@pytest.mark.parametrize(
    ("present_field", "expected_field", "missing_key", "stale"),
    [
        ("env_images", "env_images_expected", "env_images_missing", "4.1.0-STALE"),
        (
            "harness_images",
            "harness_images_expected",
            "harness_images_missing",
            "4.1.0-STALE-hw",
        ),
    ],
)
def test_wrong_image_with_right_count_is_not_complete(
    present_field: str, expected_field: str, missing_key: str, stale: str
) -> None:
    """The gate's load-bearing case: the RIGHT COUNT of the WRONG identities.

    This exists because fixing review find #14 (count -> set identity) is
    UNDEFENDED: every other test varies count and identity together, so a
    regression back to ``len(...)`` leaves the whole suite green. Only a same-
    count/different-identity case shows that "61 of 61 present" is not the same
    as "the right 61". The gate becomes load-bearing from Stage 4 onward.
    """
    m = _complete()
    expected = list(getattr(m, expected_field))
    # present = first correct, rest a stale imposter — count unchanged.
    setattr(m, present_field, [expected[0]] + [stale] * (len(expected) - 1))
    assert not m.is_complete()
    summary = m.missing_summary()
    # the real missing tag is named — never the stale imposter.
    assert summary[missing_key] == [expected[1]]


def test_manifest_round_trips_via_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write then load → identical object (the gate reads what we wrote)."""
    store: dict[str, bytes] = {}

    def fake_put(Bucket: str = "", Key: str = "", Body: bytes = b"", **kw: object) -> None:
        store[Key] = bytes(Body)

    def fake_get(Bucket: str = "", Key: str = "", **kw: object) -> dict[str, object]:
        if Key not in store:
            raise KeyError("NoSuchKey")
        return {"Body": mock.Mock(read=lambda: store[Key])}

    client = mock.Mock()
    client.put_object = mock.Mock(side_effect=fake_put)
    client.get_object = mock.Mock(side_effect=fake_get)
    monkeypatch.setenv("DATASET_BUCKET", "test-bucket")

    import swebench_eval.cache_manifest as cm

    with mock.patch.object(cm, "get_s3_client", return_value=client):
        key = write_manifest(_complete())
        loaded = load_manifest("4.1.0")

    assert key == key_for("4.1.0")
    assert loaded is not None
    assert loaded == _complete()
    assert json.loads(store[key])["swebench_version"] == "4.1.0"


def test_unseeded_manifest_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    client = mock.Mock()
    client.get_object = mock.Mock(side_effect=KeyError("NoSuchKey"))
    import swebench_eval.cache_manifest as cm

    with mock.patch.object(cm, "get_s3_client", return_value=client):
        assert load_manifest("4.1.0") is None


def test_old_int_expected_schema_is_tolerated() -> None:
    """The first published manifest had int counts, not lists (review find #14).

    A count cannot recover the tag list, so from_payload drops it (unknown).
    With no mirrors half left to block (review S1), an int-schema manifest is
    "complete" VACUOUSLY for env/harness — which is exactly why the warm job
    must be re-run with --manifest (list schema) before dispatch: H1's
    per-instance check compares against env_images_expected, and an empty one
    refuses every instance. Must not raise.
    """
    old = {
        "swebench_version": "4.1.0",
        "env_images": ["4.1.0-a"],
        "env_images_expected": 2,
        "harness_images": ["4.1.0-a-hw"],
        "harness_images_expected": 2,
        "mirrors_ok": [],
        "mirrors_expected": ["astropy/astropy"],
    }
    m = CacheManifest.from_payload(old)
    assert m.env_images_expected == []
    assert m.harness_images_expected == []
    # Unknown expected sets -> nothing to demand -> vacuously complete.
    assert m.is_complete()


# ---------------------------------------------------------------------------
# ADR-0043: per-instance records (the build tier writes one per promotion; the
# warm job merges them) and the instance_images half of the manifest.
# ---------------------------------------------------------------------------


def _s3_stub(objects: dict[str, str]) -> mock.Mock:
    """A boto3-shaped S3 stub over an in-memory {key: body} store."""
    client = mock.Mock()

    def put(Bucket: str, Key: str, Body: bytes) -> None:
        objects[Key] = Body.decode("utf-8")

    def get(Bucket: str, Key: str) -> dict[str, object]:
        if Key not in objects:
            raise KeyError(Key)
        return {"Body": mock.Mock(read=lambda: objects[Key].encode("utf-8"))}

    def list_objects_v2(Bucket: str, Prefix: str, **kw: object) -> dict[str, object]:
        keys = sorted(k for k in objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    client.put_object = mock.Mock(side_effect=put)
    client.get_object = mock.Mock(side_effect=get)
    client.list_objects_v2 = mock.Mock(side_effect=list_objects_v2)
    return client


def test_instance_record_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    from swebench_eval import cache_manifest as cm

    store: dict[str, str] = {}
    monkeypatch.setattr(cm, "get_s3_client", lambda: _s3_stub(store))
    rec = {
        "tag": "5.0.2-a__a-1-inst",
        "digest": "sha256:" + "b" * 64,
        "base_image_digest": "sha256:" + "e" * 64,
        "built_at": "t",
    }
    key = cm.write_instance_record("5.0.2", "a__a-1", rec, bucket="b")
    assert key == "cache-manifest/5.0.2/instances/a__a-1.json"
    loaded = cm.load_instance_records("5.0.2", bucket="b")
    assert loaded == {"a__a-1": rec}
    # a record under another version's prefix is not visible
    assert cm.load_instance_records("4.1.0", bucket="b") == {}


def test_instance_record_requires_the_gate_fields() -> None:
    from swebench_eval import cache_manifest as cm

    with pytest.raises(ValueError, match="base_image_digest"):
        cm.write_instance_record("5.0.2", "a__a-1", {"tag": "t", "digest": "d"}, bucket="b")


def test_manifest_instance_images_round_trip_and_old_payload_tolerated() -> None:
    m = CacheManifest(
        swebench_version="5.0.2",
        instance_images={
            "a__a-1": {"tag": "5.0.2-a__a-1-inst", "digest": "d", "base_image_digest": "e"}
        },
    )
    payload = json.loads(json.dumps(asdict(m)))
    assert CacheManifest.from_payload(payload) == m
    # an unknown future key is ignored rather than crashing the parse
    payload["some_future_field"] = 1
    assert CacheManifest.from_payload(payload) == m
