"""master-handover 1.1 / R8.2 — tail a live run's prompts from the gateway.

Reads ``/spend/logs?summarize=false`` from the LiteLLM gateway and prints the
most recent completion's request/response text — the fast route to seeing what
the model is actually doing, uniform across ALL five harnesses because it sits
at the gateway.

Deliberately queries ONLY the gateway's spend-logs endpoint, never Aurora:
connecting to Aurora resets its auto-pause clock (runbook §0.2).

Requires the gateway to have been run with
``general_settings.store_prompts_in_spend_logs: true`` (baked into
``infra/docker/litellm_config.yaml``), else the endpoint returns no prompt text.
(M3, review 2026-08-25: the key is under ``general_settings``, NOT
``litellm_settings`` — LiteLLM reads it from general_settings.)

Usage:
    python scripts/tail_trajectory.py [--n 20] [--run <run_id>] [--url <gw>/v1]

    --n        how many recent completions to show (default 20)
    --run      filter to one run_id prefix (optional)
    --url      gateway base URL (default $LITELLM_BASE_URL or
               http://localhost:4000/v1)
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from typing import Any


def _fetch(url: str, key: str) -> list[dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload: Any = json.loads(resp.read().decode("utf-8"))
    return payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []


def _dump_text(value: Any) -> str:
    """Flatten a prompt/response value (string, dict, or list of content parts)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_dump_text(v) for v in value)
    if isinstance(value, dict):
        # OpenAI-shaped content parts: [{"type":"text","text":...}] /
        # {"role":...,"content":...} / tool calls
        if "text" in value and value.get("text"):
            return str(value["text"])
        if "content" in value:
            return _dump_text(value["content"])
        return json.dumps(value, default=str)
    return str(value)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--run", default="")
    ap.add_argument("--url", default=os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1"))
    args = ap.parse_args()

    base = args.url.rstrip("/")
    spend_url = f"{base}/spend/logs?summarize=false"
    key = os.environ.get("LITELLM_MASTER_KEY", "sk-local")
    logs = _fetch(spend_url, key)

    if args.run:
        logs = [l for l in logs if args.run in str(l.get("request_id") or l.get("call_id") or "")]

    for log in logs[-args.n :]:
        print("=" * 60)
        resp = log.get("response") or {}
        model = log.get("model", "")
        rid = log.get("request_id") or log.get("call_id") or "?"
        print(f"[{rid}] model={model}")
        print("--- REQUEST (messages) ---")
        # 2026-09-02 (live-view doc, verified on real rows): this LiteLLM
        # version leaves the messages COLUMN {} — the actual conversation is in
        # proxy_server_request.  Older fields kept as fallbacks so the script
        # degrades instead of printing nothing on a different server version.
        psr = log.get("proxy_server_request") or {}
        req = log.get("request") or {}
        messages = (
            (psr.get("messages") if isinstance(psr, dict) else None)
            or log.get("messages")
            or req.get("messages")
        )
        print(_dump_text(messages))
        print("--- RESPONSE ---")
        print(_dump_text(resp.get("choices") or resp))


if __name__ == "__main__":
    main()
