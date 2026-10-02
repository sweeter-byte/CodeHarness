# SWE-bench Single-Instance Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Run CodeHarness against one specified SWE-bench instance and emit an official prediction with optional official evaluation.

**Architecture:** Keep SWE-bench orchestration under `evals/swebench` and reuse the existing evaluation subprocess lifecycle and CodeHarness adapter. Use explicit dependency injection at external boundaries so every unit test remains offline.

**Tech Stack:** Python 3.12, dataclasses, subprocess/Git, pytest, optional Hugging Face datasets and SWE-bench.

---

### Task 1: Dataset and workspace boundaries

**Files:**
- Create: `evals/tests/test_swebench_adapter.py`
- Create: `evals/swebench/__init__.py`
- Create: `evals/swebench/dataset.py`
- Create: `evals/swebench/workspace.py`

- [x] Write tests for single-row selection, missing instances, lazy dependency errors, external workspace roots, clone/checkout commands, HEAD verification, and clean-tree verification.
- [x] Run `conda run --no-capture-output -n coding-agent python -m pytest evals/tests/test_swebench_adapter.py -q` and confirm missing-module failures.
- [x] Implement immutable instance loading and isolated workspace preparation with injected external-command boundaries.
- [x] Re-run the focused tests and confirm these cases pass.

### Task 2: Thin worker and prediction

**Files:**
- Modify: `evals/adapters/codeharness.py`
- Create: `evals/swebench/worker.py`
- Create: `evals/swebench/prediction.py`
- Test: `evals/tests/test_swebench_adapter.py`

- [x] Add failing tests proving the worker sends only the fixed prefix plus problem statement, uses the case-local agent home, redacts errors, and writes token/tool statistics.
- [x] Add failing tests proving prediction JSONL has exactly the official keys and preserves empty patches.
- [x] Run the focused tests and confirm expected failures.
- [x] Extract `CodeHarnessAdapter.run_task(...)` while retaining the existing `run(EvalCase, ...)` behavior, then implement the thin worker and prediction writer.
- [x] Re-run the focused and existing adapter/worker tests.

### Task 3: Official evaluator boundary

**Files:**
- Create: `evals/swebench/evaluate.py`
- Test: `evals/tests/test_swebench_adapter.py`

- [x] Add failing tests for missing SWE-bench, missing Docker, unavailable daemon, and exact single-instance evaluator arguments.
- [x] Run the focused tests and confirm expected failures.
- [x] Implement preflight checks and the official module command with `--max_workers 1`.
- [x] Re-run the focused tests.

### Task 4: Single-instance parent CLI and artifacts

**Files:**
- Create: `evals/swebench/adapter.py`
- Test: `evals/tests/test_swebench_adapter.py`

- [x] Add failing orchestration tests for task isolation, MCP isolation, diff capture, empty patches, no implicit evaluator call, explicit evaluator calls, timeout metadata, secret redaction, and source-repository preservation.
- [x] Run the focused tests and confirm expected failures.
- [x] Implement CLI parsing and one-instance orchestration using the existing environment, worker subprocess, timeout, payload, and redaction helpers.
- [x] Re-run the focused tests until green.

### Task 5: Documentation and verification

**Files:**
- Create: `evals/swebench/README.md`

- [x] Document installation, prediction-only use, explicit official evaluation, Docker, artifacts, gold sanity-check guidance, and the single-instance limitation.
- [x] Run `conda run --no-capture-output -n coding-agent python -m pytest evals/tests/test_swebench_adapter.py`.
- [x] Run `conda run --no-capture-output -n coding-agent python -m pytest evals/tests`.
- [x] Run `conda run --no-capture-output -n coding-agent python -m pytest`.
- [x] Inspect `git diff --check`, `git status --short`, and `git diff --stat` before reporting.
