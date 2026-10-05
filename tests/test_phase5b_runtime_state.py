"""Phase 5B — Runtime-owned mutable state + lifecycle cleanup.

Verifies that state belonging to one CodeHarness Runtime is created and
released by that Runtime: no process-global TodoManager / BackgroundManager /
SESSION_STATS defaults, per-instance Agent state, workspace-rooted
permissions, and full cleanup on close() (including after a partial start).

No test here calls a real LLM API.
"""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


# ── helpers ────────────────────────────────────────────────────


def _bare_agent(agent_module, **kwargs):
    """Construct a real Agent without touching the network."""
    defaults = {
        "client": object(),
        "model": "test-model",
        "model_context_window": 4096,
        "memory_manager": False,
    }
    defaults.update(kwargs)
    return agent_module.Agent(**defaults)


class _FakeMCPManager:
    def __init__(self, configs):
        self.configs = configs
        self.closed = False

    def connect_all(self):
        pass

    def status_lines(self):
        return []

    def assemble(self, names):
        return [], {}

    def resolve(self, prefixed):
        return None

    def annotations_of(self, prefixed):
        return None

    def close_all(self):
        self.closed = True


class _RecordingWorkspaceBackend:
    def __init__(self):
        self.calls = []

    def bash(self, command, run_in_background=False, cwd=None):
        self.calls.append(("bash", command, run_in_background, cwd))
        return f"backend-bash:{command}"

    def read_file(self, path, start_line=None, end_line=None, cwd=None):
        self.calls.append(("read_file", path, start_line, end_line, cwd))
        return f"backend-read:{path}"

    def write_file(self, path, content, cwd=None):
        self.calls.append(("write_file", path, content, cwd))
        return f"backend-write:{path}"

    def edit_file(self, path, old_text, new_text, cwd=None):
        self.calls.append(("edit_file", path, old_text, new_text, cwd))
        return f"backend-edit:{path}"

    def glob(self, pattern, cwd=None):
        self.calls.append(("glob", pattern, cwd))
        return f"backend-glob:{pattern}"

    def grep(self, pattern, path=".", file_pattern=None, cwd=None):
        self.calls.append(("grep", pattern, path, file_pattern, cwd))
        return f"backend-grep:{pattern}"


def _started_runtime(monkeypatch, workspace, **harness_kwargs):
    """Build and start a CodeHarness Runtime with all I/O stubbed out."""
    from codeharness import app as app_module
    from codeharness.config import RuntimeConfig

    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)

    monkeypatch.setattr(app_module, "OpenAI", lambda **kw: object())
    monkeypatch.setattr(app_module, "load_config", lambda path: {})
    monkeypatch.setattr(app_module, "MCPManager", _FakeMCPManager)
    monkeypatch.setattr(app_module, "shutdown_runtime", lambda: None)
    monkeypatch.setattr(app_module.cron, "start", lambda **kw: None)
    monkeypatch.setattr(app_module.cron, "stop", lambda: None)
    monkeypatch.setattr(app_module.team_wakeup, "start", lambda **kw: None)
    monkeypatch.setattr(app_module.team_wakeup, "stop", lambda: None)

    config = RuntimeConfig(
        api_key="key",
        base_url="https://example.invalid",
        model="test-model",
        model_context_window=4096,
        workspace=workspace,
        mcp_config_path=workspace / "mcp.json",
        agent_home=workspace / "agent-home",
    )
    harness = app_module.CodeHarness(config, **harness_kwargs)
    harness.start()
    return harness


# ── 1-3: Agent per-instance default state ─────────────────────


def test_two_agents_have_distinct_default_todo_manager():
    from codeharness.core import agent as agent_module

    a = _bare_agent(agent_module)
    b = _bare_agent(agent_module)
    assert a.todo_manager is not b.todo_manager


def test_two_agents_have_distinct_default_background_manager():
    from codeharness.core import agent as agent_module

    a = _bare_agent(agent_module)
    b = _bare_agent(agent_module)
    assert a.background_manager is not b.background_manager


