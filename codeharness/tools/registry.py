"""ToolRegistry — lightweight tool schema + handler aggregation.

Modules own their tools (schema + handler pairs); the registry merges them
into one tool set for an Agent. Deliberately minimal: no plugin framework,
no dependency injection — just registration, duplicate-name detection and
snapshot reads.

    Module owns Tool → ToolRegistry → Agent
"""

import copy
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
    """Schema + handler pairs keyed by tool name, insertion-ordered.

    Schemas are stored and returned as deep copies, so neither the caller's
    original object nor a handed-out snapshot can mutate registry state.
    Handlers are callables and are kept by reference (never copied).
    """

    def __init__(self):
        self._schemas: dict[str, dict] = {}
        self._handlers: dict[str, Callable[..., str]] = {}

    def register(self, schema: dict, handler: Callable[..., str]) -> None:
        """Register one tool. Rejects missing names and duplicates.

        The schema is deep-copied on the way in, so later mutation of the
        caller's object cannot reach registry state.
        """
        name = _tool_name(schema)
        if name in self._schemas:
            raise DuplicateToolError(f"Tool '{name}' is already registered")
        if not callable(handler):
            raise ValueError(f"Handler for tool '{name}' must be callable")
        self._schemas[name] = copy.deepcopy(schema)
        self._handlers[name] = handler

    def extend(self, schemas: list[dict],
               handlers: dict[str, Callable[..., str]]) -> None:
        """Register a batch of tools, all-or-nothing.

        The entire batch is validated BEFORE any registry state is touched:
        every schema needs a valid ``function.name``, a matching callable
        handler, no duplicate within the batch, and no collision with an
        already-registered tool. If any check fails the registry is left
        exactly as it was (no partial registration, no rollback needed).
        """
        # ── preflight: validate the whole batch, mutate nothing ──
        batch: list[tuple[str, dict, Callable[..., str]]] = []
        seen: set[str] = set()
        for schema in schemas:
            name = _tool_name(schema)
            if name not in handlers:
                raise ValueError(f"Tool '{name}' has no matching handler")
            handler = handlers[name]
            if not callable(handler):
                raise ValueError(f"Handler for tool '{name}' must be callable")
            if name in seen:
                raise DuplicateToolError(
                    f"Tool '{name}' appears more than once in the batch")
            if name in self._schemas:
                raise DuplicateToolError(f"Tool '{name}' is already registered")
            seen.add(name)
            batch.append((name, schema, handler))
        # ── commit: the batch is fully valid, so this cannot fail midway ──
        for name, schema, handler in batch:
            self._schemas[name] = copy.deepcopy(schema)
            self._handlers[name] = handler

    def get(self, name: str) -> Callable[..., str] | None:
        """Handler lookup by tool name."""
        return self._handlers.get(name)

    def names(self) -> set[str]:
        return set(self._schemas)

    @property
    def schemas(self) -> list[dict]:
        """Deep-copied snapshot list for model calls.

        Mutating the list, or any nested dict inside a schema, never affects
        the registry's internal state.
        """
        return copy.deepcopy(list(self._schemas.values()))

    @property
    def handlers(self) -> dict[str, Callable[..., str]]:
        """Snapshot map — mutating it does not affect the registry.

        Handlers are callables, so the values stay shared by reference.
        """
        return dict(self._handlers)

    def __len__(self) -> int:
        return len(self._schemas)
