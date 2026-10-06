"""Skill tool schema and runtime-bound handler factory."""

from collections.abc import Callable

from codeharness.skills.registry import SkillRegistry

# ── Tool Schema ───────────────────────────────────────────────

SKILL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "Load the full instructions of a skill by name. "
                "Use it when a skill's description matches the current task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The skill name as shown in the skills catalog.",
                    },
                },
                "required": ["name"],
            },
        },
    },
]


def make_skill_handlers(
    skill_registry: SkillRegistry,
) -> dict[str, Callable[..., str]]:
    """Bind load_skill to one explicit runtime-owned skill registry."""
    return {"load_skill": skill_registry.load}
