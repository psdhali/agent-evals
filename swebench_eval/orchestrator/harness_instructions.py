"""Per-run harness instructions — the "prompt arm" of the efficiency work (2026-09-09).

The Laguna opencode pilot (dev/LAGUNA-PILOT-EFFICIENCY-REPORT-2026-09-09.md) showed 42% of
spend after the last edit, 809 re-reads of unchanged files, 733 hand-rolled Django settings
scripts and 124 wrong test-runner invocations. Before any enforced guard (the ADR-0019 shim,
Phase B) the owner wants to measure what plain INSTRUCTIONS buy: the same rules, stated once,
that the model may or may not follow.

Mechanism (corrected 2026-09-09 after smoke run f31723b4 showed the first version never
reached the model): run_launch publishes the text to Redis under the run id
(``redis_client.write_harness_instructions``); the harness WORKER reads it once per job and
appends it to the problem statement it hands every adapter
(``harnesses.task_framing.with_harness_instructions``), so it lands after the issue text under
a fixed heading. Appending at dispatch does NOT work on the deployed path: the worker
rebuilds the statement from the dataset mirror row (ADR-0032) and never reads the job
payload's. It is recorded in ``runs.config_snapshot`` (``harness_instructions``) and echoed by
the API's launch limits, so a run with instructions is never mistaken for a plain one. This is
advice, not a limit: the methodology page must report it as a harness-configuration change,
not under limits-enforced. Restart and open-run recovery re-publish from the snapshot.

The presets are starting points the launch screen can load; the operator may edit the text.
"""

from __future__ import annotations

from typing import TypedDict

from swebench_eval.harnesses.task_framing import (  # noqa: F401 — re-exported
    INSTRUCTIONS_HEADING,
    with_harness_instructions,
)

HARNESS_INSTRUCTIONS_MAX_CHARS = 4000


class InstructionPreset(TypedDict):
    id: str
    name: str
    harness: str  # the harness the wording was written against ("" = any)
    text: str


# Reconciled from the pilot's ledger analysis, three trajectory-reading passes and the
# rubric-v3 judge causes (repeated_reads 23%, redundant_test_runs 23%, probing_without_search
# 23%, unbounded_file_reads 12%, looping 11%). Written for opencode's tool names (grep, glob,
# read with offset/limit, bash) and the Django/astropy mix of SWE-bench Verified.
OPENCODE_LAGUNA_EFFICIENCY_V1 = """\
1. Locate, then read windows. Find the code with grep or glob using a specific path or \
pattern, then read with offset and limit (at most 200 lines per read). Never read a whole \
file. Never re-read a file or range you have not edited since you last read it: what you \
read is still in your context.
2. Bound your outputs. Grep and glob inside a specific directory, not the whole repo; do not \
list large directories; when a command prints more than a screen, pipe it through tail -40.
3. Run tests the repo's way, once per change. Django: from /testbed run \
`python tests/runtests.py <app.module.Class.test>` (labels without a `tests.` prefix; no \
pytest, no scratch scripts that call settings.configure()). Other repos: \
`pytest <path/to/test_file.py> -k <name> -x -q`. Run a test once per code change; never \
re-run a test that already passed with no code change in between.
4. Verify once, then stop. After your edit, run the test module that covers it once. If it \
passes, run at most one broader module, then stop: do not write scripts to re-prove a fix \
that has passed, do not re-run suites, do not summarise your work.
5. Do not loop. If the same action has produced the same result twice, change approach or \
stop. Keep reasoning short and proportional to the decision in front of you."""

# v2 (2026-09-09, owner-approved): only rule 4 changes. v1's "run the test module that covers
# it" assumed a covering test exists; on django-10880 (run 1a4fafd7) none did, so the rule had
# nothing to bind to and the model wrote eleven scratch reproductions, ran 31 test commands and
# spent 70 of 81 calls after its single edit. A self-written reproduction is fine — writing it
# more than once is the waste — so v2 says so and names what "stop" excludes. v1 is kept above
# as the record of the wording that run used; the 78-instance experiment runs v2.
_RULE_4_V1 = OPENCODE_LAGUNA_EFFICIENCY_V1[
    OPENCODE_LAGUNA_EFFICIENCY_V1.index("4. Verify once") : OPENCODE_LAGUNA_EFFICIENCY_V1.index(
        "5. Do not loop"
    )
]
_RULE_4_V2 = """\
4. Verify once, then stop. After your edit, run the existing test module that covers the \
change once. If no existing test covers it, write ONE reproduction (preferably a test file \
run through the project's runner), run it once after the fix, then stop; do not rewrite or \
extend it more than once. If it passes, run at most one broader module. Then stop: do not \
re-run suites, do not re-read the file you edited, do not write further scripts to re-prove \
a fix that has passed, do not summarise your work.
"""
OPENCODE_LAGUNA_EFFICIENCY_V2 = OPENCODE_LAGUNA_EFFICIENCY_V1.replace(_RULE_4_V1, _RULE_4_V2)

# Per-harness v2 presets (owner-asked 2026-09-09, for the "every harness × MiniMax × 78, plain vs
# rules" arms). Rules 3–5 are harness-neutral and BYTE-IDENTICAL to opencode's v2 (sliced from
# it, so the arms stay comparable); rules 1–2 name each harness's real tools — the opencode
# wording (grep/glob/read offset+limit) means nothing to a shell-only agent. Tool surfaces were
# read from stored trajectories of the MiniMax 500 runs: claude_code = Grep/Glob/Read(offset,
# limit)/Bash/Edit (+ Agent sub-agents); codex = one `bash` tool; mini_swe_agent = exactly one
# bash command per turn, finishes with `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`;
# custom_minimal = `bash` + `str_replace_editor` (view with view_range / str_replace / insert).
RULES_3_TO_5_V2 = OPENCODE_LAGUNA_EFFICIENCY_V2[
    OPENCODE_LAGUNA_EFFICIENCY_V2.index("3. Run tests") :
]

