"""Prompt text shared by agent flavors that advertise runtime-bound skills."""

SKILL_ROUTING_RULES = (
    "Before calling any tool, match the request against the skills available.\n"
    "A request to diagnose or fix incorrect existing behavior, a regression, "
    "a failing test, a crash, or a reported defect clearly matches bug-fix; "
    "your FIRST tool call MUST be load_skill(name=\"bug-fix\").\n"
    "Only after loading applicable skills may you call todo_write or repository "
    "tools such as bash, read_file, grep, write_file, or edit_file."
)
