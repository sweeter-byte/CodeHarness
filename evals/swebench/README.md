# SWE-bench Single-Instance Adapter

SWE-bench provides public, repository-level software-engineering tasks and an
official Docker evaluator. This adapter connects one selected SWE-bench task to
CodeHarness without adding SWE-bench to the normal CodeHarness runtime.

The adapter loads one dataset row, clones its repository at `base_commit` into
an external temporary directory, runs one isolated CodeHarness worker with the
row's `problem_statement`, captures the Git diff, and writes an official-format
prediction. It never applies the gold patch or test patch and does not decide
whether a task is resolved.

## Optional dependencies

Install the official SWE-bench package into a dedicated or explicitly chosen
Conda environment, following the current upstream compatibility requirements:

```bash
conda run --no-capture-output -n <environment> python -m pip install swebench
```

The package and its dataset dependencies are optional; importing CodeHarness
or `evals.swebench` does not require them. If they are absent, the adapter
reports:

```text
SWE-bench evaluation dependencies are not installed.
```

## Generate one prediction

From the CodeHarness repository root:

```bash
python -m evals.swebench.adapter --instance-id <instance_id>
```

The defaults are dataset `SWE-bench/SWE-bench_Lite`, split `test`, and a
30-minute worker timeout. Override them with `--dataset-name`, `--split`, and
`--timeout`. This command runs the agent and generates a prediction only; it
does not start Docker.

## Run the official evaluator explicitly

```bash
python -m evals.swebench.adapter \
  --instance-id <instance_id> \
  --evaluate
```

`--evaluate` performs three preflight checks: the `swebench` module must be
installed, the Docker executable must exist, and the Docker daemon must be
accessible. It then invokes the official evaluator with the selected dataset,
split, prediction, instance ID, run ID, and `--max_workers 1`. The official
evaluator uses Docker to create the task-specific environment and perform its
own patch application, test discovery, FAIL_TO_PASS/PASS_TO_PASS checks, and
resolved decision.

Before evaluating a CodeHarness prediction, first follow the upstream
SWE-bench gold-patch evaluation procedure manually. A successful gold run
confirms that the official evaluator and Docker environment work independently
of this adapter. CodeHarness does not run the gold patch automatically.

## Isolation and artifacts

The disposable checkout and case-local agent state live under:

```text
/tmp/codeharness-swebench/<run-id>/<instance-id>/
├── workspace/
├── agent-home/
└── prediction.jsonl
```

The persistent artifacts live under:

```text
eval_results/swebench/<run-id>/
├── config.json
├── instance.json
├── prediction.jsonl
├── agent.stdout.log
├── agent.stderr.log
├── patch.diff
└── result.json
```

The agent uses an empty case-local MCP configuration and does not inherit the
user's global MCP servers. Known API-key values are redacted from persisted
artifacts.

This phase supports exactly one instance per invocation. Fixed subsets, batch
execution, concurrency, retries, pass@k, and leaderboards are future work.
