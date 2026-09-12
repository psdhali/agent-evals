"""One client for the gateway's ``/model/info`` endpoint (compaction build 1.4).

The context-compaction spec (BUILD-SPEC rev 2 §2) and O-1 both need the gateway's
per-alias model metadata, and both need it written ONCE — not two callers each
re-implementing the same HTTP+parse and drifting apart on the field name.  The
one call, ``GET {gateway}/model/info``, returns per alias:

  ``model_info.max_input_tokens``  — the window resolution needs this (the run's
       context window, resolved per model — Stage 1.1).
  ``litellm_params.model``         — O-1's literal upstream model for per-call
       ``model_resolved`` (not the alias).

Contract (from the spec): the client FAILS fast on a genuinely bad response shape
but must never fail a run — the dispatcher treats it as an optional source and
falls back to the baked yaml, then ``DEFAULT_CONTEXT_WINDOW_TOKENS``.  This module
holds no failure policy of its own: it returns the parsed structure or ``None``,
and the dispatcher decides what ``None`` means.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# The one field name each caller reads.  Centralised so a LiteLLM version change
# to the response shape is fixed here, not in two places.
MAX_INPUT_TOKENS_KEY = "max_input_tokens"
LITELLM_PARAMS_MODEL_KEY = "model"


def fetch_model_info(
    base_url: str,
    api_key: str,
    alias: str,
    *,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Return the resolved metadata for *alias* from the live gateway.

    ``base_url`` is the gateway root (e.g. the ALB host); the endpoint is
    ``{base}/model/info?model_name={alias}``.  Returns a dict with the keys the
    callers read — ``max_input_tokens`` (int) and ``model`` (str, the literal
    upstream model) — or ``None`` when the gateway is unreachable, the alias is
    absent, or the response is not the shape LiteLLM serves.

    Never raises: a provenance/window lookup must not break dispatch.
    """
    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/model/info",
            params={"model_name": alias},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - any transport/parse failure is a
        # fallback, not a run failure (spec §2 step 3: baked yaml, then default).
        logger.warning("model/info lookup failed for %s: %s", alias, exc)
        return None

    # LiteLLM: {"data": [{ "model_name": ...,
    #                      "model_info": { "max_input_tokens": ... },
    #                      "litellm_params": { "model": "openrouter/..." }, ...}]}.
    items = data.get("data") or []
    if not isinstance(items, list):
        logger.warning("model/info for %s: unexpected shape (no data list)", alias)
        return None
    for entry in items:
        if isinstance(entry, dict) and entry.get("model_name") == alias:
            info: dict[str, Any] = {}
            model_info = entry.get("model_info") or {}
            if isinstance(model_info, dict) and model_info.get(MAX_INPUT_TOKENS_KEY):
                info[MAX_INPUT_TOKENS_KEY] = int(model_info[MAX_INPUT_TOKENS_KEY])
            params = entry.get("litellm_params") or {}
            if isinstance(params, dict) and params.get(LITELLM_PARAMS_MODEL_KEY):
                info[LITELLM_PARAMS_MODEL_KEY] = str(params[LITELLM_PARAMS_MODEL_KEY])
            if info:
                return info
            logger.warning("model/info for %s: alias found but no usable metadata", alias)
            return None
    logger.warning("model/info for %s: alias not present in gateway response", alias)
    return None
