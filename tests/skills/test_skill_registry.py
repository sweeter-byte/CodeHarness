from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import codeharness.skills.registry as registry_module
from codeharness.skills import SkillRegistry, SkillScope


def _write_skill(root: Path, directory: str, content: str) -> Path:
    manifest = root / directory / "SKILL.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(content, encoding="utf-8")
    return manifest


def test_discovers_package_owned_builtin_skills(tmp_path: Path) -> None:
    registry = SkillRegistry(
        workspace=tmp_path / "workspace",
        agent_home=tmp_path / "agent-home",
    )

    assert {"bug-fix", "code-review"} <= registry.skills.keys()
    assert registry.skills["bug-fix"].scope is SkillScope.BUILTIN
    assert registry.skills["code-review"].scope is SkillScope.BUILTIN
    assert isinstance(registry.skills["bug-fix"].source, str)
    assert isinstance(registry.skills["code-review"].source, str)


def test_discovers_user_skill_from_agent_home(tmp_path: Path) -> None:
    agent_home = tmp_path / "agent-home"
    content = """---
name: 'foo'
description: "  A   user skill  "
---
User marker
"""
    manifest = _write_skill(agent_home / "skills", "foo-dir", content)

    registry = SkillRegistry(workspace=tmp_path / "workspace", agent_home=agent_home)

    assert registry.skills["foo"].scope is SkillScope.USER
    assert registry.skills["foo"].description == "A user skill"
    assert registry.skills["foo"].source == str(manifest.resolve())
    assert registry.load("foo") == content


def test_discovers_user_skill_through_symlinked_skills_root(tmp_path: Path) -> None:
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()
    actual_skills = tmp_path / "actual-user-skills"
    content = "Symlinked user skill\n"
    manifest = _write_skill(actual_skills, "linked", content)
    (agent_home / "skills").symlink_to(actual_skills, target_is_directory=True)

    registry = SkillRegistry(workspace=tmp_path / "workspace", agent_home=agent_home)

    assert registry.skills["linked"].scope is SkillScope.USER
    assert registry.skills["linked"].source == str(manifest.resolve())
    assert registry.load("linked") == content


def test_user_skill_directory_symlink_cannot_escape_skills_root(
    tmp_path: Path,
) -> None:
    agent_home = tmp_path / "agent-home"
    skills_root = agent_home / "skills"
    skills_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    _write_skill(outside, "escaped", "Escaped skill\n")
    (skills_root / "escaped").symlink_to(
        outside / "escaped",
        target_is_directory=True,
    )

    registry = SkillRegistry(workspace=tmp_path / "workspace", agent_home=agent_home)

    assert "escaped" not in registry.skills


def test_discovers_project_skill_from_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    content = "# Bar   project\n\nProject marker\n"
    manifest = _write_skill(workspace / ".codeharness" / "skills", "bar", content)

    registry = SkillRegistry(workspace=workspace, agent_home=tmp_path / "agent-home")

    assert registry.skills["bar"].scope is SkillScope.PROJECT
    assert registry.skills["bar"].description == "Bar project"
    assert registry.skills["bar"].source == str(manifest.resolve())
    assert registry.load("bar") == content


def test_user_skill_overrides_builtin_by_manifest_name(tmp_path: Path) -> None:
    agent_home = tmp_path / "agent-home"
    user_content = (
        "---\nname: bug-fix\ndescription: User override\n---\nUSER ONLY MARKER\n"
    )
    _write_skill(agent_home / "skills", "user-bug-fix", user_content)

    registry = SkillRegistry(workspace=tmp_path / "workspace", agent_home=agent_home)

    assert registry.skills["bug-fix"].scope is SkillScope.USER
    assert registry.load("bug-fix") == user_content


