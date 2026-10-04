import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from codeharness import hooks
from codeharness.security.permission import MCP_HOST_POLICY, PermissionManager
from codeharness.tools import ToolRegistry, build_base_registry
from codeharness.tools.executor import ToolExecutor
from codeharness.workflow.definition import AgentStep, ToolStep, WorkflowConfig
from codeharness.workflow.registry import WorkflowRegistry as WorkflowDefinitionRegistry
from codeharness.workflow.runtime import (
    RunHandle,
    WorkflowContext,
    WorkflowError,
    WorkflowRuntime,
)


class _RecordingEventBus:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class _RecordingStateStore:
    def save_snapshot(self, _snapshot):
        pass

    def update_index(self, _snapshot):
        pass

    def append_journal(self, _run_id, _entry):
        pass


def _workflow_runtime(handler):
    runtime = WorkflowRuntime(
        registry=WorkflowDefinitionRegistry(),
        state_store=_RecordingStateStore(),
        event_bus=_RecordingEventBus(),
    )
    runtime.set_tool_resolver(lambda _name: handler)
    return runtime


def _tool_step(tool_name, args):
    return ToolStep(
        label=f"run {tool_name}",
        tool_name=tool_name,
        args_template=args,
        output_key="result",
    )


@pytest.fixture(autouse=True)
def _reset_pending_approval():
    hooks.PENDING_USER_ASK.value = None
    yield
    hooks.PENDING_USER_ASK.value = None


def test_executor_runs_allowed_handler_and_post_hook_once():
    events = []
    calls = []
    stats = {"tool_calls": 0}

    def trigger(event, *args):
        events.append((event, args))

    result = ToolExecutor(trigger).execute(
        tool_name="read_file",
        args={"path": "notes.txt"},
        handler=lambda path: calls.append(path) or "contents",
        session_stats=stats,
    )

    assert result.executed is True
    assert result.status == "executed"
    assert result.output == "contents"
    assert calls == ["notes.txt"]
    assert stats["tool_calls"] == 1
    assert [event for event, _args in events] == ["PreToolUse", "PostToolUse"]


@pytest.mark.parametrize(
    ("args", "handler", "exception_name", "message"),
    [
        (
            {"path": "notes.txt", "offset": 480},
            lambda path, start_line=None, end_line=None, cwd=None: "contents",
            "TypeError",
            "unexpected keyword argument 'offset'",
        ),
        (
            {},
            lambda path: "contents",
            "TypeError",
            "missing 1 required positional argument: 'path'",
        ),
        (
            {},
            lambda: (_ for _ in ()).throw(RuntimeError("handler failed")),
            "RuntimeError",
            "handler failed",
        ),
    ],
    ids=["unknown-keyword", "missing-required-argument", "handler-exception"],
)
def test_executor_returns_error_for_handler_failure(
    args, handler, exception_name, message
):
    events = []
    stats = {"tool_calls": 0}

    def trigger(event, *event_args):
        events.append((event, event_args))

    result = ToolExecutor(trigger).execute(
        tool_name="read_file",
        args=args,
        handler=handler,
        session_stats=stats,
    )

    assert result.executed is False
    assert result.status == "error"
    assert result.output.startswith(
        f"Error: Tool 'read_file' failed: {exception_name}:"
    )
    assert message in result.output
    assert stats["tool_calls"] == 1
    assert [event for event, _args in events] == ["PreToolUse"]


def test_executor_does_not_run_handler_or_post_hook_when_pre_hook_denies():
    events = []
    calls = []
    stats = {"tool_calls": 0}

    def trigger(event, *args):
        events.append((event, args))
        if event == "PreToolUse":
            return "Error: Permission denied - blocked by test"
        return None

    result = ToolExecutor(trigger).execute(
        tool_name="bash",
        args={"command": "rm -rf /"},
        handler=lambda **_kwargs: calls.append("executed") or "unsafe",
        session_stats=stats,
    )

    assert result.executed is False
    assert result.status == "blocked"
    assert "Permission denied" in result.output
    assert calls == []
    assert stats["tool_calls"] == 0
    assert [event for event, _args in events] == ["PreToolUse"]


