"""Multi-server MCP lifecycle: connect, assemble, dispatch, close.

Sits above :class:`~codeharness.mcp.client.MCPServerAdapter` (one per server) and
below the agent loop. Phase-1 responsibilities (deliberately minimal):

* connect every *enabled* configured server — a single server failing is
  warned and skipped, never fatal unless ``fail_fast=True``;
* aggregate every server's descriptors into ONE global name registry;
* assemble OpenAI tool schemas + dispatch handlers for the agent tool pool;
* resolve a prefixed name back to ``(server, raw_tool)`` and dispatch a call;
* expose ``resolve`` / ``annotations_of`` as small callables so the host
  permission layer stays decoupled from this lifecycle object;
* tear every connection down.

Explicitly OUT of scope for phase 1: automatic retry, self-healing
reconnect-and-replay (a disconnected server may already have performed a
side-effecting call, so replaying risks duplicate writes / issues / PRs),
runtime ``connect_mcp``, ``tools/list_changed`` subscription, and
teammate / subagent inheritance of MCP tools.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .adapter import ToolNameRegistry, to_openai_tool
from .client import MCPConnectError, MCPServerAdapter
from .config import ServerConfig
from .runtime import SyncMCPRuntime, get_runtime

logger = logging.getLogger(__name__)

AdapterFactory = Callable[[ServerConfig, SyncMCPRuntime], MCPServerAdapter]


class MCPAssemblyError(Exception):
    """Raised when the tool pool cannot be assembled unambiguously.

    Name-normalization collisions (cross-server, or against a native tool)
    are configuration/registration ambiguities: failing loudly here is
    preferred over letting a tool silently disappear from the pool.
    """


@dataclass(frozen=True)
class ServerStatus:
    """Outcome of one server's connect attempt."""

    name: str
    state: str  # "connected" | "failed" | "disabled"
    tools: int = 0
    error: str | None = None

    def line(self) -> str:
        if self.state == "connected":
            return f"{self.name}: connected ({self.tools} tools)"
        if self.state == "disabled":
            return f"{self.name}: disabled (skipped)"
        return f"{self.name}: FAILED - {self.error}"


def _default_factory(config: ServerConfig, runtime: SyncMCPRuntime) -> MCPServerAdapter:
    return MCPServerAdapter(config, runtime)


