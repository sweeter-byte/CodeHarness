"""Workflow mutable runtime state belongs to one CodeHarness instance."""

from pathlib import Path

from codeharness.config import RuntimeConfig
from codeharness.workflow.definition import Phase, ToolStep, WorkflowDefinition
from codeharness.workflow.events import ProgressEvent
from codeharness.workflow.state import RunSnapshot


class _FakeMCPManager:
    def __init__(self, configs):
        self.configs = configs

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
        pass


def _config(workspace: Path) -> RuntimeConfig:
    workspace.mkdir(parents=True, exist_ok=True)
    return RuntimeConfig(
        api_key="key",
        base_url="https://example.invalid",
        model="test-model",
        model_context_window=4096,
        workspace=workspace,
        mcp_config_path=workspace / "mcp.json",
        agent_home=workspace / "agent-home",
    )


def _definition(name: str) -> WorkflowDefinition:
    return WorkflowDefinition(
        name=name,
        description=f"{name} description",
        phases=[
            Phase(
                name="only",
                steps=[
                    ToolStep(
                        label="noop",
                        tool_name="read_file",
                        args_template={"path": "README.md"},
                        output_key="result",
                    )
                ],
            )
        ],
    )


def _stub_runtime_io(monkeypatch):
    from codeharness import app as app_module

    monkeypatch.setattr(app_module, "OpenAI", lambda **kw: object())
    monkeypatch.setattr(app_module, "load_config", lambda path: {})
    monkeypatch.setattr(app_module, "MCPManager", _FakeMCPManager)
    monkeypatch.setattr(app_module, "shutdown_runtime", lambda: None)
    monkeypatch.setattr(app_module.cron, "start", lambda **kw: None)
    monkeypatch.setattr(app_module.cron, "stop", lambda: True)
    monkeypatch.setattr(app_module.team_wakeup, "start", lambda **kw: None)
    monkeypatch.setattr(app_module.team_wakeup, "stop", lambda: True)
    monkeypatch.setattr(app_module.TEAM, "shutdown_all", lambda timeout: True)
    return app_module


def test_two_runtimes_start_with_isolated_workflow_components(
    monkeypatch, tmp_path
):
    app_module = _stub_runtime_io(monkeypatch)
    a = app_module.CodeHarness(_config(tmp_path / "a"))
    b = app_module.CodeHarness(_config(tmp_path / "b"))

    a.start()
    b.start()

    assert a.workflow_registry is not b.workflow_registry
    assert a.workflow_runtime is not b.workflow_runtime
    assert a.workflow_state_store is not b.workflow_state_store
    assert a.workflow_event_bus is not b.workflow_event_bus
    assert "review-changes" in a.workflow_registry
    assert "review-changes" in b.workflow_registry

    assert a.close() is True
    assert b.close() is True


def test_registry_contents_are_runtime_local(tmp_path):
    from codeharness.app import CodeHarness

    a = CodeHarness(_config(tmp_path / "a"))
    b = CodeHarness(_config(tmp_path / "b"))
    a.workflow_registry.register(_definition("only-a"))

    assert a.workflow_registry.get("only-a") is not None
    assert b.workflow_registry.get("only-a") is None


def test_workflow_handlers_call_only_the_bound_runtime():
    from codeharness.workflow.tools import make_workflow_handlers

    class SpyRuntime:
        def __init__(self, run_id):
            self.run_id = run_id
            self.calls = []

        def start(self, name, inputs):
            self.calls.append((name, inputs))
            return self.run_id, None

        def resume(self, run_id):
            return run_id, None

        def status(self, run_id):
            return {"runs": []}

    a = SpyRuntime("run_a")
    b = SpyRuntime("run_b")
    handlers_a = make_workflow_handlers(a)
    handlers_b = make_workflow_handlers(b)

    assert "run_id=run_a" in handlers_a["start_workflow"]("wf", '{"a": 1}')
    assert "run_id=run_b" in handlers_b["start_workflow"]("wf", '{"b": 2}')
    assert a.calls == [("wf", {"a": 1})]
    assert b.calls == [("wf", {"b": 2})]


def test_state_store_is_workspace_rooted_and_data_isolated(tmp_path):
    from codeharness.app import CodeHarness

    a = CodeHarness(_config(tmp_path / "a"))
    b = CodeHarness(_config(tmp_path / "b"))

    assert a.workflow_state_store.base_dir == tmp_path / "a/.codeharness/runs/workflows"
    assert b.workflow_state_store.base_dir == tmp_path / "b/.codeharness/runs/workflows"

    snapshot = RunSnapshot(run_id="run_a", workflow_name="only-a", inputs={})
    a.workflow_state_store.save_snapshot(snapshot)

    assert a.workflow_state_store.load_snapshot("run_a") is not None
    assert b.workflow_state_store.load_snapshot("run_a") is None


def test_event_history_is_runtime_local(tmp_path):
    from codeharness.app import CodeHarness

    a = CodeHarness(_config(tmp_path / "a"))
    b = CodeHarness(_config(tmp_path / "b"))
    event = ProgressEvent(event_type="test", run_id="run_a")

    a.workflow_event_bus.emit(event)

    assert a.workflow_event_bus.history("run_a") == [event]
    assert b.workflow_event_bus.history("run_a") == []


def test_closing_one_runtime_does_not_clear_another_workflow_runtime(
    monkeypatch, tmp_path
):
    app_module = _stub_runtime_io(monkeypatch)
    a = app_module.CodeHarness(_config(tmp_path / "a")).start()
    b = app_module.CodeHarness(_config(tmp_path / "b")).start()
    b.workflow_registry.register(_definition("still-b"))
    factory_b = b.workflow_runtime._agent_factory
    tools_b = b.workflow_runtime._tool_registry
    delivery_b = b.workflow_runtime._delivery_handler

    assert a.close() is True

    assert b.workflow_registry.get("still-b") is not None
    assert b.workflow_runtime._agent_factory is factory_b
    assert b.workflow_runtime._tool_registry is tools_b
    assert b.workflow_runtime._delivery_handler is delivery_b
    assert b.close() is True


def test_agent_prompt_uses_current_runtime_workflow_catalog(tmp_path):
    from codeharness.app import CodeHarness

    a = CodeHarness(_config(tmp_path / "a"))
    b = CodeHarness(_config(tmp_path / "b"))
    a.workflow_registry.register(_definition("only-a"))
    b.workflow_registry.register(_definition("only-b"))
    a.client = object()
    b.client = object()

    agent_a = a.create_agent(tools=[], handlers={}, memory_manager=False)
    agent_b = b.create_agent(tools=[], handlers={}, memory_manager=False)

    assert "only-a" in agent_a.system
    assert "only-b" not in agent_a.system
    assert "only-b" in agent_b.system
    assert "only-a" not in agent_b.system

