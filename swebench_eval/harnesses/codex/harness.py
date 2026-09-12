"""Codex CLI harness adapter.

Wraps the Codex CLI (`codex exec`) as a subprocess.  Codex is installed via
`npm install -g @openai/codex` — a subprocess tool, never a runtime dependency
(P3-2).

Routing: Codex speaks the OpenAI Responses-API wire format (``wire_api =
"responses"`` in architecture §3).  We point its ``model_providers.<id>.base_url``
at the LiteLLM gateway's ``/v1`` endpoint, which LiteLLM translates to the
configured backend model.  This validates the Responses-API path through the
gateway — the integration risk ADR-0002 flagged.

``--json`` emits a JSONL event stream we map onto §9.2's trajectory: ``item.*``
events carry the agent messages/tool calls; ``turn.completed`` events carry
usage.  Falls back to a single-event trajectory if the stream shape differs.
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
from swebench_eval.harnesses.coerce import as_dict, as_int
from swebench_eval.harnesses.compaction import (
    NATIVE_COMPACTION_MARKERS,
    compute_threshold,
    count_native_compactions,
)
from swebench_eval.harnesses.custom_minimal.trajectory import cmd_hash, cum_usage, traj_log
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
from swebench_eval.harnesses.trajectory_reasoning import reasoning_item_text

logger = logging.getLogger(__name__)

# R1 (builder1-REMAINING-WORK-single-handover): when codex cannot create its
# helper binaries (codex-linux-sandbox et al. — they are created at runtime
# under $CODEX_HOME, and codex refuses CODEX_HOME inside the process temp dir),
# it prints this warning and CONTINUES for every turn, with every exec_command
# failing silently.  A warning that guarantees total failure is not a warning.
# Treat it as a startup failure: the run is HARNESS_CRASH, never a clean
# "completed with empty patch".
_PATH_ALIAS_WARNING = "could not create PATH aliases"


class CodexHarness(HarnessAdapter):
    """Codex CLI wrapper implementing the HarnessAdapter protocol."""

    def __init__(
        self,
        codex_bin: str = "codex",
        api_base_url: str | None = None,
        api_key: str | None = None,
        # STEP 2.1 (review 2026-08-26): default was "claude-code-model" (a
        # copy-paste from the claude adapter).  Codex speaks the OpenAI RESPONSES
        # API — that alias is now an Anthropic-ADAPTER alias (STEP 2), so if this
        # default were ever used codex would hit the wrong protocol.  The run
        # config normally overrides it; a non-claude alias is the safe default.
        model: str = "cheap-oss-model",
    ) -> None:
        self._codex_bin = codex_bin
        self._api_base_url = (api_base_url or gateway_base_url()).rstrip("/")
        self._api_key = api_key or gateway_api_key()
        self._model = model

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

        # Codex looks in $CODEX_HOME for its config.toml.  Put it under the job's
        # output dir (per-run, deterministic) so the gateway provider config
        # never leaks into the user's ~/.codex AND the persisted session rollout
        # survives the run to be uploaded as the native trajectory (B3-native,
        # fix-trajectory-reasoning-export.md §3 — the old tempdir deleted the
        # rollout on exit).
        codex_home = output_dir / "codex-home"
        codex_home.mkdir(parents=True, exist_ok=True)
        _write_config(
            codex_home,
            self._api_base_url,
            self._api_key,
            self._model,
            input.context_window_tokens,
        )
        # G2 (2026-09-06): codex runs as the agent user and writes sessions
        # under CODEX_HOME — hand the root-created tree to it explicitly.
        grant_agent_ownership(codex_home)

        env = agent_environment()
        env["CODEX_HOME"] = str(codex_home)
        env["LITELLM_API_KEY"] = self._api_key  # matches config env_key

        cmd = [
            self._codex_bin,
            "exec",
            "--json",
            "-m",
            self._model,
            "--skip-git-repo-check",
            # GROUP 3d.1 (master-handover 2026-08-24, VERIFIED IN IMAGE): codex's
            # --sandbox workspace-write sandboxes each command with bubblewrap,
            # which needs unprivileged user namespaces.  Fargate does not allow
            # them, so EVERY exec_command failed with
            #   bwrap: No permissions to create a new namespace ...
            # --dangerously-bypass-approvals-and-sandbox keeps codex unchanged in
            # every other way (same Responses API wire, same tool schema) but
            # drops the bwrap layer, whose help text names our exact situation:
            # "Intended solely for running in environments that are externally
            # sandboxed."  Under ADR-0033 the harness task IS the external
            # sandbox — no egress, ephemeral Fargate task, discarded after one
            # instance — so codex's internal sandbox is redundant machinery that
            # cannot work in that environment.  Do NOT "harden" this back to a
            # bwrap sandbox: it silently re-breaks codex under Fargate.
            "--dangerously-bypass-approvals-and-sandbox",
            # R7 (owner decision MADE): prepend the shared task framing (see
            # task_framing.py) — a bare issue reads as a discussion prompt, not
            # a work order.  Turn-cap finding: codex has NO turn cap (verified
            # in the installed CLI, `codex exec --help`) — R3's shim is the
            # only bound.
            framed(input.problem_statement),
        ]

        # R8.1 (review round 2): live streaming.  A shared Popen runner drains stdout
        # line-by-line THROUGH on_line (TRAJ lines emitted as events arrive, not
        # after exit) while buffering the same text for the post-hoc parse.
        _events: list[dict[str, Any]] = []
        _turn = 0

        def _on_line(line: str) -> None:
            nonlocal _turn
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            if not isinstance(ev, dict):
                return
            _events.append(ev)
            # Same shape as the codex _write_trajectory branches below.
            etype = ev.get("type")
            if etype == "item.completed":
                item = as_dict(ev.get("item"), "codex item")
                itype = item.get("type")
                if itype == "agent_message":
                    logger.info(
                        "TRAJ %s",
                        traj_log(
                            "assistant",
                            _turn,
                            tool="NONE",
                            content=item.get("text", ""),
                            **cum_usage(input.live_usage),
                        ),
                    )
                elif itype == "reasoning":
                    logger.info(
                        "TRAJ %s",
                        traj_log(
                            "assistant",
                            _turn,
                            reasoning_chars=len(reasoning_item_text(item)),
                            **cum_usage(input.live_usage),
                        ),
                    )
                elif itype == "function_call":
                    # master-handover 3.1: WHAT the tool did — pull the command
                    # out of the arguments JSON (codex schema: {"cmd": "..."}).
                    _cmd = ""
                    try:
                        _args = json.loads(item.get("arguments") or "")
                        if isinstance(_args, dict):
                            _cmd = str(_args.get("cmd") or _args.get("command") or "")
                    except (json.JSONDecodeError, TypeError):
                        _cmd = ""
                    logger.info(
                        "TRAJ %s",
                        traj_log(
                            "assistant",
                            _turn,
                            tool=item.get("name", "NONE"),
                            cmd=_cmd,
                            cmdh=cmd_hash(_cmd),
                            **cum_usage(input.live_usage),
                        ),
                    )
                elif itype == "command_execution":
                    logger.info(
                        "TRAJ %s",
                        traj_log(
                            "tool",
                            _turn,
                            tool="bash",
                            output=item.get("aggregated_output", ""),
                            **cum_usage(input.live_usage),
                        ),
                    )
            if etype in ("item.completed", "turn.completed"):
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
            error_msg = f"codex wall-clock timeout ({input.timeout_seconds}s)"
            exit_code = 124
        elif result.returncode != 0:
            error_msg = f"codex exited with code {result.returncode}"
            # R5.1: a recognised terminal subtype (e.g. codex's "max turns"
            # marker on its own turn cap) overrides the generic crash; an
            # exit with no recognised terminal self-report keeps crash.
            terminated_reason = recognized_terminal_reason(stdout + stderr) or "crash"

        Path(raw_log_path).write_text(stdout + "\n\n--- STDERR ---\n" + stderr)

        # R1: the PATH-alias warning is FATAL — codex could not create its
        # sandbox helpers (CODEX_HOME inside the temp dir) and will reject every
        # exec_command.  A warning that guarantees total failure is not a
        # warning.  This applies regardless of exit code (codex exits 0 after
        # "proceeding" through the run with nothing executed, and non-zero on a
        # dead shell) — a run whose sandbox never worked must be a crash, never
        # a clean completed/empty-patch.
        if _PATH_ALIAS_WARNING in (stdout + stderr):
            terminated_reason = "crash"
            error_msg = (
                "codex could not create PATH aliases (CODEX_HOME inside process "
                "temp dir?) — sandbox never reached PATH; run is a startup failure"
            )

        # B3-native (fix-trajectory-reasoning-export.md §3): codex persists its
        # session rollout under $CODEX_HOME/sessions/<y>/<m>/<d>/rollout-*.jsonl.
        # Glob the newest one and surface it so the worker uploads the native
        # trajectory.  (Only meaningful if the rollout adds something over the
        # --json stdout already captured; reported either way.)
        native_trajectory_path = ""
        if codex_home.exists():
            rollouts = sorted(
                codex_home.glob("sessions/**/rollout-*.jsonl"),
                key=lambda p: p.stat().st_mtime,
            )
            if rollouts:
                native_trajectory_path = str(rollouts[-1])

        # STEP 5.1 (review 2026-08-26): the patch is extracted BEFORE the
        # trajectory is written — the old order (`_write_trajectory` then patch)
        # meant a trajectory-write crash COST THE PATCH (the deliverable).  The
        # patch is preserved first, the trajectory logged second.  R3-2 classify
        # stays.
        diff = git_diff_or_classify(repo_dir)
        patch = diff.patch
        success = patch is not None and len(patch) > 0
        if diff.timed_out:
            terminated_reason = "patch_extract_timeout"
            error_msg = "git diff timed out (30s) capturing the patch"
        if patch:
            Path(output_dir / "patch.diff").write_text(patch)

        usage = Usage()
        events = _events  # built live by the on_line reader (R8.1)
        # STEP 5.1: a log-append/trajectory failure must not fail the run.
        try:
            trajectory_written = _write_trajectory(events, trajectory_path, input.problem_statement)
        except Exception as exc:  # noqa: BLE001
            logger.warning("codex: trajectory write failed, run continues: %s", exc)
            trajectory_written = False
        if events:
            try:
                usage = _usage_from_events(events)
            except Exception as exc:  # noqa: BLE001
                logger.warning("codex: usage parse failed, run continues: %s", exc)

        return HarnessOutput(
            patch=patch,
            success=success,
            trajectory_path=trajectory_path if trajectory_written else "",
            raw_log_path=raw_log_path,
            native_trajectory_path=native_trajectory_path,
            trajectory_parsed=bool(events),
            usage=usage,
            adapter_reported_usage=usage,
            patch_extract_s=diff.patch_extract_s,
            wall_clock_seconds=time.monotonic() - start_time,
            exit_code=exit_code,
            error=error_msg,
            terminated_reason=terminated_reason,
            # D-2 (review 2026-08-26): codex's compaction is NATIVE (its own
            # auto-compact window) — record it from its own console notice
            # (NATIVE_COMPACTION_MARKERS["codex"]); None when no marker.
            compactions_fired=count_native_compactions(
                stdout + "\n" + stderr, NATIVE_COMPACTION_MARKERS["codex"]
            ),
        )


# ---------------------------------------------------------------------------
# Config + event parsing
# ---------------------------------------------------------------------------


def _write_config(
    codex_home: Path,
    api_base_url: str,
    api_key: str,
    model: str,
    context_window_tokens: int | None,
) -> None:
    """Write Codex's config.toml pointing its provider at the gateway.

    When ``context_window_tokens`` is resolved, the real ``model_context_window``
    and the compaction threshold are written too (BUILD-SPEC §3.2) — both real
    ``ConfigToml`` fields.  ``None`` leaves them unset (compaction disabled,
    codex defaults).
    """
    instructions = _write_codex_home_files(codex_home)
    compaction = ""
    if context_window_tokens is not None:
        compaction = (
            f"model_context_window = {context_window_tokens}\n"
            f"model_auto_compact_token_limit = {compute_threshold(context_window_tokens)}\n"
        )
    (codex_home / "config.toml").write_text(f"""model = {json.dumps(model)}
