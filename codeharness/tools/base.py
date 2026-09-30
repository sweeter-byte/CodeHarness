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
from codeharness.tools.coding import CODING_TOOLS, make_coding_handlers
from codeharness.tools.registry import ToolRegistry
from codeharness.tools.todo import TODO_TOOLS, TodoManager, make_todo_handler


def build_base_registry(
    todo_manager: TodoManager | None = None,
    background_manager=None,
    workspace: str | None = None,
) -> ToolRegistry:
    """Assemble a fresh registry with the base tool set.

    The todo_write handler is always bound to a per-registry TodoManager:
    the caller may inject one (so an Agent and its handler share state),
    otherwise a brand-new manager is created. There is never a
    process-global TodoManager behind the base registry.

    The cron scheduler lives in codeharness/scheduler/cron.py; the
    import is deferred so importing this package never pulls the scheduler
    in as a side effect.
    """
    from codeharness.scheduler.cron import CRON_HANDLERS, CRON_TOOLS

    if todo_manager is None:
        todo_manager = TodoManager()

    registry = ToolRegistry()
    registry.extend(
        CODING_TOOLS,
        make_coding_handlers(background_manager, default_cwd=workspace),
    )
    registry.extend(TODO_TOOLS, {"todo_write": make_todo_handler(todo_manager)})
    registry.extend(SKILL_TOOLS, SKILL_HANDLERS)
    registry.extend(CONTEXT_TOOLS, CONTEXT_HANDLERS)
    registry.extend(CRON_TOOLS, CRON_HANDLERS)
    return registry
