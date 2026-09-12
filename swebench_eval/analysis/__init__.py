"""Offline analysis — Pass A (deterministic leak detection) and Pass B (LLM judge).

offline-analysis-design.md. Runs outside the harness subnets, on the
orchestrator/ops side (§5) — never on the harness image, never touching the
in-flight run's control plane (§10.4).
"""

from __future__ import annotations
