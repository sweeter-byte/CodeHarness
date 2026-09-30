"""Tool Runtime — registry, coding tools, todo tool, base assembly.

Phase 2 layout: modules own their tools (schema + handler pairs) and the
ToolRegistry aggregates them for an Agent:

    Module owns Tool → ToolRegistry → Agent

Tool adapters that belong to other capability modules live there:
  - load_skill    → codeharness/skills/tools.py
  - read_artifact → codeharness/context/tools.py
  - cron_*        → codeharness/scheduler/cron.py
"""

from codeharness.tools.registry import DuplicateToolError, ToolRegistry
from codeharness.tools.base import build_base_registry
from codeharness.tools.todo import TodoManager, make_todo_handler

__all__ = [
    "ToolRegistry",
    "DuplicateToolError",
    "build_base_registry",
    "TodoManager",
    "make_todo_handler",
]
