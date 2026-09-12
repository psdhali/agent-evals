"""ADR-0038 — contamination (memorization) detection per instance.

At ``base_commit`` the gold test patch is NOT applied, so tests the gold patch
introduces do not exist in the repo the agent sees.  An agent that names one
knew a test it could not have read.  Post-ADR-0033 the network channel is gone,
so a hit on such a name now means memorization.

Two passes (ADR-0038 §2):

1. **Static, offline, per-dataset**: which ``FAIL_TO_PASS`` node IDs are absent
   at ``base_commit`` — a property of the dataset, committed once as a checked-in
   artifact (``data/contamination/leak_detectable.json``).  A run where no
   instance is leak-detectable cannot claim to be clean — it can only claim
   nothing was detectable.
2. **Per-attempt string search**: search each attempt's patch + trajectory for
   that instance's absent node IDs.  The non-empty result is ``leaked_node_ids``
   — evidence for review, never an automatic disqualification (§5).

``leaked_node_ids`` is necessarily a best-effort disclosure: a clean result is
evidence of no *detected* contamination, never proof of none.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from swebench_eval.dataset.base import Instance

logger = logging.getLogger(__name__)

# The committed per-dataset artifact.  It is GOLD-DERIVED and must NEVER be
# copied into any harness image or reach the harness tier — the only consumer is
# scripts/backfill_leak_detection.py, an OFFLINE orchestrator/build pass
# (B5/ADR-0038 P-1).  Do not "help" by COPYing data/ into a Dockerfile: that is
# B1's leak, re-opened (a harness agent with shell in /app could read its own
# absent-test answer key).
# V3 (switch-to-swebench-verified §5): renamed dataset-agnostic (was
# leak_detectable_lite.json) — the per-dataset build step (ADR-0038 §2) is what
# populates it, and it must not falsely imply the Lite split is the only one.
_DEFAULT_ARTIFACT = (
    Path(__file__).resolve().parents[2] / "data" / "contamination" / "leak_detectable.json"
)


def _strip_parametrized_suffix(node_id: str) -> str:
    """Drop a pytest parametrized ``[...]`` suffix from a test name.

    astropy's ids are parametrized (``test_separable[compound_model6-result6]``);
    an agent naming that test would write ``test_separable``.  Without stripping,
    the search normalises to the bracket-bearing form and can never match (P-4).
    The coarser match is accepted — ADR-0038 §5 frames any hit as evidence for
    review, not a disqualification, so a slightly-broader match is the honest
    side here.
    """
    return node_id.split("[", 1)[0].strip()


def _normalize_node_id(node_id: str) -> str:
    """The bare test identifier, so a search matches how an agent spells it.

    A FAIL_TO_PASS node id arrives in one of the forms the datasets use:
      - pytest node id        ``repo/module.py::test_name``
      - parametrized node id  ``...::test_name[param]``
      - unittest / django id  ``test_name (module.Class)``
    All three reduce to the bare ``test_name``; an agent names that, not the
    path or the class.  We search for it, and keep the match least
    false-positive-prone by also requiring identifier boundaries in the
    detector (see detect_leaked_node_ids).
    """
    s = _strip_parametrized_suffix(node_id)
    # unittest form: everything after the first ' (' is the class — drop it.
    if s.startswith("test_") and " (" in s:
        s = s.split(" (", 1)[0]
    return s.split("::")[-1].strip()


def detect_leaked_node_ids(text: str, absent_node_ids: list[str] | None) -> list[str]:
    """Return the absent node ids whose normalized form appears in *text*.

    ``text`` is an attempt's patch + trajectory (concatenated).  A node id that
    is a prefix of another's normalized form could collide — so also require the
    character after the match to be a non-identifier, avoiding ``test_foo``
    matching inside ``test_foobar``.
    """
    if not text:
        return []
    hits: set[str] = set()
    for node_id in absent_node_ids or []:
        norm = _normalize_node_id(node_id)
        if not norm or len(norm) < 3:  # too short to be a discriminating test name
            continue
        idx = 0
        while True:
            idx = text.find(norm, idx)
            if idx == -1:
                break
            end = idx + len(norm)
            # require a non-identifier boundary after (and a word-ish boundary
            # before) so `test_foo` does not match inside `test_foobar`.
            after = text[end : end + 1] if end < len(text) else ""
            before = text[idx - 1 : idx] if idx > 0 else ""
            if (not after or not (after.isalnum() or after == "_")) and (
                not before or not (before.isalnum() or before == "_")
            ):
                hits.add(node_id)
                break
            idx = end
    return sorted(hits)


def load_leak_detectable(path: str | os.PathLike[str] | None = None) -> dict[str, list[str]]:
    """Load the static leak-detectable artifact: instance_id -> absent node ids.

    Returns {} when the artifact is absent (e.g. off-image / tests) — the
    worker then records no leak detection, which is honest only if a consumer
    ALSO surfaces that the dataset was not leak-detectable.  Callers that need
    the claim surface ``None`` (unknown) separately.
    """
    p = Path(path) if path else _DEFAULT_ARTIFACT
    try:
        if not p.exists():
            return {}
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("leak: could not load leak-detectable artifact %s", p, exc_info=True)
        return {}


def leak_map_version(path: str | os.PathLike[str] | None = None) -> str:
    """A short, deterministic identifier for the leak-detectable artifact's content.

    Recorded on every scanned row (``instance_results.leak_map_version``, §2.3 /
    offline-analysis-design.md §9 DoD #3) so a scan against a stale or absent map
    is visible in the data rather than indistinguishable from a scan against the
    current one — the same discipline as ``judge_results.rubric_sha256``.

    ``"absent"`` when the file does not exist (never a fabricated hash of nothing —
    a caller checking for staleness must be able to tell "no map was on disk" from
    "some map was on disk").
    """
    p = Path(path) if path else _DEFAULT_ARTIFACT
    try:
        if not p.exists():
            return "absent"
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        return f"sha256:{digest[:16]}"
    except Exception:
        logger.warning("leak: could not hash leak-detectable artifact %s", p, exc_info=True)
        return "unreadable"


def parse_fail_to_pass(fail_to_pass: str) -> list[str]:
    """Split a FAIL_TO_PASS value into its node id list.

    The value may be newline-separated (SWE-bench's ``FAIL_TO_PASS`` column) or a
    JSON array string (analyst-normalised loads, e.g. from HuggingFace where the
    loader stores the raw ``["...", "..."]`` string).  Accept both so the
    generator and the detector agree on one id set.
    """
    s = fail_to_pass.strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            import json as _json

            parsed = _json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if x]
        except _json.JSONDecodeError:
            pass
    return [ln.strip() for ln in s.splitlines() if ln.strip()]


def _added_test_node_ids(test_patch: str) -> set[str]:
    """Parse the added test definitions a gold ``test_patch`` introduces.

    test_patch is the delta the SWE-bench test patch applies, so a test the
    patch ADDS (its ``def test_...`` / ``class Test...`` on a ``+`` line) was
    NOT present at base_commit — that is exactly the leak-detectable signal,
    with no checkout/install (brief §1.2).

    Only ``+`` (added) lines count; context/removed lines do not.  Matches the
    two shapes SWE-bench test patches use:
      - pytest: ``def test_separable(...)`` / ``async def test_foo(...)``
      - unittest: ``def test_abs(self):`` (test_patch for django etc.)
    The returned set holds the bare test/class names.
    """
    import re

    added: set[str] = set()
    for line in test_patch.splitlines():
        if not line.startswith("+"):
            continue
        body = line[1:].lstrip()
        # pytest: def test_x ... or class TestX ...
        m = re.match(r"(?:async\s+)?def\s+(test_\w+)", body) or re.match(r"class\s+(Test\w+)", body)
        if m:
            added.add(m.group(1))
    return added


def leak_detectable_via_test_patch(test_patch: str, fail_to_pass: str) -> list[str] | None:
    """Which FAIL_TO_PASS ids are absent at base, parsed from the gold test_patch.

    Returns the absent ids (leak-detectable), or ``None`` for UNKNOWN (a format
    mismatch we refuse to guess about, P-3).

    An id is absent if its bare test name appears among the test definitions the
    patch ADDS.  If the patch adds at least one test but NONE of the FAIL_TO_PASS
    ids match any added name, that is a node-id FORMAT mismatch, not a
    hundred-percent-absent repo — treat as UNKNOWN and omit (never claim "all
    absent", which would make the published honesty claim look stronger than it
    is).  If the patch adds no tests, nothing is absent (all FAIL_TO_PASS exist
    at base → leak_detectable=False).
    """
    fail_ids = parse_fail_to_pass(fail_to_pass)
    if not fail_ids:
        return []
    added = _added_test_node_ids(test_patch)
    if not added:
        return []  # patch added no tests -> none of FAIL_TO_PASS is absent
    absent: list[str] = []
    matched_any = False
    for node_id in fail_ids:
        # A FAIL_TO_PASS id maps to a bare name via the same suffix-strip the
        # detector uses.
        bare = _normalize_node_id(node_id)
        if bare in added:
            absent.append(node_id)
            matched_any = True
    if not matched_any:
        # Added tests exist but no FAIL_TO_PASS id names them -> format mismatch.
        return None
    return sorted(absent)


def compute_leak_detectable(
    instances: list[Instance], node_ids_at_base: Any
) -> dict[str, list[str]]:
    """Offline dataset pass: which FAIL_TO_PASS node ids are absent at base_commit.

    ``node_ids_at_base`` is a callable ``(repo, base_commit) -> set[str]`` of the
    test node ids present in the checked-out repo at that commit (supplied by
    the dataset-build tool that has the repo).  Runs once per dataset and the
    result is committed — never recomputed per run (ADR-0038 §2).
    """
    out: dict[str, list[str]] = {}
    for inst in instances:
        present = node_ids_at_base(inst.repo, inst.base_commit) or set()
        absent = [n for n in parse_fail_to_pass(inst.fail_to_pass) if n not in present]
        out[inst.instance_id] = sorted(absent)
    return out