def test_agent_default_registry_todo_write_shares_todo_manager():
    from codeharness.core import agent as agent_module

    a = _bare_agent(agent_module)
    handler = a.handlers["todo_write"]
    handler([{"content": "step one", "status": "in_progress"}])

    assert a.todo_manager.items == [
        {"content": "step one", "status": "in_progress"}
    ]


def test_agent_default_registry_uses_agent_workspace(tmp_path):
    from codeharness.core import agent as agent_module

    workspace = tmp_path / "runtime-workspace"
    workspace.mkdir()
    a = _bare_agent(agent_module, workspace=str(workspace))

    a.handlers["write_file"](path="agent.txt", content="bound")

    assert (workspace / "agent.txt").read_text() == "bound"
    assert a.handlers["bash"](command="pwd") == str(workspace)


def test_agent_default_registry_uses_injected_workspace_backend():
    from codeharness.core import agent as agent_module

    backend = _RecordingWorkspaceBackend()
    agent = _bare_agent(
        agent_module,
        workspace="/testbed",
        workspace_backend=backend,
    )

    assert agent.handlers["read_file"](path="src/app.py") == (
        "backend-read:src/app.py"
    )
    assert backend.calls == [("read_file", "src/app.py", None, None, None)]


def test_no_process_global_todo_or_background_default_exists():
    import codeharness.tools.todo as todo_module
    import codeharness.background as background_pkg
    import codeharness.background.manager as background_module

    assert not hasattr(todo_module, "TODO")
    assert not hasattr(todo_module, "TODO_HANDLERS")
    assert not hasattr(background_module, "BACKGROUND")
    assert not hasattr(background_pkg, "BACKGROUND")


# ── 5,8: session stats ownership + accumulation ───────────────


def test_two_agents_have_distinct_default_session_stats():
    from codeharness.core import agent as agent_module

    a = _bare_agent(agent_module)
    b = _bare_agent(agent_module)
    assert a.session_stats is not b.session_stats
    assert a.local_stats is not a.session_stats
    assert b.local_stats is not b.session_stats
    assert a.local_stats is not b.local_stats
    assert a.session_stats["tool_calls"] == 0


def test_agent_uses_injected_session_stats():
    from codeharness.core import agent as agent_module
    from codeharness.hooks import new_session_stats

    shared = new_session_stats()
    a = _bare_agent(agent_module, session_stats=shared)
    assert a.session_stats is shared


def test_tool_call_accumulates_into_agent_session_stats(monkeypatch):
    from codeharness.core import agent as agent_module

    monkeypatch.setattr(agent_module, "trigger_hooks", lambda *args: None)
    a = _bare_agent(agent_module)

    a._execute_tool(lambda: "ok", "call-1", "bash", {}, [])

    assert a.session_stats["tool_calls"] == 1


def test_token_accumulation_writes_agent_session_stats():
    from codeharness.core import agent as agent_module

    a = _bare_agent(agent_module)
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12)
    )

    a._accumulate_tokens(response)

    assert a.session_stats["prompt_tokens"] == 5
    assert a.session_stats["completion_tokens"] == 7
    assert a.session_stats["total_tokens"] == 12


def test_token_accumulation_updates_distinct_local_and_aggregate_stats():
    from codeharness.core import agent as agent_module
    from codeharness.hooks import new_session_stats

    aggregate = new_session_stats()
    a = _bare_agent(agent_module, session_stats=aggregate)
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    )

    a._accumulate_tokens(response)

    assert a.local_stats is not a.session_stats
    assert a.local_stats == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "tool_calls": 0,
    }
    assert aggregate == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "tool_calls": 0,
    }


# ── 4,5,6: two Runtimes do not share state ────────────────────


