# CodeHarness

从零搭建一个Coding Agent。

## Agent Loop

一个简单的Agent Loop可以由以下五部分组成：
- 将用户问题作为第一条消息
- 将消息和工具定义一起发送给LLM
- 追加模型回答，检查其是否请求调用工具。若无，则终止循环
- 执行模型要求的工具，收集结果
- 将结果追加到消息，执行第二步

## TOOL USE

这里的TOOL USE与TOOL CALLING等价。一个Coding Agent的TOOL的最小完备集可以为
- bash
- read file
- write file
- edit file
- glob
- grep


## Permission

Permission是设计在LLM给出工具和工具执行前的一层Harness，目的是为了避免进行危险操作。

这里的Permission分为两层，第一层是**安全路径**，我们规定CodingAgent只能在当前目录下(默认)，或者用户指定的其他目录下运行，对其余的目录无任何权限。当需要访问其他目录时，需要用户审批。

第二层是**三级阀门**设计，对LLM给出的命令进行安全性检查：
- 第一级：拒绝列表，记录永远会被拒绝的操作。
- 第二级：规则匹配，找出需要用户审批的操作。上述的**安全路径**本质就是这一层的一个子规则，但由于其很重要，所以提在前面。
- 第三级：满足规则的命令，询问用户是否审批。

后续可以在以下三个方面进行拓展：
- 在.env中配置 ALLOWED_DIRS 来支持用户指定工作目录
- 增加更多DENY_LIST / ASK_RULES规则
- 支持"本次会话记住选择"等更智能的审批策略

## Hook

对于Coding Agent Cycle，我们定义了四个关键节点：
- UserPromptSubmit
- PreToolUse
- PostToolUse
- Stop
每个节点可以包含一系列的hooks，以使得Agent Loop只关注核心逻辑，其余权限检查、token统计等，交付给hook实现。

| 节点                   | Hook  | 职责                                         |
| -------------------- | ----- | ------------------------------------------ |
| **UserPromptSubmit** | 上下文注入 | 打印当前工作目录等环境信息                              |
| **PreToolUse**       | 隐私掩码  | 检测命令/参数中的敏感信息（API Key、密码等），发现则阻止执行         |
|                      | 权限检查  | 迁移 `PermissionManager.check()`，返回拒绝标记供循环计数 |
|                      | 调用日志  | 打印工具名称和参数                                  |
| **PostToolUse**      | 输出展示  | 打印工具输出（截断）+ 大文件警告                          |
| **Stop**             | 会话统计  | 打印工具调用次数 + 累计 token 消耗                     |


## TODO Write

增加`TODO List`，以增强Agent系统的**规划能力**，原系统的执行能力不会变化。此外，增加`Reminder`机制，以保证Agent不会以往最初的`TODO List`。最后，还需要在System Prompt内引导模型**先规划再行动**

## SubAgent

`SubAgent`本身可以看作Agent的一个特殊工具.委派只有一层.子Agent继承父Agent除`Task`以外的所有工具.每个子Agent最多执行30轮,避免父Agent无限等待.

## Skill Loader

本质上是做分层加载构建`System Prompt`,使得其更灵活,避免稀释注意力.Skills本身可以看作说一种特殊的Tool,在Tools模块内实现即可.对应的`catalog`和`load`在新的模块中实现.

## Context Management

上下文管理是一个比较复杂的模块,它涉及到如何管理一块有限的"内存",后续能够借鉴操作系统中内存管理模块的一些优雅的设计.相关设计原则为
1. Token 为统一预算单位，所有阈值从 ContextBudget 推导
2. 确定性操作优先，Layer 0-3 无损/可恢复，Layer 4 有损最后执行
3. Tool Pair 不可切割，tool_use 和 tool_result 一起保留或一起归档
4. Active Request 显式保护，不依赖"碰巧还在 Tail 中"
5. 外置 + 重载入成对设计，信息从 In-Context 变为 Load-On-Demand
6. 滞回机制，Trigger 和 Target 分离避免抖动
7. 异常恢复独立路径，API overflow 走 reactive_compact，最多重试 1 次
8. 与 Subagent 互补，Subagent 提供 Context Isolation，避免主 Context 污染

