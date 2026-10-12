# 系统架构

&emsp;&emsp;CodeHarness v1 是一个本地 Coding Agent Runtime。单个 `CodeHarness` 实例负责组装模型客户端、Leader Agent、原生与 MCP 工具、上下文与记忆服务、任务图、Team、Workflow、后台任务、Cron 调度器和 Goal Loop。当前面向用户的入口是终端界面。

&emsp;&emsp;本文只描述仓库中已经实现的能力；CodeHarness 不是分布式 Agent 平台，也不是操作系统级沙箱。

## 运行时组装

&emsp;&emsp;启动链路为 `main.py` → `interfaces/cli.py` → `CodeHarness.from_env()` → `CodeHarness.start()`。配置从环境变量和 `.env` 读取，`codeharness/app.py` 是统一的组合根。

| 层级 | 当前职责 | 实现证据 |
|---|---|---|
| CLI | 启动运行时、接收多轮输入、提供上下文/清理命令并处理审批 | [`main.py`](../main.py)、[`interfaces/cli.py`](../interfaces/cli.py) |
| 组合根 | 创建 OpenAI 兼容客户端、工具注册表、Leader、存储、调度器、Team、Workflow、MCP 与 Goal Loop | [`codeharness/app.py`](../codeharness/app.py) |
| Agent Core | 执行模型与工具循环、分发工具调用、处理上下文溢出、回收后台结果并提取 Leader 记忆 | [`codeharness/core/agent.py`](../codeharness/core/agent.py) |
| 工具边界 | 定义原生编码工具，并让调用经过执行器和权限检查 | [`codeharness/tools/`](../codeharness/tools)、[`codeharness/security/permission.py`](../codeharness/security/permission.py) |
| 上下文与记忆 | 管理 Token 预算、外置大结果、归档对话、生成检查点，并保存项目级 Markdown 记忆 | [`codeharness/context/`](../codeharness/context)、[`codeharness/memory/manager.py`](../codeharness/memory/manager.py) |
| 协作 | 持久化任务 DAG，运行会话级 Teammate，提供邮箱，并可选分配 Git Worktree | [`codeharness/tasks/`](../codeharness/tasks)、[`codeharness/team/`](../codeharness/team) |
| 编排 | 解释已注册的 Workflow 定义、限制资源、记录步骤日志并恢复中断任务 | [`codeharness/workflow/`](../codeharness/workflow) |
| 外部工具 | 加载 MCP Server、注册带前缀的工具并实施宿主侧权限策略 | [`codeharness/mcp/`](../codeharness/mcp) |
| 评测 | 运行隔离的 smoke case 或单个 SWE-bench 实例，并保存独立验证产物 | [`evals/`](../evals)、[评测说明](evaluation.zh.md) |

## 执行模型

| 模式 | 生命周期与状态 | 适用场景 | 当前边界 |
|---|---|---|---|
| Agent Loop | 在同一运行时内复用 Leader 的多轮对话状态 | 交互式探索与实现 | 同一时间串行执行一个 Leader turn |
| SubAgent | 每次委派使用全新上下文，仅向 Leader 返回最终结果 | 边界明确的调研或实现 | 只提供基础工具，不支持嵌套委派、Team、任务系统或 MCP 工具 |
| Team | 独立 Agent 线程在当前运行时会话内共享任务图和文件邮箱 | 多任务协作 | Teammate 是守护线程，会随运行时关闭；进程退出后不会恢复为活跃 Worker |
| Workflow | 静态阶段与类型化步骤，状态快照和 Journal 持久化 | 有明确预算的可重复编排 | 由本地守护线程执行，不是分布式调度器 |

&emsp;&emsp;Team 的任务图会写入文件并保留到下一次启动，但活跃 Teammate 对象及其对话状态不会跨进程恢复。所谓“持久协作”是指 Teammate 在一次已启动的运行时会话中可连续接受多个任务，而不是常驻系统服务。

