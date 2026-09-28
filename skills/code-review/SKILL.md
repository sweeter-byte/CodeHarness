---
name: code-review
description: Perform thorough code reviews focusing on correctness, readability, and maintainability.
---

# Code Review Skill

When reviewing code, follow these steps:

1. **Correctness**: Check for logic errors, off-by-one bugs, and edge cases.
2. **Readability**: Ensure variable names are clear and comments explain *why*, not *what*.
3. **Maintainability**: Look for duplicated logic that could be extracted into helpers.

## Checklist

- [ ] No hardcoded secrets or credentials
- [ ] Error handling covers failure modes
- [ ] Tests exist for new behavior
