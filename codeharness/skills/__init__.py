"""Python Skill Runtime (loader only).

The actual SKILL.md files stay in the repository-root ``skills/`` directory;
this package only hosts the runtime code that scans and loads them.
"""

from .loader import SkillLoader

__all__ = ["SkillLoader"]
