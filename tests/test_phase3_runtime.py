import importlib
import inspect
from types import SimpleNamespace


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
    assert config.mcp_config_path == config.agent_home / "mcp/servers.json"


def _skills(tmp_path):
    from codeharness.skills import SkillRegistry

    return SkillRegistry(
        workspace=tmp_path,
        agent_home=tmp_path / "agent-home",
    )


def test_default_prompt_uses_exact_runtime_skill_catalog(tmp_path):
    prompt_module = importlib.import_module("codeharness.core.prompt")
    manifest = tmp_path / ".codeharness/skills/bug-fix/SKILL.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "---\nname: bug-fix\ndescription: Project override\n---\nproject body\n",
        encoding="utf-8",
    )
    skills = _skills(tmp_path)

    prompt = prompt_module.build_default_system_prompt(
        workspace=tmp_path,
        skill_registry=skills,
        workflow_catalog="workflow-a",
    )

    assert f"You are a coding agent at {tmp_path}." in prompt
    assert (
        "after loading applicable skills, call todo_write"
        in prompt
    )
    assert "task' tool" in prompt
    assert "LEADER of an optional agent team" in prompt
    assert "Available Workflows:\nworkflow-a" in prompt
    assert f"Skills available:\n{skills.catalog()}\n\n" in prompt
    assert "bug-fix: Project override" in prompt
    assert "skill_catalog" not in inspect.signature(
        prompt_module.build_default_system_prompt
    ).parameters


def test_agent_uses_injected_model_dependencies(tmp_path):
    agent_module = importlib.import_module("codeharness.core.agent")
    client = object()

    agent = agent_module.Agent(
        client=client,
        model="injected-model",
        model_context_window=654321,
        tools=[],
        handlers={},
        memory_manager=False,
        skill_registry=_skills(tmp_path),
    )

    assert agent.client is client
    assert agent.model == "injected-model"
    assert agent.context_budget.model_window == 654321


def test_agent_requires_explicit_skill_registry_even_with_custom_prompt_and_tools():
    agent_module = importlib.import_module("codeharness.core.agent")

    try:
        agent_module.Agent(
            system="custom",
            client=object(),
            model="test-model",
            model_context_window=4096,
            tools=[],
            handlers={},
            memory_manager=False,
        )
    except ValueError as exc:
        assert str(exc) == "skill_registry is required"
    else:
        raise AssertionError("Agent accepted a missing skill registry")


def test_agent_default_prompt_and_tools_share_explicit_skill_registry(
    monkeypatch, tmp_path
):
    agent_module = importlib.import_module("codeharness.core.agent")
    skills = _skills(tmp_path)
    seen = {}

    def build_prompt(workspace, *, skill_registry, workflow_catalog):
        seen["prompt"] = skill_registry
        return "system"

    def build_registry(**kwargs):
        seen["tools"] = kwargs["skill_registry"]
        return SimpleNamespace(schemas=[], handlers={})

    monkeypatch.setattr(agent_module, "build_default_system_prompt", build_prompt)
    monkeypatch.setattr(agent_module, "build_base_registry", build_registry)

    agent = agent_module.Agent(
        client=object(),
        model="test-model",
        model_context_window=4096,
        memory_manager=False,
        skill_registry=skills,
    )

    assert agent.skill_registry is skills
    assert seen == {"prompt": skills, "tools": skills}


def test_harness_immediately_owns_workspace_scoped_skill_registry(tmp_path):
    app_module = importlib.import_module("codeharness.app")
    config_module = importlib.import_module("codeharness.config")

    def config(workspace):
        return config_module.RuntimeConfig(
            api_key="key",
            base_url="https://example.invalid",
            model="test-model",
            model_context_window=4096,
            workspace=workspace,
            mcp_config_path=workspace / "mcp.json",
            agent_home=workspace / "agent-home",
        )

    workspaces = [tmp_path / "one", tmp_path / "two"]
    for index, workspace in enumerate(workspaces, start=1):
        manifest = workspace / f".codeharness/skills/project-{index}/SKILL.md"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            f"---\nname: project-{index}\ndescription: Workspace {index}\n---\n",
            encoding="utf-8",
        )

    first = app_module.CodeHarness(config(workspaces[0]))
    second = app_module.CodeHarness(config(workspaces[1]))

    assert first.skill_registry.workspace == workspaces[0].resolve()
    assert first.skill_registry.agent_home == (workspaces[0] / "agent-home").resolve()
    assert "project-1" in first.skill_registry.skills
    assert "project-2" not in first.skill_registry.skills
    assert "project-2" in second.skill_registry.skills
    assert "project-1" not in second.skill_registry.skills


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
