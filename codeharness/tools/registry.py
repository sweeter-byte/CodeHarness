"""ToolRegistry — lightweight tool schema + handler aggregation.

Modules own their tools (schema + handler pairs); the registry merges them
into one tool set for an Agent. Deliberately minimal: no plugin framework,
no dependency injection — just registration, duplicate-name detection and
snapshot reads.

    Module owns Tool → ToolRegistry → Agent
"""

from collections.abc import Callable


class DuplicateToolError(ValueError):
    """Raised when a tool name is registered twice.

    Duplicate names are never silently overwritten — if MCP or any other
    module produces a collision, assembly must fail loudly.
    """


def _tool_name(schema) -> str:
    """Extract function.name from an OpenAI tool schema; raise if missing."""
    if not isinstance(schema, dict):
        raise ValueError(f"Tool schema must be a dict, got {type(schema).__name__}")
    function = schema.get("function")
    if not isinstance(function, dict) or not function.get("name"):
        raise ValueError(f"Tool schema is missing 'function.name': {schema!r}")
    return function["name"]


class ToolRegistry:
    """Schema + handler pairs keyed by tool name, insertion-ordered."""

    def __init__(self):
        self._schemas: dict[str, dict] = {}
        self._handlers: dict[str, Callable[..., str]] = {}

    def register(self, schema: dict, handler: Callable[..., str]) -> None:
        """Register one tool. Rejects missing names and duplicates."""
        name = _tool_name(schema)
        if name in self._schemas:
            raise DuplicateToolError(f"Tool '{name}' is already registered")
        if not callable(handler):
            raise ValueError(f"Handler for tool '{name}' must be callable")
        self._schemas[name] = schema
        self._handlers[name] = handler

    def extend(self, schemas: list[dict],
               handlers: dict[str, Callable[..., str]]) -> None:
        """Register a batch of tools.

        Every schema must have a matching handler in ``handlers``. Nothing
        is registered if any pair is invalid (all-or-nothing per call).
        """
        pairs = []
        for schema in schemas:
            name = _tool_name(schema)
            if name not in handlers:
                raise ValueError(f"Tool '{name}' has no matching handler")
            pairs.append((name, schema, handlers[name]))
        for name, schema, handler in pairs:
            self.register(schema, handler)

    def get(self, name: str) -> Callable[..., str] | None:
        """Handler lookup by tool name."""
        return self._handlers.get(name)

    def names(self) -> set[str]:
        return set(self._schemas)

    @property
    def schemas(self) -> list[dict]:
        """Snapshot list for model calls — mutating it does not affect the registry."""
        return list(self._schemas.values())

    @property
    def handlers(self) -> dict[str, Callable[..., str]]:
        """Snapshot map — mutating it does not affect the registry."""
        return dict(self._handlers)

    def __len__(self) -> int:
        return len(self._schemas)
