"""Fake stdio MCP server for adapter tests — not part of the runtime package.

Run as a subprocess: ``python mcp_host/tests/fake_server.py``.

Exercises: pagination (2 pages), name-collision inputs ("a.b" vs "a_b"),
deterministic long-name truncation, env pass-through, text/image/tool-error
results, and structured-content-only results.
"""

import base64
import os
import sys

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("fake")

_PAGE1 = [
    types.Tool(
        name="echo",
        description="Echo the message back.",
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    ),
    types.Tool(name="x" * 80, input_schema={"type": "object", "properties": {}}),
    types.Tool(name="env", input_schema={"type": "object", "properties": {}}),
]
# Only served when COLLIDE_MODE=1: "a.b" and "a_b" normalize to the same
# alias, so the adapter must reject the whole server (loud connect failure).
if os.environ.get("COLLIDE_MODE") == "1":
    _PAGE1 += [
        types.Tool(name="a.b", input_schema={"type": "object", "properties": {}}),
        types.Tool(name="a_b", input_schema={"type": "object", "properties": {}}),
    ]
_PAGE2 = [
    types.Tool(name="fail", input_schema={"type": "object", "properties": {}}),
    types.Tool(name="image", input_schema={"type": "object", "properties": {}}),
    types.Tool(
        name="structured_only",
        input_schema={"type": "object", "properties": {}},
    ),
]


async def on_list_tools(ctx, params):
    if params.cursor is None:
        return types.ListToolsResult(tools=_PAGE1, next_cursor="page2")
    if params.cursor == "page2":
        return types.ListToolsResult(tools=_PAGE2)
    raise ValueError(f"bad cursor: {params.cursor}")


async def on_call_tool(ctx, params):
    name = params.name
    arguments = params.arguments or {}
    if name == "echo":
        return types.CallToolResult(
            content=[types.TextContent(text=f"echo: {arguments.get('message', '')}")]
        )
    if name == "env":
        return types.CallToolResult(
            content=[types.TextContent(text=os.environ.get("FAKE_TOKEN", "<unset>"))]
        )
    if name == "fail":
        return types.CallToolResult(
            content=[types.TextContent(text="tool said no")], is_error=True
        )
    if name == "image":
        blob = base64.b64encode(b"fake-png-bytes").decode()
        return types.CallToolResult(
            content=[types.ImageContent(data=blob, mime_type="image/png")]
        )
    if name == "structured_only":
        return types.CallToolResult(content=[], structured_content={"answer": 42})
    return types.CallToolResult(
        content=[types.TextContent(text=f"unhandled tool {name}")], is_error=True
    )


server.add_request_handler("tools/list", types.PaginatedRequestParams, on_list_tools)
server.add_request_handler("tools/call", types.CallToolRequestParams, on_call_tool)


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    try:
        anyio.run(main)
    except KeyboardInterrupt:
        sys.exit(0)
