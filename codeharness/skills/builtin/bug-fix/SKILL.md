---
name: bug-fix
description: Use this skill when diagnosing or fixing incorrect existing behavior, regressions, failing tests, crashes, or reported software defects; reproduce the reported failure before modifying code and verify the fix against the same reproducer. Do not use for new features or behavior-preserving refactors.
---

# Bug Fix Skill

## Objective

Fix the failure the user actually reported, rather than proving an assumption introduced during diagnosis.

## Workflow

### 1. Understand the reported failure

Identify the original failure condition from the report, issue, error output, or existing tests. Do not assume a specific root cause yet.

### 2. Reproduce before editing

Before changing source code, create the smallest feasible reproducer and run it. This may be an existing focused test, a temporary script, a CLI or build command, an HTTP request, or a minimal input. Confirm both that the current code fails and that the observed failure matches the report.

### 3. Diagnose before locking onto a fix

Trace the relevant code path. When several hypotheses are plausible, seek repository evidence or an experiment that distinguishes them instead of editing for the first plausible explanation.

### 4. Make the smallest necessary fix

Change the code path responsible for the original failure. Avoid unrelated behavior changes made only to satisfy a newly invented test.

### 5. Verify with the same reproducer

After the patch, rerun the same reproducer with its original failure condition unchanged. Do not weaken, delete, replace, or bypass that condition to make the result pass.

### 6. Run regression tests

Only after the original reproducer passes, run the relevant existing tests, module tests, or other reasonable regression tests. Unrelated passing tests do not replace this verification.

## Verification Rules

- Do not redefine the reported failure condition to fit the current hypothesis.
- A test created only to confirm the current hypothesis is not a substitute for reproducing the originally reported behavior.
- A passing unrelated existing test does not prove that the reported defect is fixed.
- Rerun the same reproducer after the patch whenever feasible.
- If exact reproduction is not feasible, state why, use the closest observable evidence available, and do not claim exact reproduction.
