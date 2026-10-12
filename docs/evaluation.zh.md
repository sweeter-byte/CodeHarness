# 评测说明

&emsp;&emsp;CodeHarness 提供两条评测路径：仓库内的小型 smoke harness 用于可重复的工程检查；单实例 SWE-bench adapter 在官方任务镜像中生成 Patch，并可显式调用官方评测器。

## 已报告的 SWE-bench Lite 结果

| Runtime 与模型 | 数据集 | Resolved | Resolve Rate | 说明 |
|---|---:|---:|---:|---|
| CodeHarness v1 + deepseek-flash | SWE-bench Lite（300） | 205 / 300 | 68.3% | 当前稳定 baseline |
| CodeHarness v1 + Pro | SWE-bench Lite（300） | 162 / 300 | 54.0% | 已完成对照运行，其中发生 57 次 Worker API 异常 |

&emsp;&emsp;以上是项目文档提供的已完成运行结果，本次文档整理没有重新执行全部 300 个实例。Pro 轮的 57 次 Worker API 异常使比较存在混杂：其分母同时包含基础设施/API 失败和任务级结果，因此不能据此得出“Pro 模型能力弱于 Flash”的确定结论。更严谨的对比需要先分离有效 rollout 与 Worker 异常，并在一致配置下重跑受影响实例。

## Smoke harness

&emsp;&emsp;Smoke runner 会把每个 fixture 复制到全新的临时 Workspace，建立本地 Git baseline 和 case 级 Agent Home，再启动独立 Worker 进程、执行 case 自带的验证命令并收集 Patch。Agent 最终回复的文本不参与通过/失败判定。

&emsp;&emsp;在仓库根目录运行全部 smoke case：

```bash
python -m evals.runner.runner --suite smoke
```

&emsp;&emsp;运行单个 case：

```bash
python -m evals.runner.runner --suite smoke --case python_fix_clamp
```

&emsp;&emsp;上述命令会调用已配置的模型并可能消耗 API 配额；不调用 LLM 的评测测试位于 `evals/tests/`。实现证据见 [`evals/README.md`](../evals/README.md)、[`evals/runner/runner.py`](../evals/runner/runner.py) 和 [`evals/runner/verifier.py`](../evals/runner/verifier.py)。

&emsp;&emsp;每次 smoke 运行写入以下产物：

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

&emsp;&emsp;Workspace 路径为 `<work-root>/<run-id>/<case-id>/workspace/`。设置 `CODEHARNESS_EVAL_WORK_ROOT` 时使用该目录，否则 `<work-root>` 为系统临时目录下的 `codeharness-eval/` 子目录；它与 CodeHarness 源码 Checkout 分离。

## 单实例 SWE-bench 流程

&emsp;&emsp;Adapter 读取一条公开数据集记录，启动对应官方任务镜像，对 `/testbed` 建立基线，再把 Docker Workspace 交给一个隔离的 CodeHarness Worker。Gold Patch、Test Patch、Evaluator Script 和隐藏测试列表不会暴露给 Worker。Rollout 结束后，父进程冻结 Patch、删除 rollout container、输出官方格式 prediction，并可在全新容器中启动未经修改的官方评测器。

&emsp;&emsp;在明确选择的 Conda 环境中安装可选上游依赖：

```bash
conda run --no-capture-output -n <environment> python -m pip install swebench
```

&emsp;&emsp;生成单个 prediction：

```bash
python -m evals.swebench.adapter --instance-id <instance_id>
```

&emsp;&emsp;生成并使用官方评测器验证单个 prediction：

```bash
python -m evals.swebench.adapter \
  --instance-id <instance_id> \
  --evaluate
```

&emsp;&emsp;默认数据集为 `SWE-bench/SWE-bench_Lite`，split 为 `test`，Worker timeout 为 30 分钟。`--evaluate` 使用 `--max_workers 1` 调用官方评测器。项目建议先按上游 Gold Patch 流程验证外部评测环境，再将失败归因于 CodeHarness。完整操作说明见 [`evals/swebench/README.md`](../evals/swebench/README.md)。

## 隔离与产物

&emsp;&emsp;Rollout container 禁止外部网络访问，其中的 `/testbed` 是源码状态的事实来源。宿主 Runtime 目录只作为 Worker 当前目录，并不是仓库 Clone。Case 使用空的本地 MCP 配置，不继承用户的全局 MCP Server；已知 API Key 会在持久化前脱敏。

&emsp;&emsp;临时宿主状态：

```text
/tmp/codeharness-swebench/<run-id>/<instance-id>/
├── runtime-state/
├── agent-home/
└── prediction.jsonl
```

&emsp;&emsp;持久化结果包：

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

&emsp;&emsp;Resolved/Unresolved 状态由官方评测器根据 Patch 应用、测试发现以及 `FAIL_TO_PASS` / `PASS_TO_PASS` 检查确定，不取决于 Agent 的最终回复。

## 范围与解读限制

- Adapter 每次调用只支持一个 SWE-bench 实例。
- 固定子集、批量调度、重试、并发、pass@k 和排行榜生成尚未在该路径实现。
- 如果不单独报告失败类型，Resolve Rate 会同时受到模型/Runtime 行为、API 可用性、容器状态、超时策略和评测器可靠性的影响。
- 由于 Pro 轮包含 57 次 Worker API 异常，两轮结果不能作为干净的模型能力对比。
- 复现需要 Docker、可选的上游 `swebench` 包、模型凭据，以及对应官方镜像与数据集的访问权限。
