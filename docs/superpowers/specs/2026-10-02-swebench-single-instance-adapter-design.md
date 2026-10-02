# SWE-bench Single-Instance Adapter Design

## Scope

Add an evaluation-only adapter that runs CodeHarness against exactly one
SWE-bench instance. It loads one dataset record, prepares an external Git
checkout at `base_commit`, runs one isolated worker, captures the resulting Git
diff, writes an official-format prediction, and optionally invokes the official
SWE-bench evaluator.

The adapter does not batch, retry, apply gold or test patches, discover tests,
decide whether an instance is resolved, or alter the CodeHarness runtime.

## Components

- `dataset.py` lazily imports Hugging Face `datasets`, validates the four
  required fields, returns one immutable instance, and retains the raw record.
- `workspace.py` clones `https://github.com/<owner>/<repo>.git` beneath
  `/tmp/codeharness-swebench`, checks out the exact base commit, and verifies
  both HEAD and a clean working tree.
- `worker.py` is a SWE-bench-specific child entry point. It accepts only the
  problem statement, checkout, result path, and case-local agent home. It uses
  the existing `CodeHarnessAdapter` runtime construction without creating an
  `EvalCase`.
- `adapter.py` is the single-instance parent CLI. It reuses the current
  evaluation environment loader, worker environment isolation, subprocess
  timeout/process-group cleanup, payload loading, and redaction helpers.
- `prediction.py` writes one JSONL object containing only `instance_id`,
  `model_name_or_path`, and `model_patch`.
- `evaluate.py` performs explicit dependency and Docker preflight checks and
  invokes `python -m swebench.harness.run_evaluation` only for `--evaluate`.

## Data flow

`instance_id` selects one dataset row. The workspace manager clones the row's
repository and checks out its base commit outside the source repository. The
parent starts a dedicated worker with an empty case-local MCP configuration.
The worker prefixes only the row's `problem_statement` with a short fixed
instruction and runs CodeHarness. The parent captures
`git diff --no-ext-diff --binary HEAD`, writes prediction and result artifacts,
and optionally passes the prediction to the official evaluator with one worker.

## Error handling and safety

Optional packages are imported only at use sites. Missing evaluation
dependencies produce `SWE-bench evaluation dependencies are not installed.`
Clone, checkout, HEAD, clean-tree, Docker, and evaluator failures stop with a
specific error. Known API-key values are redacted from persisted JSON and logs.
No dataset patch or test patch is sent to the worker.

## Tests

Tests use injected dataset loaders, command runners, temporary local Git
repositories, and fake workers/evaluators. They never access the network,
invoke a real model, or start Docker. Verification runs the focused adapter
tests, all evaluation tests, then the repository test suite.
