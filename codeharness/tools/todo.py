"""Todo planning tool — per-agent lightweight task list.

Migrated verbatim from the legacy root tools.py (Phase 2); the TODO system
is NOT redesigned here. TodoManager instances are per-agent (leader,
subagents and teammates each own one); there is no process-global default
— every registry / Agent creates its own TodoManager.
"""

import json

# ── Tool Schema ───────────────────────────────────────────────

TODO_TOOLS = [
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
]

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


def make_todo_handler(manager: TodoManager):
    """Build a todo_write handler bound to a per-agent TodoManager instance."""
    def handler(todos: list | str) -> str:
        output = manager.update(todos)
        print(output)
        return output
    return handler
