"""Coding Agent workspace tools: bash / read_file / write_file / edit_file / glob / grep.

Migrated verbatim from the legacy root tools.py (Phase 2); behaviour and
tool semantics are unchanged.

Workspace tools accept an optional `cwd` keyword injected by the harness
(e.g. teammate assignment directories). It is NOT part of any tool schema,
so the model never supplies it. Threads must never os.chdir() — the
process cwd is shared across the leader and all teammates.
"""

import os
import re
import subprocess
from pathlib import Path

from codeharness.background.manager import BackgroundManager, _format_bash_result
from codeharness.tools.workspace import LocalWorkspaceBackend, WorkspaceBackend

# ── Tool Schemas ─────────────────────────────────────────────

CODING_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute."},
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "Set to true to run this command in the background. "
                            "Returns immediately with a task ID; results are delivered in a later turn."
                        ),
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the content of a file. Optionally read a specific line range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read."},
                    "start_line": {"type": "integer", "description": "1-based start line (optional)."},
                    "end_line": {"type": "integer", "description": "1-based end line, inclusive (optional)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write."},
                    "content": {"type": "string", "description": "Content to write into the file."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a specific text block in a file. old_text must match exactly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit."},
                    "old_text": {"type": "string", "description": "Exact text to find and replace."},
                    "new_text": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern relative to the working directory. e.g. '**/*.py'",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents using a regex pattern. Returns matching lines with file paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for."},
                    "path": {"type": "string", "description": "File or directory to search in. Defaults to current directory."},
                    "file_pattern": {"type": "string", "description": "Optional glob to filter files, e.g. '*.py'."},
                },
                "required": ["pattern"],
            },
        },
    },
]

# ── Tool Implementations ─────────────────────────────────────


def _resolve_path(path: str, cwd: str | None) -> Path:
    """Resolve a possibly-relative path against the injected cwd."""
    p = Path(path)
    if cwd and not p.is_absolute():
        p = Path(cwd) / p
    return p


def run_bash(command: str, cwd: str = None) -> str:
    try:
        r = subprocess.run(
            command, shell=True, cwd=cwd or os.getcwd(),
            capture_output=True, text=True, errors="replace", timeout=120,
        )
        return _format_bash_result(r.stdout, r.stderr, r.returncode)
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def run_read(path: str, start_line: int = None, end_line: int = None,
             cwd: str = None) -> str:
    p = _resolve_path(path, cwd)
    if not p.exists():
        return f"Error: File not found: {path}"
    try:
        lines = p.read_text(errors="replace").splitlines(keepends=True)
    except Exception as e:
        return f"Error: {e}"
    if start_line is not None or end_line is not None:
        s = (start_line or 1) - 1
        e = end_line or len(lines)
        lines = lines[s:e]
        offset = s
    else:
        offset = 0
    numbered = [f"{i + offset + 1:>5}\t{line}" for i, line in enumerate(lines)]
    return "".join(numbered).rstrip() or "(empty file)"


def run_write(path: str, content: str, cwd: str = None) -> str:
    try:
        p = _resolve_path(path, cwd)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Written {len(content)} chars to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str, cwd: str = None) -> str:
    p = _resolve_path(path, cwd)
    if not p.exists():
        return f"Error: File not found: {path}"
    try:
        text = p.read_text(errors="replace")
    except Exception as e:
        return f"Error: {e}"
    count = text.count(old_text)
    if count == 0:
        return "Error: old_text not found in file"
    if count > 1:
        return f"Error: old_text found {count} times, must be unique"
    text = text.replace(old_text, new_text, 1)
    p.write_text(text)
    return f"Edited {path}"


def run_glob(pattern: str, cwd: str = None) -> str:
    base = Path(cwd) if cwd else Path(os.getcwd())
    matches = sorted(base.glob(pattern))
    if not matches:
        return "(no matches)"
    lines = []
    for m in matches[:200]:
        try:
            lines.append(str(m.relative_to(base)))
        except ValueError:
            lines.append(str(m))
    if len(matches) > 200:
        lines.append(f"... ({len(matches)} total matches, showing first 200. "
                     "Use a more specific pattern to narrow results.)")
    return "\n".join(lines)


def run_grep(pattern: str, path: str = ".", file_pattern: str = None,
             cwd: str = None) -> str:
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: Invalid regex: {e}"
    root = _resolve_path(path, cwd)
    if not root.exists():
        return f"Error: Path not found: {path}"
    results = []
    files = [root] if root.is_file() else root.rglob(file_pattern or "*")
    for f in files:
        if not f.is_file():
            continue
        try:
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if regex.search(line):
                    try:
                        rel = f.relative_to(Path(cwd) if cwd else os.getcwd())
                    except ValueError:
                        rel = f
                    results.append(f"{rel}:{i}: {line.strip()}")
        except (PermissionError, OSError):
            continue
        if len(results) > 500:
            results.append(
                f"... (truncated at 500 matches. "
                "Use a more specific pattern or file_pattern to narrow results.)"
            )
            break
    return "\n".join(results) if results else "(no matches)"


# ── Handler Factory ───────────────────────────────────────────


def make_coding_handlers(
    background_manager: BackgroundManager | None = None,
    default_cwd: str | None = None,
    backend: WorkspaceBackend | None = None,
) -> dict:
    """Build coding handlers bound to one background manager and workspace."""
    selected = (
        backend
        if backend is not None
        else LocalWorkspaceBackend(
            background_manager=background_manager,
            default_cwd=default_cwd,
        )
    )

    def bash(
        command: str,
        run_in_background: bool = False,
        cwd: str | None = None,
    ) -> str:
        return selected.bash(command, run_in_background, cwd=cwd)

    def read_file(
        path: str,
        start_line: int = None,
        end_line: int = None,
        cwd: str | None = None,
    ) -> str:
        return selected.read_file(path, start_line, end_line, cwd=cwd)

    def write_file(path: str, content: str, cwd: str | None = None) -> str:
        return selected.write_file(path, content, cwd=cwd)

    def edit_file(
        path: str,
        old_text: str,
        new_text: str,
        cwd: str | None = None,
    ) -> str:
        return selected.edit_file(path, old_text, new_text, cwd=cwd)

    def glob(pattern: str, cwd: str | None = None) -> str:
        return selected.glob(pattern, cwd=cwd)

    def grep(
        pattern: str,
        path: str = ".",
        file_pattern: str = None,
        cwd: str | None = None,
    ) -> str:
        return selected.grep(pattern, path, file_pattern, cwd=cwd)

    return {
        "bash": bash,
        "read_file": read_file,
        "write_file": write_file,
        "edit_file": edit_file,
        "glob": glob,
        "grep": grep,
    }
