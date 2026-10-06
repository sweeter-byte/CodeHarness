from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path


class SkillScope(Enum):
    BUILTIN = "builtin"
    USER = "user"
    PROJECT = "project"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    content: str
    scope: SkillScope
    source: str


class SkillRegistry:
    """Eagerly discover built-in, user, and project skills."""

    def __init__(self, *, workspace: str | Path, agent_home: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.agent_home = Path(agent_home).expanduser().resolve()
        self.skills: dict[str, Skill] = {}

        self._scan_root(resources.files("codeharness.skills.builtin"), SkillScope.BUILTIN)
        self._scan_root(self.agent_home / "skills", SkillScope.USER)
        self._scan_root(
            self.workspace / ".codeharness" / "skills",
            SkillScope.PROJECT,
        )

    def catalog(self) -> str:
        """Return a deterministic one-line summary for each available skill."""
        if not self.skills:
            return "(no skills available)"
        return "\n".join(
            f"- {skill.name}: {skill.description}"
            for skill in sorted(self.skills.values(), key=lambda item: item.name)
        )

    def load(self, name: str) -> str:
        """Return exact manifest content, or a deterministic unknown-skill error."""
        skill = self.skills.get(name)
        if skill is not None:
            return skill.content
        available = ", ".join(sorted(self.skills)) or "none"
        return f"Error: Unknown skill '{name}'. Available: {available}"

    def _scan_root(self, root: Path | Traversable, scope: SkillScope) -> None:
        if not root.is_dir():
            return

        resolved_root = root.resolve() if isinstance(root, Path) else None
        for directory in sorted(root.iterdir(), key=lambda item: item.name):
            if not directory.is_dir():
                continue
            manifest = directory.joinpath("SKILL.md")
            if not manifest.is_file():
                continue
            if (
                resolved_root is not None
                and not manifest.resolve().is_relative_to(resolved_root)
            ):
                continue

            content = manifest.read_text(encoding="utf-8")
            metadata, body = self._parse_frontmatter(content)

            raw_name = metadata.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            name = name or directory.name

            raw_description = metadata.get("description")
            description = (
                raw_description.strip() if isinstance(raw_description, str) else ""
            )
            description = description or body.split("\n", 1)[0]
            description = " ".join(str(description).lstrip("# ").split())

            source = (
                str(manifest.resolve())
                if isinstance(manifest, Path)
                else str(manifest)
            )
            self.skills[name] = Skill(
                name=name,
                description=description,
                content=content,
                scope=scope,
                source=source,
            )

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
        """Extract simple key/value frontmatter without a YAML dependency."""
        if not content.startswith("---"):
            return {}, content

        end = content.find("---", 3)
        if end == -1:
            return {}, content

        raw_yaml = content[3:end].strip()
        body = content[end + 3 :].strip()
        metadata: dict[str, str] = {}
        for line in raw_yaml.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in ('"', "'")
            ):
                value = value[1:-1]
            metadata[key] = value
        return metadata, body
