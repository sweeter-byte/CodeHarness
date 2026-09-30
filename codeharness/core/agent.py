import json
import os
from collections.abc import Callable
from openai import OpenAI
from codeharness.tools import build_base_registry, TodoManager
from codeharness.tools.todo import TODO as DEFAULT_TODO
import hooks
from hooks import SESSION_STATS, trigger_hooks
from codeharness.context.budget import ContextBudget
from codeharness.context.token_counter import TokenCounter
from codeharness.context.manager import ContextManager
from codeharness.memory import MemoryManager
from codeharness.background import BACKGROUND, BackgroundManager, should_run_background
from codeharness.core.prompt import build_default_system_prompt
from codeharness.config import DEFAULT_MODEL_CONTEXT_WINDOW


MAX_CONSECUTIVE_REJECTIONS = 3
TODO_TOOL_NAME = "todo_write"
TODO_REMINDER_ROUNDS = 3
MAX_REACTIVE_RETRIES = 1

# Default tool set for an Agent constructed without explicit tools/handlers
# (the leader Runtime path passes its fully-assembled registry snapshot instead).
# These are per-module snapshots, never a shared mutable pool.
_BASE_REGISTRY = build_base_registry()
DEFAULT_TOOLS = _BASE_REGISTRY.schemas
DEFAULT_HANDLERS = _BASE_REGISTRY.handlers


class Agent:
    def __init__(self, system: str = None, tools: list = None,
                 handlers: dict = None, max_rounds: int = None,
                 todo_manager: TodoManager = None,
                 memory_manager: MemoryManager = None,
                 background_manager: BackgroundManager = None,
                 interactive: bool = True,
                 client=None,
                 model: str = None,
                 model_context_window: int = None,
                 workspace: str = None,
                 approval_handler: Callable[[str], bool] | None = None,
                 status_handler: Callable[[str], None] | None = None):
        self.client = client if client is not None else OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ["DEEPSEEK_BASE_URL"],
        )
        self.model = model if model is not None else os.environ["DEEPSEEK_MODEL_ID"]
        self.model_context_window = (
            model_context_window
            if model_context_window is not None
            else int(os.environ.get(
                "MODEL_CONTEXT_WINDOW", str(DEFAULT_MODEL_CONTEXT_WINDOW)
            ))
        )

        # ── Memory Management ──
        # memory_manager=False disables memory entirely (teammates): avoids
        # concurrent .memory/ writes; the leader owns cross-session memory.
        if memory_manager is False:
            self.memory_manager = None
        else:
            self.memory_manager = memory_manager if memory_manager is not None \
                else MemoryManager(client=self.client, model=self.model)

        # ── Interaction mode ──
        # interactive=False: permission 'ask' decisions are rejected without
        # consulting an approval handler — required for teammate daemon threads.
        self.interactive = interactive
        self.approval_handler = approval_handler
        self.status_handler = status_handler

        base_system = system or build_default_system_prompt(workspace or os.getcwd())

        memory_block = self.memory_manager.load_relevant([]) if self.memory_manager else ""
        self.system = base_system + memory_block if memory_block else base_system
        self.tools = tools if tools is not None else list(DEFAULT_TOOLS)
        self.handlers = handlers if handlers is not None else dict(DEFAULT_HANDLERS)
        self.max_rounds = max_rounds
        self.todo_manager = todo_manager if todo_manager is not None else DEFAULT_TODO
        self.background_manager = background_manager if background_manager is not None \
            else BACKGROUND

        # ── Context Management ──
        self.token_counter = TokenCounter()
        _fixed = self.token_counter.estimate_tokens(self.system) + \
                 self.token_counter.estimate_tokens(str(self.tools))
        self.context_budget = ContextBudget.from_model_config(
            model_window=self.model_context_window,
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

    def _emit_status(self, message: str) -> None:
        if self.status_handler is not None:
            self.status_handler(message)

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
        hooks.PENDING_USER_ASK.value = None

        # ── PreToolUse ──
        hook_result = trigger_hooks("PreToolUse", tool_name, args)

        if hook_result is not None:
            # A hook returned non-None → execution blocked.
            output = hook_result
            self._emit_status(f"\033[31m✗ BLOCKED {tool_name}: {output}\033[0m")
            executed = False
        elif hooks.PENDING_USER_ASK.value is not None:
            # Permission hook flagged "ask" → prompt the user.
            reason = hooks.PENDING_USER_ASK.value
            hooks.PENDING_USER_ASK.value = None
            if not self.interactive:
                # Non-interactive (teammate) mode: never touch the terminal.
                # Dangerous operations are escalated to the leader via send_message.
                output = (
                    f"Error: This operation requires user approval, which is "
                    f"not available for teammates - {reason}. Do NOT retry. "
                    "Report the blocker to your leader via send_message, or "
                    "adjust your approach to avoid this operation."
                )
                self._emit_status(
                    f"\033[31m  ✗ [non-interactive] rejected: {reason}\033[0m"
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": output,
                })
                return False
            if self.approval_handler is None:
                output = (
                    "Error: This operation requires user approval, but no "
                    "approval handler is configured. Operation rejected."
                )
                self._emit_status(
                    f"\033[31m  ✗ Approval unavailable; rejected: {reason}\033[0m"
                )
                executed = False
            elif not self.approval_handler(reason):
                output = (
                    "Error: User rejected this operation. "
                    "Do NOT retry via alternative commands or paths. "
                    "If you cannot complete the task without this operation, "
                    "report what you have done and stop."
                )
                self._emit_status("\033[31m  ✗ Rejected by user\033[0m")
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
                    self._emit_status(
                        f"\033[31m✗ {consecutive_rejections} consecutive "
                        "rejections, stopping agent loop.\033[0m"
                    )
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
                        self._emit_status(
                            "\033[33m⚠ Context overflow, reactive compacting...\033[0m"
                        )
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