当前设计了四层压缩机制:
- 转存:单个工具结果超过阈值时,转存到磁盘.实现单条消息长度可控
- 裁剪:当消息数或总token数超出阈值时,需要对中间消息进行归档.实现消息总数可控
- 淘汰:当总token数超出上下文窗口上限时,对模型已读的结果进行归档.以保证API调用成功(不一定百分百成功)
- 摘要:调用LLM实现语义压缩,随后开启新对话.

对应的模块:
```text
CodeHarness/
├── context/
│   ├── __init__.py
│   ├── budget.py           # ContextBudget 预算计算
│   ├── token_counter.py    # Token 计数（优先用 API 返回值，备用本地估算）
│   ├── artifact_store.py   # 大结果外置存储 + 重新载入
│   ├── checkpoint.py       # ContextCheckpoint 结构化检查点
│   ├── compactor.py        # Layer 4 语义压缩后端
│   └── manager.py          # ContextManager 主入口，串联所有 Layer
├── agent_loop.py           # 改造：集成 ContextManager
├── tools.py                # 改造：新增 read_artifact 工具
├── hooks.py                # 改造：新增 Context Observability hook
└── ...
```

设计的数据流向是
```text
                    agent_loop
                        │
                  ┌─────┴─────┐
                  │           │
                  ▼           ▼
           ContextManager   Model.call()
                │
    ┌───────────┼───────────┐
    │           │           │
    ▼           ▼           ▼
 Budget     ArtifactStore   Compactor
    │           │               │
    ▼           ▼               ├── LLM Summary
TokenCounter  .artifacts/       └── Checkpoint
```

为避免压缩完后立刻又触发压缩,设计来"滞回机制",本质就是设置一系列阈值,不同阈值间有缓冲空间.
```text
0% ───────────────────────────
60% ───── Compact Target (压缩后尽量回到这里)
80% ───── Soft Limit (开始自动压缩)
90% ───── Hard Limit (紧急，跳过 Layer 2/3 直接 Layer 4)
100% ──── Provider Context Limit
```

上下文压缩触发的几种机制:

| Trigger              | 触发方式                                        | 实现位置                                    |
| -------------------- | ------------------------------------------- | --------------------------------------- |
| **Auto Trigger**     | 当前窗口的token数超出预设阈值时 | `ContextManager.prepare()`              |
| **Manual Trigger**   | 用户输入 `/compact`                             | `hooks.py` 的 `UserPromptSubmit` hook 拦截 |
| **Model Trigger**    | 模型调用 `compact` 工具                     | agent_loop 中检测 tool_call                |
| **Reactive Trigger** | API 返回 context overflow 异常                  | `_call_llm` 的 except 分支                 |
| **Reset**            | 用户输入 `/clear`                               | 主循环中清空 history                          |


为了更好的测试上下文压缩等情况,可以在`.env`文件中调整上下文窗口大小.


## Memory

Memory由四个机制实现:
- 记忆
- 召回
- 提取
- 合并

## Task System

两阶段:
- 先构建所有任务的节点
- 再补充各节点之间可能存在的依赖关系

需要保证构成的依赖关系图是一个DAG.此外,需要实现八个task tools:

| 工具名             | 参数                                        | 返回               |
| --------------- | ----------------------------------------- | ---------------- |
| `create_task`   | `subject: str`, `description: str = ""`   | 生成的 ID + subject |
| `update_task`   | `task_id: str`, `addBlockedBy: list[str]` | 更新后的依赖列表         |
| `can_start`     | `task_id: str`                            | bool（是否所有前置已完成）  |
| `claim_task`    | `task_id: str`, `owner: str = "agent"`    | 认领结果             |
| `complete_task` | `task_id: str`, `owner: str = "agent"`    | 完成结果 + 被解锁的下游任务  |
| `list_task`     | 无参数                                       | 表格摘要             |
| `get_task`      | `task_id: str`                            | 完整 JSON          |
| `reset_task`    | 无参数                                     |当前项目所有任务状态均为completed时,清空整个`.tasks/`目录   |

注意到,任务编排只能由父Agent完成,子Agent禁用.

## Background Task 

让Agent系统具备异步执行能力.将耗时任务放入后台执行,返回占位结果,后续轮次收集完成的结果并已通知的形式加入到消息里.

