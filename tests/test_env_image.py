"""Eval-side instance-image consumer (ADR-0043, SWE-bench 5.x).

The harness grades inside ``test_spec.image`` — the dataset row's MUTABLE
``swebench/…:latest`` reference — and pulls it from Docker Hub when it is not
local.  The consumer's job is to make sure that pull never happens on the eval
host: pull OUR ``-inst`` image from ECR by the digest ECR serves and tag it to
the exact name the harness looks up.  Tests drive it with a fake docker client;
the load-bearing behaviours are the digest-keyed idempotence (a moved tag is
re-pulled, never reused), the retag to the row's name, and the loud failures.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest import mock

import pytest

from swebench_eval.evaluation import env_image

# base64 of "AWS:secret" — derived at runtime so no high-entropy literal is
# committed (the pre-commit gitleaks hook flags it).  The login must DECODE it
# to the raw password before docker sees it (the 400 found live on the eval host).
_TOKEN = base64.b64encode(b"AWS:secret").decode()
_PASSWORD = "secret"
_REGISTRY = "123456789012.dkr.ecr.us-west-2.amazonaws.com"
_REPO = f"{_REGISTRY}/eval-dev-harness-worker"
_DIGEST = "sha256:" + "c" * 64
_STALE = "sha256:" + "d" * 64

_INSTANCE_ID = "astropy__astropy-12907"
# The row's image reference, exactly as SWE-bench/SWE-bench_Verified carries it.
_IMAGE = "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"


class _Spec:
    instance_id = _INSTANCE_ID
    image = _IMAGE


class _Image:
    def __init__(self, digest: str = _DIGEST) -> None:
        self.tag_calls: list[tuple[str, str]] = []
        self.attrs = {"RepoDigests": [f"{_REPO}@{digest}"]}

    def tag(self, name: str, tag: str) -> None:
        self.tag_calls.append((name, tag))


class _Images:
    """``get()`` returns the pulled image once pulled (so the retag can be seen),
    and raises LookupError when absent — the normal docker miss."""

    def __init__(self, present: bool = False) -> None:
        self.present = present
        self.local_digest = _DIGEST  # what the already-local image's RepoDigests say
        self.pull_calls: list[str] = []
        self._pulled: list[_Image] = []

    def get(self, name: str) -> object:
        if not self.present and not self._pulled:
            raise LookupError("no such image")
        if self._pulled:
            return self._pulled[-1]
        return _Image(self.local_digest)

    def pull(self, src: str) -> None:
        self.pull_calls.append(src)
        self._pulled.append(_Image())

    # disk hygiene (2026-09-06)
    remove_calls: list[str]
    listed: list[object]

    def remove(self, name: str, force: bool = False) -> None:
        if not hasattr(self, "remove_calls"):
            self.remove_calls = []
        if name == "missing":
            raise LookupError("no such image")
        self.remove_calls.append(name)

    def list(self) -> list[object]:
        return getattr(self, "listed", [])

    def prune(self) -> dict[str, object]:
        return {}


class _Containers:
    def list(self, all: bool = False) -> list[object]:
        return []


class _Client:
    def __init__(self, present: bool = False) -> None:
        self.images = _Images(present=present)
        self.containers = _Containers()
        self.login_calls: list[tuple[str, str, str]] = []

    def login(self, registry: str, username: str, password: str) -> None:
        self.login_calls.append((registry, username, password))


@pytest.fixture()
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> _Client:
    client = _Client()
    monkeypatch.setattr(env_image, "docker", type("D", (), {"from_env": lambda: client}))
    monkeypatch.setenv("ENV_IMAGE_REPOSITORY", _REPO)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.delenv("SWEBENCH_VERSION", raising=False)
    return client


def _aws(monkeypatch: pytest.MonkeyPatch, ecr_digest: str | None = _DIGEST) -> None:
    """A boto3 stub: ECR login token + the digest BatchGetImage reports for the -inst tag."""
    import sys

    boto3_mod = mock.Mock()
    boto3_mod.client.return_value.get_authorization_token.return_value = {
        "authorizationData": [{"authorizationToken": _TOKEN}]
    }
    boto3_mod.client.return_value.batch_get_image.return_value = {
        "images": [{"imageId": {"imageTag": "x", "imageDigest": ecr_digest}}] if ecr_digest else []
    }
    monkeypatch.setitem(sys.modules, "boto3", boto3_mod)


def test_parse_image_reference() -> None:
    """The row's image name maps back to the instance id (``_1776_`` -> ``__``)."""
    assert env_image._instance_id_from_image(_IMAGE) == _INSTANCE_ID
    assert (
        env_image._instance_id_from_image(
            "swebench/sweb.eval.x86_64.scikit-learn_1776_scikit-learn-25102:latest"
        )
        == "scikit-learn__scikit-learn-25102"
    )
    assert env_image._split_image_ref(_IMAGE) == (
        "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907",
        "latest",
    )
    assert env_image._split_image_ref(_IMAGE.rsplit(":", 1)[0]) == (
        "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907",
        "latest",
    )
    with pytest.raises(RuntimeError, match="cannot parse instance id"):
        env_image._instance_id_from_image("sweb.env.x86_64.abcdef1234567890")


