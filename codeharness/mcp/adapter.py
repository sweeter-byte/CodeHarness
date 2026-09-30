"""Pure conversion layer: naming, registry, and schema translation.

No IO and no SDK dependency in this module (SDK types are referenced only
through duck typing / ``from __future__ import annotations``), so everything
here is trivially unit-testable.

Naming contract:

* Model-visible tool name: ``mcp__{server}__{tool}`` after normalization to
  the OpenAI function-name charset ``[A-Za-z0-9_-]``.
* Names exceeding 64 chars are shortened deterministically: a readable prefix
  plus ``_x{sha256(server\\x00raw)[:8]}``, so the same raw name always maps to
  the same alias and distinct raw names essentially cannot collide.
* Collision detection here covers a **single server** only; cross-server and
  cross-builtin-tool collisions are the (future) Manager's responsibility.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    import mcp_types as types

# OpenAI function-name charset; anything else is normalized to "_".
_ILLEGAL_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")

PREFIX_TEMPLATE = "mcp__{server}__{tool}"
MAX_NAME_LEN = 64
_HASH_LEN = 8  # sha256 hex chars appended as "_x{hash}"


class ToolSchemaError(Exception):
    """Raised when an MCP tool's inputSchema is missing or not a valid object schema."""

    def __init__(self, server_name: str, raw_tool: str, reason: str):
        super().__init__(
            f"MCP tool schema error [{server_name}.{raw_tool}]: {reason}"
        )
        self.server_name = server_name
        self.raw_tool = raw_tool
        self.reason = reason


class ToolNameCollisionError(Exception):
    """Raised when two raw tool names of one server normalize to the same alias."""

    def __init__(self, server_name: str, prefixed: str, first_raw: str, second_raw: str):
        super().__init__(
            f"MCP tool name collision on server '{server_name}': "
            f"'{first_raw}' and '{second_raw}' both normalize to '{prefixed}'"
        )
        self.server_name = server_name
        self.prefixed = prefixed
        self.first_raw = first_raw
        self.second_raw = second_raw


def normalize_component(raw: str) -> str:
    """Map a server/tool name into the OpenAI function-name charset."""
    cleaned = _ILLEGAL_NAME_CHARS.sub("_", raw)
    return cleaned or "_"


def _stable_hash(server_name: str, raw_tool: str) -> str:
    digest = hashlib.sha256(f"{server_name}\x00{raw_tool}".encode()).hexdigest()
    return digest[:_HASH_LEN]


def build_prefixed_name(server_name: str, raw_tool: str) -> str:
    """Build the model-visible alias, deterministically shortened past 64 chars."""
    safe_server = normalize_component(server_name)
    safe_tool = normalize_component(raw_tool)
    prefixed = PREFIX_TEMPLATE.format(server=safe_server, tool=safe_tool)
    if len(prefixed) <= MAX_NAME_LEN:
        return prefixed
    suffix = f"_x{_stable_hash(server_name, raw_tool)}"
    keep = MAX_NAME_LEN - len(suffix)
    return prefixed[:keep] + suffix


def validate_input_schema(server_name: str, raw_tool: str, schema: Any) -> dict:
    """Strictly validate an MCP inputSchema; never silently degrade.

    A missing schema or a root that is not a JSON-Schema ``object`` aborts
    registration of the whole server (partial tool sets mislead the model).
    Legal schemas are passed through verbatim — ``$ref`` / ``anyOf`` /
    ``oneOf`` and friends are preserved without lossy pruning.
    """
    if schema is None:
        raise ToolSchemaError(server_name, raw_tool, "inputSchema is missing")
    if not isinstance(schema, dict):
        raise ToolSchemaError(
            server_name, raw_tool, f"inputSchema must be an object, got {type(schema).__name__}"
        )
    root_type = schema.get("type")
    if root_type is None:
        raise ToolSchemaError(server_name, raw_tool, "inputSchema root lacks 'type'")
    if root_type != "object":
        raise ToolSchemaError(
            server_name, raw_tool, f"inputSchema root type must be 'object', got {root_type!r}"
        )
    result = dict(schema)
    # A legal object schema without explicit properties means "any/no args";
    # adding the empty map is JSON-Schema-legal, not a lossy rewrite.
    result.setdefault("properties", {})
    return result


@dataclass(frozen=True)
class ToolDescriptor:
    """One discovered MCP tool, normalized for host-side use."""

    prefixed_name: str
    server_name: str
    raw_name: str
    description: str
    input_schema: dict
    annotations: types.ToolAnnotations | None = None


def to_openai_tool(descriptor: ToolDescriptor) -> dict:
    """Render a descriptor as an OpenAI Chat-Completions function tool."""
    return {
        "type": "function",
        "function": {
            "name": descriptor.prefixed_name,
            "description": descriptor.description,
            "parameters": descriptor.input_schema,
        },
    }


class ToolNameRegistry:
    """Bidirectional map: prefixed alias <-> (server_name, raw_tool_name).

    Holds the ground truth for dispatch. Even when an alias was shortened by
    ``build_prefixed_name``, resolve() always returns the original raw name.
    """

    def __init__(self) -> None:
        self._by_prefixed: dict[str, tuple[str, str]] = {}
        self._by_raw: dict[tuple[str, str], str] = {}

    def add(self, prefixed: str, server_name: str, raw_name: str) -> None:
        existing = self._by_prefixed.get(prefixed)
        if existing is not None:
            raise ToolNameCollisionError(server_name, prefixed, existing[1], raw_name)
        self._by_prefixed[prefixed] = (server_name, raw_name)
        self._by_raw[(server_name, raw_name)] = prefixed

    def resolve(self, prefixed: str) -> tuple[str, str] | None:
        """prefixed alias -> (server_name, raw_name), or None if unknown."""
        return self._by_prefixed.get(prefixed)

    def prefixed_for(self, server_name: str, raw_name: str) -> str | None:
        return self._by_raw.get((server_name, raw_name))

    def entries(self) -> list[tuple[str, str, str]]:
        return [(p, s, r) for p, (s, r) in self._by_prefixed.items()]

    def __len__(self) -> int:
        return len(self._by_prefixed)