def test_two_runtimes_do_not_share_todo_background_or_stats(monkeypatch, tmp_path):
    a = _started_runtime(monkeypatch, tmp_path / "a")
    b = _started_runtime(monkeypatch, tmp_path / "b")

    assert a.todo_manager is not b.todo_manager
    assert a.background_manager is not b.background_manager
    assert a.session_stats is not b.session_stats


def test_runtime_leader_and_factory_children_share_session_stats(monkeypatch, tmp_path):
    harness = _started_runtime(monkeypatch, tmp_path)

    child = harness.create_agent(system="child", memory_manager=False)

    assert harness.agent.session_stats is harness.session_stats
    assert child.session_stats is harness.session_stats


def test_runtime_leader_agent_shares_todo_manager_with_registry(monkeypatch, tmp_path):
    harness = _started_runtime(monkeypatch, tmp_path)

    handler = harness.agent.handlers["todo_write"]
    handler([{"content": "leader step", "status": "pending"}])

    assert harness.agent.todo_manager is harness.todo_manager
    assert harness.todo_manager.items == [
        {"content": "leader step", "status": "pending"}
    ]


def test_runtime_leader_coding_tools_use_configured_workspace(monkeypatch, tmp_path):
    process_cwd = tmp_path / "process-cwd"
    workspace = tmp_path / "runtime-workspace"
    process_cwd.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(process_cwd)
    harness = _started_runtime(monkeypatch, workspace)
    monkeypatch.chdir(process_cwd)

    assert harness.workspace_backend is None
    assert harness.tool_workspace == str(workspace)

    write_result = harness.agent.handlers["write_file"](
        path="leader.txt", content="leader workspace"
    )
    read_result = harness.agent.handlers["read_file"](path="leader.txt")
    bash_result = harness.agent.handlers["bash"](command="pwd")

    assert write_result == "Written 16 chars to leader.txt"
    assert "leader workspace" in read_result
    assert bash_result == str(workspace)
    assert (workspace / "leader.txt").read_text() == "leader workspace"
    assert not (process_cwd / "leader.txt").exists()


def test_runtime_keeps_state_on_host_when_tool_workspace_differs(
    monkeypatch, tmp_path
):
    from codeharness import hooks

    host_workspace = tmp_path / "host-state"
    backend = _RecordingWorkspaceBackend()
    harness = _started_runtime(
        monkeypatch,
        host_workspace,
        workspace_backend=backend,
        tool_workspace="/testbed",
    )

    assert harness.paths.workspace == host_workspace.resolve()
    assert harness.paths.tasks_dir == host_workspace / ".codeharness/state/tasks"
    assert harness.paths.workflow_runs_dir == (
        host_workspace / ".codeharness/runs/workflows"
    )
    assert harness.tool_workspace == "/testbed"
    assert hooks._perm_manager.allowed_dirs == [Path("/testbed")]
    assert hooks._perm_manager.base_dir == Path("/testbed")
    assert hooks._perm_manager.check(
        "read_file", {"path": "/testbed/src/app.py"}
    )[0] == "allow"
    assert hooks._perm_manager.check(
        "read_file", {"path": "/host-secret.txt"}
    )[0] == "ask"


def test_runtime_leader_uses_injected_workspace_backend(monkeypatch, tmp_path):
    from codeharness.tools import coding

    backend = _RecordingWorkspaceBackend()
    monkeypatch.setattr(
        coding,
        "run_read",
        lambda *args, **kwargs: pytest.fail("host read implementation called"),
    )
    harness = _started_runtime(
        monkeypatch,
        tmp_path,
        workspace_backend=backend,
        tool_workspace="/testbed",
    )

    assert harness.agent.handlers["read_file"](path="leader.py") == (
        "backend-read:leader.py"
    )
    assert harness.agent.handlers["bash"](command="pwd") == "backend-bash:pwd"
    assert "You are a coding agent at /testbed." in harness.agent.system
    assert backend.calls == [
        ("read_file", "leader.py", None, None, None),
        ("bash", "pwd", False, None),
    ]


