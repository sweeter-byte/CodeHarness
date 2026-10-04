import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from openai import OpenAI
from codeharness.tools import build_base_registry, TodoManager
from codeharness import hooks
from codeharness.hooks import new_session_stats, trigger_hooks
from codeharness.tools.executor import ToolExecutor
from codeharness.context.budget import ContextBudget
from codeharness.context.token_counter import TokenCounter
from codeharness.context.manager import ContextManager
from codeharness.memory import MemoryManager
from codeharness.background import BackgroundManager
from codeharness.core.prompt import build_default_system_prompt
from codeharness.config import DEFAULT_MODEL_CONTEXT_WINDOW
from codeharness.goal import GoalController, StopDecision


MAX_CONSECUTIVE_REJECTIONS = 3
TODO_TOOL_NAME = "todo_write"
TODO_REMINDER_ROUNDS = 3
MAX_REACTIVE_RETRIES = 1


class Agent:
    def __init__(self, system: str = None, tools: list = None,
                 handlers: dict = None, max_rounds: int = None,
                 todo_manager: TodoManager = None,
                 memory_manager: MemoryManager = None,
                 memory_dir: str | Path | None = None,
                 background_manager: BackgroundManager = None,
                 session_stats: dict = None,
                 session_stats_lock: threading.RLock = None,
                 interactive: bool = True,
                 client=None,
                 model: str = None,
                 model_context_window: int = None,
                 workspace: str = None,
                 approval_handler: Callable[[str], bool] | None = None,
                 status_handler: Callable[[str], None] | None = None,
                 goal_controller: GoalController = None,
                 workflow_catalog: str = "(no workflows available)"):
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
        # concurrent memory writes; the leader owns cross-session memory.
        if memory_manager is False:
            self.memory_manager = None
        else:
            self.memory_manager = memory_manager if memory_manager is not None \
                else MemoryManager(memory_dir=memory_dir, client=self.client, model=self.model)

        # ── Interaction mode ──
        # interactive=False: permission 'ask' decisions are rejected without
        # consulting an approval handler — required for teammate daemon threads.
        self.interactive = interactive
        self.approval_handler = approval_handler
        self.status_handler = status_handler

        self.workspace = workspace
        base_system = system or build_default_system_prompt(
            self.workspace or os.getcwd(),
            workflow_catalog=workflow_catalog,
        )

        memory_block = self.memory_manager.load_relevant([]) if self.memory_manager else ""
        self.system = base_system + memory_block if memory_block else base_system

        # ── Per-instance mutable state ──
        # Never a process-global default: each Agent owns its TodoManager,
        # BackgroundManager and (unless shared by a Runtime) session stats.
        self.todo_manager = todo_manager if todo_manager is not None else TodoManager()
        self.background_manager = background_manager if background_manager is not None \
            else BackgroundManager()
        # Every Agent owns local usage while session_stats remains the Runtime
        # aggregate shared by all Agents it creates.  The objects are always
        # distinct, including for a standalone Agent.
        self.local_stats = new_session_stats()
        self.session_stats = session_stats if session_stats is not None \
            else new_session_stats()
        self.session_stats_lock = session_stats_lock if session_stats_lock is not None \
            else threading.RLock()
        self._tool_executor = ToolExecutor(trigger_hooks)

        # When no explicit tool set is supplied, build a fresh registry bound
        # to THIS Agent's TodoManager so todo_write and self.todo_manager share
        # state. Explicit tools/handlers (the leader Runtime path) are kept as-is.
        if tools is None or handlers is None:
            registry = build_base_registry(
                todo_manager=self.todo_manager,
                background_manager=self.background_manager,
                workspace=self.workspace,
            )
            self.tools = tools if tools is not None else registry.schemas
            self.handlers = handlers if handlers is not None else registry.handlers
        else:
            self.tools = tools
            self.handlers = handlers
        self.max_rounds = max_rounds

        # ── Goal Loop ──
        # Only the Leader Agent holds a real controller; SubAgent / Teammate
        # use the null instance (always ALLOW).
        self.goal_controller = goal_controller if goal_controller is not None \
            else GoalController.null()

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
        """Add one LLM response to Agent-local and Runtime aggregate usage."""
        if response.usage:
            usage = response.usage
            self.local_stats["prompt_tokens"] += usage.prompt_tokens
            self.local_stats["completion_tokens"] += usage.completion_tokens
            self.local_stats["total_tokens"] += usage.total_tokens
            with self.session_stats_lock:
                self.session_stats["prompt_tokens"] += usage.prompt_tokens
                self.session_stats["completion_tokens"] += usage.completion_tokens
                self.session_stats["total_tokens"] += usage.total_tokens

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
        Returns True if the handler was attempted, False if policy prevented it.
        """
        result = self._tool_executor.execute(
            tool_name=tool_name,
            args=args,
            handler=handler,
            interactive=self.interactive,
            approval_handler=self.approval_handler,
            session_stats=self.session_stats,
        )

        if result.status == "blocked":
            self._emit_status(
                f"\033[31m✗ BLOCKED {tool_name}: {result.output}\033[0m"
            )
        elif result.status == "approval_unavailable":
            if not self.interactive:
                self._emit_status(
                    f"\033[31m  ✗ [non-interactive] rejected: "
                    f"{result.reason}\033[0m"
                )
            else:
                self._emit_status(
                    f"\033[31m  ✗ Approval unavailable; rejected: "
                    f"{result.reason}\033[0m"
                )
        elif result.status == "rejected":
            self._emit_status("\033[31m  ✗ Rejected by user\033[0m")

        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result.output,
        })
        return result.status not in {
            "blocked",
            "approval_unavailable",
            "rejected",
        }

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
                    # ── Goal Gate: evaluate before exiting ──
                    decision = self.goal_controller.evaluate_after_turn(
                        messages, self.background_manager
                    )

                    if decision in (StopDecision.ALLOW, StopDecision.ACHIEVED):
                        if normal_exit and self.memory_manager:
                            self.memory_manager.extract_memories(messages)
                            self.memory_manager.consolidate_memories()
                        return msg.content or ""

                    if decision == StopDecision.BLOCK:
                        feedback = self.goal_controller.build_feedback_message()
                        messages.append({"role": "user", "content": feedback})
                        continue

                    if decision == StopDecision.DEFER:
                        messages.append({
                            "role": "user",
                            "content": (
                                "[Goal Gate] Background tasks still running. "
                                "Wait for results before concluding."
                            ),
                        })
                        continue

                    # FAILED / ERROR / LIMIT → safe exit, goal stays active
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

                    attempted = self._execute_tool(
                        handler, tc.id, tc.function.name, args, messages
                    )
                    if attempted:
                        consecutive_rejections = 0
                    else:
                        consecutive_rejections += 1

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
