# CodeHarness Evaluation Harness

This directory contains the Phase 1 evaluation harness for small, repeatable
coding-agent cases. It keeps evaluation orchestration outside the production
`codeharness/` package.

## Layout

- `cases/`: JSON case definitions and immutable input fixtures.
- `runner/`: case loading, workspace preparation, subprocess orchestration,
  independent verification, and patch capture.
- `adapters/`: conversion from an evaluation case to one CodeHarness run.
- `worker.py`: child-process entry point; one process handles one case.
- `metrics/`: overall and per-category aggregation.
- `tests/`: no-LLM unit and smoke-fixture preflight tests.

## Run

Run all smoke cases:

```bash
python -m evals.runner.runner --suite smoke
```

Run one case:

```bash
python -m evals.runner.runner --suite smoke --case python_fix_clamp
```

These commands run real agents and may consume API quota. Unit tests do not.

## Results

Each run writes `eval_results/<run-id>/config.json`, per-case
`result.json`, agent/verifier logs, `patch.diff`, and `summary.json`.
Most of `eval_results/` is ignored by Git.

A case succeeds only when its worker does not time out or crash and its
independent verification command exits zero. The agent final answer never
determines success.

## Workspace isolation

Fixtures are copied into a fresh
`$CODEHARNESS_EVAL_WORK_ROOT/<run-id>/<case-id>/workspace/`, defaulting to
the system temporary directory. Each copy receives a local Git baseline and a
case-local `agent-home/`. The worker runs with that workspace as its current
directory and cannot modify the CodeHarness source repository.
