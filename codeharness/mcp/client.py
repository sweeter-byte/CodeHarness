"""Host-side adapter for a single MCP server.

Built on the MCP SDK v2 high-level ``Client``: the adapter never touches
``ClientSession`` or raw transports unless the SDK leaves no choice. One
verified gap in SDK 2.0.0: ``Client.__post_init__`` accepts
``Server | str(url) | Transport`` but **not** ``StdioServerParameters``
(it would be misrouted into the stream transport and fail with a confusing
TypeError). :class:`_StdioTransport` is the minimal shim that fills exactly
that gap; everything above it stays high-level ``Client``.

Layering contract:

* Connection-time failures raise (:class:`MCPConnectError` and friends) —
  a server that cannot start must be loud.
* Call-time failures never raise; they return an :class:`MCPCallResult`
  with a classified ``error_kind`` so a future Manager can retry on
  ``timeout``, reconnect on ``connection_lost``, or ``refresh_tools()`` on
  ``invalid_tool`` — while ``tool_error`` results flow back to the LLM.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio
import anyio.abc
import mcp_types as types
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from .adapter import (
    ToolDescriptor,
    ToolNameCollisionError,
    ToolNameRegistry,
    ToolSchemaError,
    build_prefixed_name,
    validate_input_schema,
)
from .config import ServerConfig
from .runtime import SyncMCPRuntime, get_runtime

logger = logging.getLogger(__name__)

ErrorKind = Literal[
    "tool_error",        # the MCP tool itself reported isError=True → give text to the LLM
    "timeout",           # call_timeout / connect deadline exceeded → Manager may retry
    "connection_lost",   # subprocess died, streams broken → Manager may reconnect
    "protocol_error",    # MCPError / JSON-RPC / result ValidationError
    "invalid_tool",      # raw name unknown locally → Manager may refresh_tools()
    "unknown",
]

# Guard against a malicious/buggy server paginating forever.
_MAX_LIST_PAGES = 50


class MCPConnectError(Exception):
    """Raised when connecting, handshaking, or registering tools fails."""

    def __init__(self, server_name: str, reason: str, cause: BaseException | None = None):
        super().__init__(f"MCP connect error [{server_name}]: {reason}")
        self.server_name = server_name
        self.reason = reason
        self.__cause__ = cause


class _StdioTransport:
    """Transport-protocol shim so stdio params can feed the high-level Client.

    SDK 2.0.0's ``Client`` does not special-case ``StdioServerParameters``;
    wrapping ``stdio_client`` (which yields exactly the ``(read, write)``
    streams a Transport must yield) is the SDK's own sanctioned shape —
    ``StreamableHTTPTransport`` does the same for HTTP.
    """

    def __init__(self, params: StdioServerParameters):
        self._params = params

    async def __aenter__(self):
        self._cm = stdio_client(self._params)
        return await self._cm.__aenter__()

    async def __aexit__(self, *exc_info):
        return await self._cm.__aexit__(*exc_info)


@dataclass
class MCPContentPart:
    """One normalized content block; ``meta`` keeps original non-text metadata."""

    kind: Literal["text", "image", "audio", "resource_link", "embedded_resource", "unknown"]
    text: str | None = None
    mime_type: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class MCPCallResult:
    """Structured outcome of one tools/call round.

    ``text`` is the LLM-facing view; ``parts`` and ``structured_content``
    preserve the original shape for future Manager-layer handling (artifacts,
    images, structured parsing) without re-plumbing the adapter.
    """

    text: str
    is_error: bool
    error_kind: ErrorKind | None = None
    structured_content: Any = None
    parts: list[MCPContentPart] = field(default_factory=list)


def _b64_size(data: str) -> int:
    """Approximate decoded byte size of a base64 payload."""
    try:
        return len(base64.b64decode(data))
    except Exception:  # noqa: BLE001 - malformed/padded base64 must not break normalization
        return len(data)


def normalize_call_result(result: Any, *, is_error_override: bool | None = None) -> MCPCallResult:
    """Convert an SDK ``CallToolResult`` into the host-side structured result.

    First-version policy: text is passed through verbatim; image/audio and
    binary resources become placeholders (raw metadata kept in ``parts``);
    ``structured_content`` is preserved as-is and used as the text fallback
    when the server returned no text at all.
    """
    parts: list[MCPContentPart] = []
    texts: list[str] = []

    for block in result.content or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(MCPContentPart(kind="text", text=block.text))
            texts.append(block.text)
        elif btype == "image":
            size = _b64_size(block.data)
            parts.append(MCPContentPart(
                kind="image", mime_type=block.mime_type, meta={"bytes": size},
            ))
            texts.append(f"[image {block.mime_type}, {size} bytes]")
        elif btype == "audio":
            size = _b64_size(block.data)
            parts.append(MCPContentPart(
                kind="audio", mime_type=block.mime_type, meta={"bytes": size},
            ))
            texts.append(f"[audio {block.mime_type}, {size} bytes]")
        elif btype == "resource_link":
            parts.append(MCPContentPart(
                kind="resource_link",
                mime_type=getattr(block, "mime_type", None),
                meta={"uri": block.uri, "name": getattr(block, "name", None)},
            ))
            texts.append(f"[resource_link {block.uri}]")
        elif btype == "resource":
            res = block.resource
            res_uri = getattr(res, "uri", "")
            res_mime = getattr(res, "mime_type", None)
            if hasattr(res, "text"):
                parts.append(MCPContentPart(
                    kind="embedded_resource", text=res.text,
                    mime_type=res_mime, meta={"uri": res_uri},
                ))
                texts.append(res.text)
            else:
                size = _b64_size(getattr(res, "blob", ""))
                parts.append(MCPContentPart(
                    kind="embedded_resource", mime_type=res_mime,
                    meta={"uri": res_uri, "bytes": size},
                ))
                texts.append(f"[resource blob {res_uri}, {size} bytes]")
        else:
            parts.append(MCPContentPart(kind="unknown", meta={"type": btype}))
            texts.append(f"[unrecognized content type: {btype}]")

    structured = getattr(result, "structured_content", None)
    text = "\n".join(texts)
    if not text and structured is not None:
        text = json.dumps(structured, ensure_ascii=False, default=str)

    is_error = bool(result.is_error) if is_error_override is None else is_error_override
    return MCPCallResult(
        text=text or "(no output)",
        is_error=is_error,
        error_kind="tool_error" if is_error else None,
        structured_content=structured,
        parts=parts,
    )


class MCPServerAdapter:
    """Sync facade over one MCP server connection.

    ``server=`` injects an in-process SDK ``Server``/``MCPServer`` (testing
    seam: exercises the full Client + runtime path without a subprocess).
    """

    CREATED = "created"
    CONNECTING = "connecting"
    READY = "ready"
    DISCONNECTED = "disconnected"
    CLOSED = "closed"

    def __init__(
        self,
        config: ServerConfig,
        runtime: SyncMCPRuntime | None = None,
        *,
        server: Any | None = None,
    ):
        self.config = config
        self._runtime = runtime or get_runtime()
        self._inproc_server = server
        self._handle: Any | None = None
        self._client: Client | None = None
        self._registry = ToolNameRegistry()
        self._descriptors: list[ToolDescriptor] = []
        self._state = self.CREATED

    # ── lifecycle ─────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    def connect(self) -> None:
        """Spawn/handshake, fetch ALL tool pages, register strictly. Loud on failure."""
        if self._state in (self.READY, self.CONNECTING):
            raise MCPConnectError(self.config.name, f"already {self._state}")
        if self._state == self.CLOSED:
            raise MCPConnectError(self.config.name, "adapter is closed")
        if self._inproc_server is None and self.config.transport != "stdio":
            raise NotImplementedError(
                f"transport '{self.config.transport}' is not implemented yet "
                f"(server '{self.config.name}'); only 'stdio' is supported"
            )

        self._state = self.CONNECTING
        handle = None
        try:
            if self._inproc_server is not None:
                target: Any = self._inproc_server
            else:
                # env carries ONLY explicitly declared variables; the SDK's
                # stdio transport merges them over its safe default env.
                params = StdioServerParameters(
                    command=self.config.command,
                    args=list(self.config.args),
                    env=dict(self.config.env) or None,
                )
                target = _StdioTransport(params)

            def factory():
                return Client(target, read_timeout_seconds=self.config.call_timeout)

            handle = self._runtime.spawn_persistent(factory)
            client = handle.result(timeout=self.config.connect_timeout)
            tools = self._fetch_all_tools(client)
            registry, descriptors = self._build_registration(tools)
        except (ToolSchemaError, ToolNameCollisionError) as exc:
            if handle is not None:
                handle.close()
            self._state = self.DISCONNECTED
            raise MCPConnectError(self.config.name, str(exc), cause=exc) from exc
        except TimeoutError as exc:
            if handle is not None:
                handle.close()
            self._state = self.DISCONNECTED
            raise MCPConnectError(
                self.config.name,
                f"timed out after {self.config.connect_timeout}s",
                cause=exc,
            ) from exc
        except Exception as exc:
            if handle is not None:
                handle.close()
            self._state = self.DISCONNECTED
            raise MCPConnectError(self.config.name, f"{type(exc).__name__}: {exc}", cause=exc) from exc

        self._handle = handle
        self._client = client
        self._registry = registry
        self._descriptors = descriptors
        self._state = self.READY

    def close(self) -> None:
        """Tear down the connection (subprocess reclaimed by the SDK). Idempotent."""
        handle, self._handle = self._handle, None
        self._client = None
        if handle is not None:
            handle.close()
        self._state = self.CLOSED

    # ── discovery ─────────────────────────────────────────────

    def list_descriptors(self) -> list[ToolDescriptor]:
        """Snapshot of registered tools (may be stale; see refresh_tools)."""
        self._require_client()
        return list(self._descriptors)

    def refresh_tools(self) -> list[ToolDescriptor]:
        """Re-fetch all pages and rebuild the registry atomically.

        v1 does not subscribe to ``tools/list_changed``; the tool list is a
        snapshot by design and this is the explicit invalidation entry point.
        """
        client = self._require_client()
        tools = self._fetch_all_tools(client)
        registry, descriptors = self._build_registration(tools)  # may raise
        self._registry = registry
        self._descriptors = descriptors
        return list(descriptors)

    def annotations_of(self, raw_name: str) -> Any | None:
        """Server-declared hints (readOnlyHint etc.) — display only, never authorization."""
        for d in self._descriptors:
            if d.raw_name == raw_name:
                return d.annotations
        return None

    def _fetch_all_tools(self, client: Client) -> list[types.Tool]:
        """Drive tools/list pagination to completion (bypassing the SDK cache)."""

        async def fetch() -> list[types.Tool]:
            all_tools: list[types.Tool] = []
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for _ in range(_MAX_LIST_PAGES):
                page = await client.list_tools(cursor=cursor, cache_mode="bypass")
                all_tools.extend(page.tools)
                cursor = page.next_cursor
                if cursor is None:
                    return all_tools
                if cursor in seen_cursors:
                    raise MCPConnectError(
                        self.config.name, f"server repeated list_tools cursor {cursor!r}"
                    )
                seen_cursors.add(cursor)
            raise MCPConnectError(
                self.config.name, f"list_tools exceeded {_MAX_LIST_PAGES} pages"
            )

        try:
            return self._runtime.run_sync(fetch)
        except MCPConnectError:
            raise
        except Exception as exc:
            raise MCPConnectError(self.config.name, f"list_tools failed: {exc}", cause=exc) from exc

    def _build_registration(
        self, tools: list[types.Tool]
    ) -> tuple[ToolNameRegistry, list[ToolDescriptor]]:
        """Strict per-server registration; any bad tool aborts the whole server."""
        registry = ToolNameRegistry()
        descriptors: list[ToolDescriptor] = []
        for tool in tools:
            prefixed = build_prefixed_name(self.config.name, tool.name)
            schema = validate_input_schema(self.config.name, tool.name, tool.input_schema)
            registry.add(prefixed, self.config.name, tool.name)  # collision check
            descriptors.append(ToolDescriptor(
                prefixed_name=prefixed,
                server_name=self.config.name,
                raw_name=tool.name,
                description=tool.description or "",
                input_schema=schema,
                annotations=tool.annotations,
            ))
        return registry, descriptors

    # ── invocation ────────────────────────────────────────────

    def call_tool(
        self, raw_name: str, args: dict | None = None, timeout: float | None = None
    ) -> MCPCallResult:
        """Core semantic interface. Never raises; failures are classified results."""
        if self._state == self.CLOSED:
            return self._error_result(
                "connection_lost", f"MCP error: adapter for '{self.config.name}' is closed"
            )
        if self._state != self.READY or self._client is None:
            return self._error_result(
                "connection_lost",
                f"MCP error: server '{self.config.name}' is not connected (state={self._state})",
            )
        if self._registry.prefixed_for(self.config.name, raw_name) is None:
            # Local miss — no wire request. Manager may refresh_tools() and retry.
            return self._error_result(
                "invalid_tool",
                f"MCP error: unknown tool '{raw_name}' on server '{self.config.name}'",
            )

        call_timeout = timeout if timeout is not None else self.config.call_timeout
        client = self._client

        async def _call() -> Any:
            return await client.call_tool(
                raw_name, args or {}, read_timeout_seconds=call_timeout
            )

        try:
            result = self._runtime.run_sync(_call)
        except TimeoutError:
            return self._error_result(
                "timeout",
                f"MCP error: timeout after {call_timeout}s calling '{raw_name}' "
                f"on server '{self.config.name}'",
            )
        except MCPError as exc:
            return self._error_result(
                "protocol_error",
                f"MCP error: protocol error calling '{raw_name}': "
                f"[code {exc.code}] {exc.message}",
            )
        except (anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.EndOfStream,
                ConnectionError, OSError) as exc:
            self._state = self.DISCONNECTED
            return self._error_result(
                "connection_lost",
                f"MCP error: connection lost to server '{self.config.name}': "
                f"{type(exc).__name__}: {exc}",
            )
        except ValidationError as exc:
            return self._error_result(
                "protocol_error",
                f"MCP error: server '{self.config.name}' returned an invalid result: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - last-resort trap: call_tool must never raise into the agent loop
            return self._error_result(
                "unknown",
                f"MCP error: {type(exc).__name__}: {exc}",
            )

        return normalize_call_result(result)

    def call_tool_text(
        self, raw_name: str, args: dict | None = None, timeout: float | None = None
    ) -> str:
        """COMPATIBILITY WRAPPER for the current sync ``TOOL_HANDLERS`` convention.

        The adapter's core semantic interface is :meth:`call_tool` returning
        :class:`MCPCallResult`; once the Manager layer lands, result→str
        rendering should move there and this wrapper can be retired.
        """
        return self.call_tool(raw_name, args, timeout).text

    # ── internals ─────────────────────────────────────────────

    def _require_client(self) -> Client:
        if self._state != self.READY or self._client is None:
            raise MCPConnectError(
                self.config.name, f"adapter is not connected (state={self._state})"
            )
        return self._client

    def _error_result(self, kind: ErrorKind, text: str) -> MCPCallResult:
        return MCPCallResult(text=text, is_error=True, error_kind=kind)
