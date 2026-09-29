"""Tests for mcp_host.client — MCPServerAdapter over the v2 high-level Client.

Two levels:
* in-process SDK ``Server`` via the ``server=`` test seam (fast, no subprocess);
* real stdio subprocess against ``fake_server.py`` (spawn/pagination/env/teardown).
"""

import os
import sys
import uuid
from pathlib import Path

import mcp_types as types
import pytest
from mcp.server.lowlevel import Server

from mcp_host.client import MCPConnectError, MCPServerAdapter, normalize_call_result
from mcp_host.config import ServerConfig

FAKE_SERVER = Path(__file__).parent / "fake_server.py"


def _stdio_config(**overrides) -> ServerConfig:
    base = {
        "name": "fake",
        "transport": "stdio",
        "command": sys.executable,
        "args": (str(FAKE_SERVER),),
        "connect_timeout": 30.0,
        "call_timeout": 15.0,
    }
    base.update(overrides)
    return ServerConfig(**base)


def _inproc_server() -> Server:
    srv = Server("inproc")

    async def on_list(ctx, params):
        return types.ListToolsResult(tools=[
            types.Tool(
                name="echo",
                description="Echo back.",
                input_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            ),
            types.Tool(name="fail", input_schema={"type": "object", "properties": {}}),
            types.Tool(name="image", input_schema={"type": "object", "properties": {}}),
            types.Tool(name="structured_only", input_schema={"type": "object", "properties": {}}),
        ])

    async def on_call(ctx, params):
        args = params.arguments or {}
        if params.name == "echo":
            return types.CallToolResult(
                content=[types.TextContent(text=f"echo: {args.get('message', '')}")]
            )
        if params.name == "fail":
            return types.CallToolResult(
                content=[types.TextContent(text="tool said no")], is_error=True
            )
        if params.name == "image":
            import base64
            blob = base64.b64encode(b"fake-png").decode()
            return types.CallToolResult(
                content=[types.ImageContent(data=blob, mime_type="image/png")]
            )
        if params.name == "structured_only":
            return types.CallToolResult(content=[], structured_content={"answer": 42})
        raise AssertionError(f"unexpected tool {params.name}")

    srv.add_request_handler("tools/list", types.PaginatedRequestParams, on_list)
    srv.add_request_handler("tools/call", types.CallToolRequestParams, on_call)
    return srv


# ── in-process client path ────────────────────────────────────

def test_inproc_connect_and_descriptors(runtime):
    adapter = MCPServerAdapter(_stdio_config(), runtime, server=_inproc_server())
    adapter.connect()
    try:
        assert adapter.state == MCPServerAdapter.READY
        names = {d.prefixed_name for d in adapter.list_descriptors()}
        assert names == {
            "mcp__fake__echo", "mcp__fake__fail",
            "mcp__fake__image", "mcp__fake__structured_only",
        }
    finally:
        adapter.close()
    assert adapter.state == MCPServerAdapter.CLOSED


def test_inproc_call_tool_text_and_structured(runtime):
    adapter = MCPServerAdapter(_stdio_config(), runtime, server=_inproc_server())
    adapter.connect()
    try:
        r = adapter.call_tool("echo", {"message": "hi"})
        assert not r.is_error and r.error_kind is None
        assert r.text == "echo: hi"

        # compatibility wrapper
        assert adapter.call_tool_text("echo", {"message": "yo"}) == "echo: yo"

        r = adapter.call_tool("structured_only")
        assert r.structured_content == {"answer": 42}
        assert "42" in r.text  # text fallback from structured_content
    finally:
        adapter.close()


def test_inproc_tool_error_flows_back(runtime):
    adapter = MCPServerAdapter(_stdio_config(), runtime, server=_inproc_server())
    adapter.connect()
    try:
        r = adapter.call_tool("fail")
        assert r.is_error
        assert r.error_kind == "tool_error"
        assert r.text == "tool said no"
        assert adapter.state == MCPServerAdapter.READY  # connection unaffected
    finally:
        adapter.close()


def test_inproc_image_placeholder_keeps_metadata(runtime):
    adapter = MCPServerAdapter(_stdio_config(), runtime, server=_inproc_server())
    adapter.connect()
    try:
        r = adapter.call_tool("image")
        assert "[image image/png" in r.text
        part = r.parts[0]
        assert part.kind == "image" and part.mime_type == "image/png"
        assert part.meta["bytes"] == len(b"fake-png")
    finally:
        adapter.close()


def test_unknown_tool_is_invalid_tool_not_protocol_error(runtime):
    adapter = MCPServerAdapter(_stdio_config(), runtime, server=_inproc_server())
    adapter.connect()
    try:
        r = adapter.call_tool("nonexistent")
        assert r.is_error and r.error_kind == "invalid_tool"
        assert "unknown tool" in r.text
    finally:
        adapter.close()


