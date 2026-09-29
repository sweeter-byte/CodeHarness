"""Configuration layer for MCP servers.

Loads ``mcp_servers.json`` into validated :class:`ServerConfig` objects.

Environment-variable policy (two phases):

* **Expansion phase** (here): ``${VAR}`` placeholders inside ``command`` /
  ``args`` / ``env`` values are expanded from the host ``os.environ``
  (typically populated from ``.env`` via dotenv). A placeholder that cannot
  be resolved is a hard config error — fail fast at load time.
* **Pass-through phase** (client layer): only the explicitly declared
  ``env`` mapping is handed to ``StdioServerParameters.env``. The MCP SDK's
  stdio transport merges it over its own safe default environment
  (HOME/PATH/LANG/...), so host secrets such as API keys never leak into
  third-party MCP server subprocesses unless explicitly declared here.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Transport = Literal["stdio", "streamable_http"]

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MCPConfigError(Exception):
    """Raised when a server configuration is invalid or cannot be expanded."""

    def __init__(self, server_name: str, reason: str):
        super().__init__(f"MCP config error [{server_name}]: {reason}")
        self.server_name = server_name
        self.reason = reason


@dataclass(frozen=True)
class ServerConfig:
    """Validated, fully-expanded configuration for a single MCP server."""

    name: str
    transport: Transport
    # stdio-only fields
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    # streamable_http-only field (accepted by config, not yet implemented)
    url: str | None = None
    # timeouts (seconds) — connect covers spawn+handshake+initial tool fetch,
    # call covers a single tools/call round.
    connect_timeout: float = 30.0
    call_timeout: float = 60.0
    enabled: bool = True


def _expand(value: str, server_name: str) -> str:
    """Expand ``${VAR}`` from os.environ and ``~``; raise on unresolved vars."""
    expanded = os.path.expanduser(value)

    def replace(match: re.Match) -> str:
        var = match.group(1)
        if var not in os.environ:
            raise MCPConfigError(
                server_name,
                f"environment variable ${{{var}}} is referenced but not set",
            )
        return os.environ[var]

    return _VAR_RE.sub(replace, expanded)


def validate_config(name: str, raw: dict) -> ServerConfig:
    """Validate one raw JSON entry into a ServerConfig (expansion included)."""
    if not isinstance(raw, dict):
        raise MCPConfigError(name, "server entry must be a JSON object")

    transport = raw.get("transport")
    if transport not in ("stdio", "streamable_http"):
        raise MCPConfigError(
            name, f"transport must be 'stdio' or 'streamable_http', got {transport!r}"
        )

    enabled = bool(raw.get("enabled", True))

    def _exp(value: str) -> str:
        # Disabled entries are structure-checked only: their ${VAR}s may
        # legitimately be unset (e.g. a PAT not yet configured), and they
        # are never spawned, so expansion is skipped.
        return _expand(value, name) if enabled else value

    def _timeout(key: str, default: float) -> float:
        value = raw.get(key, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise MCPConfigError(name, f"{key} must be a positive number, got {value!r}")
        return float(value)

    connect_timeout = _timeout("connect_timeout", 30.0)
    call_timeout = _timeout("call_timeout", 60.0)

    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = {}
    url: str | None = None

    if transport == "stdio":
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise MCPConfigError(name, "stdio transport requires a non-empty 'command'")
        command = _exp(command)

        raw_args = raw.get("args", [])
        if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
            raise MCPConfigError(name, "'args' must be a list of strings")
        args = tuple(_exp(a) for a in raw_args)

        raw_env = raw.get("env", {})
        if not isinstance(raw_env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw_env.items()
        ):
            raise MCPConfigError(name, "'env' must be a mapping of string to string")
        env = {k: _exp(v) for k, v in raw_env.items()}
    else:
        url = raw.get("url")
        if not isinstance(url, str) or not url.strip():
            raise MCPConfigError(
                name, "streamable_http transport requires a non-empty 'url'"
            )
        url = _exp(url)

    return ServerConfig(
        name=name,
        transport=transport,
        command=command,
        args=args,
        env=env,
        url=url,
        connect_timeout=connect_timeout,
        call_timeout=call_timeout,
        enabled=enabled,
    )


def load_config(path: str | Path) -> dict[str, ServerConfig]:
    """Load and validate every server entry from a JSON config file.

    Disabled entries are returned with ``enabled=False`` and are structure-
    checked (a typo cannot hide behind a disabled flag), but their ``${VAR}``
    placeholders are NOT expanded, so an unconfigured secret cannot break
    loading of the enabled servers. Any single invalid entry aborts the whole
    load (fail fast).
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise MCPConfigError(str(p), f"config file not found: {p}")
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise MCPConfigError(str(p), f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise MCPConfigError(str(p), "top-level JSON value must be an object")

    return {name: validate_config(name, raw) for name, raw in data.items()}
