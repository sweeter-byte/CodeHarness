from types import SimpleNamespace


def _assert_bug_fix_routing(prompt: str) -> None:
    normalized = " ".join(prompt.lower().split())
    assert (
        "your first tool call must be load_skill(name=\"bug-fix\")"
        in normalized
    )
    assert (
        "only after loading applicable skills may you call todo_write or "
        "repository tools"
        in normalized
    )


def _skills(tmp_path):
    from codeharness.skills import SkillRegistry

    return SkillRegistry(
        workspace=tmp_path,
        agent_home=tmp_path / "agent-home",
    )


def test_bug_fix_skill_is_discoverable_and_loadable(tmp_path):
    skills = _skills(tmp_path)
    skill = skills.skills["bug-fix"]
    description = skill.description.lower()

    assert "incorrect existing behavior" in description
    assert "regression" in description
    assert "reproduce" in description

    catalog = skills.catalog().lower()
    assert "bug-fix" in catalog
    assert "reproduce" in catalog

    content = skills.load("bug-fix").lower()
    assert content.startswith("---")
    assert "understand the reported failure" in content
    assert "reproduce before editing" in content
    assert "same reproducer" in content
    assert "regression tests" in content


def test_leader_prompt_routes_bug_fixes_before_other_tools(tmp_path):
    from codeharness.core.prompt import build_default_system_prompt

    skills = _skills(tmp_path)
    prompt = build_default_system_prompt(
        workspace=tmp_path,
        skill_registry=skills,
    )

    _assert_bug_fix_routing(prompt)
    assert "FIRST call todo_write" not in prompt
    assert (
        "For any multi-step task, after loading applicable skills, call "
        "todo_write"
        in prompt
    )


def test_subagent_prompt_routes_bug_fixes_before_other_tools(tmp_path):
    from codeharness.subagent import build_sub_system

    skills = _skills(tmp_path)
    _assert_bug_fix_routing(build_sub_system(str(tmp_path), skills))


def test_teammate_prompt_routes_bug_fixes_before_other_tools(tmp_path):
    from codeharness.team.teammate import TeammateState, teammate_system

    state = TeammateState(name="tester", require_plan=False)
    skills = _skills(tmp_path)

    _assert_bug_fix_routing(teammate_system(state, skills))


def test_agent_request_contains_routing_prompt_and_load_skill_schema(tmp_path):
    from codeharness.core.agent import Agent

    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(usage=None)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()),
    )
    agent = Agent(
        client=client,
        model="test-model",
        model_context_window=4096,
        memory_manager=False,
        skill_registry=_skills(tmp_path),
    )

    agent._call_llm([{"role": "user", "content": "Fix the regression."}])

    assert captured["messages"][0]["role"] == "system"
    _assert_bug_fix_routing(captured["messages"][0]["content"])
    tool_names = {
        tool["function"]["name"] for tool in captured["tools"]
    }
    assert "load_skill" in tool_names