def test_executor_non_interactive_ask_fails_closed_without_approval_callback():
    approvals = []
    calls = []
    stats = {"tool_calls": 0}

    def trigger(event, *_args):
        if event == "PreToolUse":
            hooks.PENDING_USER_ASK.value = "approval needed"

    result = ToolExecutor(trigger).execute(
        tool_name="bash",
        args={"command": "rm build.log"},
        handler=lambda **_kwargs: calls.append("executed") or "unsafe",
        interactive=False,
        approval_handler=lambda reason: approvals.append(reason) or True,
        approval_context="workflow",
        session_stats=stats,
    )

    assert result.executed is False
    assert result.status == "approval_unavailable"
    assert "workflow" in result.output.lower()
    assert approvals == []
    assert calls == []
    assert stats["tool_calls"] == 0


def test_executor_interactive_ask_runs_only_after_approval():
    approvals = []
    calls = []
    stats = {"tool_calls": 0}

    def trigger(event, *_args):
        if event == "PreToolUse":
            hooks.PENDING_USER_ASK.value = "approval needed"

    executor = ToolExecutor(trigger)
    rejected = executor.execute(
        tool_name="write_file",
        args={"path": "notes.txt", "content": "x"},
        handler=lambda **_kwargs: calls.append("rejected") or "unsafe",
        approval_handler=lambda reason: approvals.append(reason) or False,
        session_stats=stats,
    )
    approved = executor.execute(
        tool_name="write_file",
        args={"path": "notes.txt", "content": "x"},
        handler=lambda **_kwargs: calls.append("approved") or "written",
        approval_handler=lambda reason: approvals.append(reason) or True,
        session_stats=stats,
    )

    assert rejected.executed is False
    assert rejected.status == "rejected"
    assert approved.executed is True
    assert approved.output == "written"
    assert approvals == ["approval needed", "approval needed"]
    assert calls == ["approved"]
    assert stats["tool_calls"] == 1


def test_workflow_tool_step_cannot_bypass_bash_deny(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hooks,
        "_perm_manager",
        PermissionManager(allowed_dirs=[str(tmp_path)], base_dir=tmp_path),
    )
    calls = []
    runtime = _workflow_runtime(
        lambda **kwargs: calls.append(kwargs) or "dangerous command ran"
    )

    with pytest.raises(WorkflowError, match="Permission denied"):
        runtime._execute_tool_step(
            _tool_step("bash", {"command": "rm -rf /"}),
            WorkflowContext({}),
            object(),
            "phase",
            "run-id",
        )

    assert calls == []


def test_workflow_tool_step_ask_fails_closed_without_execution(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        hooks,
        "_perm_manager",
        PermissionManager(allowed_dirs=[str(tmp_path)], base_dir=tmp_path),
    )
    calls = []
    runtime = _workflow_runtime(
        lambda **kwargs: calls.append(kwargs) or "approval bypassed"
    )

    with pytest.raises(WorkflowError, match="approval.*not available"):
        runtime._execute_tool_step(
            _tool_step("bash", {"command": "rm build.log"}),
            WorkflowContext({}),
            object(),
            "phase",
            "run-id",
        )

    assert calls == []


def test_workflow_safe_read_uses_workspace_bound_native_handler(
    monkeypatch, tmp_path
):
    workspace = Path(tmp_path)
    (workspace / "notes.txt").write_text("workspace contents", encoding="utf-8")
    monkeypatch.setattr(
        hooks,
        "_perm_manager",
        PermissionManager(allowed_dirs=[str(workspace)], base_dir=workspace),
    )
    registry = build_base_registry(workspace=str(workspace))
    runtime = WorkflowRuntime(
        registry=WorkflowDefinitionRegistry(),
        state_store=_RecordingStateStore(),
        event_bus=_RecordingEventBus(),
    )
    runtime.set_tool_resolver(registry.get)

    result = runtime._execute_tool_step(
        _tool_step("read_file", {"path": "notes.txt"}),
        WorkflowContext({}),
        object(),
        "phase",
        "run-id",
    )

    assert result == {"output": "    1\tworkspace contents"}


def test_workflow_handler_error_is_reported_as_failure_not_blocked():
    def handler():
        raise ValueError("boom")

    runtime = _workflow_runtime(handler)

    with pytest.raises(WorkflowError) as exc_info:
        runtime._execute_tool_step(
            _tool_step("explode", {}),
            WorkflowContext({}),
            object(),
            "phase",
            "run-id",
        )

    message = str(exc_info.value)
    assert "ToolStep 'run explode' failed" in message
    assert "Error: Tool 'explode' failed: ValueError: boom" in message
    assert "blocked" not in message


