import os
from tools import TOOLS, TOOL_HANDLERS, SKILL_LOADER
from task_system import TASK_TOOLS

SUB_SYSTEM = (
    f"You are a subagent at {os.getcwd()}, delegated a specific subtask by a parent agent. "
    "Use tools to complete it. Act, don't explain.\n"
    "If the subtask is complex and multi-step, you MAY call todo_write to track your own "
    "plan; skip it for simple tasks.\n"
    "Your intermediate messages are DISCARDED — the parent sees ONLY your final text. "
    "So your last message must be a complete, self-contained summary of the result "
    "(findings, file changes, or why you failed).\n\n"
    f"Skills available:\n{SKILL_LOADER.catalog()}\n\n"
    "Use load_skill to read the full instructions when a skill applies."
)

SUB_MAX_ROUNDS = 30

# Inherit every base tool except 'task' and task system tools — the structural
# one-level-delegation guarantee. (SUB_TOOLS is built at import time, before
# __main__ appends TASK_TOOL / TASK_TOOLS to TOOLS; the explicit filter is
# defensive against any import-order change.)
_TASK_SYS_NAMES = {t["function"]["name"] for t in TASK_TOOLS}
SUB_TOOLS = [
    t for t in TOOLS
    if t["function"]["name"] != "task" and t["function"]["name"] not in _TASK_SYS_NAMES
]

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
    from agent_loop import Agent          # lazy import to avoid circular dependency
    from tools import TodoManager, make_todo_handler
    from codeharness.background import BackgroundManager

    print(f"\033[35m[subagent] starting: {prompt[:100]}\033[0m")
    sub_todo = TodoManager()              # per-subagent TODO, discarded with the sub-loop
    sub_bg = BackgroundManager()          # per-subagent background tasks
    sub_handlers = {k: v for k, v in TOOL_HANDLERS.items() if k != "todo_write"}
    sub_handlers["todo_write"] = make_todo_handler(sub_todo)

    sub = Agent(
        system=SUB_SYSTEM,
        tools=SUB_TOOLS,
        handlers=sub_handlers,
        max_rounds=SUB_MAX_ROUNDS,
        todo_manager=sub_todo,
        background_manager=sub_bg,
    )
    messages = [{"role": "user", "content": prompt}]
    result = sub.agent_loop(messages) or "(no summary)"
    print(f"\033[35m[subagent] done ({len(result)} chars)\033[0m")
    return result


TASK_HANDLERS = {"task": run_task}