model_provider = "litellm"
# 2026-09-06 (codex run 01788653487361028986-1de1c022, 10/41 sessions): codex 0.147.0
# builds a FALLBACK model descriptor for a model name it does not know — no
# apply_patch tool is registered — but the built-in base prompt it sends with it
# says "Use the `apply_patch` tool", so the model calls an unregistered tool and
# the router answers "unsupported call: apply_patch".  No config key adds the tool
# for a custom model; this file is codex's own prompt with those lines replaced.
model_instructions_file = {json.dumps(str(instructions))}
# B3-root-cause (fix-trajectory-reasoning-export.md §2): codex's DEFAULT request
# sends `reasoning: {{summary: \"auto\"}}` on its /v1/responses call, and that
# (alone, with no effort set) suppresses reasoning at the provider — captured:
# reasoning_output_tokens went 0 (default) → 30+ (with effort set) on the same
# real model.  `model_reasoning_effort` is the codex-side lever; it makes the
# wire request carry `reasoning: {{effort: \"high\", summary: \"auto\"}}`, which
# returns the FULL chain-of-thought (summary:auto does NOT truncate when effort
# is set — measured identical content lengths).  Codex replays reasoning
# natively on later turns, so model behaviour stays intact.
model_reasoning_effort = "high"
# Compaction build (Stage 2.5): real ConfigToml fields — model_context_window is
# the provider's window (the run resolved it once, in the dispatcher) and
# model_auto_compact_token_limit is the compaction threshold (a token count, not
# a fraction, matching claude_code's CLAUDE_CODE_AUTO_COMPACT_WINDOW).
{compaction}[model_providers.litellm]
name = "LiteLLM Gateway"
base_url = {json.dumps(api_base_url)}
env_key = "LITELLM_API_KEY"
wire_api = "responses"