&emsp;&emsp;Workflow 支持 `AgentStep`、直接调用工具的 `ToolStep`、分支汇合的 `ParallelStep`、逐项并发的 `PipelineStep` 和嵌套 `WorkflowStep`。成功步骤会写入 Journal，恢复时可以复用。定义需在 Python 中注册；条件表达式能力有意保持简单；并行分支与流水线阶段仅接受 Agent/Tool 步骤。运行时会限制并发数、Agent 调用次数、Token、嵌套深度和总时长。证据见 [`definition.py`](../codeharness/workflow/definition.py)、[`runtime.py`](../codeharness/workflow/runtime.py) 和 [`state.py`](../codeharness/workflow/state.py)。

## 权限边界

&emsp;&emsp;原生工具调用经过 `PermissionManager`，当前策略包括：

- 将解析后的路径与 Workspace/Worktree 允许根目录比较；
- 用 deny list 拒绝部分高危 Shell 模式，用 ask list 对部分变更操作请求交互审批；
- MCP 工具使用宿主侧 allow/ask/deny 表，未知工具默认需要审批。

&emsp;&emsp;这是一层尽力而为的应用权限策略，不提供进程隔离。Shell 路径提取和命令模式匹配无法覆盖全部语法，审批也不会在内核层约束子进程。执行不可信代码时，应额外使用容器、虚拟机或其他操作系统级沙箱。SWE-bench rollout 使用独立 Docker 隔离，普通 CLI 不包含该隔离层。证据见 [`codeharness/security/permission.py`](../codeharness/security/permission.py) 和 [`evals/swebench/rollout_container.py`](../evals/swebench/rollout_container.py)。

## 存储路径

&emsp;&emsp;`RuntimePaths` 在可用时根据 Git common directory 生成稳定项目 ID。运行时文件与跨会话记忆使用不同根目录。

| 路径 | 内容 |
|---|---|
| `<workspace>/.codeharness/state/tasks/` | 持久化任务 DAG，每个任务一个 JSON 文件 |
| `<workspace>/.codeharness/state/scheduler/jobs.json` | 持久化 Cron 任务定义 |
| `<workspace>/.codeharness/runs/workflows/<run-id>/` | Workflow 快照、追加式 Journal 和最终输出 |
| `<workspace>/.codeharness/runtime/team/mailboxes/` | Team 文件邮箱 |
| `<workspace>/.codeharness/artifacts/` | 外置的大型工具输出 |
| `<workspace>/.codeharness/transcripts/` | 上下文压缩归档的对话片段 |
| `<workspace>/.codeharness/worktrees/` | 可选 Team Git Worktree |
| `<CODEHARNESS_HOME>/projects/<project-id>/memory/` | 项目级、跨会话的 Leader 记忆 |

&emsp;&emsp;规范映射位于 [`codeharness/paths.py`](../codeharness/paths.py)，各目录由对应模块按需创建。

## MCP 支持范围

&emsp;&emsp;配置结构可以解析 `stdio` 和 `streamable_http`，但当前客户端仅实现 `stdio` 连接。选择 `streamable_http` 会明确触发 `NotImplementedError`，因此不能将其描述为已支持的传输方式。Server 配置只展开显式声明的环境变量，也只把显式 `env` 映射交给 MCP 子进程。证据见 [`codeharness/mcp/config.py`](../codeharness/mcp/config.py) 和 [`codeharness/mcp/client.py`](../codeharness/mcp/client.py)。

## 当前架构限制

- 运行时是本地单进程架构，并发基于线程。
- 权限检查用于降低误操作风险，不提供隔离保证。
- Team Worker 与 Leader 对话历史不能跨进程恢复。
- Workflow 状态可在本地恢复，但执行不支持远程调度或副本容错。
- MCP 当前仅支持 stdio transport。
- SWE-bench 每次只处理一个实例；批量执行、重试、pass@k 与排行榜编排不属于 v1。
