#!/usr/bin/env python3
"""The laptop per-instance image: pull the pinned official image, build the local twin, run it.

Adoption F3 (2026-09-11).  Three subcommands, all no-AWS:

    ensure <instance_id> [--rebuild]
        Pull SWE-bench's official per-instance image at the digest the committed
        snapshot pins (never the mutable ``:latest``), tag it locally under the
        dataset row's image name so the official harness grades in exactly that
        digest, then build ``swebench-eval-local:<ver>-<instance_id>`` from
        infra/docker/Dockerfile.local-instance (framework venv + agent user +
        sentinel).  Prints the local tag.

    run <instance_id> --out DIR --model ALIAS [...]
        ``docker run`` the local image with ``entrypoint.sh local-job``, the
        compose stack's gateway / MinIO reachable through ``host.docker.internal``,
        artifacts bind-mounted at DIR.  Exit code is the job's.

    base <instance_id>
        Print the pinned base reference and digest (for docs / scripts).

Used by scripts/smoke_test.py; usable on its own.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swebench_eval.dataset.swebench_loader import image_digest_snapshot_name
from swebench_eval.evaluation.env_image import DEFAULT_SWEBENCH_VERSION

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "infra" / "docker" / "Dockerfile.local-instance"
LOCAL_REPOSITORY = "swebench-eval-local"
PLATFORM = "linux/amd64"  # every official SWE-bench image is x86_64


def _snapshot_entry(instance_id: str) -> dict[str, str]:
    """``{"image": ..., "digest": ...}`` for *instance_id* from the committed snapshot."""
    name = image_digest_snapshot_name()
    path = resources.files("swebench_eval.dataset").joinpath("image_digests").joinpath(name)
    doc = json.loads(path.read_text(encoding="utf-8"))
    entry = dict(doc.get("instances", {})).get(instance_id)
    if not entry:
        raise SystemExit(
            f"{instance_id} is not in the image-digest snapshot {name} "
            "(is it a SWE-bench Verified instance id?)"
        )
    return {"image": str(entry["image"]), "digest": str(entry["digest"])}


def base_reference(instance_id: str) -> tuple[str, str, str]:
    """(pinned pull reference ``repo@sha256:…``, the row's image name, the digest)."""
    entry = _snapshot_entry(instance_id)
    repo = entry["image"].split("@", 1)[0].rsplit(":", 1)[0]
    return f"{repo}@{entry['digest']}", entry["image"], entry["digest"]


def local_tag(instance_id: str, version: str | None = None) -> str:
    version = version or os.environ.get("SWEBENCH_VERSION", DEFAULT_SWEBENCH_VERSION)
    return f"{LOCAL_REPOSITORY}:{version}-{instance_id}"


def _docker() -> Any:
    import docker

    return docker.from_env()


def _image_present(client: Any, name: str) -> bool:
    try:
        client.images.get(name)
        return True
    except Exception:  # noqa: BLE001 - "no such image" is the normal miss
        return False


def _git_sha() -> str:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            or "unknown"
        )
    except Exception:  # noqa: BLE001 - no git on the box: label unknown, build anyway
        return "unknown"


def ensure_base(instance_id: str, client: Any | None = None) -> tuple[str, str]:
    """Pull the pinned official image and tag it as the row's image name.

    Returns (row image name, digest).  Idempotent: when the local image under
    the row's name already carries the pinned digest nothing is pulled.  When it
    carries a DIFFERENT digest (a ``:latest`` pulled by hand some other day) it is
    re-tagged to the pinned one — the snapshot wins, and says so.
    """
    client = client or _docker()
    pull_ref, row_image, digest = base_reference(instance_id)
    if _image_present(client, row_image):
        repo_digests = [str(d) for d in client.images.get(row_image).attrs.get("RepoDigests", [])]
        if any(d.endswith("@" + digest) for d in repo_digests):
            print(f"base image {row_image} already local at the pinned digest")
            return row_image, digest
        print(f"WARNING: local {row_image} is not the pinned digest ({repo_digests}); re-tagging")
    print(f"pulling {pull_ref} ({PLATFORM}) ...")
    subprocess.run(["docker", "pull", "--platform", PLATFORM, pull_ref], check=True)
    img = client.images.get(pull_ref)
    repo, tag = (
        row_image.rsplit(":", 1) if ":" in row_image.rsplit("/", 1)[-1] else (row_image, "latest")
    )
    img.tag(repo, tag=tag)
    print(f"tagged {row_image} <- {pull_ref}")
    return row_image, digest


