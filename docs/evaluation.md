# Evaluation

CodeHarness has two evaluation paths: a small repository-local smoke harness for repeatable engineering checks, and a single-instance SWE-bench adapter that produces patches inside official task images and can invoke the official evaluator.

## Reported SWE-bench Lite results

| Runtime and model | Dataset | Resolved | Resolve rate | Interpretation |
|---|---:|---:|---:|---|
| CodeHarness v1 + deepseek-flash | SWE-bench Lite (300) | 205 / 300 | 68.3% | Current stable baseline |
| CodeHarness v1 + Pro | SWE-bench Lite (300) | 162 / 300 | 54.0% | Completed comparison run; 57 Worker API exceptions occurred |

These are the completed-run results supplied for the project documentation; this documentation change did not rerun all 300 instances. The Pro number is confounded by 57 Worker API exceptions: its denominator includes infrastructure/API failures as well as task-level outcomes. It therefore does not establish that the Pro model is less capable than Flash. A defensible model comparison would first separate valid rollouts from worker failures and rerun affected instances under matched settings.

## Smoke harness

The smoke runner copies each fixture into a fresh temporary workspace, creates a local Git baseline and case-local agent home, launches one worker process, runs the case's independent verification command, and captures the resulting patch. The agent's final prose response is not used as the pass/fail signal.

Run all smoke cases from the repository root:

```bash
python -m evals.runner.runner --suite smoke
```

Run one case:

```bash
python -m evals.runner.runner --suite smoke --case python_fix_clamp
```

These commands call the configured model and may consume API quota. The no-LLM evaluation tests live under `evals/tests/`. See [`evals/README.md`](../evals/README.md), [`evals/runner/runner.py`](../evals/runner/runner.py), and [`evals/runner/verifier.py`](../evals/runner/verifier.py).

Each smoke run writes:

```text
eval_results/<run-id>/
├── config.json
├── cases/<case-id>/result.json
├── cases/<case-id>/agent.stdout.log
├── cases/<case-id>/agent.stderr.log
├── cases/<case-id>/verifier.stdout.log
├── cases/<case-id>/verifier.stderr.log
├── cases/<case-id>/patch.diff
└── summary.json
```

The workspace is `<work-root>/<run-id>/<case-id>/workspace/`, where `<work-root>` is `CODEHARNESS_EVAL_WORK_ROOT` when set and otherwise the system temporary directory's `codeharness-eval/` subdirectory. It is separate from the CodeHarness source checkout.

## Single-instance SWE-bench flow

The adapter loads one public dataset row, starts its official task image, snapshots `/testbed`, and gives one isolated CodeHarness worker a Docker-backed workspace. It does not expose the gold patch, test patch, evaluator script, or hidden test lists to the worker. After the rollout, the parent freezes the patch, removes the rollout container, emits an official-format prediction, and optionally launches the unchanged official evaluator in a fresh container.

Install the optional upstream dependency in an explicitly chosen Conda environment:

```bash
conda run --no-capture-output -n <environment> python -m pip install swebench
```

Generate one prediction:

```bash
python -m evals.swebench.adapter --instance-id <instance_id>
```

Generate and officially evaluate one prediction:

```bash
python -m evals.swebench.adapter \
  --instance-id <instance_id> \
  --evaluate
```

Defaults are `SWE-bench/SWE-bench_Lite`, split `test`, and a 30-minute worker timeout. `--evaluate` runs the official evaluator with `--max_workers 1`. The project recommends validating the external evaluator with the upstream gold-patch procedure before attributing failures to CodeHarness. Full operational details are in [`evals/swebench/README.md`](../evals/swebench/README.md).

## Isolation and artifacts

The rollout container has no external network access. Its `/testbed` is the source of truth; the host runtime directory is only the worker's current directory and is not a repository clone. A case-local, empty MCP configuration prevents inheritance of the user's global MCP servers, and known API-key values are redacted before artifacts are persisted.

Temporary host state:

```text
/tmp/codeharness-swebench/<run-id>/<instance-id>/
├── runtime-state/
├── agent-home/
└── prediction.jsonl
```

Persistent result bundle:

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

The official evaluator, rather than the agent's final message, determines resolved/unresolved status from patch application, test discovery, and `FAIL_TO_PASS` / `PASS_TO_PASS` checks.

## Scope and interpretation limits

- The adapter supports exactly one SWE-bench instance per invocation.
- Fixed subsets, batch scheduling, retries, concurrency, pass@k, and leaderboard generation are not implemented in this path.
- Resolve rate combines model/runtime behavior with API availability, container health, timeout policy, and evaluator reliability unless failures are reported separately.
- The two reported model runs should not be treated as a clean capability comparison because the Pro run contains 57 Worker API exceptions.
- Reproduction requires Docker, the optional upstream `swebench` package, model credentials, and access to the relevant official images/dataset.
