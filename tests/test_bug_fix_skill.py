

def _assert_bug_fix_routing(prompt: str) -> None:
    normalized = " ".join(prompt.lower().split())
    assert "bug-fix" in normalized
    assert "before editing" in normalized


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


def test_leader_prompt_routes_bug_fixes_before_editing(tmp_path):
    from codeharness.core.prompt import build_default_system_prompt

    skills = _skills(tmp_path)
    prompt = build_default_system_prompt(
        workspace=tmp_path,
        skill_registry=skills,
    )

    _assert_bug_fix_routing(prompt)


def test_subagent_prompt_routes_bug_fixes_before_editing(tmp_path):
    from codeharness.subagent import build_sub_system

    skills = _skills(tmp_path)
    _assert_bug_fix_routing(build_sub_system(str(tmp_path), skills))


def test_teammate_prompt_routes_bug_fixes_before_editing(tmp_path):
    from codeharness.team.teammate import TeammateState, teammate_system

    state = TeammateState(name="tester", require_plan=False)
    skills = _skills(tmp_path)

    _assert_bug_fix_routing(teammate_system(state, skills))
