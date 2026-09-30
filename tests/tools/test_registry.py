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


def test_mutating_nested_snapshot_schema_does_not_affect_registry():
    registry = ToolRegistry()
    registry.register(_schema("alpha"), _handler("alpha"))
    snapshot = registry.schemas
    # mutate every level of the handed-out nested schema
    snapshot[0]["function"]["name"] = "hijacked"
    snapshot[0]["function"]["parameters"]["injected"] = True
    snapshot[0]["type"] = "mutated"
    fresh = registry.schemas[0]
    assert fresh["function"]["name"] == "alpha"
    assert fresh["type"] == "function"
    assert "injected" not in fresh["function"]["parameters"]


def test_mutating_original_schema_after_register_does_not_affect_registry():
    registry = ToolRegistry()
    original = _schema("alpha")
    registry.register(original, _handler("alpha"))
    original["function"]["name"] = "hijacked"
    original["function"]["parameters"]["injected"] = True
    stored = registry.schemas[0]
    assert stored["function"]["name"] == "alpha"
    assert "injected" not in stored["function"]["parameters"]


def test_extend_collision_with_registry_registers_nothing():
    registry = ToolRegistry()
    registry.register(_schema("existing"), _handler("existing"))
    handlers = {"new_one": _handler("new_one"), "existing": _handler("dup")}
    with pytest.raises(DuplicateToolError):
        registry.extend([_schema("new_one"), _schema("existing")], handlers)
    # the valid earlier pair must NOT have been partially registered
    assert registry.names() == {"existing"}
    assert registry.get("new_one") is None


def test_extend_internal_duplicate_registers_nothing():
    registry = ToolRegistry()
    handlers = {"alpha": _handler("alpha"), "beta": _handler("beta")}
    with pytest.raises(DuplicateToolError):
        registry.extend([_schema("alpha"), _schema("beta"), _schema("alpha")],
                        handlers)
    assert len(registry) == 0
    assert registry.names() == set()


def test_extend_non_callable_handler_registers_nothing():
    registry = ToolRegistry()
    handlers = {"alpha": _handler("alpha"), "beta": "not-callable"}
    with pytest.raises(ValueError):
        registry.extend([_schema("alpha"), _schema("beta")], handlers)
    assert len(registry) == 0
    assert registry.get("alpha") is None
