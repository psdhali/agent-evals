"""OpenRouter provisioning-API client — per-run runtime keys (ADR-0035 decision 2).

Uses OpenRouter's key-management API (``POST/PATCH/DELETE
https://openrouter.ai/api/v1/keys``), authenticated with the **provisioning**
key (``eval-dev-openrouter-management`` — D5), never the runtime key.  This
module must run ONLY in the orchestrator; the provisioning key is granted to
the orchestrator task role alone (§5.2 point 3).

**Unverified against the live OpenRouter API** (D3's verification requirement
was scoped to the LiteLLM admin surface, which this build DID verify locally —
see ``gateway/admin.py``; OpenRouter's provisioning key lives only in AWS
Secrets Manager and this session has no AWS credentials, so the request/
response shapes below follow OpenRouter's published API documentation as of
this writing, not a live round-trip).  Flagged in
``builder4-run-launch-response.md`` — the first real launch (D4) is where this
gets its first live exercise; watch it closely.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://openrouter.ai/api/v1/keys"
_TIMEOUT_S = 15.0


class OpenRouterAdminError(RuntimeError):
    """An OpenRouter key-management call failed.  Caller must fail closed (D4)."""


def _headers(provisioning_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {provisioning_key}", "Content-Type": "application/json"}


def mint_key(
    provisioning_key: str,
    *,
    name: str,
    limit_usd: float,
) -> tuple[str, str]:
    """Mint a per-run OpenRouter key.  Returns ``(raw_key, key_hash)``.

    ``limit_reset: null`` (no mid-run refill — ADR-0035 decision 2).
    ``key_hash`` is OpenRouter's own opaque, non-secret identifier for the key
    (``data.hash`` in the response) — used for later disable/delete calls and
    the only thing persisted to ``runs.openrouter_key_hash`` (rule 3: never
    the raw key).
    """
    try:
        resp = httpx.post(
            _BASE_URL,
            headers=_headers(provisioning_key),
            json={"name": name, "limit": limit_usd, "limit_reset": None},
            timeout=_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise OpenRouterAdminError(f"POST /keys failed: {exc}") from exc
    if resp.status_code >= 400:
        raise OpenRouterAdminError(f"POST /keys -> {resp.status_code}: {resp.text[:500]}")
    body: dict[str, Any] = resp.json()
    raw_key = body.get("key")
    key_hash = (body.get("data") or {}).get("hash")
    if not raw_key or not key_hash:
        raise OpenRouterAdminError(f"POST /keys returned an unexpected shape: {body!r}")
    logger.info("minted OpenRouter runtime key %s (hash=%s)", name, key_hash)
    return str(raw_key), str(key_hash)


def disable_key(provisioning_key: str, key_hash: str) -> None:
    """Disable a run's OpenRouter key (§8 finalisation: the third kill switch).

    Disable, not delete — ``disabled: true`` is the kill switch that "depends
    on nothing this project built" (ADR-0035); deletion would also work but
    disable is reversible if the finalisation needs to be inspected/retried.
    Idempotent: a 404 (already gone) is not an error — finalisation must be
    safe to retry.
    """
    try:
        resp = httpx.patch(
            f"{_BASE_URL}/{key_hash}",
            headers=_headers(provisioning_key),
            json={"disabled": True},
            timeout=_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise OpenRouterAdminError(f"PATCH /keys/{key_hash} failed: {exc}") from exc
    if resp.status_code == 404:
        logger.warning("OpenRouter key hash=%s already gone; treating disable as done", key_hash)
        return
    if resp.status_code >= 400:
        raise OpenRouterAdminError(
            f"PATCH /keys/{key_hash} -> {resp.status_code}: {resp.text[:500]}"
        )
    logger.info("disabled OpenRouter runtime key hash=%s", key_hash)


def find_key_hash(provisioning_key: str, name: str) -> str | None:
    """The hash of the (first) key named *name*, or None. Keys are minted with
    ``name=pass_id`` / ``name=run_id``, so a pass whose finalisation never ran
    (a killed task) can still be cleaned up by name — 2026-09-08."""
    offset = 0
    while True:
        try:
            resp = httpx.get(
                _BASE_URL,
                headers=_headers(provisioning_key),
                params={"offset": offset},
                timeout=_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            raise OpenRouterAdminError(f"GET /keys failed: {exc}") from exc
        if resp.status_code >= 400:
            raise OpenRouterAdminError(f"GET /keys -> {resp.status_code}: {resp.text[:500]}")
        rows = (resp.json() or {}).get("data") or []
        for row in rows:
            if isinstance(row, dict) and row.get("name") == name and row.get("hash"):
                return str(row["hash"])
        if len(rows) < 100:  # OpenRouter pages at 100
            return None
        offset += len(rows)
