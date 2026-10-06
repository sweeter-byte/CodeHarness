"""Tool set smoke tests: base registry + per-agent-flavor visibility (Phase 2).

Verifies the compatibility requirement that Leader / SubAgent / Teammate
tool visibility is unchanged after the ToolRegistry refactor.
"""

from pathlib import Path

import pytest

from codeharness.skills import SkillRegistry
from codeharness.tools import build_base_registry, build_base_schemas
from codeharness.tools.registry import DuplicateToolError

BASE_TOOL_NAMES = [
    "bash", "read_file", "write_file", "edit_file", "glob", "grep",
    "todo_write", "load_skill", "read_artifact",
    "cron_create", "cron_list", "cron_delete",
]


def _names(schemas) -> list[str]:
    return [s["function"]["name"] for s in schemas]


def _skill_registry(tmp_path: Path, workspace_name: str = "workspace") -> SkillRegistry:
    return SkillRegistry(
        workspace=tmp_path / workspace_name,
        agent_home=tmp_path / "agent-home",
    )


def _write_project_skill(workspace: Path, content: str) -> None:
    manifest = workspace / ".codeharness" / "skills" / "bug-fix" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(content, encoding="utf-8")


def test_base_registry_matches_legacy_base_set(tmp_path):
    registry = build_base_registry(skill_registry=_skill_registry(tmp_path))
    assert _names(registry.schemas) == BASE_TOOL_NAMES
    assert registry.names() == set(BASE_TOOL_NAMES)
    # every schema has a callable handler
    for name in BASE_TOOL_NAMES:
        assert callable(registry.get(name))


def test_base_schema_snapshot_matches_legacy_base_set():
    first = build_base_schemas()
    second = build_base_schemas()
    original_description = second[0]["function"]["description"]

    assert _names(first) == BASE_TOOL_NAMES
    assert _names(second) == BASE_TOOL_NAMES
    assert first is not second

    first[0]["function"]["description"] = "mutated"

    assert second[0]["function"]["description"] == original_description


def test_base_registry_instances_are_independent(tmp_path):
    skills = _skill_registry(tmp_path)
    a = build_base_registry(skill_registry=skills)
    b = build_base_registry(skill_registry=skills)
    schema = {"type": "function", "function": {"name": "only_a"}}
    a.register(schema, lambda **kw: "")
    assert "only_a" in a.names()
    assert "only_a" not in b.names()


def test_base_registries_bind_different_skill_registries(tmp_path):
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    content_a = "---\nname: bug-fix\ndescription: A\n---\nA override\n"
    content_b = "---\nname: bug-fix\ndescription: B\n---\nB override\n"
    _write_project_skill(workspace_a, content_a)
    _write_project_skill(workspace_b, content_b)
    skills_a = _skill_registry(tmp_path, "workspace-a")
    skills_b = _skill_registry(tmp_path, "workspace-b")

    registry_a = build_base_registry(skill_registry=skills_a)
    registry_b = build_base_registry(skill_registry=skills_b)

    assert registry_a.get("load_skill")("bug-fix") == content_a
    assert registry_b.get("load_skill")("bug-fix") == content_b


def test_subagent_tool_set_is_exactly_the_base_set():
    from codeharness import subagent

    assert _names(subagent.SUB_TOOLS) == BASE_TOOL_NAMES
    assert not hasattr(subagent, "TASK_HANDLERS")


def test_teammate_tool_set_visibility():
    from codeharness.team.teammate import TEAMMATE_TOOLS

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


def test_leader_registry_assembly_and_duplicate_rejection(tmp_path):
    """Mirror CodeHarness.start: base + task + task system + team + MCP."""
    from codeharness.subagent import TASK_TOOL, make_task_handler
    from codeharness.tasks import TASK_HANDLERS as TASK_SYS_HANDLERS
    from codeharness.tasks import TASK_TOOLS
    from codeharness.team.tools import TEAM_TOOLS, make_team_handlers

    skills = _skill_registry(tmp_path)
    registry = build_base_registry(skill_registry=skills)
    registry.register(
        TASK_TOOL,
        make_task_handler(
            lambda **_kwargs: None,
            workspace=str(tmp_path),
            skill_registry=skills,
        ),
    )
    registry.extend(TASK_TOOLS, TASK_SYS_HANDLERS)
    registry.extend(TEAM_TOOLS, make_team_handlers(skills))

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


def test_agent_flavors_share_project_skill_catalog_and_override(tmp_path):
    from codeharness.subagent import make_task_handler
    from codeharness.team.teammate import (
        TeammateState,
        make_teammate_handlers,
        teammate_system,
    )

    workspace = tmp_path / "workspace"
    override = (
        "---\nname: bug-fix\ndescription: Workspace bug workflow\n---\n"
        "Use the project-specific reproducer.\n"
    )
    _write_project_skill(workspace, override)
    skills = _skill_registry(tmp_path)
    created = []

    class FakeAgent:
        def __init__(self, handlers):
            self.handlers = handlers

        def agent_loop(self, _messages):
            return self.handlers["load_skill"]("bug-fix")

    def factory(**kwargs):
        created.append(kwargs)
        bound = build_base_registry(skill_registry=kwargs["skill_registry"])
        return FakeAgent(bound.handlers)

    task_handler = make_task_handler(
        factory,
        workspace=str(workspace),
        skill_registry=skills,
    )
    state = TeammateState(name="Alice", require_plan=False)
    teammate_handlers = make_teammate_handlers(state, object(), skills)

    assert task_handler("load the bug-fix skill") == override
    assert teammate_handlers["load_skill"]("bug-fix") == override
    assert skills.catalog() in created[0]["system"]
    assert skills.catalog() in teammate_system(state, skills)