def test_call_before_connect_and_after_close(runtime):
    adapter = MCPServerAdapter(_stdio_config(), runtime, server=_inproc_server())
    r = adapter.call_tool("echo", {"message": "x"})
    assert r.is_error and r.error_kind == "connection_lost"
    with pytest.raises(MCPConnectError):
        adapter.list_descriptors()

    adapter.connect()
    adapter.close()
    r = adapter.call_tool("echo", {"message": "x"})
    assert r.is_error and r.error_kind == "connection_lost"
    adapter.close()  # idempotent


def test_streamable_http_not_implemented(runtime):
    cfg = ServerConfig(name="remote", transport="streamable_http", url="https://x.invalid/mcp")
    adapter = MCPServerAdapter(cfg, runtime)
    with pytest.raises(NotImplementedError):
        adapter.connect()


# ── registration strictness (no connection needed) ────────────

def _fake_tool(name: str, schema):
    return types.Tool(name=name, input_schema=schema)


def test_invalid_schema_aborts_registration(runtime):
    adapter = MCPServerAdapter(_stdio_config(), runtime)
    with pytest.raises(Exception) as ei:
        adapter._build_registration(
            [_fake_tool("good", {"type": "object", "properties": {}}),
             _fake_tool("bad", {"type": "string"})]
        )
    assert "bad" in str(ei.value)


def test_intra_server_name_collision_aborts_registration(runtime):
    adapter = MCPServerAdapter(_stdio_config(), runtime)
    ok = {"type": "object", "properties": {}}
    with pytest.raises(Exception) as ei:
        adapter._build_registration([_fake_tool("a.b", ok), _fake_tool("a_b", ok)])
    assert "collision" in str(ei.value)


def test_registration_truncates_long_names_with_real_mapping(runtime):
    adapter = MCPServerAdapter(_stdio_config(), runtime)
    long_name = "z" * 80
    registry, descriptors = adapter._build_registration(
        [_fake_tool(long_name, {"type": "object", "properties": {}})]
    )
    d = descriptors[0]
    assert len(d.prefixed_name) == 64
    assert d.raw_name == long_name
    assert registry.resolve(d.prefixed_name) == ("fake", long_name)


# ── result normalization (pure) ───────────────────────────────

def test_normalize_empty_content_yields_no_output():
    result = types.CallToolResult(content=[])
    normalized = normalize_call_result(result)
    assert normalized.text == "(no output)"
    assert not normalized.is_error


def test_normalize_embedded_and_link_content():
    result = types.CallToolResult(content=[
        types.ResourceLink(uri="file:///tmp/a.txt", name="a.txt"),
        types.EmbeddedResource(
            resource=types.TextResourceContents(uri="file:///tmp/b.txt", text="body")
        ),
    ])
    normalized = normalize_call_result(result)
    assert "[resource_link file:///tmp/a.txt]" in normalized.text
    assert "body" in normalized.text
    kinds = [p.kind for p in normalized.parts]
    assert kinds == ["resource_link", "embedded_resource"]


# ── real stdio subprocess e2e ─────────────────────────────────

def test_stdio_e2e_pagination_env_and_teardown(runtime):
    token = f"tok-{uuid.uuid4().hex[:8]}"
    host_only = f"HOST-{uuid.uuid4().hex[:8]}"
    os.environ[host_only] = "leak-me"  # must NOT reach the subprocess
    cfg = _stdio_config(env={"FAKE_TOKEN": token})
    adapter = MCPServerAdapter(cfg, runtime)
    adapter.connect()
    try:
        # pagination: page1 (3 tools) + page2 (3 tools) all discovered
        raws = {d.raw_name for d in adapter.list_descriptors()}
        assert {"echo", "env", "fail", "image", "structured_only"} <= raws
        assert "x" * 80 in raws

        # explicit env declared in config reaches the server
        assert adapter.call_tool_text("env") == token

        # normal roundtrip over a real subprocess
        assert adapter.call_tool_text("echo", {"message": "sub"}) == "echo: sub"

        # refresh_tools works against the live connection
        assert len(adapter.refresh_tools()) == len(raws)
    finally:
        adapter.close()
    del os.environ[host_only]


def test_stdio_collision_server_fails_connect_loudly(runtime):
    # COLLIDE_MODE=1 makes fake_server list both "a.b" and "a_b", which
    # normalize to the same alias — connect() must fail loudly.
    cfg = _stdio_config(env={"COLLIDE_MODE": "1"})
    adapter = MCPServerAdapter(cfg, runtime)
    with pytest.raises(MCPConnectError) as ei:
        adapter.connect()
    assert "collision" in str(ei.value).lower()
    assert adapter.state == MCPServerAdapter.DISCONNECTED


def test_connect_failure_bad_command(runtime):
    cfg = _stdio_config(command="/nonexistent/binary-xyz", args=())
    adapter = MCPServerAdapter(cfg, runtime)
    with pytest.raises(MCPConnectError):
        adapter.connect()
    assert adapter.state == MCPServerAdapter.DISCONNECTED
    # closing a never-connected adapter is safe
    adapter.close()
