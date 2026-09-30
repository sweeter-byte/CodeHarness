"""Skill tool adapter — exposes the skill catalog to the model via load_skill.

The SkillLoader runtime stays in loader.py; this module only owns the tool
schema + handler pair. The module-level singleton preserves the legacy
behaviour (one shared catalog for leader, subagents and teammates); the
skill runtime is NOT redesigned in Phase 2.
"""

from codeharness.skills.loader import SkillLoader

# ── Skill Loader (module-level singleton) ─────────────────────
SKILL_LOADER = SkillLoader()
SKILL_LOADER.scan()

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

# ── Tool Implementation ───────────────────────────────────────


def run_load_skill(name: str) -> str:
    return SKILL_LOADER.load(name)


SKILL_HANDLERS = {
    "load_skill": run_load_skill,
}
