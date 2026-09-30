"""Minimal ToolRegistry unit tests (Phase 2)."""

import pytest

from codeharness.tools.registry import DuplicateToolError, ToolRegistry


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": name, "parameters": {}},
    }


def _handler(name: str):
    def handler(**kwargs) -> str:
        return name
    return handler


def test_register_and_lookup():
    registry = ToolRegistry()
    registry.register(_schema("alpha"), _handler("alpha"))
    assert registry.get("alpha") is not None
    assert registry.get("missing") is None
    assert registry.names() == {"alpha"}
    assert len(registry) == 1
    assert [s["function"]["name"] for s in registry.schemas] == ["alpha"]


def test_extend_pairs_schemas_with_handlers():
    registry = ToolRegistry()
    handlers = {"alpha": _handler("alpha"), "beta": _handler("beta")}
    registry.extend([_schema("alpha"), _schema("beta")], handlers)
    assert registry.names() == {"alpha", "beta"}
    assert registry.handlers["beta"] is handlers["beta"]


def test_duplicate_name_rejected():
    registry = ToolRegistry()
    registry.register(_schema("alpha"), _handler("alpha"))
    with pytest.raises(DuplicateToolError):
        registry.register(_schema("alpha"), _handler("other"))
    # the original registration is untouched
    assert len(registry) == 1


def test_schema_without_name_rejected():
    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.register({"type": "function"}, _handler("x"))


def test_extend_without_matching_handler_rejected():
    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.extend([_schema("alpha")], {})
    # all-or-nothing: a later bad pair must not register earlier ones
    with pytest.raises(ValueError):
        registry.extend([_schema("alpha"), _schema("beta")],
                        {"alpha": _handler("alpha")})
    assert len(registry) == 0


def test_snapshots_do_not_expose_internal_state():
    registry = ToolRegistry()
    registry.register(_schema("alpha"), _handler("alpha"))
    schemas = registry.schemas
    handlers = registry.handlers
    schemas.clear()
    handlers.clear()
    handlers["injected"] = _handler("injected")
    assert registry.names() == {"alpha"}
    assert len(registry.schemas) == 1
    assert "injected" not in registry.handlers