BackgroundManager类:后台任务的生命周期管理：启动、状态追踪、结果收集、进程清理.
```python
class BackgroundManager:
    __init__(max_concurrent=3)

    self.max_concurrent: int          # 最大并发后台任务数
    self.tasks: dict[str, dict]       # bg_id → 任务元信息
    self.results: dict[str, str]      # bg_id → 完成后的结果文本
    self._ready: list[str]            # 已完成但尚未被收集的 bg_id 队列
    self._lock: threading.Lock        # 保护上述共享状态
    self._counter: int                # 生成递增 bg_id 的计数器
```
| 字段           | 类型     | 说明                                                           |
| ------------ | ------ | ------------------------------------------------------------ |
| `command`    | str    | 执行的 shell 命令                                                 |
| `status`     | str    | `running` / `completed` / `failed` / `timeout` / `cancelled` |
| `thread`     | Thread | daemon 线程引用                                                  |
| `pgid`       | int    | 进程组 ID，用于统一清理子进程                                             |
| `start_time` | float  | `time.time()` 记录启动时间戳                                        |


## Cron Scheduler

将原来系统的单线程运行方式修改为三线程:

| 线程                     | 职责                                | 生命周期     |
| ---------------------- | --------------------------------- | -------- |
| 主线程                    | 接收用户输入，驱动 `agent_loop()`          | CLI 存活期间 |
| Scheduler Thread       | 每秒轮询本地时间，匹配 cron 表达式，到期任务写入持久化后入队 | 同        |
| Queue Processor Thread | 每 200ms 检查交付队列，等 Agent 空闲后注入消息    | 同        |

CronJob数据结构:
```python
@dataclass
class CronJob:
    id: str              # "cron_" + 4字节hex，与 task_system 的 "task_" 前缀风格一致
    cron: str            # 五段式 cron 表达式
    prompt: str          # 到期后交给 Agent 的任务描述
    recurring: bool      # True=周期性，False=一次性
    durable: bool        # True=持久化到磁盘，False=仅内存
    pending_delivery: bool = False   # 已到期但尚未交付
    last_fired: str | None = None    # "YYYY-MM-DD HH:MM" 防止同分钟重复入队
```

CronStore持久化层:将定时任务使用**单文件+原子写入**.

调度线程:
```text
每秒循环:
  1. 获取当前时间 moment
  2. 计算 minute_marker = moment.strftime("%Y-%m-%d %H:%M")
  3. 遍历 scheduled_jobs:
     - 跳过 pending_delivery=True 或 last_fired==minute_marker 的 job
     - 若 cron_matches(job.cron, moment):
       a. 设置 job.pending_delivery = True, job.last_fired = minute_marker
       b. 原子持久化
       c. 成功 → 加入 delivery_queue
       d. 失败 → 回滚，不入队
```

队列处理线程,用以检查当前Agent是否空闲.使用全局锁`agent_lock`,避免定时任务和用户操作并发修改`history`.

主线程：在 CLI 的 input() 循环中，用户输入后、调用 agent_loop() 前获取锁，agent_loop() 返回后释放。

Queue Processor：每 200ms 尝试 agent_lock.acquire(blocking=False)，成功才交付。

```text
Queue Processor 获取 agent_lock 成功:
  1. 从 delivery_queue 取出所有到期 job
  2. 对每个 job:
     - history.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
  3. 调用 agent.agent_loop(history)
  4. 交付成功后:
     - 一次性任务 (recurring=False) → 从 scheduled_jobs 删除，持久化
     - 周期性任务 → 重置 pending_delivery=False，持久化
  5. 交付失败（LLM 调用异常）:
     - 从 history 中移除刚追加的 [Scheduled] 消息
     - 回退 job 状态（保留 pending_delivery=True），下次重试
  6. 释放 agent_lock
```

生命周期管理:
```text
CLI 启动
  ├── 加载 .scheduled_tasks.json → 恢复 durable 任务到 scheduled_jobs
  ├── 启动 Scheduler Thread（daemon=True）
  ├── 启动 Queue Processor Thread（daemon=True）
  └── 进入 input() 循环

CLI 退出（EOFError / KeyboardInterrupt / "q"）
  ├── stop_event.set()  → 两个线程退出
  └── trigger_hooks("Stop", SESSION_STATS)
```
