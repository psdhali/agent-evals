"""Eval-side instance-image consumer (ADR-0043, SWE-bench 5.x).

The official harness grades inside ``test_spec.image`` — the dataset row's
image reference, e.g. ``swebench/sweb.eval.x86_64.django_1776_django-10097:latest``
— and ``create_container`` pulls that name from Docker Hub when it is not
local.  ``:latest`` is a MUTABLE tag and the eval hosts must never pull it:
what they grade in is OUR per-instance ``-inst`` image in ECR (built FROM the
digest the committed snapshot pins, plus the framework layers), tagged locally
to the exact name the harness looks up.  Before ``run_instance``:

  1. docker login to ECR                       (task role already permits it)
  2. docker pull <ecr>/eval-dev-harness-worker:<swebench-ver>-<instance_id>-inst
  3. docker tag it ``<test_spec.image>``       <- the name the harness expects

Idempotent across grades of the same instance, but keyed on the DIGEST ECR
serves: a local copy at a stale digest (the tag moved — a promotion) is
re-pulled, never reused.  There is no env-image half any more: 5.x has no env
images, no env hashes and no local instance-image build (the 4.1.0-era
``ensure_env_image`` and its git-redirect layer are gone with them).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from swebench_eval import aws_names

try:  # the docker SDK is a transitive dep of the official swebench package
    import docker
except ImportError:  # pragma: no cover - import guard mirrors the runner's
    docker = None

logger = logging.getLogger(__name__)

# ADR-0043: the -inst tag prefix follows the harness version so a 4.1.0 image
# is never overwritten by (or mistaken for) a 5.x one.
DEFAULT_SWEBENCH_VERSION = "5.0.2"

# The row's image reference: ``[<registry>/]<ns>/sweb.eval.<arch>.<owner>_1776_<repo>-<n>[:<tag>]``.
# SWE-bench rewrites the instance id's ``__`` to ``_1776_`` for the image name;
# instance ids themselves carry no dots, and the trailing ``-<n>`` is the PR number.
_IMAGE_REF_RE = re.compile(
    r"^(?P<repo>(?:[^/]+/)*sweb\.eval\.[^./]+\.(?P<name>[A-Za-z0-9_.-]+?-[0-9]+))(?::(?P<tag>[\w.-]+))?$"
)


def _instance_id_from_image(image: str) -> str:
    """``swebench/sweb.eval.x86_64.django_1776_django-10097:latest`` -> ``django__django-10097``."""
    m = _IMAGE_REF_RE.match(image)
    if not m:
        raise RuntimeError(f"cannot parse instance id from image reference {image!r}")
    return m.group("name").replace("_1776_", "__")


def _split_image_ref(image: str) -> tuple[str, str]:
    """``<repo>:<tag>`` -> (repo, tag); a reference without a tag means ``latest``."""
    m = _IMAGE_REF_RE.match(image)
    if not m:
        raise RuntimeError(f"cannot parse image reference {image!r}")
    return m.group("repo"), m.group("tag") or "latest"


def _image_present(client: Any, name: str) -> bool:
    try:
        client.images.get(name)
        return True
    except Exception:  # noqa: BLE001 - "no such image" is the normal miss
        return False


def _ecr_login(client: Any, registry: str, region: str) -> None:
    import base64

    import boto3

    # ECR's authorizationToken is base64("AWS:<password>") — docker needs the
    # DECODED password, not the token. Passing the token verbatim makes the
    # registry reject the login with 400 (found live on the eval host: the
    # env-image pull failed exactly so). Mirrors warm_image_cache._ecr_login.
    auth = boto3.client("ecr", region_name=region).get_authorization_token()
    data = auth["authorizationData"][0]
    user, _, password = base64.b64decode(data["authorizationToken"]).decode().partition(":")
    client.login(registry=registry, username=user, password=password)
    logger.info("logged into ECR registry %s", registry)


def _ecr_tag_digest(region: str, ecr_repo: str, tag: str) -> str | None:
    """The manifest digest ECR currently serves for ``<ecr_repo>:<tag>``.

    Uses ``BatchGetImage`` — the ONE read the eval task role already has for
    pulling (no ``DescribeImages`` grant needed).  ``None`` when the tag is
    absent or the lookup fails: the caller then falls back to whatever is
    local, logged, rather than refusing to grade (a registry hiccup must not
    fail a grade that has a perfectly good image cached).
    """
    import boto3

    repo_name = ecr_repo.split("/", 1)[1] if "/" in ecr_repo else ecr_repo
    try:
        resp = boto3.client("ecr", region_name=region).batch_get_image(
            repositoryName=repo_name, imageIds=[{"imageTag": tag}]
        )
    except Exception as exc:  # noqa: BLE001 - registry lookup is best-effort
        logger.warning("could not read ECR digest for %s:%s: %s", repo_name, tag, exc)
        return None
    images = resp.get("images") or []
    if not images:
        return None
    return str(images[0]["imageId"]["imageDigest"])


def _local_repo_digests(client: Any, name: str) -> list[str]:
    """``RepoDigests`` of the local image called ``name`` (``[]`` if absent)."""
    try:
        attrs = client.images.get(name).attrs
    except Exception:  # noqa: BLE001 - "no such image" is the normal miss
        return []
    return [str(d) for d in (attrs or {}).get("RepoDigests") or []]


def inst_tag(instance_id: str, version: str | None = None) -> str:
    """The ECR tag of the per-instance image: ``<swebench-version>-<instance_id>-inst``."""
    version = version or os.environ.get("SWEBENCH_VERSION", DEFAULT_SWEBENCH_VERSION)
    return f"{version}-{instance_id}-inst"


def ensure_instance_image(test_spec: Any) -> str | None:
    """Pre-pull + retag the ECR per-instance image so ``run_instance`` reuses it.

    Idempotent across grades of the same instance, BUT keyed on the digest ECR
    serves, not on the local name: when ``test_spec.image`` is already local,
    its ``RepoDigests`` are compared with what the ECR ``-inst`` tag points at
    NOW, and a mismatch re-pulls and re-tags.  Without this, moving the
    ``-inst`` tag (a promotion) would leave a reused host grading in the stale
    image — the ECR ``:latest`` staleness class of bug, on the grading side.
    Returns the digest actually in use (``None`` when the registry could not
    be asked), so the grade can log it.
    """
    if docker is None:
        raise RuntimeError("docker SDK unavailable; cannot consume instance images")
    image = str(test_spec.image)
    local_repo, local_tag = _split_image_ref(image)
    instance_id = _instance_id_from_image(image)
    client = docker.from_env()

    ecr_repo = os.environ.get("ENV_IMAGE_REPOSITORY", "")
    if not ecr_repo:
        raise RuntimeError(
            "ENV_IMAGE_REPOSITORY is not set (the ECR repo, e.g. "
            "<acct>.dkr.ecr.<region>.amazonaws.com/<prefix>-harness-worker)"
        )
    region = aws_names.region()
    tag = inst_tag(instance_id)
    src = f"{ecr_repo}:{tag}"
    registry = ecr_repo.split("/", 1)[0]

    current = _ecr_tag_digest(region, ecr_repo, tag)
    if _image_present(client, image):
        local = _local_repo_digests(client, image)
        if current is None:
            logger.warning(
                "instance image %s already local; ECR digest unavailable — reusing (local digests: %s)",
                image,
                local,
            )
            return None
        if any(d.endswith("@" + current) for d in local):
            logger.info("instance image %s already local at %s; reusing", image, current)
            return current
        logger.warning(
            "instance image %s is STALE locally (%s) vs ECR %s -> %s; re-pulling",
            image,
            local,
            tag,
            current,
        )

    _ensure_free_disk(client, keep=(image, src))
    _ecr_login(client, registry, region)
    logger.info("pulling instance image %s", src)
    client.images.pull(src)

    # Tag as the exact name the harness looks up (test_spec.image), so
    # create_container's images.get() hits and it never pulls the mutable tag.
    img = client.images.get(src)
    img.tag(local_repo, tag=local_tag)
    logger.info("tagged %s <- ECR instance image %s (%s)", image, src, current)
    return current


# --- Disk hygiene (2026-09-06, the 500-instance gold gate) --------------------
# Every -inst image is ~4 GB and a 500-instance run grades ~40 of them per eval
# host (c5d.2xlarge, 200 GB NVMe).  Nothing removed them: the first 500 gate
# filled the disks after ~35 images per host ("No space left on device" from
# the grade, then every later ``images.pull`` failed silently and
# ``images.get`` 404'd -> 16 instances ABANDONED).  Two fixes, both
# best-effort (disk hygiene must never fail a grade that could otherwise run):
#   * release_instance_image() after EVERY grade removes the two local tags of
#     the image just graded (a re-grade of the same instance re-pulls from ECR
#     — in-VPC, ~4 GB, seconds — which is the trade the build tier already
#     makes for its working set);
#   * _ensure_free_disk() before a pull evicts other sweb.eval images not in
#     use by a container when free space is below the floor (a reap that
#     failed earlier, or a host reused by an older worker).
_MIN_FREE_MB = int(os.environ.get("EVAL_IMAGE_MIN_FREE_MB", "25000"))
_SWEB_IMAGE_PREFIX = "sweb.eval."


def _free_disk_mb(path: str = "/var/lib/docker") -> int | None:
    try:
        st = os.statvfs(path)
    except OSError:
        try:
            st = os.statvfs("/")
        except OSError:
            return None
    return int(st.f_bavail * st.f_frsize / (1024 * 1024))


def release_instance_image(test_spec: Any) -> int:
    """Remove the local tags of the instance image just graded; returns tags removed.

    Removes ``test_spec.image`` (the harness's name) and the ECR ``-inst`` name
    it was pulled as.  Never raises: a removal failure is logged and the next
    grade's :func:`_ensure_free_disk` gets another chance.
    """
    if docker is None:
        return 0
    image = str(test_spec.image)
    names = [image]
    try:
        ecr_repo = os.environ.get("ENV_IMAGE_REPOSITORY", "")
        if ecr_repo:
            names.append(f"{ecr_repo}:{inst_tag(_instance_id_from_image(image))}")
    except Exception as exc:  # noqa: BLE001 - an unparsable name just means one tag to remove
        logger.info("no ECR name derived for %s: %s", image, exc)
    client = docker.from_env()
    removed = 0
    for name in names:
        try:
            client.images.remove(name, force=True)
            removed += 1
        except Exception as exc:  # noqa: BLE001 - "no such image" or in use: log, move on
            logger.info("instance image %s not removed: %s", name, exc)
    logger.info(
        "released instance image %s (%d tag(s) removed, free %s MiB)",
        image,
        removed,
        _free_disk_mb(),
    )
    return removed


def _ensure_free_disk(client: Any, keep: tuple[str, ...] = ()) -> None:
    """Evict unused ``sweb.eval.*`` images (and dangling layers) when free disk is low."""
    free = _free_disk_mb()
    if free is None or free >= _MIN_FREE_MB:
        return
    logger.warning(
        "free disk %s MiB below %d MiB before an instance pull — evicting", free, _MIN_FREE_MB
    )
    in_use: set[str] = set()
    try:
        for c in client.containers.list(all=True):
            in_use.add(str(getattr(c, "image", None) and c.image.id))
    except Exception as exc:  # noqa: BLE001
        logger.info("could not list containers for eviction (treating none as in use): %s", exc)
    try:
        images = list(client.images.list())
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not list images for eviction: %s", exc)
        return
    for img in images:
        tags = [str(t) for t in (getattr(img, "tags", None) or [])]
        if not tags or img.id in in_use or any(t in keep for t in tags):
            continue
        if not any(_SWEB_IMAGE_PREFIX in t or "-inst" in t for t in tags):
            continue
        try:
            client.images.remove(img.id, force=True)
            logger.info("evicted %s", tags[0])
        except Exception as exc:  # noqa: BLE001
            logger.info("could not evict %s: %s", tags[0], exc)
    try:
        client.images.prune()
    except Exception as exc:  # noqa: BLE001
        logger.info("image prune failed: %s", exc)
    logger.warning("eviction done (free %s MiB)", _free_disk_mb())