def test_inst_tag_follows_the_harness_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0043: the -inst prefix is the harness version, 5.0.2 by default, so a
    4.1.0 image is never overwritten by or mistaken for a 5.x one."""
    monkeypatch.delenv("SWEBENCH_VERSION", raising=False)
    assert env_image.inst_tag(_INSTANCE_ID) == f"5.0.2-{_INSTANCE_ID}-inst"
    monkeypatch.setenv("SWEBENCH_VERSION", "5.1.0")
    assert env_image.inst_tag(_INSTANCE_ID) == f"5.1.0-{_INSTANCE_ID}-inst"


def test_pulls_ecr_tag_and_retags_to_the_rows_image_name(
    fake_docker: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh host: login (decoded password), pull <ver>-<id>-inst from ECR, tag it
    to the row's image name so create_container's images.get() hits and the
    mutable :latest is never pulled from Docker Hub."""
    _aws(monkeypatch)
    assert env_image.ensure_instance_image(_Spec()) == _DIGEST
    assert fake_docker.login_calls == [(_REGISTRY, "AWS", _PASSWORD)]
    assert _PASSWORD != _TOKEN
    assert fake_docker.images.pull_calls == [f"{_REPO}:5.0.2-{_INSTANCE_ID}-inst"]
    pulled = fake_docker.images._pulled[-1]
    assert pulled.tag_calls == [("swebench/sweb.eval.x86_64.astropy_1776_astropy-12907", "latest")]


def test_idempotent_when_local_at_the_current_digest(
    fake_docker: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Already local AND at the digest ECR serves now → no login, no pull."""
    _aws(monkeypatch)
    fake_docker.images.present = True
    assert env_image.ensure_instance_image(_Spec()) == _DIGEST
    assert fake_docker.login_calls == []
    assert fake_docker.images.pull_calls == []


def test_repulls_when_the_ecr_tag_moved(
    fake_docker: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A promotion moved the -inst tag: the local copy is stale → re-pull + re-tag,
    never grade in the stale image."""
    _aws(monkeypatch, ecr_digest=_DIGEST)
    fake_docker.images.present = True
    fake_docker.images.local_digest = _STALE
    assert env_image.ensure_instance_image(_Spec()) == _DIGEST
    assert fake_docker.images.pull_calls == [f"{_REPO}:5.0.2-{_INSTANCE_ID}-inst"]


def test_reuses_local_when_ecr_cannot_be_asked(
    fake_docker: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registry lookup failing must not fail a grade with a good cached image:
    reuse the local one, return None (digest unknown, never fabricated)."""
    _aws(monkeypatch, ecr_digest=None)
    fake_docker.images.present = True
    assert env_image.ensure_instance_image(_Spec()) is None
    assert fake_docker.images.pull_calls == []


def test_missing_repository_raises(fake_docker: _Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENV_IMAGE_REPOSITORY")
    with pytest.raises(RuntimeError, match="ENV_IMAGE_REPOSITORY"):
        env_image.ensure_instance_image(_Spec())


# --- disk hygiene (2026-09-06, the 500-instance gold gate) ---------------------


def test_release_removes_both_local_tags(fake_docker: _Client) -> None:
    """After a grade the harness-name tag AND the ECR -inst tag are removed."""
    assert env_image.release_instance_image(_Spec()) == 2
    assert fake_docker.images.remove_calls == [_IMAGE, f"{_REPO}:5.0.2-{_INSTANCE_ID}-inst"]


def test_release_never_raises(fake_docker: _Client) -> None:
    class _Missing:
        image = "missing"

    assert env_image.release_instance_image(_Missing()) == 0  # unparsable + absent: logged only


def test_low_disk_evicts_unused_sweb_images_but_keeps_the_one_being_pulled(
    fake_docker: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Img:
        def __init__(self, id_: str, tags: list[str]) -> None:
            self.id, self.tags = id_, tags

    keep = f"{_REPO}:5.0.2-{_INSTANCE_ID}-inst"
    fake_docker.images.listed = [
        _Img("a", ["swebench/sweb.eval.x86_64.django_1776_django-11099:latest"]),
        _Img("b", [keep]),
        _Img("c", ["eval-dev-warm-job:latest"]),  # not an instance image: untouched
        _Img("d", []),  # dangling: prune()'s job, not remove()
    ]
    monkeypatch.setattr(env_image, "_free_disk_mb", lambda path="/": 1000)
    env_image._ensure_free_disk(fake_docker, keep=(_IMAGE, keep))
    assert fake_docker.images.remove_calls == ["a"]


def test_enough_disk_evicts_nothing(fake_docker: _Client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_docker.images.listed = [type("I", (), {"id": "a", "tags": ["sweb.eval.x"]})()]
    monkeypatch.setattr(env_image, "_free_disk_mb", lambda path="/": 10**6)
    env_image._ensure_free_disk(fake_docker)
    assert getattr(fake_docker.images, "remove_calls", []) == []


def test_pull_path_checks_disk_first(fake_docker: _Client, monkeypatch: pytest.MonkeyPatch) -> None:
    _aws(monkeypatch)
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(env_image, "_ensure_free_disk", lambda client, keep=(): seen.append(keep))
    env_image.ensure_instance_image(_Spec())
    assert seen == [(_IMAGE, f"{_REPO}:5.0.2-{_INSTANCE_ID}-inst")]


def test_env_image_path_is_gone() -> None:
    """ADR-0043: no env images, no local instance-image build, no git-redirect layer."""
    for name in ("ensure_env_image", "_build_local_layer", "_env_hash_from_key"):
        assert not hasattr(env_image, name), f"{name} is a 4.1.0-era path and must not exist"
    _: Any = env_image.ensure_instance_image  # the one entry point that remains
