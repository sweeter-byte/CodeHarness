"""Application runtime and composition root for CodeHarness."""

import threading
from collections.abc import Callable

from openai import OpenAI

from codeharness import hooks
from codeharness.hooks import new_session_stats, trigger_hooks
from codeharness.scheduler import cron
from codeharness.subagent import (
    TASK_HANDLERS as SUB_TASK_HANDLERS,
    TASK_TOOL,
    configure_agent_factory as configure_subagent_agent_factory,
)
from codeharness.tasks import TASK_HANDLERS as TASK_SYS_HANDLERS, TASK_TOOLS
from codeharness.team import TEAM, TEAM_HANDLERS, TEAM_TOOLS
from codeharness.team import wakeup as team_wakeup
from codeharness.workflow import (
    WORKFLOWS,
    WORKFLOW_HANDLERS,
    WORKFLOW_RUNTIME,
    WORKFLOW_TOOLS,
)
from codeharness.workflow.builtin import register_builtins as register_builtin_workflows
from codeharness.goal import GoalController, GoalEvaluator, GOAL_TOOLS, make_goal_handlers

from codeharness.config import RuntimeConfig
from codeharness.core.agent import Agent
from codeharness.background import BackgroundManager
from codeharness.mcp import (
    MCPConfigError,
    MCPManager,
    load_config,
    shutdown_runtime,
)
from codeharness.tools import build_base_registry, TodoManager


CLOSE_TIMEOUT = 5.0


