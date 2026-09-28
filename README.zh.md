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