def test_runtime_subagent_inherits_injected_workspace_backend(
    monkeypatch, tmp_path
):
    from codeharness.core import agent as agent_module
    from codeharness.tools import coding

    backend = _RecordingWorkspaceBackend()
    harness = _started_runtime(
        monkeypatch,
        tmp_path,
        workspace_backend=backend,
        tool_workspace="/testbed",
    )
    monkeypatch.setattr(
        coding,
        "run_read",
        lambda *args, **kwargs: pytest.fail("host read implementation called"),
    )
    monkeypatch.setattr(
        agent_module.Agent,
        "agent_loop",
        lambda self, messages: self.handlers["read_file"](path="child.py"),
    )

    result = harness.agent.handlers["task"]("inspect child.py")

    assert result == "backend-read:child.py"
    assert backend.calls == [("read_file", "child.py", None, None, None)]


def test_workflow_reuses_runtime_registry_workspace_backend(monkeypatch, tmp_path):
    from codeharness.workflow.definition import ToolStep
    from codeharness.workflow.runtime import WorkflowContext

    backend = _RecordingWorkspaceBackend()
    harness = _started_runtime(
        monkeypatch,
        tmp_path,
        workspace_backend=backend,
        tool_workspace="/testbed",
    )
    step = ToolStep(
        label="read through runtime registry",
        tool_name="read_file",
        args_template={"path": "workflow.py"},
        output_key="result",
    )

    result = harness.workflow_runtime._execute_tool_step(
        step,
        WorkflowContext({}),
        object(),
        "phase",
        "run-id",
    )

    assert harness.workflow_runtime._tool_registry is harness.registry
    assert result == {"output": "backend-read:workflow.py"}
    assert backend.calls == [("read_file", "workflow.py", None, None, None)]


# ── 9: Stop hook receives the Runtime session_stats ───────────


def test_stop_hook_receives_runtime_session_stats(monkeypatch, tmp_path):
    from codeharness import app as app_module

    harness = _started_runtime(monkeypatch, tmp_path)
    harness.session_stats["tool_calls"] = 7
    captured = []
    monkeypatch.setattr(
        app_module, "trigger_hooks", lambda event, *args: captured.append((event, args))
    )

    harness.close()

    assert ("Stop", (harness.session_stats,)) in captured


# ── 10,11: permission workspace + MCP callback lifecycle ──────


def test_permission_manager_rooted_at_runtime_workspace(monkeypatch, tmp_path):
    from codeharness import hooks

    workspace = Path(tmp_path).resolve()
    _started_runtime(monkeypatch, workspace)

    assert hooks._perm_manager.allowed_dirs == [workspace]


def test_close_clears_mcp_permission_resolver(monkeypatch, tmp_path):
    from codeharness import hooks

    harness = _started_runtime(monkeypatch, tmp_path)
    assert hooks._perm_manager._mcp_resolve is not None

    harness.close()

    assert hooks._perm_manager._mcp_resolve is None
    assert hooks._perm_manager._mcp_annotations_of is None


# ── 12,13,14: close clears factories and callbacks ────────────


def test_close_clears_subagent_and_team_factory(monkeypatch, tmp_path):
    from codeharness import subagent
    from codeharness.team import TEAM

    harness = _started_runtime(monkeypatch, tmp_path)
    assert subagent._agent_factory is not None
    assert TEAM._agent_factory is not None

    harness.close()

    assert subagent._agent_factory is None
    assert TEAM._agent_factory is None


def test_close_clears_runtime_and_agent_callbacks(monkeypatch, tmp_path):
    harness = _started_runtime(monkeypatch, tmp_path)
    harness.set_approval_handler(lambda reason: True)
    harness.set_status_handler(lambda message: None)
    harness.set_async_result_handler(lambda result: None)

    harness.close()

    assert harness.approval_handler is None
    assert harness.status_handler is None
    assert harness.async_result_handler is None
    assert harness.agent.approval_handler is None
    assert harness.agent.status_handler is None


# ── 15,16: cron / wakeup stop lifecycle ───────────────────────


