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


class CodeHarness:
    """Unified backend API for one leader Agent runtime."""

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.client = None
        self.registry = None
        self.mcp_manager = None
        self.agent = None
        self.history = None
        self.approval_handler = None
        self.status_handler = None
        self.async_result_handler: Callable[[str], None] | None = None
        # Runtime-owned mutable state: never shared with another Runtime.
        self.todo_manager = None
        self.background_manager = None
        self.session_stats = new_session_stats()
        self._turn_lock = threading.Lock()
        self._user_turn_pending = threading.Event()
        self._started = False
        self._closed = False
        self._cron_started = False
        self._team_wakeup_started = False

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
        if self._started:
            return self
        if self._closed:
            raise RuntimeError("CodeHarness has already been closed")

        self.client = OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
        )
        # Root native-tool permissions at the configured workspace, not at
        # the import-time cwd. Dependency direction stays Runtime → Permission.
        hooks.configure_permissions([str(self.config.workspace)])
        configure_subagent_agent_factory(self.create_agent)
        TEAM.set_agent_factory(self.create_agent)

        # Runtime-owned mutable state for the leader.
        self.todo_manager = TodoManager()
        self.background_manager = BackgroundManager()

        registry = build_base_registry(todo_manager=self.todo_manager)
        registry.register(TASK_TOOL, SUB_TASK_HANDLERS["task"])
        registry.extend(TASK_TOOLS, TASK_SYS_HANDLERS)
        registry.extend(TEAM_TOOLS, TEAM_HANDLERS)

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

        self.registry = registry
        self.agent = self.create_agent(
            tools=registry.schemas,
            handlers=registry.handlers,
            todo_manager=self.todo_manager,
            background_manager=self.background_manager,
        )
        self.history = []

        cron.start(
            delivery_handler=self._try_deliver_async,
            status_handler=self._emit_status,
        )
        self._cron_started = True
        team_wakeup.start(
            delivery_handler=self._try_deliver_async,
            status_handler=self._emit_status,
        )
        self._team_wakeup_started = True
        self._started = True
        return self

    def run(self, query: str) -> str:
        """Run one user turn while serializing access to shared history."""
        self._require_started()
        self._user_turn_pending.set()
        acquired = False
        try:
            while not acquired:
                acquired = self._turn_lock.acquire(timeout=1)
                if not acquired:
                    self._emit_status(
                        "\033[33m[Wait] A scheduled/background turn is "
                        "running, waiting...\033[0m"
                    )
            self._user_turn_pending.clear()
            trigger_hooks("UserPromptSubmit", query)
            self.history.append({"role": "user", "content": query})
            return self._run_agent_turn(interactive_approval=True)
        finally:
            self._user_turn_pending.clear()
            if acquired:
                self._turn_lock.release()

    def _try_deliver_async(self, content: str) -> bool:
        """Deliver one background event without waiting for the leader turn."""
        self._require_started()
        if self._user_turn_pending.is_set():
            return False
        if not self._turn_lock.acquire(blocking=False):
            return False

        result = None
        try:
            # Close the race where a user turn becomes pending between the
            # first check and this non-blocking lock acquisition.
            if self._user_turn_pending.is_set():
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
        self._require_started()
        with self._turn_lock:
            return self.agent.context_manager.get_observability_report(self.history)

    def compact(self) -> str:
        self._require_started()
        with self._turn_lock:
            self.history[:] = self.agent.context_manager.compact_history(self.history)
            return "✓ Context compacted."

    def clear(self) -> str:
        self._require_started()
        with self._turn_lock:
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

    def close(self) -> None:
        """Release Runtime-owned state and external resources.

        Every step is guarded so a failure in one cleanup action never
        prevents the remaining ones from running: start() may have failed
        partway (partial MCP connect, cron up but wakeup down, ...), so
        close() must be safe regardless of how far start() got.
        """
        if self._closed:
            return

        # 1. stop Team Wakeup
        try:
            if self._team_wakeup_started:
                team_wakeup.stop()
        except Exception:
            pass
        finally:
            self._team_wakeup_started = False

        # 2. stop Cron
        try:
            if self._cron_started:
                cron.stop()
        except Exception:
            pass
        finally:
            self._cron_started = False

        # 3. Stop Hook / Session Summary (Runtime-owned stats)
        try:
            if self._started:
                trigger_hooks("Stop", self.session_stats)
        except Exception:
            pass

        # 4. clear SubAgent factory
        try:
            configure_subagent_agent_factory(None)
        except Exception:
            pass

        # 5. clear Team agent factory
        try:
            TEAM.set_agent_factory(None)
        except Exception:
            pass

        # 6. clear MCP permission provider
        try:
            hooks.clear_mcp_permissions()
        except Exception:
            pass

        # 7. close MCP adapters
        try:
            if self.mcp_manager is not None:
                self.mcp_manager.close_all()
        except Exception:
            pass

        # 8. shutdown MCP runtime
        try:
            shutdown_runtime()
        except Exception:
            pass

        # 9. clear Runtime callbacks (last: shutdown may still emit status)
        self._clear_callbacks()

        # 10. mark closed
        self._started = False
        self._closed = True

    def _require_started(self) -> None:
        if not self._started or self.agent is None or self.history is None:
            raise RuntimeError("CodeHarness.start() must be called first")
