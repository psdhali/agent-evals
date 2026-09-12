"""Defensive coercion for subprocess-CLI event parsing (STEP 5.2, review 2026-08-26).

The three native-CLI harnesses (claude_code / opencode / codex) parse JSON event
streams from their subprocess stdout.  ``or {}`` guards against ``None`` and
missing keys but NOT a wrong type: a non-empty string is truthy, so
``ev.get("usage", {})`` returns a STRING when the CLI (or a provider) emits a
string where a dict belongs, and the next ``.get(...)`` raises AttributeError;
``int(u.get("input_tokens", 0))`` raises TypeError on a ``null`` and sits in the
COST path.  e3efd51's ``tool_use_result`` — a plain string, not
``{stdout, stderr}`` — is the exact shape that got through ``or {}``.

Each helper coerces to the safe empty value on a type mismatch and logs ONCE.
Coercion PRESERVES the record (the field is read as empty), whereas try/except
alone would drop the whole trajectory/record — the difference 5.2 exists to
make.  ``label`` names the field in the warning so a bad shape is debuggable
without scrolling the raw event.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def as_dict(value: Any, label: str = "value") -> dict[str, Any]:
    """Return *value* if it is a dict, else ``{}`` (logging once).

    Wrong types guarded: ``or {}`` only handles ``None``/missing; a truthy
    non-dict (e.g. a string) sails through and the next ``.get`` raises.
    """
    if isinstance(value, dict):
        return value
    if value is not None:
        logger.warning(
            "coerce: %s is %s, not dict — treating as empty", label, type(value).__name__
        )
    return {}


def as_list(value: Any, label: str = "value") -> list[Any]:
    """Return *value* if it is a list, else ``[]`` (logging once)."""
    if isinstance(value, list):
        return value
    if value is not None:
        logger.warning(
            "coerce: %s is %s, not list — treating as empty", label, type(value).__name__
        )
    return []


def as_int(value: Any, label: str = "value") -> int:
    """Return *value* as an int; a null/string/float is coerced, anything else
    (including a malformed string) is 0 — logging once.  The token/cost path
    must never raise ``TypeError``/``ValueError`` on a provider's odd usage.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            pass
    logger.warning("coerce: %s is %r — treating as 0", label, value)
    return 0
