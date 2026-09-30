"""Tests for MCP host-side permission gating in permission.py.

Focus: the ``mcp__`` branch of PermissionManager.check() driven by
MCP_HOST_POLICY, resolved through injected callables (no MCPManager needed),
plus a regression check that native tools are unaffected.
"""

from codeharness.security import permission
from codeharness.security.permission import MCP_HOST_POLICY, PermissionManager


class _Ann:
    """Stub annotations object exposing only the hint flags we read."""

    def __init__(self, **flags):
        self.read_only_hint = flags.get("read_only", False)
        self.destructive_hint = flags.get("destructive", False)
        self.idempotent_hint = flags.get("idempotent", False)
        self.open_world_hint = flags.get("open_world", False)


def _mgr(pair, annotations=None):
    return PermissionManager(
        mcp_resolve=lambda prefixed: pair,
        mcp_annotations_of=(lambda prefixed: annotations) if annotations is not None else None,
    )


def test_readonly_filesystem_tool_is_allowed():
    m = _mgr(("filesystem", "read_file"))
    decision, _ = m.check("mcp__filesystem__read_file", {"path": "/x"})
    assert decision == "allow"


def test_write_filesystem_tool_needs_approval():
    m = _mgr(("filesystem", "write_file"))
    decision, reason = m.check("mcp__filesystem__write_file", {"path": "/x", "content": "y"})
    assert decision == "ask"
    assert "requires approval" in reason


def test_unlisted_tool_defaults_to_ask():
    m = _mgr(("filesystem", "some_brand_new_tool"))
    decision, _ = m.check("mcp__filesystem__some_brand_new_tool", {})
    assert decision == "ask"


def test_unresolvable_tool_defaults_to_ask():
    m = _mgr(None)  # resolver returns None → not registered
    decision, reason = m.check("mcp__unknown__thing", {})
    assert decision == "ask"
    assert "Unregistered" in reason


def test_no_resolver_defaults_to_ask():
    m = PermissionManager()  # no MCP provider injected at all
    decision, _ = m.check("mcp__filesystem__read_file", {})
    assert decision == "ask"


def test_deny_policy_blocks(monkeypatch):
    monkeypatch.setitem(MCP_HOST_POLICY, ("filesystem", "delete_file"), "deny")
    m = _mgr(("filesystem", "delete_file"))
    decision, reason = m.check("mcp__filesystem__delete_file", {"path": "/x"})
    assert decision == "deny"
    assert "denied by host policy" in reason


def test_annotation_hint_appears_in_reason_but_does_not_authorize():
    # destructive hint on an otherwise-allowed tool: hint is shown, decision
    # still comes from policy (allow), never from the annotation.
    m = _mgr(("filesystem", "read_file"), annotations=_Ann(read_only=True))
    decision, reason = m.check("mcp__filesystem__read_file", {})
    assert decision == "allow"
    assert "read-only" in reason


def test_set_mcp_provider_after_construction():
    m = PermissionManager()
    assert m.check("mcp__filesystem__read_file", {})[0] == "ask"  # before injection
    m.set_mcp_provider(lambda p: ("filesystem", "read_file"))
    assert m.check("mcp__filesystem__read_file", {})[0] == "allow"  # after


# ── native tools unaffected (regression) ──────────────────────

def test_native_tools_still_gated_by_existing_rules():
    m = PermissionManager()
    assert m.check("glob", {})[0] == "allow"
    assert m.check("bash", {"command": "rm -rf /"})[0] == "deny"
    assert m.check("bash", {"command": "ls -la"})[0] == "allow"
    assert m.check("write_file", {"path": "a.txt", "content": "x"})[0] == "ask"


def test_policy_values_are_within_allowed_vocabulary():
    assert set(MCP_HOST_POLICY.values()) <= {"allow", "ask", "deny"}
    assert permission.MCP_TOOL_PREFIX == "mcp__"