# 2026-09-07 (codex 500-run 01788744084885192755-c10df654, matplotlib-24177 and
# matplotlib-23412): codex's `view_image` tool attaches a PNG the agent opened
# (a rendered plot) to the next request; a text-only model answers through
# OpenRouter with 404 "No endpoints found that support image input", codex
# reconnects five times and exits 1 — the whole attempt becomes HARNESS_CRASH.
# The benchmark is text; every model in the battery is driven text-only.
[tools]
view_image = false
""")


_BASE_INSTRUCTIONS_FILE = "base_instructions.md"

# codex's dangerous-command heuristic (codex-rs/shell-command/.../is_dangerous_command.rs,
# `ForcedRm`) flags every `rm -f` / `rm -rf`; with --dangerously-bypass-approvals-and-sandbox
# (approval policy "never") exec_policy.rs turns that into an outright refusal — "rm -f style
# commands are not permitted" — instead of a prompt.  A matching prefix rule skips the
# heuristic entirely (execpolicy/policy.rs: the fallback runs only when no rule matched).
# Seen live 2026-09-06 on sphinx-doc__sphinx-10614: the agent could not `rm -rf _build` in
# its own scratch directory.  The task container is disposable; nothing here is precious.
_RULES = """# swebench-eval: allow the agent to delete its own scratch files (the container is disposable).
prefix_rule(
    pattern = ["rm"],
    decision = "allow",
    justification = "benchmark agent cleaning its own scratch files; the task container is disposable",
)
"""


def _write_codex_home_files(codex_home: Path) -> Path:
    """Write the two per-run files config.toml points at; returns the instructions path.

    ``base_instructions.md`` is codex rust-v0.147.0's ``models-manager/prompt.md``
    (the prompt it uses for a fallback/unknown model) with the two ``apply_patch``
    lines replaced by shell-edit guidance — shipped as package data so the
    harness image carries it.  ``rules/default.rules`` allows ``rm`` (see
    ``_RULES``).  Both live under CODEX_HOME, which the adapter hands to the
    agent user (G2).
    """
    from importlib import resources

    text = (
        resources.files("swebench_eval.harnesses.codex")
        .joinpath(_BASE_INSTRUCTIONS_FILE)
        .read_text(encoding="utf-8")
    )
    instructions = codex_home / _BASE_INSTRUCTIONS_FILE
    instructions.write_text(text, encoding="utf-8")
    rules_dir = codex_home / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "default.rules").write_text(_RULES, encoding="utf-8")
    return instructions


def _parse_events(stdout: str) -> list[dict[str, Any]]:
    """Parse Codex's `--json` JSONL event stream."""
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
    """Write normalized `trajectory.jsonl` from Codex events.

    Maps item.completed (agent_message) events onto §9.2.  Falls back to a
    minimal user-only trajectory if nothing parses.
    """
    import json as _json
    from datetime import UTC, datetime

    def iso() -> str:
        return datetime.now(UTC).isoformat()

    lines: list[str] = []
    turn = 0

    if not events:
        # Minimal fallback, marked unparsed (P4C-3) — see claude_code.
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

    lines.append(
        _json.dumps({"turn": 0, "role": "user", "content": problem_statement, "ts": iso()})
    )

    for ev in events:
        etype = ev.get("type")
        if etype == "item.completed":
            item = as_dict(ev.get("item"), "codex item")
            itype = item.get("type")
            if itype == "agent_message":
                lines.append(
                    _json.dumps(
                        {
                            "turn": turn,
                            "role": "assistant",
                            "content": item.get("text", ""),
                            "ts": iso(),
                        },
                        ensure_ascii=False,
                    )
                )
            elif itype == "reasoning":
                # B3-root-cause (fix-trajectory-reasoning-export.md §2): codex
                # emits the model's chain-of-thought as `reasoning` items once
                # model_reasoning_effort is set.  The old normaliser had no
                # branch for them and dropped them.  Land the text on the
                # assistant record's `reasoning` field (custom_minimal
                # convention), keeping content empty — the reasoning belongs to
                # the FOLLOWING agent_message, and a dedicated record is the
                # honest representation.
                r = reasoning_item_text(item)
                rec = {"turn": turn, "role": "assistant", "content": "", "ts": iso()}
                if r:
                    rec["reasoning"] = r
                lines.append(_json.dumps(rec, ensure_ascii=False))
            elif itype == "function_call":
                lines.append(
                    _json.dumps(
                        {
                            "turn": turn,
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": item.get("id", ""),
                                    "name": item.get("name", ""),
                                    "arguments": item.get("arguments", ""),
                                }
                            ],
                            "content": "",
                            "ts": iso(),
                        },
                        ensure_ascii=False,
                    )
                )
            elif itype == "command_execution":
                # review N-2: record the shell invocation as a tool call on the
                # normalized contract (role:"tool" + normalized.command) so the
                # stuck detector can see it — codex was dropping these before.
                lines.append(
                    _json.dumps(
                        {
                            "turn": turn,
                            "role": "tool",
                            "name": "bash",
                            "normalized": {"command": item.get("command", "")},
                            "output": item.get("aggregated_output", ""),
                            "ts": iso(),
                        },
                        ensure_ascii=False,
                    )
                )
            elif itype == "error":
                lines.append(
                    _json.dumps(
                        {
                            "turn": turn,
                            "role": "result",
                            "error": item.get("message", ""),
                            "ts": iso(),
                        },
                        ensure_ascii=False,
                    )
                )
        elif etype == "turn.completed":
            u = as_dict(ev.get("usage"), "codex turn usage")
            lines.append(
                _json.dumps(
                    {
                        "turn": turn,
                        "role": "result",
                        "usage": {
                            "input_tokens": u.get("input_tokens", 0),
                            "output_tokens": u.get("output_tokens", 0),
                            "cached_input_tokens": u.get("cached_input_tokens", 0),
                        },
                        "ts": iso(),
                    },
                    ensure_ascii=False,
                )
            )
        if etype in ("item.completed", "turn.completed"):
            turn += 1

    Path(trajectory_path).write_text("\n".join(lines) + "\n")
    return True


def _usage_from_events(events: list[dict[str, Any]]) -> Usage:
    """Sum usage from turn.completed events."""
    usage = Usage()
    for ev in events:
        if ev.get("type") == "turn.completed":
            u = as_dict(ev.get("usage"), "codex usage (cost path)")
            usage.input_tokens += as_int(u.get("input_tokens"), "codex input_tokens")
            usage.output_tokens += as_int(u.get("output_tokens"), "codex output_tokens")
    return usage
