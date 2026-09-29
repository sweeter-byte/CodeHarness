import json
import os
from dotenv import load_dotenv
from openai import OpenAI
from tools import TOOLS, TOOL_HANDLERS, TODO, TodoManager, SKILL_LOADER
import hooks
from hooks import SESSION_STATS, trigger_hooks
from context.budget import ContextBudget
from context.token_counter import TokenCounter
from context.manager import ContextManager
from memory import MemoryManager
from background import BACKGROUND, BackgroundManager, should_run_background

load_dotenv(override=True)

MAX_CONSECUTIVE_REJECTIONS = 3
TODO_TOOL_NAME = "todo_write"
TODO_REMINDER_ROUNDS = 3
MAX_REACTIVE_RETRIES = 1
MODEL_CONTEXT_WINDOW = int(os.environ.get("MODEL_CONTEXT_WINDOW", "1048576"))


class Agent:
    def __init__(self, system: str = None, tools: list = None,
                 handlers: dict = None, max_rounds: int = None,
                 todo_manager: TodoManager = None,
                 memory_manager: MemoryManager = None,
                 background_manager: BackgroundManager = None):
        self.client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ["DEEPSEEK_BASE_URL"],
        )
        self.model = os.environ["DEEPSEEK_MODEL_ID"]

        # ── Memory Management ──
        self.memory_manager = memory_manager if memory_manager is not None \
            else MemoryManager(client=self.client, model=self.model)

        base_system = system or (
            f"You are a coding agent at {os.getcwd()}. Use tools to solve tasks. Act, don't explain.\n"
            f"For any multi-step task, FIRST call {TODO_TOOL_NAME} to list the plan, "
            "then update item statuses as you work; keep exactly one item in_progress.\n"
            "Delegate self-contained subtasks (e.g. tracing a call chain across many files) "
            "to the 'task' tool so their intermediate steps don't pollute your context.\n\n"
            f"Skills available:\n{SKILL_LOADER.catalog()}\n\n"
            "Use load_skill to read the full instructions when a skill applies."
        )

        memory_block = self.memory_manager.load_relevant([])
        self.system = base_system + memory_block if memory_block else base_system
        self.tools = tools if tools is not None else TOOLS
        self.handlers = handlers if handlers is not None else TOOL_HANDLERS
        self.max_rounds = max_rounds
        self.todo_manager = todo_manager if todo_manager is not None else TODO
        self.background_manager = background_manager if background_manager is not None \
            else BACKGROUND

        # ── Context Management ──
        self.token_counter = TokenCounter()
        _fixed = self.token_counter.estimate_tokens(self.system) + \
                 self.token_counter.estimate_tokens(str(self.tools))
        self.context_budget = ContextBudget.from_model_config(
            model_window=MODEL_CONTEXT_WINDOW,
            max_output_tokens=8000,
            fixed_context_tokens=_fixed,
        )
        self.context_manager = ContextManager(
            budget=self.context_budget,
            token_counter=self.token_counter,
        )
        self.context_manager.configure_llm(self.client, self.model)
        self.context_manager.system_prompt = self.system
        self.context_manager.tool_schemas = self.tools

    def _accumulate_tokens(self, response):
        """Add token usage from an LLM response to SESSION_STATS."""
        if response.usage:
            SESSION_STATS["prompt_tokens"] += response.usage.prompt_tokens
            SESSION_STATS["completion_tokens"] += response.usage.completion_tokens
            SESSION_STATS["total_tokens"] += response.usage.total_tokens

    def _call_llm(self, messages: list):
        """Single LLM call; accumulates tokens automatically."""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": self.system}] + messages,
            tools=self.tools,
            max_tokens=8000,
        )
        self._accumulate_tokens(response)
        self.token_counter.update_from_response(response)
        return response

    def _execute_tool(self, handler, tool_call_id, tool_name, args, messages):
        """
        Run PreToolUse hooks → execute (or skip) → run PostToolUse hooks.
        Returns True if the tool actually executed, False if blocked/rejected.
        """
        # Reset the "ask user" flag before each tool call.
        hooks.PENDING_USER_ASK = None

        # ── PreToolUse ──
        hook_result = trigger_hooks("PreToolUse", tool_name, args)

        if hook_result is not None:
            # A hook returned non-None → execution blocked.
            output = hook_result
            print(f"\033[31m✗ BLOCKED {tool_name}: {output}\033[0m")
            executed = False
        elif hooks.PENDING_USER_ASK is not None:
            # Permission hook flagged "ask" → prompt the user.
            reason = hooks.PENDING_USER_ASK
            print(f"\033[33m  ⚠ {reason}\033[0m")
            answer = input("\033[33m  Allow? [y/N]: \033[0m").strip().lower()
            if answer != "y":
                output = (
                    "Error: User rejected this operation. "
                    "Do NOT retry via alternative commands or paths. "
                    "If you cannot complete the task without this operation, "
                    "report what you have done and stop."
                )
                print("\033[31m  ✗ Rejected by user\033[0m")
                executed = False
            else:
                if should_run_background(tool_name, args):
                    bg_id, error = self.background_manager.start(args["command"])
                    if bg_id is not None:
                        output = f"[Background task {bg_id} started: {args['command']}]"
                    else:
                        output = error
                else:
                    output = handler(**args)
                executed = True
        else:
            # All hooks passed.
            if should_run_background(tool_name, args):
                bg_id, error = self.background_manager.start(args["command"])
                if bg_id is not None:
                    output = f"[Background task {bg_id} started: {args['command']}]"
                else:
                    output = error
            else:
                output = handler(**args)
            executed = True

        # ── PostToolUse ──
        if executed:
            SESSION_STATS["tool_calls"] += 1
            trigger_hooks("PostToolUse", tool_name, args, output)

        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": output,
        })
        return executed

    def agent_loop(self, messages: list) -> str:
        """Run until a final answer / rejection-stop / max_rounds.
        Returns the final assistant text (used as tool result by subagents)."""
        consecutive_rejections = 0
        rounds_since_todo = 0
        rounds = 0
        reactive_retries = 0
        normal_exit = True

        # ── Memory Recall: load relevant memories for this request ──
        if messages and self.memory_manager:
            memory_block = self.memory_manager.load_relevant(messages)
            if memory_block:
                self.system = self.system.rstrip() + memory_block

        # Track active request (Protected Context)
        for msg in reversed(messages):
            if msg.get("role") == "user" and not msg.get("tool_call_id"):
                self.context_manager.set_active_request(
                    str(msg.get("content", ""))
                )
                break

        try:
            while True:
                if self.max_rounds is not None and rounds >= self.max_rounds:
                    normal_exit = False
                    return f"Agent stopped after {self.max_rounds} rounds without a final answer."
                rounds += 1

                # ── Background: collect completed results ──
                notifications = self.background_manager.collect()
                for notification in notifications:
                    messages.append({
                        "role": "user",
                        "content": notification,
                    })

                # ── Context Management: prepare before LLM call ──
                messages[:] = self.context_manager.prepare(messages)

                # If user rejected too many times, force-stop the loop.
                if consecutive_rejections >= MAX_CONSECUTIVE_REJECTIONS:
                    normal_exit = False
                    messages.append({
                        "role": "user",
                        "content": f"The user has rejected {consecutive_rejections} consecutive operations. "
                                   "Do NOT retry. Report what you have accomplished so far and stop.",
                    })
                    print(f"\033[31m✗ {consecutive_rejections} consecutive rejections, stopping agent loop.\033[0m")
                    consecutive_rejections = 0
                    response = self._call_llm(messages)
                    msg = response.choices[0].message
                    messages.append(msg.model_dump())
                    return msg.content or ""

                try:
                    response = self._call_llm(messages)
                except Exception as e:
                    if ("context" in str(e).lower() or "token" in str(e).lower()
                            ) and reactive_retries < MAX_REACTIVE_RETRIES:
                        print("\033[33m⚠ Context overflow, reactive compacting...\033[0m")
                        messages[:] = self.context_manager.reactive_compact(messages)
                        reactive_retries += 1
                        continue
                    raise
                reactive_retries = 0
                msg = response.choices[0].message
                messages.append(msg.model_dump())

                if not msg.tool_calls:
                    if normal_exit and self.memory_manager:
                        self.memory_manager.extract_memories(messages)
                        self.memory_manager.consolidate_memories()
                    return msg.content or ""

                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    handler = self.handlers.get(tc.function.name)

                    if handler is None:
                        output = f"Error: Unknown tool '{tc.function.name}'"
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": output,
                        })
                        continue

                    executed = self._execute_tool(handler, tc.id, tc.function.name, args, messages)
                    if not executed:
                        consecutive_rejections += 1
                    else:
                        consecutive_rejections = 0

                # ── Todo reminder (催更机制) ──
                # Count once per round; using todo_write in this round resets it.
                if any(tc.function.name == TODO_TOOL_NAME for tc in msg.tool_calls):
                    rounds_since_todo = 0
                else:
                    rounds_since_todo += 1
                    if rounds_since_todo >= TODO_REMINDER_ROUNDS:
                        # messages[-1] is this round's last tool message; rewrite its
                        # content to inject the reminder (tool_call_id cannot be reused).
                        messages[-1]["content"] += (
                            f"\n\n<reminder>Call {TODO_TOOL_NAME} to update your todos.</reminder>\n"
                            + self.todo_manager.render()
                        )
                        rounds_since_todo = 0
        finally:
            self.background_manager.cancel_all()

    def handle_slash_command(self, query: str, history: list) -> bool:
        """Handle /context, /compact, /clear. Returns True if handled."""
        cmd = query.strip().lower()
        if not cmd.startswith("/"):
            return False
        if cmd == "/context":
            print(self.context_manager.get_observability_report(history))
        elif cmd == "/compact":
            history[:] = self.context_manager.compact_history(history)
            print("\033[33m✓ Context compacted.\033[0m")
        elif cmd == "/clear":
            history.clear()
            self.context_manager.set_active_request("")
            print("\033[33m✓ Context cleared.\033[0m")
        else:
            print(f"Unknown command: {cmd}")
            print("Available: /context, /compact, /clear")
        return True


if __name__ == "__main__":
    from subagent import TASK_TOOL, TASK_HANDLERS as SUB_TASK_HANDLERS
    from task_system import TASK_TOOLS, TASK_HANDLERS as TASK_SYS_HANDLERS

    # Compose the parent agent's full tool set: base tools + delegation + task system.
    # subagent.py imported SUB_TOOLS before this append, so subagents never see these.
    TOOLS.append(TASK_TOOL)
    TOOLS.extend(TASK_TOOLS)
    TOOL_HANDLERS.update(SUB_TASK_HANDLERS)
    TOOL_HANDLERS.update(TASK_SYS_HANDLERS)

    agent = Agent()
    print("Agent Loop (type q to quit, /context /compact /clear for context mgmt)\n")

    history = []
    while True:
        try:
            query = input("\033[36m>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        # ── Slash Commands (intercepted before entering agent loop) ──
        if agent.handle_slash_command(query, history):
            continue

        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent.agent_loop(history)

        last = history[-1]
        if last.get("content"):
            print(last["content"])
        print()

    # Session ended 
    trigger_hooks("Stop", SESSION_STATS)
