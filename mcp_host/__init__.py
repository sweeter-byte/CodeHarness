"""Host-side MCP adapter layer for CodeHarness.

Scope (this stage): single-server adaptation only — config, naming/schema
conversion, shared sync runtime, and one ``MCPServerAdapter`` per server.
Multi-server lifecycle (Manager), dynamic ``connect_mcp``, tool-pool
assembly, and host permission policy are deliberately out of scope.
"""

from .adapter import (
    ToolDescriptor,
    ToolNameCollisionError,
    ToolNameRegistry,
    ToolSchemaError,
    build_prefixed_name,
    normalize_component,
    to_openai_tool,
    validate_input_schema,
)
from .client import (
    MCPCallResult,
    MCPConnectError,
    MCPContentPart,
    MCPServerAdapter,
    normalize_call_result,
)
from .config import MCPConfigError, ServerConfig, load_config, validate_config
from .runtime import SyncMCPRuntime, get_runtime, shutdown_runtime

__all__ = [
    "MCPCallResult",
    "MCPConfigError",
    "MCPConnectError",
    "MCPContentPart",
    "MCPServerAdapter",
    "ServerConfig",
    "SyncMCPRuntime",
    "ToolDescriptor",
    "ToolNameCollisionError",
    "ToolNameRegistry",
    "ToolSchemaError",
    "build_prefixed_name",
    "get_runtime",
    "load_config",
    "normalize_call_result",
    "normalize_component",
    "shutdown_runtime",
    "to_openai_tool",
    "validate_config",
    "validate_input_schema",
]
