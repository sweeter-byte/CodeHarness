"""Host-side MCP adapter layer for CodeHarness.

Scope (this stage): config loading, naming/schema conversion, a shared sync
runtime, one ``MCPServerAdapter`` per server, and an ``MCPManager`` that owns
multi-server lifecycle + tool-pool assembly + dispatch. Still out of scope:
runtime ``connect_mcp``, ``tools/list_changed`` subscription, automatic
retry / reconnect-replay, and teammate/subagent MCP inheritance.
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
from .manager import MCPAssemblyError, MCPManager, ServerStatus
from .runtime import SyncMCPRuntime, get_runtime, shutdown_runtime

__all__ = [
    "MCPAssemblyError",
    "MCPCallResult",
    "MCPConfigError",
    "MCPConnectError",
    "MCPContentPart",
    "MCPManager",
    "MCPServerAdapter",
    "ServerConfig",
    "ServerStatus",
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