def test_workflow_allowed_handler_error_keeps_failure_payload():
    def handler():
        raise ValueError("boom")

    runtime = _workflow_runtime(handler)
    step = ToolStep(
        label="run explode",
        tool_name="explode",
        args_template={},
        output_key="result",
        allow_failure=True,
    )

    result = runtime._execute_tool_step(
        step,
        WorkflowContext({}),
        object(),
        "phase",
        "run-id",
    )

    assert result == {
        "output": "Error: Tool 'explode' failed: ValueError: boom",
        "error": "boom",
        "exit_code": -1,
    }


def test_workflow_allowed_empty_handler_error_preserves_empty_reason():
    def handler():
        raise ValueError()

    runtime = _workflow_runtime(handler)
    step = ToolStep(
        label="run explode",
        tool_name="explode",
        args_template={},
        output_key="result",
        allow_failure=True,
    )

    result = runtime._execute_tool_step(
        step,
        WorkflowContext({}),
        object(),
        "phase",
        "run-id",
    )

    assert result == {
        "output": "Error: Tool 'explode' failed: ValueError: ",
        "error": "",
        "exit_code": -1,
    }


@pytest.mark.parametrize(
    ("decision", "should_execute"),
    [("allow", True), ("ask", False), ("deny", False)],
)
def test_workflow_mcp_tool_step_obeys_harness_policy(
    monkeypatch, tmp_path, decision, should_execute
):
    tool_name = f"mcp__fake__{decision}"
    pair = ("fake-server", f"{decision}-tool")
    monkeypatch.setitem(MCP_HOST_POLICY, pair, decision)
    monkeypatch.setattr(
        hooks,
        "_perm_manager",
        PermissionManager(
            allowed_dirs=[str(tmp_path)],
            base_dir=tmp_path,
            mcp_resolve=lambda name: pair if name == tool_name else None,
        ),
    )
    calls = []
    runtime = _workflow_runtime(
        lambda **kwargs: calls.append(kwargs) or "mcp result"
    )

    if should_execute:
        result = runtime._execute_tool_step(
            _tool_step(tool_name, {"value": "payload"}),
            WorkflowContext({}),
            object(),
            "phase",
            "run-id",
        )
        assert result == {"output": "mcp result"}
    else:
        with pytest.raises(WorkflowError, match="MCP tool"):
            runtime._execute_tool_step(
                _tool_step(tool_name, {"value": "payload"}),
                WorkflowContext({}),
                object(),
                "phase",
                "run-id",
            )

    assert calls == ([{"value": "payload"}] if should_execute else [])


def test_workflow_agent_whitelist_uses_current_runtime_registry():
    native_handler = lambda **_kwargs: "native"
    mcp_handler = lambda **_kwargs: "mcp"
    registry = ToolRegistry()
    registry.register(
        {
            "type": "function",
            "function": {
                "name": "runtime_native",
                "description": "runtime-bound native handler",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        native_handler,
    )
    registry.register(
        {
            "type": "function",
            "function": {
                "name": "mcp__runtime__read",
                "description": "runtime MCP handler",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        mcp_handler,
    )
    created = []

    class FakeAgent:
        def __init__(self):
            self.local_stats = {"total_tokens": 0}
            self.session_stats = {"total_tokens": 0}

        def agent_loop(self, _messages):
            return "done"

    runtime = WorkflowRuntime(
        registry=WorkflowDefinitionRegistry(),
        state_store=_RecordingStateStore(),
        event_bus=_RecordingEventBus(),
    )
    runtime.set_agent_factory(
        lambda **kwargs: created.append(kwargs) or FakeAgent()
    )
    runtime.set_tool_registry(registry)
    snapshot = SimpleNamespace(
        agent_calls_used=0,
        tokens_used=0,
        completed_steps=[],
    )
    handle = RunHandle("run-id", threading.current_thread(), snapshot)
    with runtime._lock:
        runtime._runs[handle.run_id] = handle

    runtime._execute_agent_step(
        AgentStep(
            label="runtime tools",
            prompt_template="use runtime tools",
            output_key="result",
            tools=["runtime_native", "mcp__runtime__read"],
        ),
        WorkflowContext({}),
        snapshot,
        {},
        WorkflowConfig(),
        "phase",
        "run-id",
        time.time() + 60,
    )

    assert {
        schema["function"]["name"] for schema in created[0]["tools"]
    } == {"runtime_native", "mcp__runtime__read"}
    assert created[0]["handlers"] == {
        "runtime_native": native_handler,
        "mcp__runtime__read": mcp_handler,
    }
