#!/usr/bin/env python3
"""Snapshot the content digest behind every instance's official image (ADR-0043).

SWE-bench 5.x datasets name each instance's grading image as a MUTABLE tag
(``swebench/sweb.eval.x86_64.<owner>_1776_<repo>-<n>:latest``).  A dataset
revision therefore does not pin image bytes.  This script resolves each row's
``image`` reference to its amd64 manifest digest via the Docker Hub API and
writes the map to ``swebench_eval/dataset/image_digests/<dataset>-<rev12>.json`` (package data, so the deployed containers carry it) — the second
half of the pin.  The build (``build_phase0_instances_v2.py --base official``)
reads the base digest FROM THIS FILE, never from a live tag lookup, and the
dispatcher's launchable gate compares each ``-inst`` sentinel against it.

Refuses to overwrite an existing snapshot: re-resolving a moved tag would
silently change the pin.  Pass ``--force`` only for a deliberate re-pin (which
is an ADR-level decision — record why in the commit message).

Usage:
    uv run python scripts/snapshot_image_digests.py            # active dataset + pinned revision
    uv run python scripts/snapshot_image_digests.py --check    # verify the committed file still matches the Hub
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from swebench_eval import aws_names

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_HUB_API = "https://hub.docker.com/v2/repositories"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SNAPSHOT_DIR = _REPO_ROOT / "swebench_eval" / "dataset" / "image_digests"


def snapshot_path(dataset_name: str, revision: str) -> Path:
    """``swebench_eval/dataset/image_digests/<dataset leaf>-<rev12>.json``."""
    leaf = dataset_name.rsplit("/", 1)[-1]
    return _SNAPSHOT_DIR / f"{leaf}-{revision[:12]}.json"


def split_image_ref(image: str) -> tuple[str, str]:
    """``swebench/sweb.eval.x86_64.x_1776_y-1:latest`` -> (repo, tag)."""
    name, sep, tag = image.rpartition(":")
    if not sep or "/" in tag:
        return image, "latest"
    return name, tag


def hub_jwt() -> str | None:
    """A Docker Hub API JWT, or ``None`` for anonymous access.

    Credentials come from ``DOCKERHUB_USERNAME``/``DOCKERHUB_TOKEN`` or, as the
    build tier does (``build_phase0_instances_v2._dockerhub_login``), from the
    Secrets Manager secret named by ``DOCKERHUB_SECRET_ID`` (``{username,
    accessToken}``).  The token is never logged; a login failure falls back to
    anonymous with a warning (the anonymous tags API is rate-limited hard
    enough that 500 lookups do not fit — found on the first snapshot run).
    """
    import os

    user = os.environ.get("DOCKERHUB_USERNAME", "")
    token = os.environ.get("DOCKERHUB_TOKEN", "")
    secret_id = os.environ.get("DOCKERHUB_SECRET_ID", "")
    if not (user and token) and secret_id:
        try:
            import boto3

            raw = boto3.client("secretsmanager", region_name=aws_names.region())
            doc = json.loads(raw.get_secret_value(SecretId=secret_id)["SecretString"])
            user, token = str(doc.get("username", "")), str(doc.get("accessToken", ""))
        except Exception as exc:  # noqa: BLE001 - anonymous is a valid (slow) fallback
            print(f"warning: could not read {secret_id}: {type(exc).__name__}; going anonymous")
    if not (user and token):
        return None
    body = json.dumps({"username": user, "password": token}).encode("utf-8")
    req = urllib.request.Request(
        "https://hub.docker.com/v2/users/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            jwt = str(json.loads(resp.read().decode("utf-8")).get("token") or "")
    except urllib.error.HTTPError as exc:
        print(f"warning: Docker Hub login failed (HTTP {exc.code}); going anonymous")
        return None
    return jwt or None


def resolve_digest(image: str, *, retries: int = 10, jwt: str | None = None) -> str:
    """The amd64 manifest digest behind *image* on Docker Hub; raises if absent.

    Same contract as ``build_phase0_instances_v2.resolve_official_digest``:
    a tag with no amd64 image is an error — the fleet is x86_64.  A 429 is
    retried with a patient backoff (the Hub's window is minutes, not seconds).
    """
    repo, tag = split_image_ref(image)
    url = f"{_HUB_API}/{repo}/tags/{tag}"
    headers = {"Authorization": f"JWT {jwt}"} if jwt else {}
    delay = 15.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=30
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 1.6, 120.0)
                continue
            raise RuntimeError(f"{image}: Docker Hub returned HTTP {exc.code}") from exc
    digest = str(data.get("digest") or "")
    archs = {str(img.get("architecture")) for img in data.get("images", []) or []}
    if not digest.startswith("sha256:"):
        raise RuntimeError(f"{image}: Docker Hub returned no manifest digest ({data!r})")
    if "amd64" not in archs:
        raise RuntimeError(f"{image}: no amd64 image behind it (architectures={archs})")
    return digest


def load_snapshot(path: Path) -> dict[str, dict[str, str]]:
    """The committed snapshot's ``instances`` map (``instance_id -> {image, digest, resolved_at}``)."""
    return dict(json.loads(path.read_text(encoding="utf-8"))["instances"])


def build_snapshot(
    rows: list[Any],
    *,
    workers: int = 4,
    partial: Path | None = None,
    jwt: str | None = None,
) -> dict[str, dict[str, str]]:
    """Resolve every row's ``image``; returns ``instance_id -> {image, digest, resolved_at}``.

    *partial* (a scratch file) is updated after every resolution and read back
    on start, so a run killed by the Hub's rate limit resumes instead of
    re-spending the budget it already used.
    """
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: dict[str, dict[str, str]] = {}
    if partial and partial.exists():
        out = dict(json.loads(partial.read_text(encoding="utf-8")))
        print(f"resuming: {len(out)} already resolved in {partial}")
    missing = [r.instance_id for r in rows if not r.image]
    if missing:
        raise SystemExit(
            f"{len(missing)} rows carry no image field (not a 5.x dataset?): {missing[:5]}"
        )
    todo = [
        r for r in rows if r.instance_id not in out or out[r.instance_id].get("image") != r.image
    ]
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(resolve_digest, r.image, jwt=jwt): r for r in todo}
        for i, fut in enumerate(cf.as_completed(futures), 1):
            row = futures[fut]
            out[row.instance_id] = {"image": row.image, "digest": fut.result(), "resolved_at": now}
            if partial:
                partial.write_text(json.dumps(out, sort_keys=True), encoding="utf-8")
            if i % 25 == 0 or i == len(todo):
                print(f"  resolved {len(out)}/{len(rows)}")
    return dict(sorted(out.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing snapshot (re-pin)"
    )
    parser.add_argument(
        "--check", action="store_true", help="compare the committed snapshot with the Hub"
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--only",
        default="",
        help="--check only these comma-separated instance ids (a subset build needs only its "
        "own pins verified — 10 Hub lookups instead of 500)",
    )
    parser.add_argument(
        "--partial",
        type=Path,
        default=None,
        help="scratch file for resumable progress (default: none; use a tmp path outside the repo)",
    )
    args = parser.parse_args()
    jwt = hub_jwt()
    print("Docker Hub API:", "authenticated" if jwt else "anonymous (slow; expect 429 backoffs)")

    from swebench_eval.dataset.swebench_loader import _DATASET_NAME, SwebenchLiteLoader

    loader = SwebenchLiteLoader(include_gold=True)
    revision = loader.revision
    if not revision or revision == "main":
        raise SystemExit("refusing to snapshot an unpinned revision — pin SWEBENCH_REVISION first")
    path = snapshot_path(_DATASET_NAME, revision)
    rows = loader.load()

    if args.check:
        committed = load_snapshot(path)
        only = {i.strip() for i in args.only.split(",") if i.strip()}
        if only:
            rows = [r for r in rows if r.instance_id in only]
            committed = {k: v for k, v in committed.items() if k in only}
            missing = only - set(committed)
            if missing:
                raise SystemExit(f"not in the committed snapshot: {sorted(missing)}")
        live = build_snapshot(rows, workers=args.workers, jwt=jwt)
        moved = [k for k in committed if committed[k]["digest"] != live.get(k, {}).get("digest")]
        print(f"{path.name}: {len(committed)} pinned, {len(moved)} moved on the Hub")
        for k in moved[:20]:
            print(
                f"  MOVED {k}: pinned {committed[k]['digest'][:19]} live {live[k]['digest'][:19]}"
            )
        return 1 if moved else 0

    if path.exists() and not args.force:
        raise SystemExit(
            f"{path} exists — the pin is committed; use --force only for a deliberate re-pin"
        )

    instances = build_snapshot(rows, workers=args.workers, partial=args.partial, jwt=jwt)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "dataset": _DATASET_NAME,
        "revision": revision,
        "count": len(instances),
        "note": "ADR-0043: instance_id -> amd64 manifest digest of the row's (mutable) image tag, "
        "resolved once; builds pull by this digest, never by tag.",
        "instances": instances,
    }
    path.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {path} ({len(instances)} instances)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
