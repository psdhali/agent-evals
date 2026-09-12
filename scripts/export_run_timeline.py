#!/usr/bin/env python3
"""Export one run's live-run timeline for the publication site.

dev/LIVE-RUN-TIMELINE-SITE-DATA-CONTRACT-AND-BUILD-PLAN-2026-09-04.md §2 / §4.5.

Reads ``GET /runs/{id}/export`` (the provenance block) and ``GET /runs/{id}/timeline``
(the raw ticks, capacity rows, events, lane rows and llm_calls) through the API tunnel,
then writes the site's files:

    <site>/data/timelines/<run_id>/manifest.json, fleet.json, instances.json, discovery.json
    <site>/data/timelines/<run_id>/judge.json, judge_report.md   (when the run was judged)
    <site>/static/timelines/<run_id>/calls/<instance>__<attempt>.json

``judge.json`` (2026-09-08) is every LLM-judge verdict (all rubric dimensions, reasoning,
evidence, honesty flags) + per-kind finding counts + the pass ledger; ``judge_report.md`` is
the latest pass report. Both come from ``GET /runs/{id}/judge/results`` and ``/judge/passes``
and go through the same scrub.

The build is pure (``swebench_eval.orchestrator.timeline_export``): allowlist-pruned,
denylist-scrubbed (the export FAILS on a hit — nothing is written), decimated to <= 720
points, columnar with integer ``t`` since the window start. Re-running on an unchanged run
with the same ``--exported-at`` produces byte-identical files.

``--raw DIR`` also writes the Releases bundle: ``timeline-raw-<run_id>.json.gz`` — the
undecimated, unscrubbed API responses (NOT for the site; check it before attaching it).

Usage:
    .venv/bin/python scripts/export_run_timeline.py --run <run_id> --site ../site
    .venv/bin/python scripts/export_run_timeline.py --run <run_id> --site ../site --raw ./releases
Env:  EVAL_API_URL (default http://127.0.0.1:8000 — the SSM tunnel).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import pathlib
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from swebench_eval.orchestrator import timeline_export


def _get(base: str, path: str) -> dict[str, Any]:
    url = base.rstrip("/") + path
    with urllib.request.urlopen(url, timeout=600) as resp:
        data: dict[str, Any] = json.load(resp)
        return data


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:  # noqa: BLE001
        return ""


def resolve_site_root(site: pathlib.Path) -> pathlib.Path:
    """Where ``data/`` and ``static/`` go for ``--site``.

    ``--site`` is the site REPO root (which contains ``site/``). Given the ``site/``
    directory itself — or any directory that already holds ``data/`` or ``static/`` — write
    there directly instead of nesting a second ``site/`` inside it (2026-09-08: every ad-hoc
    export under ``dev/exports/<run>/site`` had become ``dev/exports/<run>/site/site``).
    """
    if site.name == "site" or (site / "data").is_dir() or (site / "static").is_dir():
        return site
    return site / "site"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--run", required=True, help="run_id")
    parser.add_argument("--site", required=True, help="path to the site repo root (contains site/)")
    parser.add_argument("--raw", default=None, help="directory for the Releases raw bundle")
    parser.add_argument("--api", default=os.environ.get("EVAL_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--target-points", type=int, default=timeline_export.TARGET_POINTS)
    parser.add_argument(
        "--exported-at",
        default=None,
        help="ISO stamp for the manifest (default now; pass a fixed one for idempotent re-runs)",
    )
    args = parser.parse_args()

    run_q = urllib.parse.quote(args.run, safe="")
    export = _get(args.api, f"/runs/{run_q}/export")
    timeline = _get(args.api, f"/runs/{run_q}/timeline")
    judge = {
        "results": _get(args.api, f"/runs/{run_q}/judge/results").get("results") or [],
        "passes": _get(args.api, f"/runs/{run_q}/judge/passes").get("passes") or [],
    }
    exported_at = args.exported_at or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        files = timeline_export.build_site_files(
            timeline,
            export,
            target_points=args.target_points,
            exporter_sha=_git_sha(),
            exported_at=exported_at,
            judge=judge,
        )
    except timeline_export.ScrubError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    site_root = resolve_site_root(pathlib.Path(args.site))
    data_dir = site_root / "data" / "timelines" / args.run
    static_dir = site_root / "static" / "timelines" / args.run / "calls"
    data_dir.mkdir(parents=True, exist_ok=True)
    static_dir.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        target = (
            static_dir / name[len("calls/") :] if name.startswith("calls/") else data_dir / name
        )
        target.write_bytes(data)
    manifest = json.loads(files["manifest.json"])
    print(
        f"wrote {len(files)} files for {args.run}: "
        f"{manifest['files']['fleet.json']['points']} points, "
        f"{manifest['files']['fleet.json']['events']} events, "
        f"{manifest['files']['instances.json']['lanes']} lanes, "
        f"{manifest['files']['calls']['count']} call files, "
        f"window {manifest['window']['seconds']} s, scrub hits 0"
    )
    judge_entry = manifest["files"].get("judge.json")
    if judge_entry:
        print(
            f"judge: {judge_entry['judged_attempts']} judged attempts, "
            f"findings any={judge_entry['finding_counts']['any']}, "
            f"report={'yes' if 'judge_report.md' in files else 'none'}"
        )
    else:
        print("judge: no judgments for this run — judge.json not written")

    if args.raw:
        raw_dir = pathlib.Path(args.raw)
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"timeline-raw-{args.run}.json.gz"
        with gzip.open(raw_path, "wt") as fh:
            json.dump({"export": export, "timeline": timeline, "judge": judge}, fh)
        print(f"raw bundle: {raw_path} ({raw_path.stat().st_size} bytes) — NOT scrubbed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