class MCPManager:
    """Application-lifecycle object owning several MCP server adapters.

    Not a process singleton: create it explicitly, hand its tools/handlers to
    the agent, and ``close_all()`` it on shutdown. The underlying
    ``SyncMCPRuntime`` *is* shared process-wide (passed in or lazily fetched).
    """

    def __init__(
        self,
        configs: dict[str, ServerConfig],
        runtime: SyncMCPRuntime | None = None,
        *,
        adapter_factory: AdapterFactory | None = None,
    ):
        self._configs = dict(configs)
        self._runtime = runtime or get_runtime()
        self._adapter_factory = adapter_factory or _default_factory
        self._adapters: dict[str, MCPServerAdapter] = {}
        self._status: dict[str, ServerStatus] = {}
        self._registry = ToolNameRegistry()

    # ── lifecycle ─────────────────────────────────────────────

    def connect_all(self, *, fail_fast: bool = False) -> None:
        """Connect every enabled server. Per-server failure is recorded/skipped.

        ``fail_fast=True`` re-raises the first connection error (used by tests
        and strict startups); the default keeps the agent alive when one
        server is misconfigured or its binary is missing.
        """
        for name, config in self._configs.items():
            if not config.enabled:
                self._status[name] = ServerStatus(name, "disabled")
                continue

            adapter = self._adapter_factory(config, self._runtime)
            try:
                adapter.connect()
            except MCPConnectError as exc:
                self._status[name] = ServerStatus(name, "failed", error=str(exc))
                logger.warning("[MCP] server '%s' failed to connect: %s", name, exc)
                if fail_fast:
                    raise
                continue
            except Exception as exc:  # noqa: BLE001 - a bad server must not kill startup
                reason = f"{type(exc).__name__}: {exc}"
                self._status[name] = ServerStatus(name, "failed", error=reason)
                logger.warning("[MCP] server '%s' failed to connect: %s", name, reason)
                if fail_fast:
                    raise
                continue

            self._adapters[name] = adapter
            tool_count = len(adapter.list_descriptors())
            self._status[name] = ServerStatus(name, "connected", tools=tool_count)
            logger.info("[MCP] server '%s' connected (%d tools)", name, tool_count)

    def close_all(self) -> None:
        """Close every adapter (subprocesses reclaimed). Idempotent."""
        for name, adapter in list(self._adapters.items()):
            try:
                adapter.close()
            except Exception as exc:  # noqa: BLE001 - teardown must never mask the original error
                logger.warning("[MCP] error closing server '%s': %s", name, exc)
        self._adapters.clear()
        self._registry = ToolNameRegistry()

    @property
    def status(self) -> dict[str, ServerStatus]:
        return dict(self._status)

    def status_lines(self) -> list[str]:
        """Human-readable per-server connect outcome (a skipped server stays visible)."""
        return [st.line() for st in self._status.values()]

    # ── assembly ──────────────────────────────────────────────

    def assemble(
        self, native_names: set[str] | None = None
    ) -> tuple[list[dict], dict[str, Callable[..., str]]]:
        """Build the MCP portion of the tool pool: (openai_tools, handlers).

        Raises :class:`MCPAssemblyError` on any name collision — against a
        native tool or across servers after normalization. Handlers are
        generated per tool (no hand-maintained mapping); each is a closure
        that freezes its own prefixed name and routes back through
        :meth:`call_tool_text`.
        """
        native = set(native_names or ())
        registry = ToolNameRegistry()
        tools: list[dict] = []
        handlers: dict[str, Callable[..., str]] = {}

        for server_name, adapter in self._adapters.items():
            for descriptor in adapter.list_descriptors():
                prefixed = descriptor.prefixed_name

                if prefixed in native:
                    raise MCPAssemblyError(
                        f"MCP tool '{prefixed}' (server '{server_name}', raw "
                        f"'{descriptor.raw_name}') collides with a native tool name"
                    )

                existing = registry.resolve(prefixed)
                if existing is not None:
                    raise MCPAssemblyError(
                        f"MCP tool name collision after normalization: '{prefixed}' "
                        f"is produced by both {existing} and "
                        f"{(server_name, descriptor.raw_name)}"
                    )

                registry.add(prefixed, server_name, descriptor.raw_name)
                tools.append(to_openai_tool(descriptor))
                handlers[prefixed] = self._make_handler(prefixed)

        self._registry = registry
        return tools, handlers

    def _make_handler(self, prefixed: str) -> Callable[..., str]:
        # Closure (not functools.partial) so a tool argument literally named
        # ``prefixed`` can never shadow the bound alias.
        def handler(**kwargs: Any) -> str:
            return self.call_tool_text(prefixed, kwargs)

        return handler

    # ── resolution & dispatch ─────────────────────────────────

    def resolve(self, prefixed: str) -> tuple[str, str] | None:
        """prefixed alias -> (server_name, raw_tool_name), or None if unknown."""
        return self._registry.resolve(prefixed)

    def annotations_of(self, prefixed: str) -> Any | None:
        """Server-declared hints for a tool (display only, never authorization)."""
        pair = self._registry.resolve(prefixed)
        if pair is None:
            return None
        adapter = self._adapters.get(pair[0])
        if adapter is None:
            return None
        return adapter.annotations_of(pair[1])

    def call_tool_text(self, prefixed: str, args: dict | None = None) -> str:
        """Dispatch one call and render it as text for the LLM.

        Never raises and never auto-retries: whatever the adapter classifies
        (tool_error / timeout / connection_lost / protocol_error /
        invalid_tool) is surfaced verbatim so the model can react. Replaying a
        call after ``connection_lost`` is intentionally NOT done — the server
        may already have committed a side effect.
        """
        pair = self._registry.resolve(prefixed)
        if pair is None:
            return f"MCP error: unknown tool '{prefixed}'"
        server_name, raw_name = pair
        adapter = self._adapters.get(server_name)
        if adapter is None:
            return f"MCP error: server '{server_name}' is not connected"
        return adapter.call_tool(raw_name, args or {}).text
