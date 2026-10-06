"""Prompt text shared by agent flavors that advertise runtime-bound skills."""

SKILL_ROUTING_RULES = (
    "When a skill clearly matches the task, load it before taking task-specific "
    "actions.\nFor bug fixes and regressions, load the bug-fix skill before "
    "editing code."
)
