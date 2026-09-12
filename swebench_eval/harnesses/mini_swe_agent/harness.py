"""mini-swe-agent harness adapter — SPIKE (Phase 3).

SWE-bench's own reference agent, invoked headlessly.  Installed via
``uv tool install mini-swe-agent`` (P3-2 — not a runtime dependency).

SPIKE status (architecture.md §3 marks it "pending headless-invocation
spike"): this adapter validates whether mini-swe-agent can be driven
headlessly against the LiteLLM gateway.  Documented limitations:

- v2 uses ``--task`` + ``--yolo`` + ``--model-class litellm``;
  ``--exit-immediately`` avoids the exit prompt in non-interactive mode.
- Model routing: litellm-native (same as Aider).  Custom gateway base is
  configured via litellm config file since the CLI has no direct
  ``--api-base`` flag.
- The CLI runs in the *current working directory* (git repo must be the cwd).
- ``--output`` writes a JSONL trajectory that maps directly onto §9.2's
  normalized schema.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from swebench_eval.harnesses.base import (
    HarnessAdapter,
    HarnessInput,
    HarnessOutput,
    TerminatedReason,
    Usage,
    recognized_terminal_reason,
)
from swebench_eval.harnesses.compaction import compute_threshold
from swebench_eval.harnesses.custom_minimal.trajectory import cmd_hash, cum_usage, traj_log
from swebench_eval.harnesses.git_utils import git_diff_or_classify
from swebench_eval.harnesses.proc import run_streaming
from swebench_eval.harnesses.repo_prep import ensure_prepared_repo
from swebench_eval.harnesses.routing import agent_environment, gateway_api_key, gateway_base_url
from swebench_eval.harnesses.task_framing import located

logger = logging.getLogger(__name__)


class MiniSweAgentHarness(HarnessAdapter):
    """mini-swe-agent CLI wrapper implementing the HarnessAdapter protocol.

    SPIKE: validates headless invocation.  Not production-ready — clone runs
    from a temp `git clone` copy so the agent's own local checkout is used.
    """

    def __init__(
        self,
        bin_path: str = "mini-swe-agent",
        api_base_url: str | None = None,
        api_key: str | None = None,
        model: str = "cheap-oss-model",
    ) -> None:
        self._bin_path = bin_path
        # P4C-1: mini-swe-agent has no --api-base flag, but its litellm routing
        # honours OPENAI_BASE_URL/OPENAI_API_KEY.  Resolve those from the shared
        # routing helper (gateway or the per-worker shim) so ADR-0019's shim
        # actually meters it — otherwise it silently bypasses F21.
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
        # mini writes its native JSON doc here; we normalise it into trajectory_path
        # afterwards (review N-3).
        mini_raw_trajectory = str(output_dir / "mini_raw_trajectory.json")

        # Back up the repo to a temp location.  mini-swe-agent runs in-place
        # (cwd) and edits the working tree; we diff from the base_commit after.
        # P4C-1: route through the shared helper (gateway or shim) via env that
        # mini's internal litellm honours — without this it bypasses the shim's
        # budget metering.  Unlike other adapters there is no --api-base flag.
        env = agent_environment()
        env["OPENAI_BASE_URL"] = self._api_base_url
        env["OPENAI_API_BASE"] = self._api_base_url  # litellm also reads this
        env["OPENAI_API_KEY"] = self._api_key
        # mini-swe-agent v2 runs an interactive one-time onboarding unless
        # MSWEA_CONFIGURED is set (run/utilities/config.py:configure_if_first_time);
        # headless it aborts.  Mark it configured and name the model so the run
        # proceeds straight to the shim/gateway.
        env["MSWEA_CONFIGURED"] = "1"
        env["MSWEA_MODEL_NAME"] = f"openai/{self._model}"
        # mini's litellm cost tracker has no price for the gateway alias and
        # raises unless told to ignore (mini_swe_agent/models/litellm_model.py).
        env["MSWEA_COST_TRACKING"] = "ignore_errors"

        # Compaction build (Stage 2.2): when a context window is resolved for the
        # run, hand mini the pruner agent.  The agent class is loaded from
        # /app/mini_pruning_agent.py via PYTHONPATH (the module lives OUTSIDE
        # swebench_eval/ so importing it never drags the package __init__ chain
        # into mini's isolated uv-tool venv, which lacks our deps).  ``-c
        # mini.yaml`` is REQUIRED whenever any ``-c`` is passed — otherwise
        # mini's DEFAULT config is silently dropped.
        # 2026-09-08 (task_framing.py): mini's own instance_template never names
        # the repo directory — 3/10 sampled run-1 instances opened with `find
        # /workspace`. Location line only (R7: no double framing). The SAME text
        # is what the trajectory records as turn 0 (X3: the record must be what
        # the model actually saw).
        task_text = located(input.problem_statement)
        cmd = [
            self._bin_path,
            "--task",
            task_text,
            "--yolo",
            "--exit-immediately",
            "--model-class",
            "litellm",
            # mini's internal litellm needs a provider prefix on the model
            # (else "LLM Provider NOT provided"); litellm strips `openai/` and
            # sends the bare alias to the shim/gateway base.
            "--model",
            f"openai/{self._model}",
            "--cost-limit",
            "0",  # disable CLI cost limit; budget enforced by the harness wrapper
            "--output",
            mini_raw_trajectory,
        ]
        _window = input.context_window_tokens
        if _window is not None:
            # Cross-harness alignment (2026-09-02): use the SHARED compute_threshold
            # so mini compacts at the exact same trigger as every other harness
            # (235,929 at W=262,144). The old hardcoded `W - 32_768` both used the
            # pre-reduction reserve AND skipped the 0.90·W safety cap — it happened
            # to coincide with compute_threshold only at the old reserve and only
            # for W <= 327,680.
            threshold = compute_threshold(_window)
            env["PYTHONPATH"] = "/app"
            cmd += [
                "--agent-class",
                "mini_pruning_agent.PruningAgent",
                # mini's config file: passing ANY -c drops the default, so the
                # real mini.yaml is always included first.
                "-c",
                "mini.yaml",
                "-c",
                f"agent.context_window={_window}",
                "-c",
                f"agent.compact_at_tokens={threshold}",
            ]

        # R8.1: live streaming — Popen + reader thread.  mini's LIVE trajectory signal
        # is its native JSON doc (--output mini_raw_trajectory), written
        # incrementally; stdout is the CLI progress.  A watcher thread tails the
        # native file and emits TRAJ per message as it appears (the reviewer's
        # "cheapest route for mini specifically"); the shared runner streams
        # stdout live (buffered for the post-hoc parse/raw_log).
        _tail_stop = threading.Event()
        _seen = {"messages": 0}
        # GROUP 3.6 (master-handover 2026-08-24): TRAJ turn must be a MODEL CALL,
        # not a message index.  The old `len(msgs[:seen])+1` counted every
        # assistant+tool message, roughly doubling the turn count (live showed
        # 1,5,7…349 vs the trajectory's correct 0,1,2…).  Count assistant
        # messages: each is one model response == the shim turn cap's (3.12)
        # definition of a turn.
        _turns = 0

        def _tail_native() -> None:
            """Poll mini_raw_trajectory; emit TRAJ for newly-arrived messages."""
            nonlocal _turns
            while not _tail_stop.wait(0.5):
                try:
                    native_path = Path(mini_raw_trajectory)
                    if not native_path.exists():
                        continue
                    doc = json.loads(native_path.read_text(errors="replace"))
                    msgs = doc.get("messages") if isinstance(doc, dict) else None
                    if not isinstance(msgs, list):
                        continue
                    for m in msgs[_seen["messages"] :]:
                        if not isinstance(m, dict):
                            continue
                        role = m.get("role")
                        content = m.get("content") or ""
                        reasoning = m.get("reasoning_content") or ""
                        tcs = m.get("tool_calls") or []
                        if not isinstance(tcs, list):
                            tcs = []
                        tool_name = (
                            (tcs[0].get("function") or {}).get("name", "NONE") if tcs else "NONE"
                        )
                        # master-handover 3.1: WHAT the tool did — the command is
                        # the tool call's arguments (mini's own schema).
                        _cmd = ""
                        if tcs:
                            _args = (tcs[0].get("function") or {}).get("arguments") or ""
                            try:
                                _pv = json.loads(_args)
                                if isinstance(_pv, dict):
                                    _cmd = str(
                                        _pv.get("command")
                                        or _pv.get("cmd")
                                        or _pv.get("pattern")
                                        or ""
                                    )
                            except (json.JSONDecodeError, TypeError):
                                _cmd = ""
                        logger.info(
                            "TRAJ %s",
                            traj_log(
                                "assistant" if role == "assistant" else str(role or "agent"),
                                _turns,
                                tool=tool_name,
                                cmd=_cmd,
                                cmdh=cmd_hash(_cmd),
                                content=str(content),
                                reasoning_chars=len(str(reasoning)),
                                **cum_usage(input.live_usage),
                            ),
                        )
                        if role == "assistant":
                            _turns += 1
                    _seen["messages"] = len(msgs)
                except Exception as exc:  # noqa: BLE001 — best-effort tail; never kill the run
                    logger.debug("mini native tail (transient): %s", exc)
                    continue

        tail_thread = threading.Thread(target=_tail_native, daemon=True, name="mini-traj")
        tail_thread.start()

        result = run_streaming(
            cmd,
            cwd=repo_dir,
            env=env,
            timeout=input.timeout_seconds,
            on_line=None,
        )
        _tail_stop.set()
        tail_thread.join(timeout=5)

        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
        if result.timed_out:
            terminated_reason = "timeout"
            error_msg = f"mini-swe-agent wall-clock timeout ({input.timeout_seconds}s)"
            exit_code = 124
        elif result.returncode != 0:
            error_msg = f"mini-swe-agent exited with code {result.returncode}"
            # R5.1: a recognised terminal subtype overrides crash; no
            # recognised terminal self-report keeps crash.
            terminated_reason = recognized_terminal_reason(stdout + stderr) or "crash"

        # Write raw log.
        Path(raw_log_path).write_text(stdout + "\n\n--- STDERR ---\n" + stderr)

        # Extract the patch from git diff.
        diff = git_diff_or_classify(repo_dir)
        patch = diff.patch
        success = patch is not None and len(patch) > 0
        if diff.timed_out:
            terminated_reason = "patch_extract_timeout"
            error_msg = "git diff timed out (30s) capturing the patch"

        if patch:
            Path(output_dir / "patch.diff").write_text(patch)

        # Normalise mini's native trajectory into the §9.2 contract (review N-3):
        # its `messages` array carries tool_calls (function.name/arguments) plus
        # role:"tool" outputs, so the stuck detector can evaluate mini instead of
        # getting insufficient_data from a file in an unknown format.
        trajectory_parsed = False
        raw_mini = Path(mini_raw_trajectory)
        if raw_mini.exists():
            normalized = _normalize_mini_trajectory(raw_mini.read_text(errors="replace"), task_text)
            if len(normalized) > 1:  # user turn + at least one tool/assistant event
                Path(trajectory_path).write_text(
                    "\n".join(json.dumps(ev, ensure_ascii=False) for ev in normalized) + "\n"
                )
                trajectory_parsed = True
        if not Path(trajectory_path).exists() or Path(trajectory_path).stat().st_size == 0:
            Path(trajectory_path).write_text(
                json.dumps(
                    {
                        "turn": 0,
                        "role": "user",
                        "content": task_text,
                        "unparsed": True,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                )
                + "\n"
            )

        return HarnessOutput(
            patch=patch,
            success=success,
            trajectory_path=trajectory_path,
            raw_log_path=raw_log_path,
            # B6/E11a: mini's NATIVE trajectory (its own JSON document, pre-
            # normalisation) was written to disk and then died with the task — no
            # other adapter produces a separate native file. Surface it so the
            # worker uploads it beside the normalized trajectory.
            native_trajectory_path=mini_raw_trajectory,
            trajectory_parsed=trajectory_parsed,
            usage=Usage(),
            # mini has NO second meter (never parses usage) — adapter_reported_usage
            # stays None so the cross-check columns are NULL and the gate skips it
            # (R2-1), never 0.
            patch_extract_s=diff.patch_extract_s,
            wall_clock_seconds=time.monotonic() - start_time,
            exit_code=exit_code,
            error=error_msg,
            terminated_reason=terminated_reason,
            # Compaction build (Stage 2.2): surface the pruner's pass count from
            # its own log lines (the module logs "COMPACT #N fired").  The pruner
            # runs inside mini's process, so the harness cannot read
            # n_compactions directly — it parses the log.  None = no pruner
            # (window not set) or no pass fired.
            compactions_fired=_count_compactions(stdout + "\n" + stderr),
        )


def _count_compactions(text: str) -> int | None:
    """Count the pruner passes from its 'COMPACT #N fired' log lines.

    Returns None when there is no 'COMPACT #' marker at all (distinguishing
    "no pruner attached / no pass fired" from an honest 0 — though a real 0
    passes is also recorded as 0).  The counter is what Stage 6 checks.
    """
    import re

    if not text or "COMPACT" not in text:
        return None
    counts = [int(n) for n in re.findall(r"COMPACT #(\d+) fired", text)]
    return counts[-1] if counts else 0


# ---------------------------------------------------------------------------
# Normalized-trajectory conversion (review N-3)
# ---------------------------------------------------------------------------


def _extract_command(arguments: str) -> str:
    """Pull the ``command`` out of an OpenAI function-call ``arguments`` JSON."""

    try:
        parsed = json.loads(arguments) if arguments else {}
        if isinstance(parsed, dict):
            cmd = parsed.get("command")
            return cmd if isinstance(cmd, str) else str(parsed)
    except json.JSONDecodeError:
        pass
    return str(arguments)


def _normalize_mini_trajectory(raw_text: str, problem_statement: str) -> list[dict[str, object]]:
    """Convert mini-swe-agent's native trajectory into the §9.2 contract (N-3).

    mini writes a single JSON doc whose ``messages`` array is the OpenAI chat-tool
    shape: assistant messages carry ``tool_calls`` (``function.name`` /
    ``function.arguments``), and the matching outputs are ``role:"tool"`` messages
    keyed by ``tool_call_id``.  We turn each assistant tool call into a
    ``role:"tool"`` event (``name``, ``normalized.command``, ``output``) so the
    stuck detector can evaluate mini instead of reporting ``insufficient_data``.
    """
    from datetime import UTC
    from datetime import datetime as _dt

    def iso() -> str:
        return _dt.now(UTC).isoformat()

    try:
        doc = json.loads(raw_text)
    except json.JSONDecodeError:
        return []
    messages = doc.get("messages") if isinstance(doc, dict) else None
    if not isinstance(messages, list):
        return []

    outputs = {
        m.get("tool_call_id"): str(m.get("content") or "")
        for m in messages
        if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id")
    }

    out: list[dict[str, object]] = [
        {"turn": 0, "role": "user", "content": problem_statement, "ts": iso()}
    ]
    turn = 1
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        # B3-root-cause (fix-trajectory-reasoning-export.md §6): split text and
        # reasoning.  The old code folded reasoning_content into content AND
        # guarded the assistant event with `if content and not tcs` — so a
        # normal working turn (text/reasoning + a tool call) emitted NO
        # assistant record and the reasoning silently vanished from the
        # normalised trajectory (found live: 4/4 assistant turns had tool calls,
        # zero assistant records in trajectory.jsonl).  Emit the assistant
        # record whenever EITHER text or reasoning exists, keep them on separate
        # fields, then still emit the tool events.
        content = m.get("content") or ""
        reasoning = m.get("reasoning_content") or ""
        tcs = m.get("tool_calls") or []
        if not isinstance(tcs, list):
            tcs = []
        if content or reasoning:
            rec: dict[str, object] = {
                "turn": turn,
                "role": "assistant",
                "content": str(content),
                "ts": iso(),
            }
            if reasoning:
                rec["reasoning"] = str(reasoning)
            # TRAJ is emitted LIVE by the native-file tail thread (R8.1) while
            # this runs; this post-hoc path only writes the normalized JSONL.
            out.append(rec)
            turn += 1
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name") or "bash"
            out.append(
                {
                    "turn": turn,
                    "role": "tool",
                    "name": str(name),
                    "normalized": {"command": _extract_command(fn.get("arguments") or "")},
                    "output": outputs.get(tc.get("id"), ""),
                    "ts": iso(),
                }
            )
            turn += 1
    return out
