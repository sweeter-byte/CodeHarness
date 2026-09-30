"""Base tool registry — the tool set every agent flavor starts from.

Mirrors the legacy root tools.py base set exactly (Phase 2):
  bash, read_file, write_file, edit_file, glob, grep, todo_write,
  load_skill, read_artifact, cron_create, cron_list, cron_delete

Each call to build_base_registry() returns an INDEPENDENT registry, so
different agents can never mutate each other's tool set through a shared
list/dict.
"""

from codeharness.context.tools import CONTEXT_HANDLERS, CONTEXT_TOOLS
from codeharness.skills.tools import SKILL_HANDLERS, SKILL_TOOLS
from codeharness.tools.coding import CODING_HANDLERS, CODING_TOOLS
from codeharness.tools.registry import ToolRegistry
from codeharness.tools.todo import TODO_HANDLERS, TODO_TOOLS


def build_base_registry() -> ToolRegistry:
    """Assemble a fresh registry with the base tool set.

    The cron module still lives at the repo root (cron_scheduler.py); the
    import is deferred so importing this package never pulls the scheduler
    in as a side effect.
    """
    from cron_scheduler import CRON_HANDLERS, CRON_TOOLS

    registry = ToolRegistry()
    registry.extend(CODING_TOOLS, CODING_HANDLERS)
    registry.extend(TODO_TOOLS, TODO_HANDLERS)
    registry.extend(SKILL_TOOLS, SKILL_HANDLERS)
    registry.extend(CONTEXT_TOOLS, CONTEXT_HANDLERS)
    registry.extend(CRON_TOOLS, CRON_HANDLERS)
    return registry