_CLAUDE_CODE_RULES_1_2 = """\
1. Locate, then read windows. Find the code with Grep or Glob using a specific path or \
pattern, then Read with offset and limit (at most 200 lines per Read). Never Read a whole \
file. Never re-read a file or range you have not edited since you last read it: what you \
read is still in your context. Do not spawn sub-agents to explore; one Grep is cheaper than \
a delegated search.
2. Bound your outputs. Grep and Glob inside a specific directory, not the whole repo; do not \
list large directories; when a Bash command prints more than a screen, pipe it through \
tail -40.
"""

_CODEX_RULES_1_2 = """\
1. Locate, then read windows. Find the code with `rg -n PATTERN DIR` or `grep -rn PATTERN DIR` \
restricted to a specific directory, then read with `sed -n 'START,ENDp' FILE` (at most 200 \
lines per read). Never `cat` a whole file. Never re-read a file or range you have not edited \
since you last read it: what you read is still in your context.
2. Bound your outputs. Search inside a specific directory, not the whole repo; never `ls -R` \
or `find` from the repo root; when a command prints more than a screen, pipe it through \
`tail -40`. One command per step, doing one thing.
"""

_MINI_RULES_1_2 = """\
1. Locate, then read windows. Find the code with `grep -rn PATTERN DIR` restricted to a \
specific directory, then read with `nl -ba FILE | sed -n 'START,ENDp'` (at most 200 lines per \
command). Never `cat` a whole file. Never re-read a file or range you have not edited since \
you last read it: earlier output is still in your context.
2. Bound your outputs. Every command's output stays in your context for the rest of the task: \
search inside a specific directory; never `ls -R` or `find` from the repo root; never print a \
whole test log; pipe anything longer than a screen through `tail -40`. Make an edit with one \
command (`sed -i`, a heredoc, or a `python - <<'EOF'` script), not by printing the file first.
"""

# mini's OWN instance template follows the task text (so it comes AFTER these rules): its
# "Recommended Workflow" says write a reproduction script, re-run it, then "test edge cases",
# and its "Useful command examples" show `nl -ba f | sed -n '10,20p'` / `sed -i` / heredocs
# (2026-09-09, read from a stored native trajectory). Rules 1-2 reuse those idioms so the
# model sees one recipe; rule 6 has to say out loud that rule 4 replaces workflow steps 2/4/5,
# or the later, more specific "test edge cases" wins. The submit command matches theirs.
_MINI_RULE_6 = """
6. Rule 4 replaces steps 2, 4 and 5 of the "Recommended Workflow" below: one reproduction, \
run once after the fix, no edge-case sweep. Then finish by submitting: your next command is \
`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` on its own — nothing in between."""

_CUSTOM_MINIMAL_RULES_1_2 = """\
1. Locate, then read windows. Find the code with `grep -rn PATTERN DIR` in bash restricted to \
a specific directory, then `str_replace_editor view` with a `view_range` (at most 200 lines \
per view). Never view a whole file. Never re-view a file or range you have not edited since \
you last viewed it: what you saw is still in your context.
2. Bound your outputs. Search inside a specific directory, not the whole repo; never `ls -R` \
or `find` from the repo root; when a bash command prints more than a screen, pipe it through \
`tail -40`. Edit with `str_replace` or `insert`, never by re-creating a file.
"""

CLAUDE_CODE_EFFICIENCY_V2 = _CLAUDE_CODE_RULES_1_2 + RULES_3_TO_5_V2
CODEX_EFFICIENCY_V2 = _CODEX_RULES_1_2 + RULES_3_TO_5_V2
MINI_SWE_AGENT_EFFICIENCY_V2 = _MINI_RULES_1_2 + RULES_3_TO_5_V2 + _MINI_RULE_6
CUSTOM_MINIMAL_EFFICIENCY_V2 = _CUSTOM_MINIMAL_RULES_1_2 + RULES_3_TO_5_V2

PRESETS: list[InstructionPreset] = [
    {
        "id": "opencode-laguna-efficiency-v2",
        "name": "opencode efficiency rules v2 (2026-09-09; the Laguna/MiniMax 78-run arms)",
        "harness": "opencode",
        "text": OPENCODE_LAGUNA_EFFICIENCY_V2,
    },
    {
        "id": "claude_code-efficiency-v2",
        "name": "claude_code efficiency rules v2 (2026-09-09)",
        "harness": "claude_code",
        "text": CLAUDE_CODE_EFFICIENCY_V2,
    },
    {
        "id": "codex-efficiency-v2",
        "name": "codex efficiency rules v2 (2026-09-09)",
        "harness": "codex",
        "text": CODEX_EFFICIENCY_V2,
    },
    {
        "id": "mini_swe_agent-efficiency-v2",
        "name": "mini_swe_agent efficiency rules v2 (2026-09-09)",
        "harness": "mini_swe_agent",
        "text": MINI_SWE_AGENT_EFFICIENCY_V2,
    },
    {
        "id": "custom_minimal-efficiency-v2",
        "name": "custom_minimal efficiency rules v2 (2026-09-09)",
        "harness": "custom_minimal",
        "text": CUSTOM_MINIMAL_EFFICIENCY_V2,
    },
    {
        "id": "opencode-laguna-efficiency-v1",
        "name": "opencode × Laguna efficiency rules v1 (2026-09-09, as run on django-10880)",
        "harness": "opencode",
        "text": OPENCODE_LAGUNA_EFFICIENCY_V1,
    },
]