def test_cron_stop_releases_threads_callbacks_and_store(monkeypatch, tmp_path):
    from codeharness.scheduler import cron

    monkeypatch.chdir(tmp_path)
    cron.start(
        delivery_handler=lambda content: True,
        store_path=tmp_path / "jobs.json",
        status_handler=lambda m: None,
    )
    assert cron._scheduler_thread is not None
    assert cron._processor_thread is not None
    assert cron.get_store() is not None

    cron.stop()

    assert cron.RUNTIME_STOP.is_set()
    assert cron._scheduler_thread is None
    assert cron._processor_thread is None
    assert cron._delivery_handler is None
    assert cron._status_handler is None
    assert cron.get_store() is None
    assert cron.run_cron_list() == "(cron scheduler not initialized)"


def test_team_wakeup_stop_releases_thread_and_status(monkeypatch, tmp_path):
    from codeharness.team import wakeup

    monkeypatch.chdir(tmp_path)
    wakeup.start(delivery_handler=lambda content: True, status_handler=lambda m: None)
    thread = wakeup._thread
    assert thread is not None

    wakeup.stop()

    assert wakeup._stop_event.is_set()
    assert wakeup._thread is None
    assert wakeup._status_handler is None
    assert thread.is_alive() is False


# ── 17: close is safe after a partial start failure ───────────


def test_close_is_safe_after_partial_start_failure(monkeypatch, tmp_path):
    from codeharness import app as app_module
    from codeharness import subagent
    from codeharness.config import RuntimeConfig
    from codeharness.team import TEAM

    workspace = Path(tmp_path)
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)

    monkeypatch.setattr(app_module, "OpenAI", lambda **kw: object())
    monkeypatch.setattr(app_module, "load_config", lambda path: {})
    monkeypatch.setattr(app_module, "MCPManager", _FakeMCPManager)
    monkeypatch.setattr(app_module, "shutdown_runtime", lambda: None)
    monkeypatch.setattr(app_module.cron, "start", lambda **kw: None)
    cron_stopped = []
    monkeypatch.setattr(app_module.cron, "stop", lambda: cron_stopped.append(True))

    def _boom(**kwargs):
        raise RuntimeError("wakeup failed to start")

    monkeypatch.setattr(app_module.team_wakeup, "start", _boom)
    wakeup_stopped = []
    monkeypatch.setattr(
        app_module.team_wakeup,
        "stop",
        lambda: wakeup_stopped.append(True) or True,
    )

    config = RuntimeConfig(
        api_key="key",
        base_url="https://example.invalid",
        model="test-model",
        model_context_window=4096,
        workspace=workspace,
        mcp_config_path=workspace / "mcp.json",
        agent_home=workspace / "agent-home",
    )
    harness = app_module.CodeHarness(config)

    with pytest.raises(RuntimeError):
        harness.start()

    # start() got as far as configuring factories + cron before failing.
    assert subagent._agent_factory is not None
    assert TEAM._agent_factory is not None

    harness.close()  # must not raise

    assert harness._closed is True
    assert cron_stopped == [True]
    assert wakeup_stopped == [True]
    assert subagent._agent_factory is None
    assert TEAM._agent_factory is None


# ── 18: sequential Runtimes do not inherit session state ──────


def test_sequential_runtimes_do_not_inherit_state(monkeypatch, tmp_path):
    a = _started_runtime(monkeypatch, tmp_path)
    a.session_stats["tool_calls"] = 42
    a.todo_manager.update([{"content": "old", "status": "pending"}])
    stats_a = a.session_stats
    todo_a = a.todo_manager
    bg_a = a.background_manager
    a.close()

    b = _started_runtime(monkeypatch, tmp_path)

    assert b.session_stats is not stats_a
    assert b.session_stats["tool_calls"] == 0
    assert b.todo_manager is not todo_a
    assert b.todo_manager.items == []
    assert b.background_manager is not bg_a
    b.close()
