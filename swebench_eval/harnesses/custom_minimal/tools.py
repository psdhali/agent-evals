"""Tool definitions for the custom minimal harness.

Two tools, matching the SWE-bench convention:
- ``bash`` — run a shell command in the repository directory.
- ``str_replace_editor`` — view, create, or edit files using string replacement.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from swebench_eval.harnesses.routing import agent_environment, agent_spawn_kwargs

# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command inside the repository directory. "
            "Use this to explore the codebase, run tests, lint, and verify changes. "
            "The command runs in a sandboxed subprocess with a 60-second timeout. "
            "Output is truncated to 8000 characters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "str_replace_editor",
            "description": "Custom editing tool for viewing, creating, and editing files. "
            "State persists across calls.\n\n"
            "Commands:\n"
            "- `view`: Show the contents of a file. Args: `path` (required), "
            "`view_range` (optional, e.g. [1, 50]).\n"
            "- `create`: Create a new file. Args: `path` (required), "
            "`file_text` (required — the full file content).\n"
            "- `str_replace`: Replace one occurrence of `old_str` with `new_str` "
            "in a file. Args: `path` (required), `old_str` (required — must match "
            "exactly including whitespace), `new_str` (required).\n"
            "- `insert`: Insert text at a specific line. Args: `path` (required), "
            "`insert_line` (required — 1-indexed line number), `new_str` (required).\n"
            "- `undo_edit`: Revert the last edit. Args: `path` (required).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "enum": ["view", "create", "str_replace", "insert", "undo_edit"],
                        "description": "The editing command to run.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file/directory.",
                    },
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "Optional [start, end] line range for view.",
                    },
                    "file_text": {
                        "type": "string",
                        "description": "Full file content (for `create`).",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "Exact string to replace (for `str_replace`).",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement string (for `str_replace` and `insert`).",
                    },
                    "insert_line": {
                        "type": "integer",
                        "description": "1-indexed line number (for `insert`).",
                    },
                },
                "required": ["command", "path"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

# Per-path edit history for undo support.
_EDIT_HISTORY: dict[str, list[str]] = {}
_MAX_OUTPUT_CHARS = 8000


def execute_tool(name: str, args: dict[str, Any], repo_dir: Path) -> str:
    """Execute a tool call and return the result string."""
    if name == "bash":
        return _run_bash(args.get("command", ""), repo_dir)
    elif name == "str_replace_editor":
        return _run_editor(args, repo_dir)
    else:
        return f"Error: unknown tool '{name}'"


def _run_bash(command: str, repo_dir: Path) -> str:
    """Run a shell command and return its output.

    The shell runs with the allow-listed ``agent_environment()`` (C3 / B1):
    custom_minimal's bash tool is in-process and previously inherited the
    WORKER's full environment — including AWS_CONTAINER_CREDENTIALS_RELATIVE_URI,
    ARTIFACTS_BUCKET, RESULTS_QUEUE_URL + the task role (which could let the
    agent enqueue its own verdict), REDIS_URL and the master key.  Same
    allow-list the five subprocess adapters use.
    """
    if not command.strip():
        return "Error: empty command"

    try:
        result = subprocess.run(  # noqa: PLW1510
            command,
            shell=True,
            cwd=repo_dir,
            env=agent_environment(),
            capture_output=True,
            text=True,
            timeout=60,
            **agent_spawn_kwargs(),  # G2: the shell runs as the unprivileged agent user
        )
        output = result.stdout
        if result.stderr:
            output += "\n[stderr]\n" + result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out (60s)"
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


def _run_editor(args: dict[str, Any], repo_dir: Path) -> str:
    """Execute a str_replace_editor command."""
    command = args.get("command", "")
    path_str = args.get("path", "")

    # Resolve path — must be absolute or relative to repo_dir.
    file_path = Path(path_str)
    if not file_path.is_absolute():
        file_path = repo_dir / file_path
    file_path = file_path.resolve()

    # Security: don't allow escaping the repo.
    try:
        file_path.relative_to(repo_dir.resolve())
    except ValueError:
        return "Error: path is outside the repository"

    if command == "view":
        return _editor_view(file_path, args.get("view_range"))
    elif command == "create":
        return _editor_create(file_path, args.get("file_text", ""))
    elif command == "str_replace":
        return _editor_str_replace(file_path, args.get("old_str", ""), args.get("new_str", ""))
    elif command == "insert":
        return _editor_insert(file_path, args.get("insert_line", 1), args.get("new_str", ""))
    elif command == "undo_edit":
        return _editor_undo(file_path)
    else:
        return f"Error: unknown editor command '{command}'"


def _editor_view(file_path: Path, view_range: list[int] | None) -> str:
    """View file contents, optionally with a line range."""
    if not file_path.exists():
        return f"Error: file not found: {file_path}"

    try:
        lines = file_path.read_text().splitlines()
    except Exception as exc:  # noqa: BLE001
        return f"Error reading file: {exc}"

    total = len(lines)

    if view_range and len(view_range) == 2:
        start = max(1, view_range[0])
        end = min(total, view_range[1])
    else:
        start, end = 1, total

    if start > total:
        return f"Error: view_range start {start} exceeds file length {total}"

    result_lines = []
    for i in range(start - 1, end):
        result_lines.append(f"{i + 1:>6}|{lines[i]}")
    return "\n".join(result_lines)


def _editor_create(file_path: Path, content: str) -> str:
    """Create a new file, failing if it already exists."""
    if file_path.exists():
        return f"Error: file already exists: {file_path}"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    return f"File created: {file_path}"


def _editor_str_replace(file_path: Path, old_str: str, new_str: str) -> str:
    """Replace one occurrence of old_str with new_str."""
    if not file_path.exists():
        return f"Error: file not found: {file_path}"

    try:
        content = file_path.read_text()
    except Exception as exc:  # noqa: BLE001
        return f"Error reading file: {exc}"

    count = content.count(old_str)
    if count == 0:
        return f"Error: old_str not found in {file_path}"
    if count > 1:
        return (
            f"Error: old_str found {count} times in {file_path}. "
            "Make the search string more specific."
        )

    # Save for undo.
    _EDIT_HISTORY.setdefault(str(file_path), []).append(content)

    new_content = content.replace(old_str, new_str, 1)
    file_path.write_text(new_content)
    return f"Replaced 1 occurrence in {file_path}"


def _editor_insert(file_path: Path, insert_line: int, new_str: str) -> str:
    """Insert text at a specific line number."""
    if not file_path.exists():
        return f"Error: file not found: {file_path}"

    try:
        lines = file_path.read_text().splitlines(keepends=True)
    except Exception as exc:  # noqa: BLE001
        return f"Error reading file: {exc}"

    if insert_line < 1 or insert_line > len(lines) + 1:
        return f"Error: insert_line {insert_line} out of range (1-{len(lines) + 1})"

    _EDIT_HISTORY.setdefault(str(file_path), []).append(file_path.read_text())

    lines.insert(insert_line - 1, new_str + "\n")
    file_path.write_text("".join(lines))
    return f"Inserted at line {insert_line} in {file_path}"


def _editor_undo(file_path: Path) -> str:
    """Undo the last edit to a file."""
    key = str(file_path)
    if key not in _EDIT_HISTORY or not _EDIT_HISTORY[key]:
        return f"Error: nothing to undo for {file_path}"
    previous = _EDIT_HISTORY[key].pop()
    file_path.write_text(previous)
    return f"Undo applied to {file_path}"
