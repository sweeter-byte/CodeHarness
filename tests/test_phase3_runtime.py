import importlib
from pathlib import Path


def test_runtime_config_reads_environment(monkeypatch, tmp_path):
    config_module = importlib.import_module("codeharness.config")
    dotenv_calls = []
    monkeypatch.setattr(
        config_module,
        "load_dotenv",
        lambda **kwargs: dotenv_calls.append(kwargs),
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("DEEPSEEK_MODEL_ID", "test-model")
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW", "123456")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))

    config = config_module.RuntimeConfig.from_env()

    assert dotenv_calls == [{"override": True}]
    assert config.api_key == "test-key"
    assert config.base_url == "https://example.invalid"
    assert config.model == "test-model"
    assert config.model_context_window == 123456
    assert config.workspace == tmp_path
    assert config.mcp_config_path.name == "mcp_servers.json"


def test_default_prompt_preserves_workspace_skills_team_and_todo_rules():
    prompt_module = importlib.import_module("codeharness.core.prompt")

    prompt = prompt_module.build_default_system_prompt(
        workspace=Path("/tmp/workspace"),
        skill_catalog="skill-a\nskill-b",
    )

    assert "You are a coding agent at /tmp/workspace." in prompt
    assert "FIRST call todo_write" in prompt
    assert "task' tool" in prompt
    assert "LEADER of an optional agent team" in prompt
    assert "skill-a\nskill-b" in prompt


def test_agent_uses_injected_model_dependencies():
    agent_module = importlib.import_module("codeharness.core.agent")
    client = object()

    agent = agent_module.Agent(
        client=client,
        model="injected-model",
        model_context_window=654321,
        tools=[],
        handlers={},
        memory_manager=False,
    )

    assert agent.client is client
    assert agent.model == "injected-model"
    assert agent.context_budget.model_window == 654321


def test_main_import_has_no_startup_side_effects():
    main_module = importlib.import_module("main")

    assert callable(main_module.main)


def test_cli_routes_slash_commands_through_harness():
    cli_module = importlib.import_module("interfaces.cli")

    class FakeHarness:
        def __init__(self):
            self.calls = []

        def set_approval_handler(self, handler):
            self.approval_handler = handler

        def set_status_handler(self, handler):
            self.status_handler = handler

        def set_async_result_handler(self, handler):
            self.async_result_handler = handler

        def context_info(self):
            self.calls.append("context")
            return "context report"

        def compact(self):
            self.calls.append("compact")
            return "compacted"

        def clear(self):
            self.calls.append("clear")
            return "cleared"

        def run(self, query):
            self.calls.append(("run", query))
            return "answer"

    queries = iter(["/context", "/compact", "/clear", "hello", "q"])
    output = []
    harness = FakeHarness()

    cli_module.CLI(
        harness,
        input_fn=lambda _prompt: next(queries),
        output_fn=output.append,
    ).run()

    assert harness.calls == ["context", "compact", "clear", ("run", "hello")]
    assert "context report" in output
    assert "compacted" in output
    assert "cleared" in output
    assert "answer" in output
