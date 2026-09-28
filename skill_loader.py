from pathlib import Path

class SkillLoader:
    """Scan a ``skills/`` directory and build an in-memory registry."""

    def __init__(self, skills_dir: str | Path = "skills"):
        self.skills_dir = Path(skills_dir)
        self.skills: dict[str, dict] = {}

    # ── Scanning ──────────────────────────────────────────────

    def scan(self) -> None:
        """Walk ``skills/*/SKILL.md``, parse frontmatter, populate registry."""
        self.skills.clear()
        skills_root = self.skills_dir.resolve()
        for manifest in sorted(self.skills_dir.glob("*/SKILL.md")):
            if (not manifest.is_file()
                    or not manifest.resolve().is_relative_to(skills_root)):
                continue
            content = manifest.read_text(encoding="utf-8")
            metadata, body = self._parse_frontmatter(content)

            raw_name = metadata.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            name = name or manifest.parent.name

            raw_description = metadata.get("description")
            description = (raw_description.strip()
                           if isinstance(raw_description, str) else "")
            description = description or body.split("\n", 1)[0]
            description = " ".join(str(description).lstrip("# ").split())

            self.skills[name] = {
                "name": name,
                "description": description,
                "content": content,
            }

    # ── Public API ────────────────────────────────────────────

    def catalog(self) -> str:
        """Return a one-line-per-skill summary for the system prompt."""
        if not self.skills:
            return "(no skills available)"
        lines = [
            f"- {s['name']}: {s['description']}"
            for s in self.skills.values()
        ]
        return "\n".join(lines)

    def load(self, name: str) -> str:
        """Return the full SKILL.md content for *name*, or an error string."""
        skill = self.skills.get(name)
        if skill:
            return skill["content"]
        available = ", ".join(self.skills) or "none"
        return f"Error: Unknown skill '{name}'. Available: {available}"

    # ── Frontmatter parser ────────────────────────────────────

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict, str]:
        """Extract YAML-like frontmatter between ``---`` delimiters.

        Returns (metadata_dict, body_text).  No external YAML dependency.
        """
        if not content.startswith("---"):
            return {}, content

        # Find the closing ``---``.
        end = content.find("---", 3)
        if end == -1:
            return {}, content

        raw_yaml = content[3:end].strip()
        body = content[end + 3:].strip()

        metadata: dict = {}
        for line in raw_yaml.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            # Strip optional surrounding quotes.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            metadata[key] = value

        return metadata, body
