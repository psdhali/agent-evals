"""Harness adapter registry — the single name→adapter source of truth.

R4-1 / review P4C-2: the harness list must come from ONE adapter map, never a
hand-written duplicate.  This is that map.  The worker dispatches from it, and
the routing tests and smoke_test's ``--harness`` choices derive their lists from
it — so adding or removing a harness changes exactly this one file, and an
adapter can't be silently omitted from the shim-routing guard.

Construction arguments still differ per adapter (the worker supplies them), but
the SET of harnesses — and which must be metered by the ADR-0019 shim — is
defined here and nowhere else.
"""

from __future__ import annotations

from swebench_eval.harnesses.aider.harness import AiderHarness
from swebench_eval.harnesses.claude_code.harness import ClaudeCodeHarness
from swebench_eval.harnesses.codex.harness import CodexHarness
from swebench_eval.harnesses.custom_minimal import CustomMinimalHarness
from swebench_eval.harnesses.mini_swe_agent.harness import MiniSweAgentHarness
from swebench_eval.harnesses.opencode.harness import OpenCodeHarness

# name → adapter class.
HARNESS_ADAPTERS: dict[str, type] = {
    "custom_minimal": CustomMinimalHarness,
    "aider": AiderHarness,
    "mini_swe_agent": MiniSweAgentHarness,
    "claude_code": ClaudeCodeHarness,
    "codex": CodexHarness,
    "opencode": OpenCodeHarness,
}

# Harnesses that MUST be metered by the ADR-0019 per-worker shim.  Every harness
# in the registry, including custom_minimal (ADR-0037 / M0 §1).  The shim is the
# SINGLE measurement plane across all six so a "better harness" finding is a
# difference in the harnesses, not in their instrumentation.
#
# custom_minimal's own in-loop budget trip (R5-2) is UNCHANGED — that exclusion
# was about ENFORCEMENT, never about measurement.  It now routes its model calls
# through the shim like the others; the two paths are never summed.
SHIM_ROUTED_HARNESSES: frozenset[str] = frozenset(HARNESS_ADAPTERS)
