"""Shared task framing for the unframed harness adapters (R7, owner decision MADE).

A SWE-bench `problem_statement` is a GitHub issue description ("It would be nice
to optionally preserve the dtypes…").  Handed that text alone, an agent may
reasonably explore and summarise rather than edit — which is exactly what the
battery measured: the harnesses that produced patches (mini_swe_agent,
custom_minimal) are the ones that were TOLD to edit code; claude_code/codex/
opencode got a bare issue and produced nothing.

The owner decided (2026-08-24, R7): apply this framing to ALL THREE unframed
harnesses (claude_code, codex, opencode); custom_minimal and mini_swe_agent must
NOT be double-framed (both already have their own framing).  One shared module,
NOT per-adapter copies — three copies of a prompt string drift, and a harness
comparison where the framings differ by a word nobody noticed is worse than one
where they are all missing.

The framing lives in the USER message (prompt-prepend), matching what already
works: mini's framing is its `instance_template`, i.e. the user message.

Do NOT use AGENTS.md: codex and opencode read it from the working directory
(/testbed = the repo), a file there appears in `git status` and could land in
the extracted patch, contaminating the artifact we grade.

2026-09-08 (owner, after run 6's cross-harness trajectory scan): mini's own
framing (its default `instance_template`) tells the agent to edit code but
never names the directory — 3 of 10 sampled mini-swe instances opened with
`find /workspace` and a wasted "No such file" before recovering. custom_minimal
already states the absolute path in its second system message (K-1). So mini
gets ONLY the location line below (`located()`), not the full TASK_FRAMING —
R7's "no double framing" still holds; this is the one sentence its framing lacks.
"""

from __future__ import annotations

TASK_FRAMING = """The repository is checked out at /testbed and is your working
directory. Fix the issue described below by editing the source code, then verify
your fix by running the relevant tests.

Do not modify test files. When you are done, stop.

The model you are running as is text-only. Never open, read, view or attach
images or other media (PNG, JPG, SVG renders, PDFs, screenshots): a request that
carries an image is rejected by the model provider and ends your session. Verify
visual output by reading the source, checking numeric values, or running tests.

Issue:
"""


def framed(problem_statement: str) -> str:
    """Prepend the shared task framing to the bare problem statement."""
    return TASK_FRAMING + problem_statement


# Exactly TASK_FRAMING's first sentence — the four harnesses that state a
# location state it identically. Deliberately NO "do not cd" clause (owner,
# 2026-09-08): that could deter legitimate `cd` into subdirectories.
REPO_LOCATION = "The repository is checked out at /testbed and is your working directory."


def located(problem_statement: str) -> str:
    """Prepend ONLY the repository-location line (mini-swe: its own template
    already frames the task; it just never says where the repo is)."""
    return REPO_LOCATION + "\n\n" + problem_statement


# 2026-09-09 (efficiency prompt arm): per-run operator instructions. Unlike TASK_FRAMING
# they vary per run and are recorded on it (runs.config_snapshot.harness_instructions);
# run_launch publishes them to Redis and the harness WORKER appends them to the problem
# statement it hands every adapter (harness_worker._with_run_instructions) — so they land
# AFTER the issue text, after whatever framing the adapter adds, for every harness alike.
# The dispatcher's job payload cannot carry them: on the deployed path the worker rebuilds
# the statement from the dataset mirror row (ADR-0032) and never reads the payload's.
INSTRUCTIONS_HEADING = "## Working rules (framework-provided)"


def with_harness_instructions(problem_statement: str, instructions: str | None) -> str:
    """The problem statement, then the instructions under the fixed heading. ``None`` or
    blank returns the statement unchanged — a run without instructions is byte-identical to
    one launched before this existed."""
    text = (instructions or "").strip()
    if not text:
        return problem_statement
    return f"{problem_statement.rstrip()}\n\n{INSTRUCTIONS_HEADING}\n{text}\n"
