"""Unit tests for codeharness.mcp.adapter — pure naming / registry / schema logic."""

import pytest

from codeharness.mcp.adapter import (
    MAX_NAME_LEN,
    ToolDescriptor,
    ToolNameCollisionError,
    ToolNameRegistry,
    ToolSchemaError,
    build_prefixed_name,
    normalize_component,
    to_openai_tool,
    validate_input_schema,
)

# ── naming ────────────────────────────────────────────────────

def test_normalize_component_replaces_illegal_chars():
    assert normalize_component("docs.one/get-version") == "docs_one_get-version"
    assert normalize_component("plain_name-1") == "plain_name-1"
    assert normalize_component("") == "_"


def test_prefixed_name_basic():
    assert build_prefixed_name("docs", "search") == "mcp__docs__search"
    assert build_prefixed_name("docs", "one/get.version") == "mcp__docs__one_get_version"


def test_prefixed_name_distinct_after_normalization_does_not_collide():
    # Different raw names that normalize identically must be caught by the
    # registry, not by build_prefixed_name — verify they indeed map equal.
    assert build_prefixed_name("s", "a.b") == build_prefixed_name("s", "a_b")


def test_long_name_truncated_deterministically():
    raw = "x" * 80
    first = build_prefixed_name("srv", raw)
    second = build_prefixed_name("srv", raw)
    assert first == second                     # deterministic alias
    assert len(first) == MAX_NAME_LEN          # respects the 64-char cap
    assert first.startswith("mcp__srv__")      # readable prefix kept
    assert "_x" in first                       # hash suffix appended


def test_long_name_different_raws_diverge():
    a = build_prefixed_name("srv", "y" * 70 + "a")
    b = build_prefixed_name("srv", "y" * 70 + "b")
    assert a != b  # same readable prefix, different hash suffix


# ── registry ──────────────────────────────────────────────────

def test_registry_roundtrip():
    reg = ToolNameRegistry()
    reg.add("mcp__s__t", "s", "t")
    assert reg.resolve("mcp__s__t") == ("s", "t")
    assert reg.prefixed_for("s", "t") == "mcp__s__t"
    assert reg.resolve("mcp__s__nope") is None
    assert len(reg) == 1
    assert reg.entries() == [("mcp__s__t", "s", "t")]


def test_registry_detects_collision():
    reg = ToolNameRegistry()
    reg.add("mcp__s__a_b", "s", "a.b")
    with pytest.raises(ToolNameCollisionError) as ei:
        reg.add("mcp__s__a_b", "s", "a_b")
    assert "a.b" in str(ei.value) and "a_b" in str(ei.value)


# ── schema validation ─────────────────────────────────────────

def test_valid_schema_passthrough_preserves_advanced_keywords():
    schema = {
        "type": "object",
        "properties": {"q": {"$ref": "#/definitions/Query"}},
        "anyOf": [{"required": ["q"]}, {"required": ["id"]}],
        "additionalProperties": False,
        "required": [],
    }
    out = validate_input_schema("s", "t", schema)
    assert out == schema  # verbatim, no lossy pruning


def test_object_schema_without_properties_gets_empty_map():
    out = validate_input_schema("s", "t", {"type": "object"})
    assert out == {"type": "object", "properties": {}}


@pytest.mark.parametrize("bad", [
    None,                                        # missing
    "not-a-dict",                                # wrong container
    {"properties": {}},                          # no root type
    {"type": "string"},                          # non-object root
    {"type": ["object", "null"]},                # type union, not plain object
])
def test_invalid_schema_raises_with_context(bad):
    with pytest.raises(ToolSchemaError) as ei:
        validate_input_schema("srv", "tool1", bad)
    assert "srv" in str(ei.value) and "tool1" in str(ei.value)


# ── openai rendering ──────────────────────────────────────────

def test_to_openai_tool_fixed_keynames():
    d = ToolDescriptor(
        prefixed_name="mcp__s__t",
        server_name="s",
        raw_name="t",
        description="does things",
        input_schema={"type": "object", "properties": {}},
    )
    tool = to_openai_tool(d)
    assert tool["type"] == "function"
    fn = tool["function"]
    assert fn["name"] == "mcp__s__t"
    assert fn["description"] == "does things"
    assert fn["parameters"] == {"type": "object", "properties": {}}