def ensure_local_image(instance_id: str, rebuild: bool = False, base_commit: str = "") -> str:
    """Build (or reuse) the local per-instance image; returns its tag."""
    client = _docker()
    tag = local_tag(instance_id)
    row_image, digest = ensure_base(instance_id, client)
    if not rebuild and _image_present(client, tag):
        labels = client.images.get(tag).attrs.get("Config", {}).get("Labels") or {}
        if labels.get("swebench_eval.base_digest") == digest:
            print(f"local image {tag} already built from the pinned base")
            return tag
        print(f"local image {tag} was built from another base; rebuilding")
    pull_ref = f"{row_image.rsplit(':', 1)[0]}@{digest}"
    sha = _git_sha()
    cmd = [
        "docker",
        "build",
        "--platform",
        PLATFORM,
        "-f",
        str(DOCKERFILE),
        "-t",
        tag,
        "--label",
        f"swebench_eval.base_digest={digest}",
        "--label",
        f"swebench_eval.instance_id={instance_id}",
        "--build-arg",
        f"BASE_IMAGE={pull_ref}",
        "--build-arg",
        f"INSTANCE_ID={instance_id}",
        "--build-arg",
        f"BASE_COMMIT={base_commit}",
        "--build-arg",
        f"BASE_IMAGE_REF={row_image}",
        "--build-arg",
        f"BASE_IMAGE_DIGEST={digest}",
        "--build-arg",
        f"FRAMEWORK_SHA_BUILD_ARG={sha}",
        str(ROOT),
    ]
    print("building", tag, "...")
    subprocess.run(cmd, check=True, env={**os.environ, "DOCKER_BUILDKIT": "1"})
    return tag


def _host_url(url: str) -> str:
    """Rewrite a localhost URL so a container reaches the host's compose stack."""
    return (
        url.replace("://localhost", "://host.docker.internal")
        .replace("://127.0.0.1", "://host.docker.internal")
        .replace("://0.0.0.0", "://host.docker.internal")
    )


def run_local_job(
    instance_id: str,
    out_dir: Path,
    model: str,
    *,
    image: str | None = None,
    run_id: str | None = None,
    attempt: int = 1,
    timeout_s: int = 1800,
    max_cost_usd: float = 1.0,
    max_turns: int = 100,
    context_window_tokens: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> int:
    """``docker run`` the local image's ``local-job``; returns the container's exit code."""
    image = image or local_tag(instance_id)
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "LITELLM_BASE_URL": _host_url(
            os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1")
        ),
        "LITELLM_MASTER_KEY": os.environ.get("LITELLM_MASTER_KEY", "sk-local"),
        "S3_ENDPOINT_URL": _host_url(os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")),
        "DATASET_BUCKET": os.environ.get("DATASET_BUCKET", "eval-dataset"),
        "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        "EVAL_PACER_ENABLED": os.environ.get("EVAL_PACER_ENABLED", "0"),
        "HARNESS_AGENT_USER": os.environ.get("HARNESS_AGENT_USER", "agent"),
    }
    if os.environ.get("LITELLM_API_KEY"):
        env["LITELLM_API_KEY"] = os.environ["LITELLM_API_KEY"]
    for key in ("SWEBENCH_DATASET", "SWEBENCH_REVISION"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env.update(extra_env or {})
    cmd = ["docker", "run", "--rm", "--platform", PLATFORM, "-v", f"{out_dir}:/out"]
    if platform.system() == "Linux":
        cmd += ["--add-host", "host.docker.internal:host-gateway"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [
        image,
        "local-job",
        "--instance-id",
        instance_id,
        "--model",
        model,
        "--out",
        "/out",
        "--attempt",
        str(attempt),
        "--timeout-s",
        str(timeout_s),
        "--max-cost-usd",
        str(max_cost_usd),
        "--max-turns",
        str(max_turns),
    ]
    if run_id:
        cmd += ["--run-id", run_id]
    if context_window_tokens:
        cmd += ["--context-window-tokens", str(context_window_tokens)]
    print("running", image, "local-job for", instance_id, "->", out_dir)
    return subprocess.run(cmd, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_base = sub.add_parser("base")
    p_base.add_argument("instance_id")
    p_ensure = sub.add_parser("ensure")
    p_ensure.add_argument("instance_id")
    p_ensure.add_argument("--rebuild", action="store_true")
    p_ensure.add_argument("--base-commit", default="")
    p_run = sub.add_parser("run")
    p_run.add_argument("instance_id")
    p_run.add_argument("--out", required=True)
    p_run.add_argument("--model", default="cheap-oss-model")
    p_run.add_argument("--image", default=None)
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--attempt", type=int, default=1)
    p_run.add_argument("--timeout-s", type=int, default=1800)
    p_run.add_argument("--max-cost-usd", type=float, default=1.0)
    p_run.add_argument("--max-turns", type=int, default=100)
    args = parser.parse_args(argv)

    if args.cmd == "base":
        pull_ref, row_image, digest = base_reference(args.instance_id)
        print(json.dumps({"pull": pull_ref, "image": row_image, "digest": digest}, indent=2))
        return 0
    if args.cmd == "ensure":
        print(
            ensure_local_image(args.instance_id, rebuild=args.rebuild, base_commit=args.base_commit)
        )
        return 0
    return run_local_job(
        args.instance_id,
        Path(args.out),
        args.model,
        image=args.image,
        run_id=args.run_id,
        attempt=args.attempt,
        timeout_s=args.timeout_s,
        max_cost_usd=args.max_cost_usd,
        max_turns=args.max_turns,
    )


if __name__ == "__main__":
    raise SystemExit(main())
