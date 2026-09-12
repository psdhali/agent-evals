"""Claude Code harness adapter.

Wraps the Claude Code CLI as a subprocess.  Claude Code is installed as a CLI
(``~/.local/bin/claude``) — a subprocess tool, never a runtime dependency (P3-2).

Routing (F2 hardest case): Claude Code speaks Anthropic's ``/v1/messages`` wire
format.  We point ``ANTHROPIC_BASE_URL`` at the LiteLLM gateway, which translates
to the configured backend model (qwen3-coder) — so "Claude Code as harness, a
completely different model as backend" works with zero Claude-Code-side changes.

``--output-format stream-json --verbose`` emits a per-event JSON stream we map onto
§9.2's normalized trajectory: ``assistant`` events carry tool_use blocks; the
``result`` event carries final usage.  Falls back to a single-event trajectory if
the stream shape differs at runtime (honest-spike pattern).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from swebench_eval.harnesses.base import (
    HarnessAdapter,
    HarnessInput,
    HarnessOutput,
    TerminatedReason,
    Usage,
    recognized_terminal_reason,
)
from swebench_eval.harnesses.coerce import as_dict, as_int, as_list
from swebench_eval.harnesses.compaction import (
    NATIVE_COMPACTION_MARKERS,  # fallback only — compact_boundary events are authoritative
    OUTPUT_RESERVE,
    compute_threshold,
    count_native_compactions,
)

# claude's requested max_tokens IS the CLI's compaction reserve, so it must equal
# the shared OUTPUT_RESERVE for the AUTO_COMPACT_WINDOW identity to land the
# trigger on compute_threshold(W) (see the export site below).
_CLAUDE_MAX_OUTPUT = OUTPUT_RESERVE
from swebench_eval.harnesses.custom_minimal.trajectory import cum_usage, traj_log
from swebench_eval.harnesses.git_utils import git_diff_or_classify
from swebench_eval.harnesses.proc import run_streaming
from swebench_eval.harnesses.repo_prep import ensure_prepared_repo
from swebench_eval.harnesses.routing import (
    agent_environment,
    gateway_api_key,
    gateway_base_url,
    grant_agent_ownership,
)
from swebench_eval.harnesses.task_framing import framed
from swebench_eval.harnesses.trajectory_reasoning import thinking_block_text

logger = logging.getLogger(__name__)

# B7/E11b (round-review): the tool surface is "native capability, minus network
# tools" — the reviewer proved on django-10924 that a tool ALLOWLIST is not a
# network control (deny WebFetch while granting Bash on an open host = the agent
# reaches the network anyway). A1/ADR-0033 has since removed the network at the
# route layer, so claude_code can be un-crippled: drop the over-tight
# --allowedTools (which also denied Task subagents, Skill, Workflow — we were
# scoring a crippled harness), keep --disallowedTools for the two web tools, and
# grant a permission mode so Bash/Edit/Write are auto-approved (the honest
# successor to the deny-list, safe only BECAUSE no route exists — the reviewer
# explicitly sequenced this AFTER A1).
DISALLOWED_TOOLS = "WebFetch WebSearch"
PERMISSION_MODE = "bypassPermissions"


class ClaudeCodeHarness(HarnessAdapter):
    """Claude Code CLI wrapper implementing the HarnessAdapter protocol."""

    def __init__(
        self,
        claude_bin: str = "claude",
        api_base_url: str | None = None,
        api_key: str | None = None,
        model: str = "claude-code-model",
        disallowed_tools: str = DISALLOWED_TOOLS,
        permission_mode: str = PERMISSION_MODE,
    ) -> None:
        self._claude_bin = claude_bin
        # Claude Code speaks Anthropic's wire format — no /v1 suffix.
        self._api_base_url = (api_base_url or gateway_base_url()).rstrip("/")
        self._api_base_url = self._api_base_url.removesuffix("/v1")
        self._api_key = api_key or gateway_api_key()
        self._model = model
        # B7/E11b: the tool surface is declared once (class attrs) and reflected
        # into the run's config snapshot so the five-harness column compares the
        # harnesses, not our configuration choices.
        self._disallowed_tools = disallowed_tools
        self._permission_mode = permission_mode

    @classmethod
    def tool_surface(cls) -> dict[str, object]:
        """The resolved tool surface this harness runs with (B7/E11b).

        Recorded in each run's config_snapshot so a published RESOLVED number says
        what the agent was allowed to touch. "native minus network" is the policy
        for the CLI harnesses — the CLI's own inventory, minus the web tools; the
        network layer (A1) is the actual control.
        """
        return {
            "tools": "native minus network",
            "disallowed_tools": DISALLOWED_TOOLS,
            "permission_mode": PERMISSION_MODE,
        }

    def run(self, input: HarnessInput) -> HarnessOutput:
        start_time = time.monotonic()
        terminated_reason: TerminatedReason = "completed"
        error_msg = ""
        exit_code = 0

        repo_dir = Path(input.repo_checkout_path)

        # 5b: the worker prepares the repo via SWE-bench's install_repo_script
        # (repo_prep.py) BEFORE the adapter runs.  Assert rather than clone: the
        # script removed origin and pruned future history, so cloning/fetching
        # here would fail (no origin) or silently undo the hardening.
        try:
            ensure_prepared_repo(repo_dir)
        except Exception as exc:  # noqa: BLE001
            return HarnessOutput(
                patch=None,
                success=False,
                trajectory_path="",
                raw_log_path="",
                exit_code=1,
                error=f"repo not prepared: {exc}",
                terminated_reason="crash",
                wall_clock_seconds=time.monotonic() - start_time,
            )

        output_dir = Path(input.output_dir or repo_dir.parent)
        raw_log_path = str(output_dir / "harness_stdout.log")
        trajectory_path = str(output_dir / "trajectory.jsonl")

        env = agent_environment()
        env["ANTHROPIC_BASE_URL"] = self._api_base_url
        env["ANTHROPIC_AUTH_TOKEN"] = self._api_key
        env["ANTHROPIC_MODEL"] = self._model
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        # Root guard fix (2026-08-23, claude-code-root-guard-regression §3): the
        # container runs as root (SWE-bench images do), and Claude Code refuses
        # `bypassPermissions` for root outside a declared sandbox
        # (`isRootOutsideDeliberateSandbox`).  This container IS a deliberate
        # sandbox: network-isolated (ADR-0033), ephemeral, one instance per task.
        # Declaring it keeps B7's tool surface (native minus WebFetch/WebSearch)
        # while lifting the root refusal.  Verified in the real image with this
        # exact perimission-mode -> the CLI starts, bypassPermissions honoured.
        env["IS_SANDBOX"] = "1"

        # B3-native (fix-trajectory-reasoning-export.md §3): Claude Code writes
        # one session JSONL per run.  Without CLAUDE_CONFIG_DIR it lands in
        # $HOME/.claude and dies with the task.  Point it under the job's output
        # dir so the file is per-run, deterministic, and uploaded as the native
        # trajectory (glob projects/*/*.jsonl beneath it).
        # G2 (2026-09-06, owner request): create it here and hand it to the
        # agent user explicitly — the same root-owned-config-dir defect that
        # killed opencode, closed before it can appear here.
        claude_cfg_dir = output_dir / ".claude"
        claude_cfg_dir.mkdir(parents=True, exist_ok=True)
        grant_agent_ownership(claude_cfg_dir)
        env["CLAUDE_CONFIG_DIR"] = str(claude_cfg_dir)

        # Redirect EVERY internal Claude Code model designation to the gateway
        # alias.  Without these, Claude Code's internal fallbacks (small-fast
        # model, agentic subtasks, etc.) reach for real Claude model names that
        # the LiteLLM gateway has no route for — observed as `ProxyModelNotFound
        # Error: ... model=claude-opus-4-7` in the gateway logs.  The alias here
        # resolves via the gateway to the configured backend (qwen).
        for _key in (
            "ANTHROPIC_SMALL_FAST_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_FABLE_MODEL",
        ):
            env[_key] = self._model

        # The backend model is not a Claude model with a known context window;
        # disable Claude's unknown-model enforcement, then tell it the REAL
        # window (BUILD-SPEC §3.1, compaction build 2.5).  CLAUDE_CODE_MAX_
        # CONTEXT_TOKENS replaces the old hardcoded 1000000 — not harmless once
        # the provider is pinned to a 262 K model — and CLAUDE_CODE_AUTO_COMPACT_
        # WINDOW is a TOKEN count (not a fraction) that env beats settings with.
        # Both are omitted when the run resolved no window (None = compaction
        # disabled), so a no-window run stays exactly as before.
        env["CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT"] = "1"
        _window = input.context_window_tokens
        if _window is not None:
            env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(_window)
            # Cross-harness alignment (2026-09-02): claude must compact at the SAME
            # trigger as every other harness — compute_threshold(W) = 235,929 at
            # W=262,144 — for a fair comparison.  The CLI treats
            # AUTO_COMPACT_WINDOW as a WINDOW and subtracts its own output reserve
            # (= the max_tokens it requests) from it to get the trigger.  So to
            # land the trigger exactly on compute_threshold(W), export
            # compute_threshold(W) + that reserve; the CLI subtracts the reserve
            # back off, yielding compute_threshold(W).
            #   History: the ORIGINAL bug exported compute_threshold(W) itself,
            #   double-subtracting the reserve (trigger 196,608, proven live run
            #   01788315793731547650 pre_tokens=197,621).  The first fix exported
            #   the RAW window (trigger 245,760) but that diverged from the other
            #   four harnesses (229,376 then) and left ZERO output margin at the
            #   window edge.  This is the aligned, safer form.
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(
                compute_threshold(_window) + _CLAUDE_MAX_OUTPUT
            )
        # The CLI requests max_tokens on EVERY call (measured 193/193 on run
        # 01788315793731547650) and that request size is what the provider
        # validates against the window, so it IS the compaction reserve — it must
        # equal OUTPUT_RESERVE for the identity above to hold.  16,384 keeps ~2.5x
        # headroom over the worst observed output+thinking (~6.4k, 4,096 thinking
        # included).  setdefault: a task definition can still override.
        env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", str(_CLAUDE_MAX_OUTPUT))

        # B3-root-cause (fix-trajectory-reasoning-export.md §1): request the
        # backend's chain-of-thought.  Without MAX_THINKING_TOKENS Claude Code
        # never asks for thinking on an unknown backend (stderr:
        # `[claude-code:unrecognized_model]`) and the session carries zero
        # thinking blocks — reasoning silently lost even though the gateway
        # returns it.  Proven live: same run, +this env var → thinking blocks
        # appear in both the stream-json stdout and the session file.
        #
        # Fairness: MAX_THINKING_TOKENS maps to the Anthropic thinking budget,
        # and the gateway translates that budget_tokens -> upstream
        # reasoning_effort: 1024=low, 2048=medium, >=4096=high (measured live).
        # The harness param OVERRIDES the gateway's pinned reasoning_effort, so
        # this must be >=4096 for claude to reason at high like the other
        # harnesses (the gateway default) — 1024 would silently reason at low.
        #
        # F2 (compaction build 2.6): setdefault, not hard assignment.  The old
        # `env[...] = "4096"` was a fairness bug in two ways: agent_environment()
        # copies the worker's env and the adapter then OVERWRITES it, so a task
        # definition could never change the thinking budget, AND it maps to the
        # provider's reasoning_effort — exactly the tuning value that must stay
        # settable.  Hard assignment stays for the ISOLATION flags above
        # (IS_SANDBOX, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC), which must NOT
        # be overridable.
        env.setdefault("MAX_THINKING_TOKENS", "4096")

        cmd = [
            self._claude_bin,
            "-p",
            # R7 (owner decision MADE): a bare SWE-bench issue reads as a
            # discussion prompt, not a work order — the battery's claude_code
            # run made 14 Bash calls and ZERO Write/Edit.  Shared framing, in
            # the user prompt (prompt-prepend, the mechanism all three share).
            framed(input.problem_statement),
            "--model",
            self._model,
            # B7/E11b: deny ONLY WebFetch/WebSearch via --disallowedTools and drop
            # the over-tight --allowedTools (which also denied Task/Skill/Workflow —
            # we were scoring a crippled harness). --permission-mode auto-grants
            # Bash/Edit/Write; safe because A1's route table gives the agent no
            # network path (the reviewer sequenced this AFTER A1 for exactly that
            # reason). The tools stay VISIBLE and are refused on call.
            "--disallowedTools",
            self._disallowed_tools,
            "--permission-mode",
            self._permission_mode,
            "--output-format",
            "stream-json",
            "--verbose",
            # R7 + master-handover 3.12: --max-turns is the native belt-and-braces
            # alongside the shim's authoritative turn cap (all five harnesses get
            # the same 500-turn limit from the shim).  Drive it from the job's
            # max_turns_per_instance so one setting moves all; omit when None
            # (deliberately unlimited).
            *(
                ["--max-turns", str(input.max_turns_per_instance)]
                if input.max_turns_per_instance is not None
                else []
            ),
        ]

        # R8.1: live streaming — the shared Popen runner feeds TRAJ events to on_line
        # as they arrive (stream-json emits one JSON event per line) while
        # buffering the same text for the post-hoc parse.
        _events: list[dict[str, Any]] = []
        # GROUP 3d.2 (master-handover 2026-08-24): TRAJ turn must be a MODEL
        # CALL, not an event index.  claude's stream-json emits MANY events per
        # call (system, deltas, tool_use, tool_result, user…), so `turn =
        # len(_events)` ran away — live logs showed turn=23734 against 16 real
        # API calls (1,483× inflation).  Count assistant messages: each is one
        # model response == the same "turn" the shim's turn cap (3.12) counts.
        _turn = 0
        # Compaction-point recording (owner request 2026-09-02): the CLI's
        # stream-json emits {"type":"system","subtype":"compact_boundary",
        # "compact_metadata":{"trigger","pre_tokens","post_tokens",...}} at each
        # compaction.  Capture every one WITH the model-call turn it landed on —
        # "compaction at call 177 of 191, pre 197,621" is the shape results
        # analysis needs (did compaction change the outcome?).  This is also the
        # authoritative counter: the NATIVE_COMPACTION_MARKERS substrings never
        # match this CLI's actual notice (proven on a live run: 1 real
        # compaction, marker count 0).
        _compaction_events: list[dict[str, Any]] = []

        def _on_line(line: str) -> None:
            nonlocal _turn
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            if not isinstance(ev, dict):
                return
            _events.append(ev)
            etype = ev.get("type")
            if etype == "system" and ev.get("subtype") == "compact_boundary":
                meta = ev.get("compact_metadata") or {}
                point = {
                    "at_model_call": _turn,
                    "trigger": meta.get("trigger"),
                    "pre_tokens": meta.get("pre_tokens"),
                    "post_tokens": meta.get("post_tokens"),
                }
                _compaction_events.append(point)
                logger.info("TRAJ %s", traj_log("system", _turn, content=f"COMPACTION {point}"))
            if etype == "user":
                msg = ev.get("message", {})
                content = _content_to_text(msg.get("content"))
                logger.info("TRAJ %s", traj_log("user", _turn, content=content))
            elif etype == "assistant":
                msg = ev.get("message", {})
                content_blocks = msg.get("content") or []
                text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
                tool_calls = [
                    b.get("name", "NONE") for b in content_blocks if b.get("type") == "tool_use"
                ]
                reasoning = "".join(
                    thinking_block_text(b) for b in content_blocks if b.get("type") == "thinking"
                )
                tool_name = tool_calls[0] if tool_calls else "NONE"
                logger.info(
                    "TRAJ %s",
                    traj_log(
                        "assistant",
                        _turn,
                        tool=tool_name,
                        content=text,
                        reasoning_chars=len(reasoning),
                        **cum_usage(input.live_usage),
                    ),
                )
                _turn += 1

        result = run_streaming(
            cmd,
            cwd=repo_dir,
            env=env,
            timeout=input.timeout_seconds,
            on_line=_on_line,
        )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
        if result.timed_out:
            terminated_reason = "timeout"
            error_msg = f"claude wall-clock timeout ({input.timeout_seconds}s)"
            exit_code = 124
        else:
            # R5.1 (M1-reviewed): the terminal reason is in the LAST result
            # event (`stream-json --verbose`), not the exit code.  claude hits
            # --max-turns 50 → subtype "error_max_turns"; today that is
            # recorded HARNESS_CRASH (or — on an exit-0 turn-cap — EMPTY_PATCH).
            #
            # M1 (review): on a ZERO exit we trust ONLY the structured
            # `result.subtype` — never a whole-transcript scan, which would
            # reclassify a clean patch-producing run that merely *mentions*
            # "max turns" (proven: subtype "success" + "so we don't burn max
            # turns" → max_turns_exceeded → FAILED_HARNESS → never graded).  On
            # a NON-zero exit the transcript scan is a safe fallback — worst
            # case crash→max_turns_exceeded, both FAILED_HARNESS, no patch lost.
            recognized = _terminal_subtype_from_result(stdout)
            if result.returncode != 0:
                error_msg = f"claude exited with code {result.returncode}"
                terminated_reason = (
                    recognized or recognized_terminal_reason(stdout + stderr) or "crash"
                )
            elif recognized is not None:
                error_msg = f"claude stopped: {recognized}"
                terminated_reason = recognized

        Path(raw_log_path).write_text(stdout + "\n\n--- STDERR ---\n" + stderr)

        # B3-native (fix-trajectory-reasoning-export.md §3): the run's session
        # JSONL is under CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<uuid>.jsonl.
        # Glob the newest one and surface it so the worker uploads the native
        # trajectory (tool_use/tool_result, parentUuid, compaction markers,
        # thinking blocks) beside the normalised trajectory.
        native_trajectory_path = ""
        _cc_dir = Path(env["CLAUDE_CONFIG_DIR"])
        if _cc_dir.exists():
            sessions = sorted(_cc_dir.glob("projects/*/*.jsonl"), key=lambda p: p.stat().st_mtime)
            if sessions:
                native_trajectory_path = str(sessions[-1])

        # R3-2: classify a patch-extraction timeout here (artifacts already on
        # disk), not let it propagate — that would destroy this attempt's
        # artifacts AND call rows and redeliver a deterministic failure five times.
        #
        # STEP 5.1 (review 2026-08-26): the patch is extracted BEFORE the
        # trajectory is written.  The old order (`_write_trajectory` then patch)
        # meant a crash inside the trajectory writer COST THE PATCH — the whole
        # value of the run (the 159-turn claude_code run was recorded
        # FAILED_HARNESS with patch_path NULL for exactly this reason).  The
        # patch is the deliverable; the trajectory is the log.  Preserve the
        # patch first, log second.
        diff = git_diff_or_classify(repo_dir)
        patch = diff.patch
        success = patch is not None and len(patch) > 0
        if diff.timed_out:
            terminated_reason = "patch_extract_timeout"
            error_msg = "git diff timed out (30s) capturing the patch"
        if patch:
            Path(output_dir / "patch.diff").write_text(patch)

        # Parse the event stream for usage + trajectory.
        usage = Usage()
        events = _events  # built live by the on_line reader (R8.1)
        # STEP 5.1 (review 2026-08-26): a log-append/trajectory failure must not
        # fail the run — the patch is already extracted above.  Best-effort:
        # log a warning, mark trajectory_written False, continue.
        try:
            trajectory_written = _write_trajectory(events, trajectory_path, input.problem_statement)
        except Exception as exc:  # noqa: BLE001
            logger.warning("claude: trajectory write failed, run continues: %s", exc)
            trajectory_written = False
        if events:
            try:
                summary = events[-1]  # result event
                usage = _usage_from_result(summary)
            except Exception as exc:  # noqa: BLE001
                logger.warning("claude: usage parse failed, run continues: %s", exc)

        return HarnessOutput(
            patch=patch,
            success=success,
            trajectory_path=trajectory_path if trajectory_written else "",
            raw_log_path=raw_log_path,
            native_trajectory_path=native_trajectory_path,
            trajectory_parsed=bool(events),
            usage=usage,
            # M0 §1.3 (R2-1): the adapter's OWN parse as the stored cross-check
            # against the shim meter (never added to it).
            adapter_reported_usage=usage,
            # M0 §4.3 / R2-3: the patch-extraction wall time from the shared helper.
            patch_extract_s=diff.patch_extract_s,
            wall_clock_seconds=time.monotonic() - start_time,
            exit_code=exit_code,
            error=error_msg,
            terminated_reason=terminated_reason,
            # Compaction recording rebuilt 2026-09-02: the stream's
            # compact_boundary events are the authoritative count (the D-2
            # marker substrings never match this CLI's real notice — proven
            # live: 1 real compaction, marker count 0).  Markers remain only as
            # a fallback for a CLI build that doesn't emit the event.  None
            # stays reserved for "no output observed at all" (Trap 3).
            compactions_fired=(
                len(_compaction_events)
                if _compaction_events
                else count_native_compactions(
                    stdout + "\n" + stderr, NATIVE_COMPACTION_MARKERS["claude_code"]
                )
            ),
            compaction_tokens_before=(
                _compaction_events[-1].get("pre_tokens") if _compaction_events else None
            ),
            compaction_tokens_after=(
                _compaction_events[-1].get("post_tokens") if _compaction_events else None
            ),
            context_window_tokens=_window,
            compaction_events=_compaction_events or None,
        )


# ---------------------------------------------------------------------------
# Event-stream parsing
# ---------------------------------------------------------------------------


def _terminal_subtype_from_result(stdout: str) -> TerminatedReason | None:
    """R5.1 (M1-reviewed): read the terminal subtype from the LAST ``result`` event.

    Claude Code's ``stream-json --verbose`` stream ends with a ``result`` event
    whose ``subtype`` records HOW the run stopped — ``max_turns_exceeded`` when
    ``--max-turns 50`` is hit, ``success`` on a clean finish, plus
    ``error_during_execution`` / ``auth_error`` / ``shell_command_error`` etc.
    The adapter previously mapped ANY non-zero exit to ``crash``, so a
    turn-capped run was recorded HARNESS_CRASH.

    This reads ONLY the structured ``result.subtype`` — it NEVER text-scans the
    whole transcript.  A clean run that merely *mentions* max turns in its
    reasoning must not be reclassified (M1: a patch-producing run was being
    recorded as a never-graded max-turns failure).  On a ``success`` (or any
    unrecognised subtype) it returns None; the caller keeps its default — crash
    on non-zero, completed on zero.  The caller may text-scan as a fallback
    ONLY on a non-zero exit, where crash→max_turns can lose no patch (both
    FAILED_HARNESS).
    """
    for ev in reversed(_parse_stream_events(stdout)):
        if ev.get("type") != "result":
            continue
        subtype = ev.get("subtype")
        if isinstance(subtype, str) and subtype:
            # The result event's SUBTYPE string is itself a terminal marker —
            # treat the whole string through the shared recognizer so both
            # "max_turns_exceeded" and any variant ("max-turns-exceeded",
            # "max_turns_reached", …) map to the same reason.  A clean
            # "success" maps to None (no marker matches it) and is returned
            # as-is — never fall through to a transcript scan.
            return recognized_terminal_reason(subtype.replace("-", " "))
        # A result event with NO subtype ended the run without a recorded
        # terminal marker — None is the honest answer.
        return None
    # No result event at all (stream cut before the terminal event).
    return None


def _parse_stream_events(stdout: str) -> list[dict[str, Any]]:
    """Parse Claude Code's `stream-json --verbose` stdout into events.

    Returns a list of raw event dicts (assistant / user / result / system).
    Unknown lines are skipped defensively.
    """
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _write_trajectory(
    events: list[dict[str, Any]],
    trajectory_path: str,
    problem_statement: str,
) -> bool:
    """Write normalized `trajectory.jsonl` from the event stream.

    Maps model messages onto §9.2's schema: turn = running index across
    assistant/user events; tool calls flattened from `content` blocks.
    Falls back to a minimal user-then-result trajectory if nothing parses.
    """
    import json as _json
    from datetime import UTC, datetime

    def iso() -> str:
        return datetime.now(UTC).isoformat()

    lines: list[str] = []

    if not events:
        # Minimal fallback: problem statement only, marked unparsed (P4C-3) so a
        # run with zero parseable events is distinguishable from a short healthy one.
        lines.append(
            _json.dumps(
                {
                    "turn": 0,
                    "role": "user",
                    "content": problem_statement,
                    "unparsed": True,
                    "ts": iso(),
                }
            )
        )
        Path(trajectory_path).write_text("\n".join(lines) + "\n")
        return True

    # Turn 0: the user problem.
    lines.append(
        _json.dumps({"turn": 0, "role": "user", "content": problem_statement, "ts": iso()})
    )

    # M2 (review 2026-08-25): Claude Code surfaces tool results as `tool_result`
    # content blocks inside a top-level **`user`** event — it never emits a
    # dedicated `tool_result` event (verified live against the real 2.1.234
    # binary). The tool_result branch below therefore never fires in production.
    # We record the tool_use id -> name mapping from assistant events so a
    # tool_result can be tagged with its REAL tool name (Bash/Read/Edit/…), not
    # a hardcoded "bash".
    tool_name_by_id: dict[str, str] = {}

    for turn, ev in enumerate(events):
        etype = ev.get("type")
        if etype == "user":
            msg = as_dict(ev.get("message"), "claude message")
            # STEP 5.2 (review 2026-08-26): coerce the container with as_list, then
            # drop non-dict ELEMENTS — a content list may carry a bare string (a
            # malformed/odd block), and the branches below call .get on each block.
            # The user branch already guarded with isinstance(b, dict); the
            # assistant branch did not (lines 591/599/612), which raised
            # AttributeError on a string element.  Filter once, at the top.
            content_blocks = [
                b for b in as_list(msg.get("content"), "claude content") if isinstance(b, dict)
            ]
            text_parts = []
            tool_results = []
            for b in content_blocks:
                btype = b.get("type")
                if btype == "text":
                    text_parts.append(str(b.get("text", "")))
                elif btype == "tool_result":
                    tool_results.append(b)
            # M2: emit role:"tool" records for any tool_result blocks — WHAT CAME
            # BACK (output), so an LLM judge sees actions-and-results, not
            # actions-without-results (189 tool_calls, 0 role:tool before this).
            tu_result = ev.get("tool_use_result") or {}
            # M2-b (Stage 6, 2026-08-26): some Claude Code tool events carry
            # `tool_use_result` as a plain STRING (the raw tool output), not the
            # dict shape `{"stdout":…,"stderr":…}`. Treat the string as stdout so
            # the record carries the output instead of crashing with
            # `'str' object has no attribute 'get'` (caught live on scikit-25102
            # at turn 159 → FAILED_HARNESS; the gate exists to catch exactly this).
            if isinstance(tu_result, str):
                tu_result = {"stdout": tu_result, "stderr": ""}
            tu_result = as_dict(tu_result, "claude tool_use_result")
            for b in tool_results:
                tid = b.get("tool_use_id", "")
                _out = b.get("content", "")
                if isinstance(_out, list):
                    _out = " ".join(
                        str(_p.get("text", "")) if isinstance(_p, dict) else str(_p) for _p in _out
                    )
                lines.append(
                    _json.dumps(
                        {
                            "turn": turn,
                            "role": "tool",
                            "tool_call_id": tid,
                            "name": tool_name_by_id.get(tid, ""),
                            "output": str(_out),
                            # M2: keep is_error + the raw stdout/stderr that ride
                            # alongside — an errored tool call must not read as empty.
                            "is_error": b.get("is_error", False),
                            "stdout": tu_result.get("stdout", ""),
                            "stderr": tu_result.get("stderr", ""),
                            "ts": iso(),
                        },
                        ensure_ascii=False,
                    )
                )
            # Emit a user record only when the event actually carried user text; a
            # pure tool_result event (the common case) would otherwise append an
            # empty `{"role":"user","content":""}` record for every turn.
            if text_parts:
                lines.append(
                    _json.dumps(
                        {
                            "turn": turn,
                            "role": "user",
                            "content": "".join(text_parts),
                            "ts": iso(),
                        }
                    )
                )
        elif etype == "assistant":
            msg = as_dict(ev.get("message"), "claude message")
            # STEP 5.2: same element-guard as the user branch above — filter
            # non-dict blocks so the .get on each block below can't raise.
            content_blocks = [
                b for b in as_list(msg.get("content"), "claude content") if isinstance(b, dict)
            ]
            text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
            tool_calls = [
                {
                    "id": b.get("id", ""),
                    "name": b.get("name", ""),
                    "input": b.get("input", {}),
                }
                for b in content_blocks
                if b.get("type") == "tool_use"
            ]
            # M2: remember tool_use id -> real name for the matching tool_result.
            for b in content_blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    tool_name_by_id[b["id"]] = b.get("name", "")
            # Reasoning: Claude Code surfaces the backend's chain-of-thought as
            # `type:"thinking"` content blocks (proven live with
            # MAX_THINKING_TOKENS set).  The old normaliser kept only text
            # blocks and dropped these — the whole reason this harness looked
            # reasoning-less.  Land it on the assistant record's `reasoning`
            # field (the §9.2 convention custom_minimal already writes).
            reasoning = "".join(
                thinking_block_text(b) for b in content_blocks if b.get("type") == "thinking"
            )
            rec = {
                "turn": turn,
                "role": "assistant",
                "content": text,
                "tool_calls": tool_calls,
                "ts": iso(),
            }
            if reasoning:
                rec["reasoning"] = reasoning
            lines.append(_json.dumps(rec, ensure_ascii=False))
        elif etype == "result":
            usage = as_dict(ev.get("usage"), "claude result usage")
            lines.append(
                _json.dumps(
                    {
                        "turn": turn,
                        "role": "result",
                        "usage": usage,
                        "is_error": ev.get("is_error", False),
                        "ts": iso(),
                    },
                    ensure_ascii=False,
                )
            )

    Path(trajectory_path).write_text("\n".join(lines) + "\n")
    return True


def _content_to_text(content: Any) -> str:
    """Extract text from a message content block or a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _usage_from_result(result_ev: dict[str, Any]) -> Usage:
    """Read input/output tokens and cost from the result event."""
    usage = Usage()
    u = as_dict(result_ev.get("usage"), "claude result usage (cost path)")
    usage.input_tokens = as_int(u.get("input_tokens"), "claude input_tokens")
    usage.output_tokens = as_int(u.get("output_tokens"), "claude output_tokens")
    usage.cost_usd = float(as_int(result_ev.get("total_cost_usd"), "claude total_cost_usd"))
    return usage
