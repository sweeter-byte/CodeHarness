"""Tests for codeharness.mcp.manager — multi-server lifecycle, assembly, dispatch.

Uses the in-process ``server=`` seam of MCPServerAdapter (via an injected
adapter_factory) so no subprocess is spawned.
"""

import mcp_types as types
import pytest
from mcp.server.lowlevel import Server

from codeharness.mcp.client import MCPServerAdapter
from codeharness.mcp.config import ServerConfig
from codeharness.mcp.manager import MCPAssemblyError, MCPManager


def _inproc_server(tool_names):
    """An in-process SDK Server exposing the given tools (echo-style calls)."""
    srv = Server("inproc")

    async def on_list(ctx, params):
        return types.ListToolsResult(tools=[
            types.Tool(
                name=n,
                description=f"{n} tool",
                input_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                },
            )
            for n in tool_names
        ])

    async def on_call(ctx, params):
        args = params.arguments or {}
        return types.CallToolResult(
            content=[types.TextContent(text=f"{params.name}:{args.get('message', '')}")]
        )

    srv.add_request_handler("tools/list", types.PaginatedRequestParams, on_list)
    srv.add_request_handler("tools/call", types.CallToolRequestParams, on_call)
    return srv


def _config(name, **overrides) -> ServerConfig:
    base = {
        "name": name,
        "transport": "stdio",
        "command": "unused-inproc",
        "args": (),
        "connect_timeout": 15.0,
        "call_timeout": 10.0,
    }
    base.update(overrides)
    return ServerConfig(**base)


def _factory(servers_by_name):
    """adapter_factory: in-process for known names, real (failing) otherwise."""
    def make(cfg, rt):
        if cfg.name in servers_by_name:
            return MCPServerAdapter(cfg, rt, server=servers_by_name[cfg.name])
        return MCPServerAdapter(cfg, rt)
    return make


def _manager(configs, servers_by_name, runtime):
    return MCPManager(configs, runtime, adapter_factory=_factory(servers_by_name))


# ── connect_all / status ──────────────────────────────────────

def test_connect_all_records_status_and_tool_counts(runtime):
    configs = {
        "alpha": _config("alpha"),
        "beta": _config("beta"),
    }
    servers = {"alpha": _inproc_server(["echo", "ping"]), "beta": _inproc_server(["echo"])}
    mgr = _manager(configs, servers, runtime)
    mgr.connect_all()
    try:
        st = mgr.status
        assert st["alpha"].state == "connected" and st["alpha"].tools == 2
        assert st["beta"].state == "connected" and st["beta"].tools == 1
        assert any("alpha: connected (2 tools)" in line for line in mgr.status_lines())
    finally:
        mgr.close_all()


def test_disabled_server_is_skipped(runtime):
    configs = {"alpha": _config("alpha"), "off": _config("off", enabled=False)}
    servers = {"alpha": _inproc_server(["echo"])}
    mgr = _manager(configs, servers, runtime)
    mgr.connect_all()
    try:
        assert mgr.status["off"].state == "disabled"
        assert mgr.status["alpha"].state == "connected"
    finally:
        mgr.close_all()


def test_connect_failure_warns_and_skips(runtime):
    configs = {
        "alpha": _config("alpha"),
        "broken": _config("broken", command="/nonexistent/binary-xyz"),
    }
    servers = {"alpha": _inproc_server(["echo"])}
    mgr = _manager(configs, servers, runtime)
    mgr.connect_all()  # must not raise
    try:
        assert mgr.status["broken"].state == "failed"
        assert mgr.status["broken"].error
        assert mgr.status["alpha"].state == "connected"  # the good one survives
    finally:
        mgr.close_all()


def test_connect_failure_fail_fast_raises(runtime):
    from codeharness.mcp.client import MCPConnectError
    configs = {"broken": _config("broken", command="/nonexistent/binary-xyz")}
    mgr = _manager(configs, {}, runtime)
    with pytest.raises(MCPConnectError):
        mgr.connect_all(fail_fast=True)
    mgr.close_all()


# ── assemble ──────────────────────────────────────────────────

def test_assemble_builds_tools_and_dispatching_handlers(runtime):
    configs = {"alpha": _config("alpha")}
    servers = {"alpha": _inproc_server(["echo"])}
    mgr = _manager(configs, servers, runtime)
    mgr.connect_all()
    try:
        tools, handlers = mgr.assemble(set())
        names = {t["function"]["name"] for t in tools}
        assert names == {"mcp__alpha__echo"}
        assert tools[0]["type"] == "function"
        # handler(**args) dispatches back to the server and returns str
        assert handlers["mcp__alpha__echo"](message="hi") == "echo:hi"
        # resolve maps the alias back to (server, raw)
        assert mgr.resolve("mcp__alpha__echo") == ("alpha", "echo")
        assert mgr.resolve("mcp__alpha__nope") is None
    finally:
        mgr.close_all()


def test_assemble_native_collision_raises(runtime):
    configs = {"alpha": _config("alpha")}
    servers = {"alpha": _inproc_server(["echo"])}
    mgr = _manager(configs, servers, runtime)
    mgr.connect_all()
    try:
        with pytest.raises(MCPAssemblyError, match="native"):
            mgr.assemble({"mcp__alpha__echo"})
    finally:
        mgr.close_all()


def test_assemble_cross_server_collision_raises(runtime):
    # "a.b" and "a_b" both normalize to "a_b", so tool "t" collides.
    configs = {"a.b": _config("a.b"), "a_b": _config("a_b")}
    servers = {"a.b": _inproc_server(["t"]), "a_b": _inproc_server(["t"])}
    mgr = _manager(configs, servers, runtime)
    mgr.connect_all()
    try:
        with pytest.raises(MCPAssemblyError, match="collision"):
            mgr.assemble(set())
    finally:
        mgr.close_all()


# ── dispatch errors & close ───────────────────────────────────

def test_call_unknown_tool_returns_error_text(runtime):
    configs = {"alpha": _config("alpha")}
    servers = {"alpha": _inproc_server(["echo"])}
    mgr = _manager(configs, servers, runtime)
    mgr.connect_all()
    mgr.assemble(set())
    try:
        assert "unknown tool" in mgr.call_tool_text("mcp__alpha__ghost", {})
    finally:
        mgr.close_all()


def test_annotations_of_unknown_is_none(runtime):
    configs = {"alpha": _config("alpha")}
    servers = {"alpha": _inproc_server(["echo"])}
    mgr = _manager(configs, servers, runtime)
    mgr.connect_all()
    mgr.assemble(set())
    try:
        assert mgr.annotations_of("mcp__alpha__echo") is None  # no annotations declared
        assert mgr.annotations_of("mcp__nope__x") is None
    finally:
        mgr.close_all()


def test_close_all_is_idempotent_and_clears_adapters(runtime):
    configs = {"alpha": _config("alpha")}
    servers = {"alpha": _inproc_server(["echo"])}
    mgr = _manager(configs, servers, runtime)
    mgr.connect_all()
    mgr.assemble(set())
    mgr.close_all()
    mgr.close_all()  # idempotent
    # after close, dispatch surfaces a connection error rather than raising
    assert "MCP error" in mgr.call_tool_text("mcp__alpha__echo", {"message": "x"})
