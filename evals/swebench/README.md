# SWE-bench Single-Instance Adapter

SWE-bench provides public, repository-level software-engineering tasks and an
official Docker evaluator. This adapter connects one selected SWE-bench task to
CodeHarness without adding SWE-bench to the normal CodeHarness runtime.

The adapter loads one dataset row, starts its official SWE-bench image, snapshots
the startup `/testbed` filesystem, and lends that container to one isolated
CodeHarness worker. The parent process captures the final container diff, removes
the rollout container, and writes an official-format prediction. It never passes
the gold patch, test patch, evaluator script, or hidden test lists to the worker.

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
`--timeout`. This command runs the agent in a disposable official rollout
container and generates a prediction; it does not run the official evaluator.

## Run the official evaluator explicitly

```bash
python -m evals.swebench.adapter \
  --instance-id <instance_id> \
  --evaluate
```

After the candidate patch is frozen and the rollout container has been removed,
`--evaluate` invokes the official evaluator with the selected dataset, split,
prediction, instance ID, run ID, and `--max_workers 1`. The evaluator creates a
fresh task-specific container for patch application, test discovery,
FAIL_TO_PASS/PASS_TO_PASS checks, and the resolved decision.

Before evaluating a CodeHarness prediction, first follow the upstream
SWE-bench gold-patch evaluation procedure manually. A successful gold run
confirms that the official evaluator and Docker environment work independently
of this adapter. CodeHarness does not run the gold patch automatically.

## Isolation and artifacts

Host-only runtime state and case-local agent state live under:

```text
/tmp/codeharness-swebench/<run-id>/<instance-id>/
├── runtime-state/
├── agent-home/
└── prediction.jsonl
```

The source of truth is `/testbed` in the official rollout container. The host
runtime directory is only the worker process cwd and is not a repository clone.

The persistent artifacts live under:

```text
eval_results/swebench/<run-id>/
├── config.json
├── instance.json
├── rollout.json
├── prediction.jsonl
├── agent.stdout.log
├── agent.stderr.log
├── patch.diff
└── result.json
```

`instance.json` contains only public task identity/input fields and the official
image reference; hidden scoring fields are not persisted there. The agent uses
an empty case-local MCP configuration and does not inherit the user's global MCP
servers. Known API-key values are redacted from persisted artifacts.

This phase supports exactly one instance per invocation. Fixed subsets, batch
execution, concurrency, retries, pass@k, and leaderboards are future work.
