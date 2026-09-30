"""Application runtime and composition root for CodeHarness."""

from openai import OpenAI

import cron_scheduler
import hooks
from hooks import SESSION_STATS, trigger_hooks
from subagent import (
    TASK_HANDLERS as SUB_TASK_HANDLERS,
    TASK_TOOL,
    configure_agent_factory as configure_subagent_agent_factory,
)
from task_system import TASK_HANDLERS as TASK_SYS_HANDLERS, TASK_TOOLS
from team import TEAM, TEAM_HANDLERS, TEAM_TOOLS
from team import wakeup as team_wakeup

from codeharness.config import RuntimeConfig
from codeharness.core.agent import Agent
from codeharness.mcp import (
    MCPConfigError,
    MCPManager,
    load_config,
    shutdown_runtime,
)
from codeharness.tools import build_base_registry
from codeharness.tools.todo import TODO as DEFAULT_TODO


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
        configure_subagent_agent_factory(self.create_agent)
        TEAM.set_agent_factory(self.create_agent)

        registry = build_base_registry()
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
            todo_manager=DEFAULT_TODO,
        )
        self.history = []

        cron_scheduler.start(agent=self.agent, history=self.history)
        self._cron_started = True
        team_wakeup.start(agent=self.agent, history=self.history)
        self._team_wakeup_started = True
        self._started = True
        return self

    def run(self, query: str) -> str:
        """Run one user turn while serializing access to shared history."""
        self._require_started()
        cron_scheduler.UI_BUSY = True
        acquired = False
        try:
            trigger_hooks("UserPromptSubmit", query)
            while not acquired:
                acquired = cron_scheduler.agent_lock.acquire(timeout=1)
                if not acquired:
                    self._emit_status(
                        "\033[33m[Wait] A scheduled task is running, waiting...\033[0m"
                    )
            cron_scheduler.UI_BUSY = False
            self.history.append({"role": "user", "content": query})
            return self.agent.agent_loop(self.history)
        finally:
            cron_scheduler.UI_BUSY = False
            if acquired:
                cron_scheduler.agent_lock.release()

    def context_info(self) -> str:
        self._require_started()
        cron_scheduler.UI_BUSY = True
        try:
            return self.agent.context_manager.get_observability_report(self.history)
        finally:
            cron_scheduler.UI_BUSY = False

    def compact(self) -> str:
        self._require_started()
        cron_scheduler.UI_BUSY = True
        try:
            self.history[:] = self.agent.context_manager.compact_history(self.history)
            return "✓ Context compacted."
        finally:
            cron_scheduler.UI_BUSY = False

    def clear(self) -> str:
        self._require_started()
        cron_scheduler.UI_BUSY = True
        try:
            self.history.clear()
            self.agent.context_manager.set_active_request("")
            return "✓ Context cleared."
        finally:
            cron_scheduler.UI_BUSY = False

    def close(self) -> None:
        """Close background services and external resources in legacy order."""
        if self._closed:
            return
        try:
            if self._team_wakeup_started:
                team_wakeup.stop()
                self._team_wakeup_started = False
            if self._cron_started:
                cron_scheduler.stop()
                self._cron_started = False
            if self._started:
                trigger_hooks("Stop", SESSION_STATS)
        finally:
            try:
                if self.mcp_manager is not None:
                    self.mcp_manager.close_all()
            finally:
                shutdown_runtime()
                self._started = False
                self._closed = True

    def _require_started(self) -> None:
        if not self._started or self.agent is None or self.history is None:
            raise RuntimeError("CodeHarness.start() must be called first")
