import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest


def _agent(agent_module, **kwargs):
    defaults = {
        "client": object(),
        "model": "test-model",
        "model_context_window": 4096,
        "tools": [],
        "handlers": {},
        "memory_manager": False,
    }
    defaults.update(kwargs)
    return agent_module.Agent(**defaults)


def _ask_on_pre_tool_use(monkeypatch, agent_module, reason="approval needed"):
    def trigger_hooks(event, *args):
        if event == "PreToolUse":
            agent_module.hooks.PENDING_USER_ASK.value = reason
        return None

    monkeypatch.setattr(agent_module, "trigger_hooks", trigger_hooks)


def test_agent_core_has_no_terminal_input_or_print_calls():
    from codeharness.core import agent as agent_module

    source = inspect.getsource(agent_module.Agent)

    assert "input(" not in source
    assert "print(" not in source


def test_approval_handler_true_executes_ask_tool(monkeypatch):
    from codeharness.core import agent as agent_module

    _ask_on_pre_tool_use(monkeypatch, agent_module)
    approvals = []
    calls = []
    agent = _agent(
        agent_module,
        approval_handler=lambda reason: approvals.append(reason) or True,
    )
    messages = []

    executed = agent._execute_tool(
        lambda value: calls.append(value) or "done",
        "call-1",
        "write_file",
        {"value": "payload"},
        messages,
    )

    assert executed is True
    assert approvals == ["approval needed"]
    assert calls == ["payload"]
    assert messages[-1]["content"] == "done"


def test_approval_handler_false_rejects_ask_tool(monkeypatch):
    from codeharness.core import agent as agent_module

    _ask_on_pre_tool_use(monkeypatch, agent_module)
    calls = []
    agent = _agent(agent_module, approval_handler=lambda _reason: False)
    messages = []

    executed = agent._execute_tool(
        lambda: calls.append("called"), "call-2", "bash", {}, messages
    )

    assert executed is False
    assert calls == []
    assert "User rejected" in messages[-1]["content"]


def test_non_interactive_agent_never_calls_approval_handler(monkeypatch):
    from codeharness.core import agent as agent_module

    _ask_on_pre_tool_use(monkeypatch, agent_module)
    approvals = []
    agent = _agent(
        agent_module,
        interactive=False,
        approval_handler=lambda reason: approvals.append(reason) or True,
    )
    messages = []

    executed = agent._execute_tool(lambda: "unsafe", "call-3", "bash", {}, messages)

    assert executed is False
    assert approvals == []
    assert "not available for teammates" in messages[-1]["content"]


def test_interactive_agent_without_handler_fails_closed(monkeypatch):
    from codeharness.core import agent as agent_module

    _ask_on_pre_tool_use(monkeypatch, agent_module)
    calls = []
    agent = _agent(agent_module, interactive=True, approval_handler=None)
    messages = []

    executed = agent._execute_tool(
        lambda: calls.append("called"), "call-4", "bash", {}, messages
    )

    assert executed is False
    assert calls == []
    assert "approval handler is configured" in messages[-1]["content"]


def test_status_handler_receives_agent_status(monkeypatch):
    from codeharness.core import agent as agent_module

    monkeypatch.setattr(
        agent_module,
        "trigger_hooks",
        lambda event, *args: "blocked by test" if event == "PreToolUse" else None,
    )
    statuses = []
    agent = _agent(agent_module, status_handler=statuses.append)

    executed = agent._execute_tool(lambda: "unused", "call-5", "bash", {}, [])

    assert executed is False
    assert any("BLOCKED bash: blocked by test" in status for status in statuses)


def test_handler_error_is_returned_as_observation_without_rejection_status(
    monkeypatch,
):
    from codeharness.core import agent as agent_module

    monkeypatch.setattr(agent_module, "trigger_hooks", lambda *args: None)
    statuses = []
    agent = _agent(agent_module, status_handler=statuses.append)
    messages = []

    attempted = agent._execute_tool(
        lambda path: f"read {path}",
        "call-error",
        "read_file",
        {"offset": 480},
        messages,
    )

    assert attempted is True
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "call-error"
    assert messages[-1]["content"].startswith(
        "Error: Tool 'read_file' failed: TypeError:"
    )
    assert "unexpected keyword argument 'offset'" in messages[-1]["content"]
    assert statuses == []
    assert agent.session_stats["tool_calls"] == 1


