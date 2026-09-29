"""End-to-end test against the real Filesystem MCP Server (stdio, via npx).

Skipped when node/npx is unavailable. The first run may download
``@modelcontextprotocol/server-filesystem``, hence the generous timeouts.
Only read-only tools are invoked.
"""

import shutil

import pytest

from mcp_host.config import ServerConfig
from mcp_host.manager import MCPManager

pytestmark = pytest.mark.skipif(
    shutil.which("npx") is None,
    reason="npx (Node) not available for the Filesystem MCP e2e test",
)


def test_filesystem_manager_end_to_end(runtime, tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("MCP-E2E-OK")

    cfg = ServerConfig(
        name="filesystem",
        transport="stdio",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem", str(tmp_path)),
        connect_timeout=180.0,
        call_timeout=30.0,
    )
    manager = MCPManager({"filesystem": cfg}, runtime)
    manager.connect_all()
    try:
        st = manager.status["filesystem"]
        assert st.state == "connected", st.error
        assert st.tools > 0

        tools, handlers = manager.assemble(set())
        names = {t["function"]["name"] for t in tools}
        assert "mcp__filesystem__read_file" in names
        assert manager.resolve("mcp__filesystem__read_file") == ("filesystem", "read_file")

        # Dispatch through the generated handler exactly as the agent loop would.
        out = handlers["mcp__filesystem__read_file"](path=str(target))
        assert "MCP-E2E-OK" in out
    finally:
        manager.close_all()