def test_project_skill_overrides_user_and_builtin_by_manifest_name(
    tmp_path: Path,
) -> None:
    agent_home = tmp_path / "agent-home"
    workspace = tmp_path / "workspace"
    _write_skill(
        agent_home / "skills",
        "user-bug-fix",
        "---\nname: bug-fix\ndescription: User override\n---\nUSER MARKER\n",
    )
    project_content = (
        "---\nname: bug-fix\ndescription: Project override\n---\nPROJECT MARKER\n"
    )
    _write_skill(
        workspace / ".codeharness" / "skills",
        "project-bug-fix",
        project_content,
    )

    registry = SkillRegistry(workspace=workspace, agent_home=agent_home)

    assert list(registry.skills).count("bug-fix") == 1
    assert registry.catalog().count("- bug-fix:") == 1
    assert registry.skills["bug-fix"].scope is SkillScope.PROJECT
    assert registry.load("bug-fix") == project_content
    assert "PROJECT MARKER" in registry.load("bug-fix")


def test_base_registry_uses_explicit_project_skill_override(
    tmp_path: Path,
) -> None:
    from codeharness.tools import build_base_registry

    workspace = tmp_path / "workspace"
    project_content = (
        "---\nname: bug-fix\ndescription: Project tool override\n---\n"
        "PROJECT TOOL MARKER\n"
    )
    _write_skill(
        workspace / ".codeharness" / "skills",
        "project-bug-fix",
        project_content,
    )
    skills = SkillRegistry(
        workspace=workspace,
        agent_home=tmp_path / "agent-home",
    )

    registry = build_base_registry(skill_registry=skills)

    assert "- bug-fix: Project tool override" in skills.catalog()
    assert registry.get("load_skill")("bug-fix") == skills.skills["bug-fix"].content


def test_project_skills_are_isolated_between_workspaces(tmp_path: Path) -> None:
    workspace_one = tmp_path / "workspace-one"
    workspace_two = tmp_path / "workspace-two"
    _write_skill(
        workspace_one / ".codeharness" / "skills",
        "only-one",
        "Only workspace one\n",
    )
    _write_skill(
        workspace_two / ".codeharness" / "skills",
        "only-two",
        "Only workspace two\n",
    )

    first = SkillRegistry(workspace=workspace_one, agent_home=tmp_path / "agent-home")
    second = SkillRegistry(workspace=workspace_two, agent_home=tmp_path / "agent-home")

    assert "only-one" in first.skills
    assert "only-two" not in first.skills
    assert "only-two" in second.skills
    assert "only-one" not in second.skills


def test_fresh_process_finds_builtins_outside_repository_cwd(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    code = """
from pathlib import Path
from codeharness.skills import SkillRegistry

registry = SkillRegistry(workspace=Path.cwd(), agent_home=Path.cwd() / ".agent")
assert "bug-fix" in registry.skills
assert "code-review" in registry.skills
print("builtins-found")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "builtins-found"


def test_zip_backed_builtin_resource_has_stable_string_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive_path = tmp_path / "skills.zip"
    content = "---\nname: zipped\ndescription: Zip resource\n---\nZip marker\n"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("builtin/zipped/SKILL.md", content)

    with zipfile.ZipFile(archive_path) as archive:
        resource_root = zipfile.Path(archive, at="builtin/")
        monkeypatch.setattr(registry_module.resources, "files", lambda package: resource_root)

        registry = SkillRegistry(
            workspace=tmp_path / "workspace",
            agent_home=tmp_path / "agent-home",
        )

        assert registry.skills["zipped"].scope is SkillScope.BUILTIN
        assert registry.skills["zipped"].source == str(
            resource_root.joinpath("zipped", "SKILL.md")
        )
        assert registry.load("zipped") == content


def test_catalog_and_unknown_skill_error_are_sorted(tmp_path: Path) -> None:
    registry = SkillRegistry(
        workspace=tmp_path / "workspace",
        agent_home=tmp_path / "agent-home",
    )

    catalog_names = [line.split(":", 1)[0][2:] for line in registry.catalog().splitlines()]
    assert catalog_names == sorted(catalog_names)
    assert registry.load("missing") == (
        "Error: Unknown skill 'missing'. Available: bug-fix, code-review"
    )