def test_non_policy_nonexecution_is_not_treated_as_rejection():
    from codeharness.core import agent as agent_module

    agent = _agent(agent_module)
    agent._tool_executor = SimpleNamespace(
        execute=lambda **_kwargs: SimpleNamespace(
            executed=False,
            output="cancelled",
            status="cancelled",
            reason=None,
        )
    )
    messages = []

    attempted = agent._execute_tool(
        lambda: "unused", "call-cancelled", "read_file", {}, messages
    )

    assert attempted is True
    assert messages[-1]["content"] == "cancelled"


def test_agent_loop_continues_after_repeated_handler_errors(monkeypatch):
    from codeharness.core import agent as agent_module

    monkeypatch.setattr(agent_module, "trigger_hooks", lambda *args: None)

    class FakeMessage:
        def __init__(self, content=None, tool_call=None):
            self.content = content
            self.tool_calls = [tool_call] if tool_call is not None else None

        def model_dump(self):
            dumped = {"role": "assistant", "content": self.content}
            if self.tool_calls:
                dumped["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in self.tool_calls
                ]
            return dumped

    def tool_call(call_id, arguments):
        return SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(
                name="read_file",
                arguments=arguments,
            ),
        )

    responses = iter(
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=FakeMessage(
                            tool_call=tool_call(f"bad-{index}", '{"offset": 480}')
                        )
                    )
                ]
            )
            for index in range(3)
        ]
        + [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=FakeMessage(
                            tool_call=tool_call("corrected", '{"path": "notes.txt"}')
                        )
                    )
                ]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(message=FakeMessage(content="done"))]
            ),
        ]
    )
    calls = []
    agent = _agent(
        agent_module,
        handlers={"read_file": lambda path: calls.append(path) or "contents"},
        max_rounds=6,
    )
    agent.context_manager.prepare = lambda current_messages: current_messages
    agent._call_llm = lambda _messages: next(responses)
    messages = [{"role": "user", "content": "read the file"}]

    result = agent.agent_loop(messages)

    assert result == "done"
    assert calls == ["notes.txt"]
    assert sum(
        message.get("role") == "tool"
        and message["content"].startswith("Error: Tool 'read_file' failed:")
        for message in messages
    ) == 3
    assert agent.session_stats["tool_calls"] == 4


def test_agent_background_bash_always_executes_through_handler(monkeypatch):
    from codeharness.core import agent as agent_module

    monkeypatch.setattr(agent_module, "trigger_hooks", lambda *args: None)
    background_calls = []
    handler_calls = []
    background = SimpleNamespace(
        start=lambda *args, **kwargs: background_calls.append((args, kwargs))
    )
    agent = _agent(agent_module, background_manager=background)
    messages = []

    executed = agent._execute_tool(
        lambda **kwargs: handler_calls.append(kwargs) or "started by handler",
        "call-background",
        "bash",
        {"command": "pwd", "run_in_background": True},
        messages,
    )

    assert executed is True
    assert background_calls == []
    assert handler_calls == [{"command": "pwd", "run_in_background": True}]
    assert messages[-1]["content"] == "started by handler"


def test_subagent_uses_configured_agent_factory_and_workspace(monkeypatch, tmp_path):
    from codeharness import subagent

    created = []

    class FakeAgent:
        def agent_loop(self, messages):
            assert messages == [{"role": "user", "content": "inspect it"}]
            return "summary"

    def factory(**kwargs):
        created.append(kwargs)
        return FakeAgent()

    monkeypatch.setattr(subagent, "_agent_factory", None, raising=False)
    subagent.configure_agent_factory(factory, workspace=str(tmp_path))

    assert subagent.run_task("inspect it") == "summary"
    assert len(created) == 1
    assert str(tmp_path) in created[0]["system"]
    assert "tools" not in created[0]
    assert "handlers" not in created[0]
    assert created[0]["interactive"] is True


def test_subagent_without_factory_fails_loudly(monkeypatch):
    from codeharness import subagent

    monkeypatch.setattr(subagent, "_agent_factory", None, raising=False)

    with pytest.raises(RuntimeError, match="agent factory"):
        subagent.run_task("inspect it")


def test_team_manager_uses_configured_agent_factory(monkeypatch, tmp_path):
    from codeharness.team import manager as manager_module

    created = []
    task = SimpleNamespace(id="task_factory", subject="subject", description="desc")

    class FakeAgent:
        pass

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            pass

    manager = manager_module.TeamManager()
    manager.set_agent_factory(
        lambda **kwargs: created.append(kwargs) or FakeAgent()
    )
    monkeypatch.setattr(
        manager_module.TASKS,
        "claim",
        lambda *args, **kwargs: (task, str(tmp_path), None),
    )
    monkeypatch.setattr(manager_module.threading, "Thread", FakeThread)

    result = manager.spawn(task.id, name="Alice")

    assert result.startswith("Spawned teammate Alice")
    assert len(created) == 1
    assert created[0]["interactive"] is False
    assert created[0]["memory_manager"] is False
    assert manager.get_state("Alice").agent.__class__ is FakeAgent


