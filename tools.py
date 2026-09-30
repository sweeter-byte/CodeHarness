import json
import os
import re
import subprocess
from pathlib import Path

from codeharness.skills import SkillLoader
from codeharness.background.manager import _format_bash_result

# ── Skill Loader (module-level singleton) ─────────────────────
SKILL_LOADER = SkillLoader()
SKILL_LOADER.scan()

# ── Tool Schemas ─────────────────────────────────────────────

TOOLS = [
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
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": (
                "Create or update the task plan. Call it before starting multi-step "
                "work and update item statuses as you progress. Each update replaces "
                "the whole list (max 20 items); exactly one item may be in_progress."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": (
                            "Full task list. Each item: {content: str, "
                            "status: pending|in_progress|completed}"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "Short description of the step."},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "description": "Current state of the step.",
                                },
                            },
                            "required": ["content", "status"],
                        },
                    },
                },
                "required": ["todos"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "Load the full instructions of a skill by name. "
                "Use it when a skill's description matches the current task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The skill name as shown in the skills catalog.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_artifact",
            "description": (
                "Read a previously saved artifact by its ID. "
                "Use when you see 'artifact://xxx' references in earlier tool results "
                "and need the full content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "string",
                        "description": "The artifact ID from an artifact:// reference.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "1-based start line (optional, for large artifacts).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of lines to return (optional).",
                    },
                },
                "required": ["artifact_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cron_create",
            "description": (
                "Schedule a recurring or one-shot task. The prompt will be delivered "
                "to the agent at the specified time. Use cron expressions to define timing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cron": {
                        "type": "string",
                        "description": "5-field cron expression (minute hour day month weekday).",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Task description delivered to the agent when triggered.",
                    },
                    "recurring": {
                        "type": "boolean",
                        "description": "True for recurring, False for one-shot. Default True.",
                    },
                    "durable": {
                        "type": "boolean",
                        "description": "True to persist across restarts. Default True.",
                    },
                },
                "required": ["cron", "prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cron_list",
            "description": "Show all scheduled tasks: ID, cron expression, prompt, recurring status.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cron_delete",
            "description": "Remove a scheduled task by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "ID of the scheduled job to remove.",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
]

# ── Tool Implementations ─────────────────────────────────────
# Workspace tools accept an optional `cwd` keyword injected by the harness
# (e.g. teammate assignment directories). It is NOT part of any tool schema,
# so the model never supplies it. Threads must never os.chdir() — the
# process cwd is shared across the leader and all teammates.


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


# ── Todo Planning ─────────────────────────────────────────────

_TODO_STATUSES = {"pending", "in_progress", "completed"}
_TODO_MARKS = {"pending": " ", "in_progress": ">", "completed": "x"}
_MAX_TODO_ITEMS = 20


class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, todos: list | str) -> str:
        """Validate and replace the current list, then return the rendered view."""
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except json.JSONDecodeError as e:
                return f"Error: invalid JSON for todos: {e}"
        if not isinstance(todos, list) or not todos:
            return "Error: todos must be a non-empty list"
        if len(todos) > _MAX_TODO_ITEMS:
            return f"Error: too many items ({len(todos)}), max {_MAX_TODO_ITEMS}"

        validated = []
        in_progress_count = 0
        for raw in todos:
            if not isinstance(raw, dict):
                return "Error: each todo item must be an object"
            content = raw.get("content")
            if not isinstance(content, str) or not content.strip():
                return "Error: every todo item needs a non-empty 'content'"
            status = raw.get("status", "pending")
            if status not in _TODO_STATUSES:
                return f"Error: invalid status '{status}', must be one of {sorted(_TODO_STATUSES)}"
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"content": content.strip(), "status": status})

        if in_progress_count > 1:
            return "Error: only one item may be in_progress at a time"
        self.items = validated
        return self.render()

    def render(self) -> str:
        """[ ] pending, [>] in progress, [x] completed."""
        if not self.items:
            return "TODO: (empty — call todo_write to create your plan)"
        lines = [f"[{_TODO_MARKS[i['status']]}] {i['content']}" for i in self.items]
        done = sum(1 for i in self.items if i["status"] == "completed")
        return "TODO:\n" + "\n".join(lines) + f"\n({done}/{len(self.items)} completed)"

    def reset(self):
        self.items = []


TODO = TodoManager()


def run_todo_write(todos: list | str) -> str:
    output = TODO.update(todos)
    print(output)
    return output


def make_todo_handler(manager: TodoManager):
    """Build a todo_write handler bound to a per-agent TodoManager instance."""
    def handler(todos: list | str) -> str:
        output = manager.update(todos)
        print(output)
        return output
    return handler


def run_load_skill(name: str) -> str:
    return SKILL_LOADER.load(name)


def run_read_artifact(artifact_id: str, offset: int = None, limit: int = None) -> str:
    """Rehydration: read back a previously externalized artifact."""
    from codeharness.context.artifact_store import ARTIFACT_STORE
    return ARTIFACT_STORE.read(artifact_id, offset, limit)


# ── Cron Scheduler Tools ──────────────────────────────────────

def run_cron_create(cron: str, prompt: str, recurring: bool = True, durable: bool = True) -> str:
    from cron_scheduler import get_store, validate_cron
    store = get_store()
    if store is None:
        return "Error: Cron scheduler not initialized"
    err = validate_cron(cron)
    if err:
        return f"Error: Invalid cron expression - {err}"
    if not prompt or not prompt.strip():
        return "Error: Prompt cannot be empty"
    job = store.add(cron, prompt.strip(), recurring, durable)
    return f"Created {job.id}: cron='{job.cron}', recurring={job.recurring}, durable={job.durable}"


def run_cron_list() -> str:
    from cron_scheduler import get_store
    store = get_store()
    if store is None:
        return "(cron scheduler not initialized)"
    jobs = store.list_all()
    if not jobs:
        return "(no scheduled tasks)"
    lines = []
    for j in jobs:
        rec = "recurring" if j.recurring else "one-shot"
        dur = "durable" if j.durable else "memory"
        lines.append(f"{j.id} | {j.cron:<15} | {rec:<10} | {dur:<8} | {j.prompt}")
    return "\n".join(lines)


def run_cron_delete(job_id: str) -> str:
    from cron_scheduler import get_store
    store = get_store()
    if store is None:
        return "Error: Cron scheduler not initialized"
    if store.remove(job_id):
        return f"Deleted {job_id}"
    return f"Error: Job not found: {job_id}"


# ── Tool Handler Map ──────────────────────────────────────────

TOOL_HANDLERS = {
    "bash":       run_bash,
    "read_file":  run_read,
    "write_file": run_write,
    "edit_file":  run_edit,
    "glob":       run_glob,
    "grep":       run_grep,
    "todo_write": run_todo_write,
    "load_skill": run_load_skill,
    "read_artifact": run_read_artifact,
    "cron_create": run_cron_create,
    "cron_list":   run_cron_list,
    "cron_delete": run_cron_delete,
}