class CodeHarness:
    """Unified backend API for one leader Agent runtime."""

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.client = None
        self.registry = None
        self.mcp_manager = None
        self.agent = None
        self.history = None
        self.goal_controller = None
        self.approval_handler = None
        self.status_handler = None
        self.async_result_handler: Callable[[str], None] | None = None
        # Runtime-owned mutable state: never shared with another Runtime.
        self.todo_manager = None
        self.background_manager = None
        self.session_stats = new_session_stats()
        self._turn_lock = threading.Lock()
        self._user_turn_pending = threading.Event()
        self._closing = threading.Event()
        self._started = False
        self._closed = False
        self._cron_started = False
        self._team_wakeup_started = False
        self._workflow_started = False

    @classmethod
    def from_env(cls) -> "CodeHarness":
        return cls(RuntimeConfig.from_env())

    def set_approval_handler(self, handler) -> None:
        self.approval_handler = handler
        if self.agent is not None:
            self.agent.approval_handler = handler

    def set_status_handler(self, handler) -> None:
        self.status_handler = handler
        if self.agent is not None:
            self.agent.status_handler = handler

    def set_async_result_handler(
        self, handler: Callable[[str], None] | None
    ) -> None:
        self.async_result_handler = handler

    def _emit_status(self, message: str) -> None:
        if self.status_handler is not None:
            self.status_handler(message)

    def create_agent(self, **kwargs):
        """Create an Agent with this Runtime's shared model configuration."""
        if self.client is None:
            raise RuntimeError("CodeHarness.start() must initialize the client first")
        defaults = {
            "client": self.client,
            "model": self.config.model,
            "model_context_window": self.config.model_context_window,
            "workspace": str(self.config.workspace),
            "approval_handler": self.approval_handler,
            "status_handler": self.status_handler,
            "session_stats": self.session_stats,
        }
        defaults.update(kwargs)
        return Agent(**defaults)

    def start(self) -> "CodeHarness":
        """Assemble and start the complete leader runtime once."""
        if self._closed:
            raise RuntimeError("CodeHarness has already been closed")
        if self._closing.is_set():
            raise RuntimeError("CodeHarness is closing")
        if self._started:
            return self

        self.client = OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
        )
        # Root native-tool permissions at the configured workspace, not at
        # the import-time cwd. Dependency direction stays Runtime → Permission.
        hooks.configure_permissions([str(self.config.workspace)])
        configure_subagent_agent_factory(
            self.create_agent,
            workspace=str(self.config.workspace),
        )
        TEAM.set_agent_factory(self.create_agent)

        # Runtime-owned mutable state for the leader.
        self.todo_manager = TodoManager()
        self.background_manager = BackgroundManager()

        registry = build_base_registry(
            todo_manager=self.todo_manager,
            background_manager=self.background_manager,
            workspace=str(self.config.workspace),
        )
        registry.register(TASK_TOOL, SUB_TASK_HANDLERS["task"])
        registry.extend(TASK_TOOLS, TASK_SYS_HANDLERS)
        registry.extend(TEAM_TOOLS, TEAM_HANDLERS)

        # ── Goal Loop ──
        evaluator_model = self.config.evaluator_model or self.config.model
        self.goal_controller = GoalController(
            evaluator=GoalEvaluator(
                client=self.client,
                model=evaluator_model,
            ),
            status_handler=self._emit_status,
        )
        registry.extend(GOAL_TOOLS, make_goal_handlers(self.goal_controller))

        # ── Workflow Runtime ──
        register_builtin_workflows(WORKFLOWS)
        registry.extend(WORKFLOW_TOOLS, WORKFLOW_HANDLERS)
        WORKFLOW_RUNTIME.set_agent_factory(self.create_agent)
        WORKFLOW_RUNTIME.set_registry(WORKFLOWS)

        try:
            mcp_configs = load_config(self.config.mcp_config_path)
        except MCPConfigError as exc:
            self._emit_status(
                f"\033[33m[MCP] config load failed; continuing without MCP: {exc}\033[0m"
            )
            mcp_configs = {}

        self.mcp_manager = MCPManager(mcp_configs)
        self.mcp_manager.connect_all()
        for line in self.mcp_manager.status_lines():
            self._emit_status(f"\033[90m[MCP] {line}\033[0m")

        mcp_tools, mcp_handlers = self.mcp_manager.assemble(registry.names())
        registry.extend(mcp_tools, mcp_handlers)
        hooks.configure_mcp_permissions(
            self.mcp_manager.resolve,
            self.mcp_manager.annotations_of,
        )
        WORKFLOW_RUNTIME.set_tool_registry(registry)

        self.registry = registry
        self.agent = self.create_agent(
            tools=registry.schemas,
            handlers=registry.handlers,
            todo_manager=self.todo_manager,
            background_manager=self.background_manager,
            goal_controller=self.goal_controller,
        )
        self.history = []

        self._cron_started = True
        cron.start(
            delivery_handler=self._try_deliver_async,
            status_handler=self._emit_status,
        )
        self._team_wakeup_started = True
        team_wakeup.start(
            delivery_handler=self._try_deliver_async,
            status_handler=self._emit_status,
        )
        WORKFLOW_RUNTIME.set_delivery_handler(self._try_deliver_async)
        self._workflow_started = True
        self._closing.clear()
        self._started = True
        return self

    def run(self, query: str) -> str:
        """Run one user turn while serializing access to shared history."""
        self._require_not_closing()
        self._require_started()
        self._user_turn_pending.set()
        acquired = False
        try:
            while not acquired:
                self._require_not_closing()
                acquired = self._turn_lock.acquire(timeout=1)
                if not acquired:
                    self._emit_status(
                        "\033[33m[Wait] A scheduled/background turn is "
                        "running, waiting...\033[0m"
                    )
            self._user_turn_pending.clear()
            self._require_not_closing()
            trigger_hooks("UserPromptSubmit", query)
            self.history.append({"role": "user", "content": query})
            return self._run_agent_turn(interactive_approval=True)
        finally:
            self._user_turn_pending.clear()
            if acquired:
                self._turn_lock.release()

    def _try_deliver_async(self, content: str) -> bool:
        """Deliver one background event without waiting for the leader turn."""
        if self._closing.is_set():
            return False
        self._require_started()
        if self._user_turn_pending.is_set():
            return False
        if not self._turn_lock.acquire(blocking=False):
            return False

        result = None
        try:
            # Close the race where a user turn becomes pending between the
            # first check and this non-blocking lock acquisition.
            if self._closing.is_set() or self._user_turn_pending.is_set():
                return False
            injected = {"role": "user", "content": content}
            self.history.append(injected)
            try:
                result = self._run_agent_turn(interactive_approval=False)
            except Exception:
                self._remove_injected_message(injected)
                raise
        finally:
            self._turn_lock.release()

        # The agent turn is committed; a failed presentation must not make
        # cron/team re-run it.
        if result and self.async_result_handler is not None:
            try:
                self.async_result_handler(result)
            except Exception as exc:
                try:
                    self._emit_status(
                        "\033[31m[Runtime] Failed to present async result: "
                        f"{exc}\033[0m"
                    )
                except Exception:
                    pass
        return True

    def _run_agent_turn(self, *, interactive_approval: bool) -> str:
        previous = hooks.INTERACTIVE_APPROVAL_ALLOWED.value
        hooks.INTERACTIVE_APPROVAL_ALLOWED.value = interactive_approval
        try:
            return self.agent.agent_loop(self.history)
        finally:
            hooks.INTERACTIVE_APPROVAL_ALLOWED.value = previous

    def _remove_injected_message(self, injected: dict) -> None:
        """Best-effort removal of the user event for a failed async turn."""
        for index in range(len(self.history) - 1, -1, -1):
            if self.history[index] is injected:
                self.history.pop(index)
                return

    def context_info(self) -> str:
        self._require_not_closing()
        self._require_started()
        with self._turn_lock:
            self._require_not_closing()
            return self.agent.context_manager.get_observability_report(self.history)

    def compact(self) -> str:
        self._require_not_closing()
        self._require_started()
        with self._turn_lock:
            self._require_not_closing()
            self.history[:] = self.agent.context_manager.compact_history(self.history)
            return "✓ Context compacted."

    def clear(self) -> str:
        self._require_not_closing()
        self._require_started()
        with self._turn_lock:
            self._require_not_closing()
            self.history.clear()
            self.agent.context_manager.set_active_request("")
            return "✓ Context cleared."

    def _clear_callbacks(self) -> None:
        """Drop interface callbacks once the lifecycle has fully ended.

        Runs LAST: the Stop hook and shutdown steps may still emit status,
        so the handlers must stay reachable until everything else is done.
        """
        self.approval_handler = None
        self.status_handler = None
        self.async_result_handler = None
        if self.agent is not None:
            self.agent.approval_handler = None
            self.agent.status_handler = None

    def close(self) -> bool:
        """Quiesce Runtime workers, then release dependencies.

        A timeout retains Runtime state so a later close() can safely retry.
        """
        if self._closed:
            return True
        self._closing.set()

        def stop_producers() -> tuple[bool, bool]:
            cron_stopped = not self._cron_started
            wakeup_stopped = not self._team_wakeup_started
            if self._cron_started:
                try:
                    cron_stopped = cron.stop() is not False
                except Exception:
                    cron_stopped = False
                if cron_stopped:
                    self._cron_started = False
            if self._team_wakeup_started:
                try:
                    wakeup_stopped = team_wakeup.stop() is not False
                except Exception:
                    wakeup_stopped = False
                if wakeup_stopped:
                    self._team_wakeup_started = False
            return cron_stopped, wakeup_stopped

        # Stop sources of new async turns before waiting for the active Turn.
        cron_stopped, wakeup_stopped = stop_producers()

        # Only a successfully started Runtime can have an active Turn.
        if self._started:
            if not self._turn_lock.acquire(timeout=CLOSE_TIMEOUT):
                return False
            self._turn_lock.release()

        # A producer may have timed out while completing its delivery.
        retry_cron, retry_wakeup = stop_producers()
        cron_stopped = cron_stopped or retry_cron
        wakeup_stopped = wakeup_stopped or retry_wakeup

        try:
            team_stopped = TEAM.shutdown_all(timeout=CLOSE_TIMEOUT)
        except Exception:
            team_stopped = False

        try:
            workflow_stopped = WORKFLOW_RUNTIME.shutdown_all(timeout=CLOSE_TIMEOUT)
        except Exception:
            workflow_stopped = False
        if self._workflow_started and workflow_stopped:
            self._workflow_started = False

        if not (cron_stopped and wakeup_stopped and team_stopped and workflow_stopped):
            return False

        try:
            if self._started:
                trigger_hooks("Stop", self.session_stats)
        except Exception:
            pass

        try:
            configure_subagent_agent_factory(None)
        except Exception:
            pass

        try:
            TEAM.set_agent_factory(None)
        except Exception:
            pass

        try:
            WORKFLOW_RUNTIME.set_agent_factory(None)
            WORKFLOW_RUNTIME.set_tool_registry(None)
            WORKFLOW_RUNTIME.set_delivery_handler(None)
            WORKFLOW_RUNTIME.set_registry(None)
        except Exception:
            pass

        try:
            hooks.clear_mcp_permissions()
        except Exception:
            pass

        try:
            if self.mcp_manager is not None:
                self.mcp_manager.close_all()
        except Exception:
            pass

        try:
            shutdown_runtime()
        except Exception:
            pass

        self._clear_callbacks()
        self._started = False
        self._closed = True
        return True

    def _require_not_closing(self) -> None:
        if self._closing.is_set():
            raise RuntimeError("CodeHarness is closing")

    def _require_started(self) -> None:
        if not self._started or self.agent is None or self.history is None:
            raise RuntimeError("CodeHarness.start() must be called first")
