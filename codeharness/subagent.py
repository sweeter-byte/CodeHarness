import os
from codeharness.tools import build_base_registry, TodoManager
from codeharness.skills.tools import SKILL_LOADER


def build_sub_system(workspace: str | None) -> str:
    effective_workspace = workspace or os.getcwd()
    return (
        f"You are a subagent at {effective_workspace}, delegated a specific subtask "
        "by a parent agent. Use tools to complete it. Act, don't explain.\n"
        "If the subtask is complex and multi-step, you MAY call todo_write to track "
        "your own plan; skip it for simple tasks.\n"
        "Your intermediate messages are DISCARDED — the parent sees ONLY your final "
        "text. So your last message must be a complete, self-contained summary of "
        "the result (findings, file changes, or why you failed).\n\n"
        f"Skills available:\n{SKILL_LOADER.catalog()}\n\n"
        "Use load_skill to read the full instructions when a skill applies."
    )


SUB_MAX_ROUNDS = 30


_agent_factory = None
_workspace = None


def configure_agent_factory(factory, workspace: str | None = None) -> None:
    """Configure the Runtime-owned factory and workspace for one-shot agents."""
    global _agent_factory, _workspace
    _agent_factory = factory
    _workspace = workspace


# The subagent tool set is exactly the base tool set, built explicitly from
# its own registry — never a filtered view of the leader's pool. 'task'
# (second-level delegation), task system tools, team tools and MCP tools are
# Leader-only by construction: they are registered by the leader Runtime
# registry, not in the base registry.
SUB_TOOLS = build_base_registry().schemas

TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "task",
        "description": (
            "Run a subagent with a fresh conversation context and return its final text. "
            "Use it for self-contained subtasks (e.g. tracing a call chain, exploring code) "
            "whose intermediate tool results you don't need to keep. "
            "The prompt must be fully self-contained: the subagent sees NOTHING else."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Complete, standalone description of the subtask.",
                },
            },
            "required": ["prompt"],
        },
    },
}


def run_task(prompt: str) -> str:
    """Run a nested agent loop in a fresh context; return its final text."""
    from codeharness.background import BackgroundManager

    if _agent_factory is None:
        raise RuntimeError(
            "SubAgent agent factory is not configured; start CodeHarness first"
        )

    print(f"\033[35m[subagent] starting: {prompt[:100]}\033[0m")
    sub_todo = TodoManager()              # per-subagent TODO, discarded with the sub-loop
    sub_bg = BackgroundManager()          # per-subagent background tasks

    sub = _agent_factory(
        system=build_sub_system(_workspace),
        max_rounds=SUB_MAX_ROUNDS,
        todo_manager=sub_todo,
        background_manager=sub_bg,
        interactive=True,
    )
    messages = [{"role": "user", "content": prompt}]
    result = sub.agent_loop(messages) or "(no summary)"
    print(f"\033[35m[subagent] done ({len(result)} chars)\033[0m")
    return result


TASK_HANDLERS = {"task": run_task}
