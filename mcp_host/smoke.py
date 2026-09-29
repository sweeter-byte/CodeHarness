"""Smoke test against the real Filesystem MCP Server (stdio, via npx).

Usage (from the project root, conda env `coding-agent`):

    python -m mcp_host.smoke [server_name]     # default: filesystem

Requires node/npx; the first run downloads @modelcontextprotocol/server-filesystem.
Only read-only tools are invoked (list_directory / read_file).
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")
# ${WORKSPACE} in mcp_servers.json defaults to the project root.
os.environ.setdefault("WORKSPACE", str(PROJECT_ROOT))

from mcp_host import (
    MCPServerAdapter,
    load_config,
    shutdown_runtime,
    to_openai_tool,
)


def main(server_name: str = "filesystem") -> int:
    config_path = PROJECT_ROOT / "mcp_servers.json"
    configs = load_config(config_path)
    if server_name not in configs:
        print(f"server '{server_name}' not in {config_path}; have: {list(configs)}")
        return 1
    cfg = configs[server_name]
    if not cfg.enabled:
        print(f"server '{server_name}' is disabled in config")
        return 1

    adapter = MCPServerAdapter(cfg)
    print(f"connecting to '{server_name}' via {cfg.transport}: {cfg.command} {' '.join(cfg.args)}")
    adapter.connect()
    print(f"connected, state={adapter.state}")
    try:
        descriptors = adapter.list_descriptors()
        print(f"\ndiscovered {len(descriptors)} tools:")
        for d in descriptors:
            hints = []
            if d.annotations is not None:
                if getattr(d.annotations, "read_only_hint", False):
                    hints.append("read-only")
                if getattr(d.annotations, "destructive_hint", False):
                    hints.append("destructive")
            hint = f"  [{', '.join(hints)}]" if hints else ""
            print(f"  {d.prefixed_name}  (raw: {d.raw_name}){hint}")

        # Show one OpenAI-rendered schema to verify the conversion path.
        sample = to_openai_tool(descriptors[0])
        print(f"\nopenai schema sample: {sample['function']['name']} "
              f"params keys={list(sample['function']['parameters'])}")

        workspace = os.environ["WORKSPACE"]
        r = adapter.call_tool("list_directory", {"path": workspace})
        print(f"\nlist_directory({workspace}) -> is_error={r.is_error} kind={r.error_kind}")
        print(r.text[:400])

        readme = Path(workspace) / "README.md"
        if readme.is_file():
            r = adapter.call_tool("read_file", {"path": str(readme)})
            print(f"\nread_file(README.md) -> is_error={r.is_error}, {len(r.text)} chars")
            print(r.text[:200])

        r = adapter.call_tool("no_such_tool", {})
        print(f"\nunknown tool -> is_error={r.is_error} kind={r.error_kind}: {r.text}")
        return 0
    finally:
        adapter.close()
        shutdown_runtime()
        print(f"\nclosed, state={adapter.state}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "filesystem"))
