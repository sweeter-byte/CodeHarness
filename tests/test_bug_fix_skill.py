from pathlib import Path


def _assert_bug_fix_routing(prompt: str) -> None:
    normalized = " ".join(prompt.lower().split())
    assert "bug-fix" in normalized
    assert "before editing" in normalized


def test_bug_fix_skill_is_discoverable_and_loadable():
    from codeharness.skills.tools import SKILL_LOADER, run_load_skill

    skill = SKILL_LOADER.skills["bug-fix"]
    description = skill["description"].lower()

    assert "incorrect existing behavior" in description
    assert "regression" in description
    assert "reproduce" in description

    catalog = SKILL_LOADER.catalog().lower()
    assert "bug-fix" in catalog
    assert "reproduce" in catalog

    content = run_load_skill("bug-fix").lower()
    assert content.startswith("---")
    assert "understand the reported failure" in content
    assert "reproduce before editing" in content
    assert "same reproducer" in content
    assert "regression tests" in content


def test_leader_prompt_routes_bug_fixes_before_editing():
    from codeharness.core.prompt import build_default_system_prompt

    prompt = build_default_system_prompt(
        workspace=Path("/tmp/workspace"),
        skill_catalog="- bug-fix: test description",
    )

    _assert_bug_fix_routing(prompt)


def test_subagent_prompt_routes_bug_fixes_before_editing():
    from codeharness.subagent import build_sub_system

    _assert_bug_fix_routing(build_sub_system("/tmp/workspace"))


def test_teammate_prompt_routes_bug_fixes_before_editing():
    from codeharness.team.teammate import TeammateState, teammate_system

    state = TeammateState(name="tester", require_plan=False)

    _assert_bug_fix_routing(teammate_system(state))
