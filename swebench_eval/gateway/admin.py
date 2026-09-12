"""LiteLLM admin-API client — per-run virtual keys + rotatable model registration.

BUILDER4-RUN-LAUNCH-ORCHESTRATOR-2026-08-26 §5.2 (ADR-0035, "already accepted —
you are implementing it, not designing it"). Every call here uses the gateway
MASTER key (an admin credential); this module must never run inside the
untrusted harness container — it is orchestrator-only.

Unlike :mod:`swebench_eval.gateway.model_info` (whose contract is "never
raises, a window lookup must not break dispatch"), every function here RAISES
on failure.  A launch that cannot mint its key or register its alias must
fail closed (D4) — a silently-degraded budget cap is the exact defect ADR-0035
exists to close.

Verified locally 2026-08-26 against a real ``ghcr.io/berriai/litellm:main-stable``
container (D3) — see :mod:`swebench_eval.gateway.rotatable_models` for the full
finding: ``/model/update`` only works on a db-model (created via
``/model/new``), never on one declared in ``litellm_config.yaml``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT_S = 15.0


class GatewayAdminError(RuntimeError):
    """A LiteLLM admin-API call failed.  The caller must fail the launch closed."""


def _headers(master_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"}


def _admin_url(base_url: str, path: str) -> str:
    """Every route in this module (``/key/*``, ``/model/*``) lives at the
    LiteLLM proxy ROOT, never under ``/v1``. Callers pass whatever
    ``gateway_base_url()`` currently is — that's the OpenAI-compatible
    chat-completions base the HARNESS routes through, and it legitimately
    ends in ``/v1`` (deployed as ``http://gateway.eval.internal:4000/v1``).
    Reusing it unmodified here silently 404s every admin call
    (``.../v1/key/generate`` doesn't exist) — found live 2026-08-28 when the
    first real ``POST /runs`` hit ``_provision_keys`` and failed closed on
    ``/key/generate -> 404``. Strip a trailing ``/v1`` so this module works
    regardless of which base_url flavor the caller has on hand."""
    root = base_url.rstrip("/").removesuffix("/v1")
    return f"{root}{path}"


def _post(base_url: str, master_key: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        resp = httpx.post(
            _admin_url(base_url, path),
            headers=_headers(master_key),
            json=body,
            timeout=_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise GatewayAdminError(f"POST {path} failed: {exc}") from exc
    if resp.status_code >= 400:
        raise GatewayAdminError(f"POST {path} -> {resp.status_code}: {resp.text[:500]}")
    result: dict[str, Any] = resp.json()
    return result


def _get(base_url: str, master_key: str, path: str, params: dict[str, str]) -> dict[str, Any]:
    try:
        resp = httpx.get(
            _admin_url(base_url, path),
            headers=_headers(master_key),
            params=params,
            timeout=_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise GatewayAdminError(f"GET {path} failed: {exc}") from exc
    if resp.status_code >= 400:
        raise GatewayAdminError(f"GET {path} -> {resp.status_code}: {resp.text[:500]}")
    result: dict[str, Any] = resp.json()
    return result


def find_db_model_id(base_url: str, master_key: str, alias: str) -> str | None:
    """Return the ``model_info.id`` of *alias*'s db-model row, or ``None``.

    Only a row with ``model_info.db_model is True`` counts — a config-yaml row
    shares the ``model_name`` but can never be ``/model/update``d (verified,
    see module docstring), so it must never be mistaken for the rotatable one.
    A gateway can (today, for the 7 not-yet-migrated aliases) carry ONLY a
    config row for *alias*; that is not an error here, it just means no
    db-model exists yet — the caller decides what that means.
    """
    row = find_db_model(base_url, master_key, alias)
    return None if row is None else str(row["id"])


def find_db_model(base_url: str, master_key: str, alias: str) -> dict[str, Any] | None:
    """*alias*'s db-model row as ``{"id", "model_info"}``, or ``None`` (same rule as
    :func:`find_db_model_id`: only a ``db_model: true`` row counts)."""
    data = _get(base_url, master_key, "/model/info", {"model_name": alias})
    for entry in data.get("data") or []:
        if not isinstance(entry, dict) or entry.get("model_name") != alias:
            continue
        info = entry.get("model_info") or {}
        if info.get("db_model") is True:
            return {"id": str(info.get("id")), "model_info": dict(info)}
    return None


# The registration-spec model_info fields the gateway must agree with. They are what the
# run's context window is resolved from (gateway/model_info.py) and what LiteLLM's own
# context checks read; a stale figure on a row registered under an older spec would cap or
# refuse prompts the model actually accepts.
_RECONCILED_MODEL_INFO_KEYS = ("max_input_tokens", "max_output_tokens")


def ensure_model_registered(
    base_url: str,
    master_key: str,
    alias: str,
    litellm_params: dict[str, object],
    model_info: dict[str, object] | None = None,
) -> str:
    """Idempotently ensure *alias* exists as a db-model; return its model id.

    First call for an alias creates it via ``/model/new`` (never re-created on
    a later run — decision 5's "no dynamically created deployments" holds:
    exactly one deployment ever exists per rotatable alias, reused by every
    run that alias sees, key-rotated each time).  A later run for the SAME
    alias finds the existing row via :func:`find_db_model` and reuses it.

    2026-09-05: an existing row whose ``model_info`` window disagrees with the
    spec is DELETED and re-registered (the gpt-5-mini discovery alias was
    registered at 262,144 before the owner moved the family to its real
    400,000 — nothing else would ever have corrected it). Measured live the
    same day: ``/model/update`` with a new ``model_info`` returns 200 and
    changes nothing — LiteLLM honours only ``model_info.id`` there — so the
    only path that actually moves the window is ``/model/delete`` + ``/model/new``.
    The row gets a NEW id; every caller rotates the key onto the id this
    function returns immediately afterwards, so the spec's placeholder key
    never serves, and no live run ever holds an alias being reconciled (the
    check runs at registration time only).
    """
    row = find_db_model(base_url, master_key, alias)
    if row is not None:
        existing_id = str(row["id"])
        current = row.get("model_info") or {}
        drift = {
            k: (current.get(k), model_info[k])
            for k in _RECONCILED_MODEL_INFO_KEYS
            if model_info and k in model_info and current.get(k) != model_info[k]
        }
        if not drift:
            return existing_id
        _post(base_url, master_key, "/model/delete", {"id": existing_id})
        logger.info(
            "gateway db-model %s (id=%s) model_info drifted from the spec (%s) — deleted, "
            "re-registering (/model/update cannot change model_info, measured 2026-09-05)",
            alias,
            existing_id,
            ", ".join(f"{k} {old} -> {new}" for k, (old, new) in drift.items()),
        )
    body: dict[str, Any] = {"model_name": alias, "litellm_params": litellm_params}
    if model_info:
        body["model_info"] = model_info
    resp = _post(base_url, master_key, "/model/new", body)
    model_id = resp.get("model_id") or (resp.get("model_info") or {}).get("id")
    if not model_id:
        raise GatewayAdminError(f"/model/new for {alias!r} returned no model id: {resp!r}")
    logger.info("registered gateway db-model %s (id=%s)", alias, model_id)
    return str(model_id)


def rotate_model_key(
    base_url: str,
    master_key: str,
    alias: str,
    model_id: str,
    new_api_key: str,
    *,
    upstream_model: str,
    litellm_params: dict[str, Any] | None = None,
) -> None:
    """Rotate *alias*'s (db-model) upstream ``api_key`` via ``/model/update``.

    **``/model/update`` REPLACES litellm_params wholesale — measured live 2026-09-01** (a
    db-model updated with only ``{model, api_key}`` lost its ``extra_body`` provider pin, proven
    functionally: a pinned-to-blocked-provider alias 404'd before rotation and served 200 after).
    The old two-field rotation therefore silently stripped every other param — temperature,
    top_p, top_k, reasoning_effort, allowed_openai_params, rpm/tpm — from EVERY rotated alias
    after its first run: a real generation-config drift across past runs, found while verifying
    the F4 provider pin, not by review.

    Callers must now pass the alias's FULL ``litellm_params`` (its registration spec); this
    function overlays the new key on top. ``upstream_model`` alone remains accepted for the
    transitional callers but logs a loud warning, because it reproduces the wipe.
    Raises :class:`GatewayAdminError` on any non-2xx (D4: fail closed, never silently keep
    serving the previous run's key).
    """
    if litellm_params is None:
        logger.warning(
            "rotate_model_key(%s): called WITHOUT full litellm_params — this REPLACES the "
            "alias's params with just {model, api_key}, wiping generation params/pins "
            "(measured). Pass the registration spec's litellm_params.",
            alias,
        )
        params: dict[str, Any] = {"model": upstream_model}
    else:
        params = dict(litellm_params)
        params["model"] = upstream_model
    params["api_key"] = new_api_key
    _post(
        base_url,
        master_key,
        "/model/update",
        {
            "model_name": alias,
            "litellm_params": params,
            "model_info": {"id": model_id},
        },
    )
    logger.info("rotated gateway db-model %s (id=%s) onto a new upstream key", alias, model_id)


def generate_key(
    base_url: str,
    master_key: str,
    *,
    key_alias: str,
    models: list[str],
    max_budget: float | None,
    rpm_limit: int | None = None,
    tpm_limit: int | None = None,
    metadata: dict[str, object] | None = None,
) -> tuple[str, str]:
    """Mint a per-run LiteLLM virtual key.  Returns ``(raw_key, litellm_key_id)``.

    ``litellm_key_id`` is LiteLLM's own non-secret ``token`` field (the SHA-256
    hash of the key it uses as its own DB primary key) — safe to persist;
    ``raw_key`` is the caller's responsibility to route to the task's
    ``containerOverrides`` and NEVER to a log/fixture/commit/config_snapshot
    (rule 3).  ``key_alias`` (set to the run_id by the caller) is the durable
    handle :func:`delete_key` revokes by, independent of which id field a
    given LiteLLM version happens to return.

    ``max_budget=None`` mints the key with NO LiteLLM spend ceiling (the field
    is omitted, which LiteLLM treats as unlimited).  Owner decision 2026-09-06:
    LiteLLM prices this account's models from its own table — v1.99.1 charges
    MiniMax cache reads at 5x the real rate — so its counter hit a $10 cap at
    $2.90 of provider-billed spend and killed the run's tail (codex run
    01788653487361028986-1de1c022).  The per-run OpenRouter key carries the
    same dollar limit and is billed at the real price; that is the cap.
    """
    body: dict[str, Any] = {
        "key_alias": key_alias,
        "models": models,
    }
    if max_budget is not None:
        body["max_budget"] = max_budget
    if rpm_limit is not None:
        body["rpm_limit"] = rpm_limit
    if tpm_limit is not None:
        body["tpm_limit"] = tpm_limit
    if metadata:
        body["metadata"] = metadata
    resp = _post(base_url, master_key, "/key/generate", body)
    raw_key = resp.get("key")
    key_id = resp.get("token") or resp.get("key_name") or key_alias
    if not raw_key:
        raise GatewayAdminError(f"/key/generate for {key_alias!r} returned no key: {resp!r}")
    logger.info("minted LiteLLM virtual key for %s (id=%s)", key_alias, key_id)
    return str(raw_key), str(key_id)


def block_key(base_url: str, master_key: str, key_id: str) -> None:
    """Block *key_id* (LiteLLM's ``token`` — ``runs.litellm_key_id``), idempotent.

    BUILDER4-GATEWAY-PAUSE-KEY-BLOCK-DESIGN-2026-08-31.md §1: a blocked key's
    calls fail closed with a stable, distinguishable 401 (``type: auth_error``,
    ``"Key is blocked"`` in the message) rather than a generic model failure —
    non-destructive and reversible via :func:`unblock_key`.  Verified live
    against a real ``litellm:main-stable`` container.  A key that's already
    blocked (or already gone) is NOT an error — pause must be safe to retry,
    same discipline as :func:`delete_key`.
    """
    try:
        _post(base_url, master_key, "/key/block", {"key": key_id})
    except GatewayAdminError as exc:
        logger.warning("LiteLLM key block for %s: %s (treating as already-blocked)", key_id, exc)
        return
    logger.info("blocked LiteLLM virtual key %s", key_id)


def unblock_key(base_url: str, master_key: str, key_id: str) -> None:
    """Unblock *key_id* (idempotent) — the same raw key works again immediately.

    A key that's already unblocked (or already gone) is NOT an error — resume
    must be safe to retry, same discipline as :func:`delete_key`.
    """
    try:
        _post(base_url, master_key, "/key/unblock", {"key": key_id})
    except GatewayAdminError as exc:
        logger.warning(
            "LiteLLM key unblock for %s: %s (treating as already-unblocked)", key_id, exc
        )
        return
    logger.info("unblocked LiteLLM virtual key %s", key_id)


def delete_key(base_url: str, master_key: str, key_alias: str) -> None:
    """Revoke the LiteLLM virtual key aliased *key_alias* (idempotent).

    A key that no longer exists (already deleted, or minting failed before it
    was created) is NOT an error here — finalisation must be safe to retry.
    """
    try:
        _post(base_url, master_key, "/key/delete", {"key_aliases": [key_alias]})
    except GatewayAdminError as exc:
        logger.warning("LiteLLM key delete for %s: %s (treating as already-gone)", key_alias, exc)
        return
    logger.info("revoked LiteLLM virtual key %s", key_alias)


# --- rotate -> served handoff -------------------------------------------------------------

# Every gateway replica except the one that handled /model/update re-reads the DB on
# `proxy_config_reload_interval_seconds` (litellm_config.yaml: 5 s; LiteLLM's default is 30 s
# and v1.99.1 has no sync endpoint — verified in the image). The wait below therefore demands
# a run of 200s spanning at least twice that interval, so both replicas have provably served
# the rotated key before the caller's first real call.
_REPLICA_RELOAD_INTERVAL_S = 5.0
_SERVED_MIN_SPAN_S = 2 * _REPLICA_RELOAD_INTERVAL_S
_SERVED_SPACING_S = 1.0


def await_alias_served(
    base_url: str,
    bearer: str,
    alias: str,
    *,
    min_span_s: float = _SERVED_MIN_SPAN_S,
    spacing_s: float = _SERVED_SPACING_S,
    timeout_s: float = 120.0,
    what: str = "rotated key",
) -> int:
    """Block until EVERY gateway replica serves *alias* on its rotated upstream key.

    Bring-up 2026-09-03, found live twice within ten minutes: ``/model/update`` writes the new
    key to the gateway's DB, but each of the N gateway replicas keeps its cached router until
    its own periodic reload, and the ALB round-robins requests across them. The old checks
    returned on the FIRST non-401 (judge) / first 200 (discovery) — which proves ONE replica.
    The judge then 401'd "User not found" on the other replica three seconds later (judge task
    judge-01788481792836629782); the discovery probe's single 401 put that replica's
    deployment into LiteLLM's (since disabled) cooldown and its first burst read eight
    synthetic 429s as provider overload.

    So: one 1-token call per *spacing_s*; the streak counts 200s and is RESET by any other
    status (401 = stale replica; 429 = pressure, not ready; anything else = not proven) or a
    transport error. The streak must span *min_span_s* (10 s = twice the replica reload
    interval — the owner's number) before the alias counts as served everywhere. Raises
    ``RuntimeError`` at *timeout_s*. Returns the number of calls made (for logs/tests).
    """
    import time as _time

    started = _time.monotonic()
    deadline = started + timeout_s
    streak_started: float | None = None
    calls = 0
    last_status: int | str = "none"
    while True:
        calls += 1
        try:
            resp = httpx.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {bearer}"},
                json={
                    "model": alias,
                    "messages": [{"role": "user", "content": "ready-check"}],
                    "max_tokens": 1,
                },
                timeout=30.0,
            )
            last_status = resp.status_code
            if resp.status_code == 200:
                streak_started = streak_started if streak_started is not None else _time.monotonic()
            else:
                streak_started = None
        except httpx.HTTPError as exc:  # gateway blip — not evidence, restart the streak
            last_status = f"transport:{exc.__class__.__name__}"
            streak_started = None
        now = _time.monotonic()
        if streak_started is not None and now - streak_started >= min_span_s:
            logger.info(
                "%s for %s served by every replica: %d probe(s), %.1fs",
                what,
                alias,
                calls,
                now - started,
            )
            return calls
        if now >= deadline:
            raise RuntimeError(
                f"{what} for {alias!r} still not served by every replica after "
                f"{timeout_s:.0f}s ({calls} probes, last status {last_status}) — gateway "
                f"replicas did not pick up /model/update"
            )
        _time.sleep(spacing_s)