def test_team_manager_without_factory_fails_before_claim(monkeypatch):
    from codeharness.team import manager as manager_module

    manager = manager_module.TeamManager()
    claim_calls = []
    monkeypatch.setattr(
        manager_module.TASKS,
        "claim",
        lambda *args, **kwargs: claim_calls.append(args),
    )

    result = manager.spawn("task_factory", name="Alice")

    assert result.startswith("Error: agent factory")
    assert claim_calls == []


def test_runtime_agent_factory_inherits_model_and_workspace(monkeypatch, tmp_path):
    from codeharness import app as app_module
    from codeharness.config import RuntimeConfig

    created = []

    class FakeAgent:
        def __init__(self, **kwargs):
            created.append(kwargs)

    config = RuntimeConfig(
        api_key="key",
        base_url="https://example.invalid",
        model="runtime-model",
        model_context_window=123456,
        workspace=Path(tmp_path),
        mcp_config_path=Path(tmp_path) / "mcp.json",
        agent_home=Path(tmp_path) / "agent-home",
    )
    harness = app_module.CodeHarness(config)
    harness.client = object()
    monkeypatch.setattr(app_module, "Agent", FakeAgent)

    child = harness.create_agent(system="child")

    assert isinstance(child, FakeAgent)
    assert created == [{
        "system": "child",
        "client": harness.client,
        "model": "runtime-model",
        "model_context_window": 123456,
        "workspace": str(tmp_path),
        "memory_dir": harness.paths.project_memory_dir,
        "approval_handler": None,
        "status_handler": None,
        "session_stats": harness.session_stats,
        "session_stats_lock": harness._session_stats_lock,
        "workflow_catalog": harness.workflow_registry.catalog(),
    }]


def test_runtime_agent_factory_respects_explicit_overrides(monkeypatch, tmp_path):
    from codeharness import app as app_module
    from codeharness.config import RuntimeConfig

    created = []

    class FakeAgent:
        def __init__(self, **kwargs):
            created.append(kwargs)

    config = RuntimeConfig(
        api_key="key",
        base_url="https://example.invalid",
        model="runtime-model",
        model_context_window=123456,
        workspace=Path(tmp_path),
        mcp_config_path=Path(tmp_path) / "mcp.json",
        agent_home=Path(tmp_path) / "agent-home",
    )
    harness = app_module.CodeHarness(config)
    harness.client = object()
    monkeypatch.setattr(app_module, "Agent", FakeAgent)
    override_client = object()

    harness.create_agent(client=override_client, model="override")

    assert created[0]["client"] is override_client
    assert created[0]["model"] == "override"


def test_cli_registers_handlers_and_approval_accepts_only_y():
    from interfaces.cli import CLI

    class FakeHarness:
        def set_approval_handler(self, handler):
            self.approval_handler = handler

        def set_status_handler(self, handler):
            self.status_handler = handler

        def set_async_result_handler(self, handler):
            self.async_result_handler = handler

    answers = iter(["Y", "yes", "n"])
    output = []
    harness = FakeHarness()
    cli = CLI(
        harness,
        input_fn=lambda _prompt: next(answers),
        output_fn=output.append,
    )

    assert harness.approval_handler("first reason") is True
    assert harness.approval_handler("second reason") is False
    assert harness.approval_handler("third reason") is False
    harness.status_handler("working")
    assert output == ["first reason", "second reason", "third reason", "working"]


def test_main_constructs_cli_once_before_start(monkeypatch):
    import main as main_module

    events = []

    class FakeHarness:
        @classmethod
        def from_env(cls):
            events.append("from_env")
            return cls()

        def start(self):
            events.append("start")

        def close(self):
            events.append("close")

    class FakeCLI:
        def __init__(self, harness):
            events.append("cli_init")
            self.harness = harness

        def run(self):
            events.append("run")

    monkeypatch.setattr(main_module, "CodeHarness", FakeHarness)
    monkeypatch.setattr(main_module, "CLI", FakeCLI)

    main_module.main()

    assert events == ["from_env", "cli_init", "start", "run", "close"]


def test_runtime_uses_shared_team_manager_for_factory_configuration():
    from codeharness import app as app_module
    from codeharness.team import TEAM

    assert app_module.TEAM is TEAM
