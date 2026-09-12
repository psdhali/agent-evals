"""5b env-path selection for SWE-bench grading images (evaluation §8).

The eval host must build instance images from the local env image (namespace
None) in 5b — that is the full-split, zero-Docker-Hub-pulls path.  A typo'd
env value must not silently fall back to the pull path (or crash): the mapping
is pinned here.
"""

from __future__ import annotations

import pytest

from swebench_eval.workers.eval_worker import _eval_image_namespace


def test_default_is_legacy_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_IMAGE_NAMESPACE", raising=False)
    # Unset: historical "swebench" pull (local/dev Docker runs).
    assert _eval_image_namespace() == "swebench"


def test_empty_string_means_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_IMAGE_NAMESPACE", "")
    assert _eval_image_namespace() is None


def test_build_and_none_spell_it_out(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("build", "none"):
        monkeypatch.setenv("EVAL_IMAGE_NAMESPACE", value)
        assert _eval_image_namespace() is None


def test_unknown_value_is_an_explicit_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    # A registry namespace is honored verbatim, never silently coerced.
    monkeypatch.setenv("EVAL_IMAGE_NAMESPACE", "my-registry/team")
    assert _eval_image_namespace() == "my-registry/team"
