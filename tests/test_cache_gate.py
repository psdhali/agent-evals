"""Warm-cache precondition gate on the dispatch path (5b, #71; ADR-0031; ADR-0043).

ADR-0031: admission is PER-INSTANCE, not whole-run completeness.  ADR-0043
re-keys it: the admission list is the manifest's ``instance_images`` (the
``-inst`` images the build tier pushed), and each entry's recorded
``base_image_digest`` must equal the committed image-digest snapshot's digest
for that instance — an image built from a moved ``:latest`` is refused by
name.  ``check_cache_precondition`` still refuses when the warm job never
published a manifest, and when it published one with nothing built.  OFF by
default so local dev keeps working without a cache; ON under
``ENFORCE_CACHE_GATE=1`` (the AWS control-plane task env).
"""

from __future__ import annotations

from unittest import mock

import pytest

from swebench_eval.cache_manifest import CacheManifest
from swebench_eval.dataset.base import Instance
from swebench_eval.orchestrator.control_plane import dispatcher
from swebench_eval.orchestrator.run_config import RunConfig

_INSTANCE_ID = "astropy__astropy-12907"
_OTHER_ID = "django__django-11099"
_PINNED = "sha256:" + "a" * 64
_MOVED = "sha256:" + "b" * 64
_INST_DIGEST = "sha256:" + "c" * 64


def _manifest(base_digest: str = _PINNED) -> CacheManifest:
    return CacheManifest(
        swebench_version="5.0.2",
        instance_images={
            _INSTANCE_ID: {
                "tag": f"5.0.2-{_INSTANCE_ID}-inst",
                "digest": _INST_DIGEST,
                "base_image_digest": base_digest,
            }
        },
    )


def _snapshot() -> dict[str, str]:
    return {_INSTANCE_ID: _PINNED, _OTHER_ID: _PINNED}


def _instance(instance_id: str = _INSTANCE_ID) -> Instance:
    return Instance(
        instance_id=instance_id,
        repo="astropy/astropy",
        base_commit="c",
        problem_statement="p",
    )


def _dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest: CacheManifest,
    snapshot: dict[str, str] | None,
    instance_id: str = _INSTANCE_ID,
) -> list[dict[str, object]]:
    """Run dispatch_run with the gate on; returns the sent message bodies."""
    monkeypatch.setenv("ENFORCE_CACHE_GATE", "1")
    monkeypatch.setattr(dispatcher, "load_manifest", lambda version: manifest)
    monkeypatch.setattr(
        "swebench_eval.dataset.swebench_loader.load_image_digest_snapshot", lambda: snapshot
    )
    monkeypatch.setattr(dispatcher, "register_run", lambda *a, **k: None)
    # The dispatcher always ends dispatch_run by seeding run_summary.expected
    # (M1.8), which needs Postgres — this test targets the cache gate, not the
    # summary write, and CI has no Postgres.
    monkeypatch.setattr(dispatcher, "_seed_expected", lambda *a, **k: None)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(dispatcher, "send_message", lambda queue, body: sent.append(dict(body)))
    dispatcher.dispatch_run("gated-run", [_instance(instance_id)], RunConfig())
    return sent


# ---------------------------------------------------------------------------
# check_cache_precondition — what still refuses
# ---------------------------------------------------------------------------


def test_gate_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local dev has no cache and must not be gated: load_manifest is never hit."""
    monkeypatch.delenv("ENFORCE_CACHE_GATE", raising=False)
    with mock.patch.object(dispatcher, "load_manifest") as loader:
        dispatcher.check_cache_precondition("5.0.2")
    loader.assert_not_called()


def test_gate_refuses_when_cache_never_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """No manifest at all is still a real precondition — the build never ran."""
    monkeypatch.setenv("ENFORCE_CACHE_GATE", "1")
    with (
        mock.patch.object(dispatcher, "load_manifest", return_value=None),
        pytest.raises(dispatcher.CachePreconditionError, match="not built"),
    ):
        dispatcher.check_cache_precondition("5.0.2")


def test_gate_refuses_manifest_with_nothing_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """A manifest listing no instance images is the one thing the per-instance
    check cannot discover on its own — refuse it here, loudly."""
    monkeypatch.setenv("ENFORCE_CACHE_GATE", "1")
    empty = CacheManifest(swebench_version="5.0.2")
    with (
        mock.patch.object(dispatcher, "load_manifest", return_value=empty),
        pytest.raises(dispatcher.CachePreconditionError, match="no instance images"),
    ):
        dispatcher.check_cache_precondition("5.0.2")


def test_gate_passes_partial_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0031: whole-run completeness never gates; one built instance passes."""
    monkeypatch.setenv("ENFORCE_CACHE_GATE", "1")
    with mock.patch.object(dispatcher, "load_manifest", return_value=_manifest()) as loader:
        assert dispatcher.check_cache_precondition("5.0.2") is loader.return_value


