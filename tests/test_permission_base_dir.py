"""Tests for PermissionManager workspace-anchored path resolution.

Focus: relative paths in permission checks must resolve against the
manager's base_dir (the Runtime workspace), not the process cwd — the same
paths the coding tool handlers (bound to RuntimeConfig.workspace) use.
Also covers the re-ordered check() pipeline: path boundary checks run
BEFORE the read-only auto-allow, plus the conservative glob '..' rule.
No LLM calls are involved.
"""

import sys
from pathlib import Path

import pytest

# Make the project root importable regardless of how pytest is invoked.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from codeharness.security.permission import PermissionManager


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A workspace, an outside dir, and a process cwd distinct from both."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # The process cwd deliberately differs from the workspace.
    cwd = tmp_path / "process_cwd"
    cwd.mkdir()
    (cwd / "src").mkdir()  # same-named dir under cwd to catch cwd-based bugs
    monkeypatch.chdir(cwd)
    mgr = PermissionManager(
        allowed_dirs=[str(workspace)], base_dir=str(workspace)
    )
    return mgr, workspace, outside, cwd


def test_workspace_differs_from_process_cwd(layout):
    mgr, workspace, outside, cwd = layout
    assert Path.cwd() == cwd
    assert workspace != cwd
    assert mgr.base_dir == workspace.resolve()
    assert mgr.allowed_dirs == [workspace.resolve()]


def test_relative_escape_resolves_against_workspace_not_cwd(layout):
    # "../outside" from the workspace lands outside the allowed dir → ask.
    # (Both the workspace and the process cwd are siblings under tmp_path,
    # so this only distinguishes bases when combined with the next test.)
    mgr, workspace, outside, cwd = layout
    decision, reason = mgr.check("read_file", {"path": "../outside/secret.txt"})
    assert decision == "ask"
    assert "outside allowed directories" in reason


def test_relative_inside_workspace_allowed_even_when_cwd_differs(layout):
    # "src/main.py" resolves to workspace/src/main.py (inside → allow).
    # If resolution were cwd-based it would land in process_cwd/src/main.py,
    # which is NOT an allowed dir, so an "ask" here would expose the bug.
    mgr, workspace, outside, cwd = layout
    decision, _ = mgr.check("read_file", {"path": "src/main.py"})
    assert decision == "allow"


def test_relative_outside_workspace_asks(layout):
    mgr, workspace, outside, cwd = layout
    decision, _ = mgr.check("read_file", {"path": "../process_cwd/src/main.py"})
    assert decision == "ask"


def test_grep_path_inside_workspace_allowed(layout):
    # grep is read-only, but read-only must NOT skip the boundary check.
    mgr, workspace, outside, cwd = layout
    decision, _ = mgr.check("grep", {"pattern": "x", "path": "src"})
    assert decision == "allow"


def test_grep_path_outside_workspace_asks(layout):
    mgr, workspace, outside, cwd = layout
    decision, reason = mgr.check("grep", {"pattern": "x", "path": "../outside"})
    assert decision == "ask"
    assert "outside allowed directories" in reason


def test_glob_ordinary_patterns_allowed(layout):
    mgr, workspace, outside, cwd = layout
    assert mgr.check("glob", {"pattern": "*.py"})[0] == "allow"
    assert mgr.check("glob", {"pattern": "src/**/*.py"})[0] == "allow"
    assert mgr.check("glob", {"pattern": "tests/*"})[0] == "allow"
    # plain '**' recursion must never be blocked
    assert mgr.check("glob", {"pattern": "**"})[0] == "allow"


def test_glob_parent_traversal_asks(layout):
    mgr, workspace, outside, cwd = layout
    for pattern in ("../*", "../../secret/*", ".."):
        decision, reason = mgr.check("glob", {"pattern": pattern})
        assert decision == "ask", pattern
        assert "escape" in reason


def test_absolute_path_inside_workspace_allowed(layout):
    mgr, workspace, outside, cwd = layout
    decision, _ = mgr.check("read_file", {"path": str(workspace / "src" / "a.py")})
    assert decision == "allow"


def test_absolute_path_outside_workspace_asks(layout):
    mgr, workspace, outside, cwd = layout
    decision, _ = mgr.check("read_file", {"path": str(outside / "secret.txt")})
    assert decision == "ask"


def test_bash_relative_paths_use_workspace_as_base(layout):
    mgr, workspace, outside, cwd = layout
    # "cat src/main.py" resolves to workspace/src/main.py → inside → allow
    # (no deny/ask bash rule matches a plain cat).
    decision, _ = mgr.check("bash", {"command": "cat src/main.py"})
    assert decision == "allow"
    # "../outside/secret.txt" escapes the workspace → ask, regardless of cwd.
    decision, reason = mgr.check("bash", {"command": "cat ../outside/secret.txt"})
    assert decision == "ask"
    assert "outside allowed directories" in reason


def test_mcp_policy_unchanged_and_independent_of_paths(layout):
    # MCP tools go through the host policy first; path rules never apply.
    mgr, workspace, outside, cwd = layout
    mgr.set_mcp_provider(lambda p: ("filesystem", "read_file"))
    decision, _ = mgr.check(
        "mcp__filesystem__read_file", {"path": str(outside / "x")}
    )
    assert decision == "allow"
    mgr.set_mcp_provider(lambda p: ("filesystem", "write_file"))
    decision, _ = mgr.check("mcp__filesystem__write_file", {"path": "a"})
    assert decision == "ask"


def test_write_tools_inside_workspace_still_ask(layout):
    mgr, workspace, outside, cwd = layout
    decision, reason = mgr.check("write_file", {"path": "a.txt", "content": "x"})
    assert decision == "ask"
    assert "File modification" in reason
    decision, _ = mgr.check("edit_file", {"path": "src/a.py"})
    assert decision == "ask"


def test_standalone_construction_falls_back_to_cwd(tmp_path, monkeypatch):
    # No base_dir → legacy behavior: resolve against the process cwd.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(workspace)
    mgr = PermissionManager()  # allowed_dirs and base_dir default to cwd
    assert mgr.base_dir == workspace.resolve()
    assert mgr.check("read_file", {"path": "a.txt"})[0] == "allow"
    assert mgr.check("read_file", {"path": "../outside/a.txt"})[0] == "ask"
