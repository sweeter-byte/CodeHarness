"""Tool set smoke tests: base registry + per-agent-flavor visibility (Phase 2).

Verifies the compatibility requirement that Leader / SubAgent / Teammate
tool visibility is unchanged after the ToolRegistry refactor.
"""

from codeharness.tools import build_base_registry
from codeharness.tools.registry import DuplicateToolError

import pytest

BASE_TOOL_NAMES = [
    "bash", "read_file", "write_file", "edit_file", "glob", "grep",
    "todo_write", "load_skill", "read_artifact",
    "cron_create", "cron_list", "cron_delete",
]


def _names(schemas) -> list[str]:
    return [s["function"]["name"] for s in schemas]


def test_base_registry_matches_legacy_base_set():
    registry = build_base_registry()
    assert _names(registry.schemas) == BASE_TOOL_NAMES
    assert registry.names() == set(BASE_TOOL_NAMES)
    # every schema has a callable handler
    for name in BASE_TOOL_NAMES:
        assert callable(registry.get(name))


def test_base_registry_instances_are_independent():
    a = build_base_registry()
    b = build_base_registry()
    schema = {"type": "function", "function": {"name": "only_a"}}
    a.register(schema, lambda **kw: "")
    assert "only_a" in a.names()
    assert "only_a" not in b.names()


def test_subagent_tool_set_is_exactly_the_base_set():
    import subagent

    assert _names(subagent.SUB_TOOLS) == BASE_TOOL_NAMES
    assert set(subagent.SUB_HANDLERS) == set(BASE_TOOL_NAMES)
    assert "task" not in subagent.SUB_HANDLERS
    assert callable(subagent.TASK_HANDLERS["task"])


def test_teammate_tool_set_visibility():
    from team.teammate import TEAMMATE_TOOLS

    names = _names(TEAMMATE_TOOLS)
    # task-board order follows TASK_TOOLS declaration order (legacy behaviour)
    expected = (
        [n for n in BASE_TOOL_NAMES
         if n not in ("cron_create", "cron_list", "cron_delete")]
        + ["can_start", "claim_task", "complete_task", "list_task", "get_task"]
        + ["send_message", "submit_plan"]
    )
    assert names == expected
    forbidden = {
        "task", "create_task", "update_task", "release_task", "reset_tasks",
        "spawn_teammate", "shutdown_teammate", "list_teammates",
        "create_worktree", "remove_worktree", "approve_plan",
        "cron_create", "cron_list", "cron_delete",
    }
    assert forbidden.isdisjoint(names)


def test_leader_registry_assembly_and_duplicate_rejection():
    """Mirror agent_loop __main__: base + task + task system + team + MCP."""
    from subagent import TASK_TOOL, TASK_HANDLERS as SUB_TASK_HANDLERS
    from task_system import TASK_TOOLS, TASK_HANDLERS as TASK_SYS_HANDLERS
    from team.tools import TEAM_TOOLS, TEAM_HANDLERS

    registry = build_base_registry()
    registry.register(TASK_TOOL, SUB_TASK_HANDLERS["task"])
    registry.extend(TASK_TOOLS, TASK_SYS_HANDLERS)
    registry.extend(TEAM_TOOLS, TEAM_HANDLERS)

    # fake MCP tool joins through the same registry path
    mcp_schema = {"type": "function",
                  "function": {"name": "mcp__srv__tool", "parameters": {}}}
    registry.extend([mcp_schema], {"mcp__srv__tool": lambda **kw: ""})

    names = registry.names()
    assert set(BASE_TOOL_NAMES) <= names
    assert {"task", "send_message", "approve_plan", "mcp__srv__tool"} <= names
    assert {t["function"]["name"] for t in TASK_TOOLS} <= names
    assert len(registry.schemas) == len(names)  # no schema lost to a clash

    # a colliding registration fails loudly instead of overwriting
    with pytest.raises(DuplicateToolError):
        registry.register(mcp_schema, lambda **kw: "hijack")
    assert registry.get("mcp__srv__tool") is not None
