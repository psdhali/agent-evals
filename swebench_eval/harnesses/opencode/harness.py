"""OpenCode harness adapter.

Wraps the OpenCode CLI (`opencode run`) as a subprocess.  OpenCode is installed
via `npm install -g opencode-ai` — a subprocess tool, never a runtime dependency
(P3-2).

Routing: OpenCode uses provider configs loaded from `opencode.json`.  We write a
temp config with an OpenAI-compatible provider (`@ai-sdk/openai-compatible`)
pointed at the LiteLLM gateway, and a `models` entry for the gateway alias.  The
alias routes through the gateway to the configured backend model.

``--format json --pure`` emits raw JSON events we map onto §9.2's trajectory;
``--pure`` disables external plugins for a reproducible headless run.  Falls back
to a minimal single-event trajectory if the stream shape differs.
"""

from __future__ import annotations

import json
import logging
import tempfile
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
    OUTPUT_RESERVE,
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

logger = logging.getLogger(__name__)

# OpenAI-compatible SDK package OpenCode uses for custom providers.
_OPENAI_COMPAT_NPM = "@ai-sdk/openai-compatible"


class OpenCodeHarness(HarnessAdapter):
    """OpenCode CLI wrapper implementing the HarnessAdapter protocol."""

    def __init__(
        self,
        opencode_bin: str = "opencode",
        api_base_url: str | None = None,
        api_key: str | None = None,
        model: str = "claude-code-model",
    ) -> None:
        self._opencode_bin = opencode_bin
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

        env = agent_environment()
        env["OPENAI_API_KEY"] = self._api_key  # fallback for the compat provider

        # B3-root-cause (fix-trajectory-reasoning-export.md §4): keep ALL of
        # opencode's run state under the job's output dir, never the image's
        # $HOME.  XDG_CONFIG_HOME was already isolated; XDG_DATA_HOME (the
        # SQLite DB — the harness's native trajectory) and XDG_STATE_HOME /
        # XDG_CACHE_HOME were NOT, so the DB landed in ~/.local/share/opencode
        # and died with the task.  With this the DB is per-run, deterministic,
        # out of $HOME, and unambiguous to export (§ below).
        data_home = str(output_dir / "opencode-data")
        env["XDG_DATA_HOME"] = data_home
        env["XDG_STATE_HOME"] = str(output_dir / "opencode-state")
        env["XDG_CACHE_HOME"] = str(output_dir / "opencode-cache")

        # GROUP 3c (master-handover 2026-08-24, VERIFIED IN IMAGE): opencode
        # fetches its model registry from models.opencode.ai at startup and
        # npm-installs the declared provider at runtime.  Under ADR-0033 isolation
        # both fail and surface as the opaque UnknownError ("Failed to fetch
        # models.dev" — established by pulling the -hw and running opencode
        # offline: identical runs, no seed; baseline ERRORs=1 vs
        # OPENCODE_DISABLE_MODELS_FETCH=true ERRORs=0).  Same intent as
        # claude_code's CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC — this adapter
        # previously had NO behaviour vars, only plumbing (API key + XDG dirs).
        env["OPENCODE_DISABLE_MODELS_FETCH"] = "true"  # verified sufficient on its own
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
        env["OPENCODE_DISABLE_SHARE"] = "true"
        env["OPENCODE_DISABLE_LSP_DOWNLOAD"] = "true"
        # M1 (review 2026-08-25): disable opencode's inotify file watcher. Its
        # notify-rs threads keep the inotify handle open after the instance is
        # disposed (three anon_inode:inotify fds live 20s past "disposing
        # instance"; only in a git repo like /testbed, so it ALWAYS hits), so
        # `opencode run` never exits → wall-clock `timeout` → HARNESS_TIMEOUT →
        # FAILED_HARNESS → zero graded instances. Verified end-to-end on the
        # production path: + this var → `completed` in 18.2 s vs `timeout` at the
        # full 240.5 s, byte-identical patch. The adapter carries its own
        # requirement (belt-and-braces with the task-def var) so it holds even if
        # infra forgets.
        env["OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER"] = "true"

        with tempfile.TemporaryDirectory(prefix="opencode-config-") as cfg_dir:
            env["XDG_CONFIG_HOME"] = cfg_dir  # isolate opencode config
            # CRITICAL (deep-investigation root cause): opencode.json MUST be
            # written or opencode has NO litellm/gateway provider and
            # `-m litellm/...` fails as an opaque UnknownError before any call.
            # Commit bfe23d1 ACCIDENTALLY deleted this call (kept the empty
            # opencode.json path) — every opencode run since then died at
            # "loop step=0" with zero outbound HTTP. Restored here.
            cfg_path = Path(cfg_dir)
            _write_opencode_json(
                cfg_path,
                self._api_base_url,
                self._api_key,
                self._model,
                input.context_window_tokens,
            )
            # G2 (2026-09-06): TemporaryDirectory is 0700 root:root; opencode
            # runs as the agent user and must be able to mkdir/write under it.
            grant_agent_ownership(cfg_path)
            # deep-investigation instrumentation: confirm the config + the
            # shim's actual baseURL reached the adapter, and that the shim
            # (dynamic port) was reachable from THIS container before launch.
            opencode_cfg = cfg_path / "opencode" / "opencode.json"
            logger.info("OPENCODE base_url=%s cfg=%s", self._api_base_url, cfg_path)
            # The per-run gateway key is in this file; never let it reach the log.
            logger.info(
                "OPENCODE config content: %s",
                opencode_cfg.read_text(errors="replace").replace(self._api_key, "***REDACTED***"),
            )
            logger.info(
                "OPENCODE env OPENCODE_*: %s",
                {k: v for k, v in env.items() if k.startswith("OPENCODE_")},
            )
            try:
                import urllib.request

                base_host = self._api_base_url.removesuffix("/v1")
                # NICE (review 2026-08-25): the probe must send the key — a
                # healthy shim answers this with 401 without it, logging a false
                # "NOT reachable" on the exact diagnostic meant to prove
                # reachability (and a bogus llm_calls row).
                _req = urllib.request.Request(
                    base_host + "/v1/models",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                urllib.request.urlopen(_req, timeout=3)
                logger.info("OPENCODE shim reachable at %s", base_host)
            except Exception as exc:  # noqa: BLE001
                logger.error("OPENCODE shim NOT reachable at %s: %r", self._api_base_url, exc)

            cmd = [
                self._opencode_bin,
                "run",
                "--format",
                "json",
                "--pure",
                # B3-root-cause (fix-trajectory-reasoning-export.md §4): without
                # --thinking, opencode's `--format json` stdout carries NO
                # reasoning events (its run.ts only emits `type:"reasoning"`
                # parts when the flag is set — upstream PR #7434 to change that
                # is unmerged).  The adapter's normalised trajectory is built
                # from stdout, so reasoning never reached it.  Proven live:
                # +--thinking → `type:"reasoning"` events with the chain text
                # appear in stdout (3/3 runs), and it is also persisted in the
                # session DB.  Capture happens by default; this surfaces it.
                "--thinking",
                "-m",
                f"litellm/{self._model}",
                "--dir",
                str(repo_dir),
                # R7 (owner decision MADE): prepend the shared task framing.
                # Turn-cap finding: opencode run has NO turn cap (verified in
                # the installed CLI, `opencode run --help`) — R3's shim is the
                # only bound.
                framed(input.problem_statement),
            ]

            # R8.1: live streaming — a shared Popen runner drains stdout line-by-line,
            # feeding TRAJ events to on_line as they arrive while buffering the
            # same text for the post-hoc parse.
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
                etype = ev.get("type") or ev.get("event")
                # M6 (review 2026-08-25): assistant text rides at `part.text`
                # (`{"type":"text","part":{"type":"text","text":...}}`), not
                # top-level — `ev.get("text") or ...` reads empty. Shared helper.
                text = _event_text(ev)
                if etype in ("message", "assistant", "text", "tick"):
                    logger.info(
                        "TRAJ %s",
                        traj_log("assistant", _turn, content=text, **cum_usage(input.live_usage)),
                    )
                    _turn += 1
                elif etype == "reasoning":
                    part = as_dict(ev.get("part"), "opencode reasoning part")
                    rtext = part.get("text", "")
                    logger.info(
                        "TRAJ %s",
                        traj_log(
                            "assistant",
                            _turn,
                            reasoning_chars=len(rtext or ""),
                            **cum_usage(input.live_usage),
                        ),
                    )
                    _turn += 1
                elif etype == "tool_use":
                    # STEP 5.2 (review 2026-08-26): `ev.get("part") or {}` does
                    # NOT protect against a truthy non-dict (a string part sails
                    # through and the next .get raises).  as_dict coerces while
                    # preserving the record and logging once.
                    part = as_dict(ev.get("part"), "opencode part")
                    state = as_dict(part.get("state"), "opencode part state")
                    # master-handover 3.1: WHAT the tool did — the command lives
                    # in state.input.command/pattern (opencode native schema).
                    _inp = as_dict(state.get("input"), "opencode part state input")
                    _cmd = _inp.get("command") or _inp.get("pattern") or _inp.get("request") or ""
                    # M6 (review): carry state.status + error text so an errored
                    # tool call is NOT read as an empty one (it emitted
                    # status="error" + text, trajectory said output:"").
                    _status = state.get("status") or ""
                    _out = str(state.get("output", "") or "")
                    if _status == "error" and not _out:
                        _out = str(state.get("error", "") or "")
                    logger.info(
                        "TRAJ %s",
                        traj_log(
                            "tool",
                            _turn,
                            tool=part.get("tool", "NONE"),
                            cmd=str(_cmd),
                            cmdh=cmd_hash(str(_cmd)),
                            output=_out,
                            status=_status,
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
                error_msg = f"opencode wall-clock timeout ({input.timeout_seconds}s)"
                exit_code = 124
            elif result.returncode != 0:
                error_msg = f"opencode exited with code {result.returncode}"
                # R5.1: a recognised terminal subtype overrides crash; no
                # recognised terminal self-report keeps crash.
                terminated_reason = recognized_terminal_reason(stdout + stderr) or "crash"

            # R2 (builder1-REMAINING-WORK-single-handover): without it every
            # opencode dispatch returns the same ~235 bytes.  The server log is
            # inside the job dir (XDG_DATA_HOME isolated above).  Append it to
            # raw_log (already uploaded) — no new artifact kind.  Read AFTER the
            # subprocess exits; tail it; never raise (a missing log is not a
            # failed run).
            try:
                _server_text = ""
                if data_home:
                    for _lg in sorted(Path(data_home).glob("opencode/log/*.log")):
                        try:
                            _server_text += (
                                f"\n--- {_lg.name} ---\n"
                                + _lg.read_text(errors="replace")[-200_000:]
                            )
                        except OSError as exc:
                            _server_text += f"\n(could not read {_lg}: {exc})"
                Path(raw_log_path).write_text(
                    stdout
                    + "\n\n--- STDERR ---\n"
                    + stderr
                    + ("\n\n--- OPENCODE SERVER LOG ---\n" + _server_text if _server_text else "")
                )
                if _server_text:
                    logger.info("R2: appended opencode server log to raw_log")
            except OSError as exc:
                # Best-effort: a log-append failure must not fail the run.
                logger.warning("R2: could not append opencode server log: %s", exc)

            # STEP 5.1 (review 2026-08-26): the patch is extracted BEFORE the
            # trajectory is written — the old order (_write_trajectory then
            # patch, patch OUTSIDE this try) meant either a trajectory-write
            # crash OR an uncaught propagation cost the patch.  The patch is the
            # deliverable; the trajectory is the log.  R3-2 classify stays.
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
                trajectory_written = _write_trajectory(
                    events, trajectory_path, input.problem_statement
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("opencode: trajectory write failed, run continues: %s", exc)
                trajectory_written = False
            if events:
                try:
                    usage = _usage_from_events(events)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("opencode: usage parse failed, run continues: %s", exc)

            # B3-root-cause (fix-trajectory-reasoning-export.md §4): opencode's
            # native trajectory is the SQLite session DB (session/message/part
            # rows incl. reasoning parts).  The stdout `--format json` stream
            # only carries a subset, so the DB is the richer artifact.  With
            # XDG_DATA_HOME pointed under output_dir the DB is per-run and at a
            # deterministic path.  Export ONLY this run's session via scoped
            # SELECTs (session/message/part) to a JSONL native trajectory —
            # NEVER the .db file or .dump: the DB also holds plaintext OAuth
            # tokens in `account`/`credential` tables.
            # STEP 5.1 applies here too (run 6, 2026-09-08, django-15278): the
            # export is a log, the patch is the deliverable — an export failure
            # must never propagate out of run() and cost the patch.
            native_path = str(output_dir / "opencode_native.jsonl")
            try:
                native_written = _export_native_trajectory(
                    Path(data_home) / "opencode" / "opencode.db",
                    native_path,
                    str(repo_dir),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("opencode: native export failed, run continues: %s", exc)
                native_written = False
            if not native_written:
                native_path = ""

        # patch/success/diff were extracted inside the try above (STEP 5.1:
        # patch FIRST, trajectory second, so a trajectory failure never costs
        # the patch).  R3-2 patch_extract_timeout set there too.

        return HarnessOutput(
            patch=patch,
            success=success,
            trajectory_path=trajectory_path if trajectory_written else "",
            raw_log_path=raw_log_path,
            # B3-root-cause: opencode's native trajectory (the scoped session
            # DB export) rides the existing upload path, reasoning included.
            native_trajectory_path=native_path,
            trajectory_parsed=bool(events),
            usage=usage,
            adapter_reported_usage=usage,
            patch_extract_s=diff.patch_extract_s,
            wall_clock_seconds=time.monotonic() - start_time,
            exit_code=exit_code,
            error=error_msg,
            terminated_reason=terminated_reason,
            # D-2 (review 2026-08-26): opencode's compaction is NATIVE (its own
            # auto-compact gate) — record it from its own console notice
            # (NATIVE_COMPACTION_MARKERS["opencode"]); None when no marker.
            compactions_fired=count_native_compactions(
                stdout + "\n" + stderr, NATIVE_COMPACTION_MARKERS["opencode"]
            ),
        )


# ---------------------------------------------------------------------------
# Config + event parsing
# ---------------------------------------------------------------------------


def _export_native_trajectory(db_path: Path, out_path: str, repo_dir: str) -> bool:
    """Export opencode's native trajectory (one session) to a JSONL file.

    Scoped SELECT only — session/message/part for the run's session.  NEVER
    copies the .db or dumps tables: the DB holds plaintext OAuth tokens
    (``account``/``control_account``/``credential``) for interactive
    ``opencode auth login`` flows.  The scoped SELECT is the protection; the
    former substring guard ("any 'access_token' in the export raises", DoD
    B.8) was removed 2026-09-08 (owner decision, run 6): it fired on the
    MODEL's own text (django-15278 — the ticket's ``refreshed_access_token``
    field quoted in a test script), crashed the harness and lost the patch,
    while guarding nothing real — this deployment never logs opencode in (the
    per-run key arrives via env/config, the data dir is a fresh per-run temp
    dir), and whatever the model types is already in the stdout trajectory
    and harness log, which have no such guard.

    Session selection: prefer the newest session whose ``directory`` matches
    *repo_dir* (realpath-normalised — opencode stores the canonical absolute
    path; a symlink/trailing-slash mismatch would otherwise silently miss),
    then fall back to the newest session overall.  The fallback is safe because
    XDG_DATA_HOME is per-run, so every session in this DB belongs to this run.

    Returns False when no DB/session exists (opencode never wrote one — e.g.
    the run crashed before the first message) so the caller leaves
    native_trajectory_path empty and the worker skips the upload.
    """
    if not db_path.exists():
        return False
    import os
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT id FROM session WHERE directory = ? "
            "ORDER BY time_created DESC, id DESC LIMIT 1",
            (os.path.realpath(repo_dir),),
        ).fetchone()
        if row is None:
            # per-run DB fallback: newest session is this run's.
            row = con.execute(
                "SELECT id FROM session ORDER BY time_created DESC, id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return False
        session_id = row[0]
        lines: list[str] = []
        rows = con.execute(
            "SELECT data, time_created FROM message "
            "WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        )
        for data, _t in rows:
            lines.append(data)
        rows = con.execute(
            "SELECT data, time_created FROM part " "WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        )
        for data, _t in rows:
            lines.append(data)
    finally:
        con.close()

    exported = "\n".join(lines) + "\n"
    Path(out_path).write_text(exported)
    return True


def _usage_from_events(events: list[dict[str, Any]]) -> Usage:
    """Best-effort usage from OpenCode events (inconsistent across modes, §3).

    Main source (review 2026-08-25, narrowed): real opencode ``--format json``
    emits ``step_finish`` events carrying ``part.cost`` and ``part.tokens`` —
    the legacy branch looked for ``totalCost`` on a ``usage|done|completed|
    result`` event, which nothing emits, so opencode trajectories carried no
    usage at all. Parse ``step_finish``'s ``part``; keep ``totalCost`` as the
    fallback for other modes.
    """
    usage = Usage()
    for ev in events:
        etype = ev.get("type") or ev.get("event")
        # STEP 5.2 (review 2026-08-26): as_dict — a truthy non-dict `part`
        # (e.g. a string) sailed past `isinstance(part, dict)`'s sibling but the
        # branch structure relied on it being a dict; coerce so a malformed event
        # is read as empty (cost path must never raise).
        part = as_dict(ev.get("part"), "opencode usage part")
        if etype == "step_finish":
            cost = part.get("cost")
            if cost is not None:
                # non-numeric cost string (provider oddity) -> 0, never raise.
                usage.cost_usd += _as_float(cost)
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                usage.input_tokens += as_int(tokens.get("input"), "opencode tokens.input")
                usage.output_tokens += as_int(tokens.get("output"), "opencode tokens.output")
            elif tokens is not None:
                # scalar tokens block (some simple modes emit a bare count)
                usage.output_tokens += as_int(tokens, "opencode tokens")
            continue
        cost = ev.get("totalCost")
        if cost is not None:
            usage.cost_usd = _as_float(cost)
    return usage


def _as_float(value: Any) -> float:
    """Coerce a usage cost to float; a non-numeric string/None/object -> 0.0.

    STEP 5.2: the old bare ``float(cost)`` raised ValueError on a non-numeric
    string (e.g. ``cost: "abc"``), which stood in the COST path — the one place
    a malformed value must never fail the run (it corrupts the spend).
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("coerce: cost %r is not numeric — treating as 0.0", value)
        return 0.0


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


# Found live 2026-09-07 (opencode 1.18.18, run 01788806316939151254-632ee018): opencode has a
# permission system, and its `external_directory` rule — ANY tool call on a path outside the
# project directory (/testbed): writing a scratch test to /tmp, `rm /tmp/x`, reading a stdlib
# file under /opt/miniconda3, grepping the env — defaults to `ask`. In non-interactive
# `opencode run` that is "permission requested: external_directory (…); auto-rejecting": the
# tool returns "The user rejected permission to use this specific tool call" and the SESSION
# ENDS on the spot. 8/291 instances at the 50% mark (3 EMPTY_PATCH with no edit at all, 5 cut
# short after their patch). Every other harness reads the stdlib and writes /tmp freely, so
# this was an opencode-only handicap, not a model outcome. Allow explicitly — each key below
# is a permission kind opencode 1.18 evaluates by name (seen in its own log:
# `evaluated permission=<kind> … action.action=allow|ask`), so a future default flip cannot
# bite either. webfetch stays at opencode's default (the shim/network posture blocks it anyway).
_PERMISSIONS: dict[str, str] = {
    "external_directory": "allow",
    "edit": "allow",
    "bash": "allow",
    "read": "allow",
    "grep": "allow",
    "glob": "allow",
}


def _write_opencode_json(
    cfg_dir: Path,
    base_url: str,
    api_key: str,
    model: str,
    context_window_tokens: int | None,
) -> None:
    """Write an opencode.json (in an isolated XDG config dir) with the gateway provider."""
    # OpenCode looks in <XDG_CONFIG_HOME>/opencode/opencode.json.
    opencode_cfg = cfg_dir / "opencode" / "opencode.json"
    opencode_cfg.parent.mkdir(parents=True, exist_ok=True)
    # master-handover response (response-opencode-issue-…): the production config
    # is written HERE, and opencode is shim-routed (base_url is the LocalProxy's
    # DYNAMIC port, not the gateway ALB) — the one difference the in-image mock
    # (fixed port) could not reproduce.  Record exactly what opencode was given
    # so a zero-call run can be checked against whether the shim was listening.
    logger.info("R2-write: opencode.json baseURL=%s model=%s", base_url, model)

    provider_entry: dict[str, object] = {
        "npm": _OPENAI_COMPAT_NPM,
        "name": "LiteLLM Gateway",
        "options": {"baseURL": base_url, "apiKey": api_key},
        "models": {model: {"name": model}},
    }
    config: dict[str, object] = {
        "provider": {"litellm": provider_entry},
        "permission": dict(_PERMISSIONS),
    }

    # Compaction build (Stage 2.5): opencode has NEVER auto-compacted — its gate
    # is `if (model.limit.context === 0) return false`, and the config we write
    # declares no `limit` while models.dev is disabled.  Declaring the model's
    # limit resolves the gate; `compaction.auto` enables the pass; `reserved` is
    # W - threshold so opencode's threshold (`limit.input - compaction.reserved`)
    # is exactly our number (32 768 for W = 262 144).  None = compaction
    # disabled — the config stays exactly as it was.
    if context_window_tokens is not None:
        threshold = compute_threshold(context_window_tokens)
        reserve = context_window_tokens - threshold
        provider_entry["models"] = {
            model: {
                "name": model,
                "limit": {
                    "context": context_window_tokens,
                    "input": context_window_tokens,
                    # Must not exceed `reserve` (= W - threshold), or a call right
                    # at the compaction trigger could request more output than the
                    # remaining window and 400. Cap at the shared OUTPUT_RESERVE
                    # (2026-09-02 cross-harness alignment) — observed outputs are
                    # ~2 K, so this is generous.
                    "output": OUTPUT_RESERVE,
                },
            }
        }
        config["compaction"] = {"auto": True, "reserved": reserve}

    opencode_cfg.write_text(json.dumps(config, indent=2))


def _parse_events(stdout: str) -> list[dict[str, Any]]:
    """Parse OpenCode's `--format json` event stream."""
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


def _event_text(ev: dict[str, Any]) -> str:
    """Assistant text from an opencode event: top-level text/content OR ``part.text``.

    M6 (review 2026-08-25): real ``--format json`` text events carry the text at
    ``part.text`` (``{"type":"text","part":{"type":"text","text":...}}``), not
    top-level. The old ``ev.get("text") or ev.get("content")`` read empty and
    every opencode assistant record logged ``content:""``. The neighbouring
    ``reasoning`` branch already reads ``part.text``; this closes the same gap
    for the message/assistant/text/tick branch.
    """
    text = ev.get("text") or ev.get("content") or ""
    if text:
        return str(text)
    part = ev.get("part")
    if isinstance(part, dict):
        pt = part.get("text") or ""
        if pt:
            return str(pt)
    return ""


def _write_trajectory(
    events: list[dict[str, Any]],
    trajectory_path: str,
    problem_statement: str,
) -> bool:
    """Write normalized `trajectory.jsonl` from OpenCode events.

    OpenCode's output shape varies across modes (§3); map what's parseable and
    fall back to a minimal user-only trajectory otherwise (honest-spike pattern).
    """
    import json as _json
    from datetime import UTC, datetime

    def iso() -> str:
        return datetime.now(UTC).isoformat()

    lines: list[str] = []
    lines.append(
        _json.dumps({"turn": 0, "role": "user", "content": problem_statement, "ts": iso()})
    )

    if not events:
        # Minimal fallback, marked unparsed (P4C-3) so it can't pass as healthy.
        fallback = {
            "turn": 0,
            "role": "user",
            "content": problem_statement,
            "unparsed": True,
            "ts": iso(),
        }
        Path(trajectory_path).write_text(_json.dumps(fallback) + "\n")
        return True

    turn = 1
    for ev in events:
        etype = ev.get("type") or ev.get("event")
        # M6 (review 2026-08-25): assistant text rides at `part.text` — shared helper.
        text = _event_text(ev)
        if etype in ("message", "assistant", "text", "tick"):
            lines.append(
                _json.dumps(
                    {"turn": turn, "role": "assistant", "content": text, "ts": iso()},
                    ensure_ascii=False,
                )
            )
            turn += 1
        elif etype == "reasoning":
            # B3-root-cause (fix-trajectory-reasoning-export.md §4): with
            # --thinking, opencode emits `type:"reasoning"` events in stdout.
            # The old normaliser had no branch and dropped them.  Land the text
            # on an assistant record's `reasoning` field; it precedes the text
            # event of the same turn.
            part = as_dict(ev.get("part"), "opencode reasoning part")
            rtext = part.get("text", "")
            rec = {"turn": turn, "role": "assistant", "content": "", "ts": iso()}
            if isinstance(rtext, str) and rtext:
                rec["reasoning"] = rtext
            lines.append(_json.dumps(rec, ensure_ascii=False))
            turn += 1
        elif etype == "tool_use":
            # review N-2: record opencode's native tool_use events (part.tool +
            # part.state.input/output) as role:"tool" on the normalized contract so
            # the stuck detector can see them — they were dropped before.
            part = as_dict(ev.get("part"), "opencode part")
            state = as_dict(part.get("state"), "opencode part state")
            inp = as_dict(state.get("input"), "opencode part state input")
            cmd = inp.get("command") or inp.get("pattern") or _json.dumps(inp, ensure_ascii=False)
            # M6 (review): carry state.status + error text so an errored tool call
            # is not read as an empty one (it emitted status="error" + text but the
            # trajectory recorded output:"" with no status — a judge read that as
            # "the tool returned nothing").
            _status = state.get("status") or ""
            _out = str(state.get("output", "") or "")
            if _status == "error" and not _out:
                _out = str(state.get("error", "") or "")
            lines.append(
                _json.dumps(
                    {
                        "turn": turn,
                        "role": "tool",
                        "name": part.get("tool", ""),
                        "normalized": {"command": cmd},
                        "output": _out,
                        "status": _status,
                        "ts": iso(),
                    },
                    ensure_ascii=False,
                )
            )
            turn += 1
        elif etype == "step_finish":
            # opencode usage rides step_finish's part.cost / part.tokens (review
            # 2026-08-25, narrowed from a key-name change). Nothing else emits
            # usage in `--format json`.
            _part = as_dict(ev.get("part"), "opencode step_finish part")
            _cost = _part.get("cost")
            if _cost is not None:
                _usage: dict[str, Any] = {"cost": _cost}
                _tok = _part.get("tokens")
                if isinstance(_tok, dict):
                    _usage["input_tokens"] = _tok.get("input", 0)
                    _usage["output_tokens"] = _tok.get("output", 0)
                lines.append(
                    _json.dumps(
                        {
                            "turn": turn,
                            "role": "result",
                            "usage": _usage,
                            "ts": iso(),
                        },
                        ensure_ascii=False,
                    )
                )
                turn += 1
        elif etype in ("usage", "done", "completed", "result") and ev.get("totalCost") is not None:
            lines.append(
                _json.dumps(
                    {
                        "turn": turn,
                        "role": "result",
                        "usage": {"totalCost": ev.get("totalCost")},
                        "ts": iso(),
                    },
                    ensure_ascii=False,
                )
            )
            turn += 1

    Path(trajectory_path).write_text("\n".join(lines) + "\n")
    return True