def test_old_manifest_without_instance_images_still_parses() -> None:
    """A pre-ADR-0043 payload (env/-hw halves only) loads; instance_images is empty."""
    m = CacheManifest.from_payload(
        {
            "swebench_version": "4.1.0",
            "env_images": ["4.1.0-abc"],
            "env_images_expected": 3,  # the oldest int schema
            "harness_images": [],
            "harness_images_expected": [],
        }
    )
    assert m.instance_images == {}
    assert m.env_images_expected == []


def test_dispatch_run_gates_when_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: with the gate on and no manifest, dispatch_run refuses
    before enqueuing anything (send_message never called)."""
    monkeypatch.setenv("ENFORCE_CACHE_GATE", "1")
    with (
        mock.patch.object(dispatcher, "load_manifest", return_value=None),
        mock.patch.object(dispatcher, "send_message") as send,
        mock.patch.object(dispatcher, "register_run"),
        pytest.raises(dispatcher.CachePreconditionError),
    ):
        dispatcher.dispatch_run("gated-run", [_instance()], RunConfig())
    send.assert_not_called()


# ---------------------------------------------------------------------------
# per-instance admission (ADR-0031 + ADR-0043) — present AND built from the pin
# ---------------------------------------------------------------------------


def test_built_from_the_pinned_digest_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _dispatch(monkeypatch, manifest=_manifest(), snapshot=_snapshot())
    assert len(sent) == 1
    assert sent[0]["instance_id"] == _INSTANCE_ID
    # ADR-0043: no env images — the family is selected from the instance id.
    assert sent[0]["env_image_key"] == ""


def test_instance_without_an_image_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The instance is in the snapshot but no -inst was built → refused, naming it."""
    with pytest.raises(dispatcher.CachePreconditionError, match=_OTHER_ID):
        _dispatch(monkeypatch, manifest=_manifest(), snapshot=_snapshot(), instance_id=_OTHER_ID)


def test_image_built_from_a_moved_tag_is_refused_naming_both_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mutation-check case: the -inst exists but was built FROM a different
    base digest than the committed snapshot pins (``:latest`` moved between the
    snapshot and the build).  Admitting on presence alone would grade in an
    environment the pin does not describe."""
    with pytest.raises(dispatcher.CachePreconditionError) as exc:
        _dispatch(monkeypatch, manifest=_manifest(base_digest=_MOVED), snapshot=_snapshot())
    msg = str(exc.value)
    assert _MOVED in msg and _PINNED in msg and _INSTANCE_ID in msg


def test_unknown_base_digest_is_admitted_on_presence_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No snapshot for this pin (local/unpinned) or an image that recorded no base
    digest cannot be checked: admit on presence and say so — unknown is never
    rendered as a mismatch, nor silently as a match."""
    import logging

    caplog.set_level(logging.WARNING)
    sent = _dispatch(monkeypatch, manifest=_manifest(base_digest=""), snapshot=_snapshot())
    assert len(sent) == 1
    assert any("could not check the base digest" in r.message for r in caplog.records)

    caplog.clear()
    sent = _dispatch(monkeypatch, manifest=_manifest(), snapshot=None)
    assert len(sent) == 1
    assert any("could not check the base digest" in r.message for r in caplog.records)


def test_admission_helper_returns_the_entry() -> None:
    entry = dispatcher.check_instance_image_admission(_INSTANCE_ID, _manifest(), _snapshot())
    assert entry["digest"] == _INST_DIGEST
