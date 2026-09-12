"""Aider harness adapter.

Wraps the Aider CLI as a subprocess.  Aider is installed via
``uv tool install aider-chat`` (P3-2) — it is not a runtime dependency.

Invocation:
    aider --message "<problem>" --yes-always --no-auto-commits \
          --openai-api-base <gateway>/v1 --openai-api-key <key> --model <alias>

Aider is built on litellm internally, so pointing it at the gateway's
OpenAI-compatible endpoint routes it through the same cost/rate-limit
enforcement as the custom harness.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from swebench_eval.harnesses.base import (
    HarnessAdapter,
    HarnessInput,
    HarnessOutput,
    TerminatedReason,
    Usage,
)
from swebench_eval.harnesses.git_utils import git_diff_or_classify
from swebench_eval.harnesses.repo_prep import ensure_prepared_repo
from swebench_eval.harnesses.routing import agent_environment, gateway_api_key, gateway_base_url

logger = logging.getLogger(__name__)


def _build_fix_message(problem_statement: str) -> str:
    """Message for a headless, single-shot aider run that must actually edit.

    With no files in the chat, aider's system prompt tells the model to identify
    the files it wants and STOP to let the user add them — so it reasons about
    files and never edits.  This directive instructs it to find the relevant
    source files itself (via the repo map / reading), add them to the chat, and
    apply the fix without asking.
    """
    return (
        problem_statement
        + "\n\nFix the bug described above. Use the repository context to identify "
        "which source file(s) need to change. Find the relevant file(s) yourself, "
        "add them to the chat, and apply the fix. Do NOT ask which files to add or "
        "stop to let the user add them — locate them and edit them directly. "
        "Then verify your change."
    )


def _parse_chat_history(history: str, problem_statement: str) -> list[dict[str, object]]:
    """Recover assistant turns from aider's `.aider.chat.history.md`.

    Aider records the conversation as markdown: system notices start with ``>``,
    the user's echoed prompt is a ``####`` block, and the model's replies are the
    remaining prose.  Aider has no shell-command tool — its action is an edit — so
    this recovers a *conversational* trajectory for the audit trail / dashboard,
    not detector-evaluable tool events (the stuck detector rightly stays
    ``insufficient_data`` for aider, which the review permits).

    Blocks that are only echoes of the problem statement are dropped, so a run
    where the model never actually replied (failed model call) stays unparsed
    rather than being misreported as a real conversation.
    """
    from datetime import UTC
    from datetime import datetime as _dt

    def iso() -> str:
        return _dt.now(UTC).isoformat()

    ps = " ".join(problem_statement.split())
    # Aider's own CLI invocation echo (the run command), not a model reply.
    _INVOCATION = ("--openai-api-base", "--yes-always", "--no-auto-commits", "--model")

    blocks: list[list[str]] = []
    cur: list[str] = []
    for raw in history.splitlines():
        s = raw.strip()
        if not s or s.startswith((">", "#")) or any(m in s for m in _INVOCATION):
            if cur:
                blocks.append(cur)
                cur = []
            continue
        cur.append(s)
    if cur:
        blocks.append(cur)

    events: list[dict[str, object]] = []
    for i, b in enumerate(blocks):
        text = " ".join(x.strip() for x in b).strip()
        if not text:
            continue
        compact = " ".join(text.split())
        if compact in ps:  # pure echo of the problem — not an assistant reply
            continue
        events.append({"turn": i + 1, "role": "assistant", "content": text, "ts": iso()})
    return events


class AiderHarness(HarnessAdapter):
    """Aider CLI wrapper implementing the HarnessAdapter protocol."""

    def __init__(
        self,
        aider_bin: str = "aider",
        api_base_url: str | None = None,
        api_key: str | None = None,
        model: str = "cheap-oss-model",
        max_turns: int = 1,  # Aider runs one shot per invocation by default
    ) -> None:
        self._aider_bin = aider_bin
        self._api_base_url = (api_base_url or gateway_base_url()).rstrip("/")
        self._api_key = api_key or gateway_api_key()
        self._model = model
        self._max_turns = max_turns

    def run(self, input: HarnessInput) -> HarnessOutput:
        start_time = time.monotonic()
        terminated_reason: TerminatedReason = "completed"
        error_msg = ""
        exit_code = 0

        repo_dir = Path(input.repo_checkout_path)

        # 5b: the worker prepares the repo via SWE-bench's install_repo_script
        # (repo_prep.py) BEFORE the adapter runs.  Assert rather than clone: the
        # script removed origin and pruned future history, so cloning/fetching
        # here would fail (no origin) or silently re-expose the gold patch.
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

        # Output directory for artifacts.
        output_dir = Path(input.output_dir or repo_dir.parent)
        raw_log_path = str(output_dir / "harness_stdout.log")
        trajectory_path = str(output_dir / "trajectory.jsonl")

        env = agent_environment()
        env["OPENAI_API_BASE"] = self._api_base_url
        env["OPENAI_API_KEY"] = self._api_key

        cmd = [
            self._aider_bin,
            "--message",
            _build_fix_message(input.problem_statement),
            "--yes-always",
            "--no-auto-commits",
            "--openai-api-base",
            self._api_base_url,
            "--openai-api-key",
            self._api_key,
            # Aider routes the bare alias through its internal litellm, which bails
            # with "LLM Provider NOT provided" unless the model has a provider
            # prefix.  `openai/` routes it to the OpenAI-compatible provider with
            # our api_base; litellm strips the prefix, so the gateway still
            # receives the bare `cheap-oss-model` alias it actually has.
            "--model",
            f"openai/{self._model}",
            "--no-analytics",
        ]

        try:
            result = subprocess.run(  # noqa: PLW1510
                cmd,
                cwd=repo_dir,
                # stdin MUST be closed. `codex exec` reads stdin ("Reading additional input
                # from stdin...") and BLOCKS forever when it inherits a TTY — reproduced as a
                # hang with zero output. Inherited stdin is a hazard for every CLI here, so
                # every adapter closes it explicitly rather than relying on how it was launched.
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=input.timeout_seconds,
                env=env,
            )
            exit_code = result.returncode
            stdout = result.stdout
            stderr = result.stderr
            if result.returncode != 0:
                error_msg = f"aider exited with code {result.returncode}"
                terminated_reason = "crash"
        except subprocess.TimeoutExpired as exc:
            terminated_reason = "timeout"
            error_msg = f"aider wall-clock timeout ({input.timeout_seconds}s)"
            exit_code = 124
            stdout = (
                (exc.stdout or b"").decode()
                if isinstance(exc.stdout, bytes)
                else str(exc.stdout or "")
            )
            stderr = (
                (exc.stderr or b"").decode()
                if isinstance(exc.stderr, bytes)
                else str(exc.stderr or "")
            )

        # Write raw log.
        Path(raw_log_path).write_text(stdout + "\n\n--- STDERR ---\n" + stderr)

        # Extract the patch from git diff.
        diff = git_diff_or_classify(repo_dir)
        patch = diff.patch
        success = patch is not None and len(patch) > 0
        if diff.timed_out:
            terminated_reason = "patch_extract_timeout"
            error_msg = "git diff timed out (30s) capturing the patch"

        # Write patch to disk.
        if patch:
            Path(output_dir / "patch.diff").write_text(patch)

        # Trajectory — recover the conversational trajectory from aider's
        # `.aider.chat.history.md` (written into the repo cwd by default).  Aider
        # has no structured tool-call stream, but the conversation gives a real
        # audit trail for the dashboard instead of a bare one-line fallback.  When
        # nothing clean is recoverable (e.g. the model call itself failed) we fall
        # back to the unparsed one-line marker (P4C-3) so "nothing to see" is not
        # mistaken for "saw nothing".
        history_events: list[dict[str, object]] = []
        chat_history = repo_dir / ".aider.chat.history.md"
        if chat_history.exists():
            history_events = _parse_chat_history(
                chat_history.read_text(errors="replace"), input.problem_statement
            )

        trajectory_lines: list[str] = []
        if history_events:
            trajectory_lines.append(
                json.dumps(
                    {
                        "turn": 0,
                        "role": "user",
                        "content": input.problem_statement,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                )
            )
            trajectory_lines.extend(json.dumps(ev, ensure_ascii=False) for ev in history_events)
        else:
            trajectory_lines.append(
                json.dumps(
                    {
                        "turn": 0,
                        "role": "user",
                        "content": input.problem_statement,
                        "unparsed": True,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                )
            )
        Path(trajectory_path).write_text("\n".join(trajectory_lines) + "\n")

        return HarnessOutput(
            patch=patch,
            success=success,
            trajectory_path=trajectory_path,
            raw_log_path=raw_log_path,
            trajectory_parsed=bool(history_events),
            usage=Usage(),
            # aider has NO second meter (it never parses usage) — leave
            # adapter_reported_usage None so the cross-check columns are NULL and
            # the §8 gate skips it honestly (R2-1), rather than 0.
            patch_extract_s=diff.patch_extract_s,
            wall_clock_seconds=time.monotonic() - start_time,
            exit_code=exit_code,
            error=error_msg,
            terminated_reason=terminated_reason,
        )
